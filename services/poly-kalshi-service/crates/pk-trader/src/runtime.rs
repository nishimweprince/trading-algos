//! The trader's runtime: the engine thread, and the window, resolution and
//! health loops.
//!
//! - **Engine thread**: the trade path. Per line: forward to the observer
//!   pipe, apply to the engine, hand any order intent to the order path. Its
//!   queue delay (`queue_us`) and work (`engine_us`) are measured.
//! - **Windows** (every 2 s): the current window and, inside
//!   `PK_DISCOVER_LEAD_SECONDS`, the next one are identified for every asset
//!   and written to the tape as control lines before their ids are subscribed.
//!   An unidentified window 30 s after it opened is alerted, unless Kalshi is
//!   in its weekly closure (one message marks the closure and one the reopening).
//! - **Resolutions** (every 15 s): each venue's outcome for every closed
//!   window, polled until known and written as control lines; Polymarket's
//!   `market_resolved` WS event usually beats it. Open positions from before a
//!   restart get their windows back first.
//! - **Health**: stale feeds, the observer's liveness, a heartbeat log line,
//!   and (with an order path) cancel-all once after any halt.
//!
//! Live, [`Runtime::live_view`] is what the order path's reconciler reads of
//! the ledger, and [`Runtime::flatten`] halts and sells every open position.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::{Duration, Instant};

use crossbeam_channel::Receiver;
use parking_lot::Mutex;
use pk_core::window::{Window, window_open};
use pk_core::{NS, WINDOW_S};
use pk_engine::Engine;
use pk_feeds::{Discovery, FeedHandle, PolyShards};
use pk_svc::{Alerts, Settings};
use pk_tape::Recorder;
use serde_json::{Value, json};
use tokio::sync::mpsc;

use crate::exec_bridge::{ExecBridge, ExecMsg, Held, LiveView};
use crate::ingest::{Ingest, Line};
use crate::link::ObserverLink;
use crate::now_ns;
use crate::observer::SharedJournal;
use pk_core::journal::Sink;

pub const TICK: Duration = Duration::from_secs(2);
pub const RESOLUTION_POLL: Duration = Duration::from_secs(15);
const RESOLUTION_GIVE_UP_S: f64 = 3600.0;
/// ~6 windows of 5 assets x 2 tokens.
const RECYCLE_POLY_AFTER_REMOVED: u64 = 60;
/// Kalshi answered 429 to a burst of per-asset discovery at restart.
const DISCOVER_STAGGER: Duration = Duration::from_millis(250);
const KALSHI_STATUS_S: f64 = 30.0;
/// Settled winners are redeemed while their window is still known.
const REDEEM_HORIZON_S: i64 = pk_engine::KEEP_WINDOWS_S;

pub struct Runtime {
    pub settings: Settings,
    pub engine: Mutex<Engine>,
    pub ingest: Arc<Ingest>,
    pub alerts: Alerts,
    pub discovery: Discovery,
    pub kalshi: Option<FeedHandle>,
    pub poly: Option<PolyShards>,
    pub link: Option<Arc<ObserverLink>>,
    /// Single-process mode (`PK_OBSERVER=false`): the tape is written here.
    pub recorder: Option<Mutex<Recorder>>,
    pub journal_direct: Option<SharedJournal>,
    pub exec: Mutex<Option<Arc<ExecBridge>>>,
    pub exec_tx: Mutex<Option<mpsc::UnboundedSender<ExecMsg>>>,
    pub started_ns: i64,
    pub lines: AtomicU64,
    pub engine_errors: AtomicU64,
    pub kalshi_closed: AtomicBool,
    kalshi_status_at: Mutex<Option<f64>>,
    discover_failed: Mutex<HashMap<(String, i64), f64>>,
    resolution_given_up: Mutex<HashSet<(String, i64, &'static str)>>,
    cpu: Mutex<(Instant, Duration, Option<f64>)>,
    pub telegram: Mutex<Option<Arc<crate::telegram::Stats>>>,
    pub stopping: AtomicBool,
    /// The order path has cancelled everything for the current halt.
    halt_cancelled: AtomicBool,
}

impl Runtime {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        settings: Settings,
        engine: Engine,
        ingest: Arc<Ingest>,
        alerts: Alerts,
        discovery: Discovery,
        kalshi: Option<FeedHandle>,
        poly: Option<PolyShards>,
        link: Option<Arc<ObserverLink>>,
        recorder: Option<Recorder>,
        journal_direct: Option<SharedJournal>,
    ) -> Self {
        Self {
            settings,
            engine: Mutex::new(engine),
            ingest,
            alerts,
            discovery,
            kalshi,
            poly,
            link,
            recorder: recorder.map(Mutex::new),
            journal_direct,
            exec: Mutex::new(None),
            exec_tx: Mutex::new(None),
            started_ns: now_ns(),
            lines: AtomicU64::new(0),
            engine_errors: AtomicU64::new(0),
            kalshi_closed: AtomicBool::new(false),
            kalshi_status_at: Mutex::new(None),
            discover_failed: Mutex::new(HashMap::new()),
            resolution_given_up: Mutex::new(HashSet::new()),
            cpu: Mutex::new((Instant::now(), cpu_time::ProcessTime::now().as_duration(), None)),
            telegram: Mutex::new(None),
            stopping: AtomicBool::new(false),
            halt_cancelled: AtomicBool::new(false),
        }
    }

    pub fn control(&self, body: Value) {
        self.ingest.push('c', body.to_string());
    }

    /// A record from outside the engine (the order path): stamped like the
    /// engine's and journaled the same way.
    pub fn journal(&self, t_ns: i64, mut record: Value) {
        if let Some(o) = record.as_object_mut() {
            for (k, v) in &self.engine.lock().stamp {
                o.insert(k.clone(), v.clone());
            }
        }
        if let Some(link) = &self.link {
            link.journal(t_ns, &record);
        } else if let Some(j) = &self.journal_direct {
            j.0.lock().write(t_ns, &record);
        }
    }

    // --- the engine thread ----------------------------------------------------------

    pub fn engine_loop(self: Arc<Self>, rx: Receiver<Line>) {
        while let Ok(Line { recv_ns, venue, text }) = rx.recv() {
            let picked = Instant::now();
            self.lines.fetch_add(1, Ordering::Relaxed);
            if let Some(link) = &self.link {
                link.line(recv_ns, venue, &text);
            } else if let Some(r) = &self.recorder {
                r.lock().write(venue, recv_ns, &text);
            }
            let (intents, hints) = {
                let mut e = self.engine.lock();
                let queue_us = (now_ns() - recv_ns) as f64 / 1e3;
                let ok = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    e.handle_line(recv_ns, venue, &text)
                }));
                if ok.is_err() {
                    // One bad frame must not stop the feed.
                    self.engine_errors.fetch_add(1, Ordering::Relaxed);
                    *e.counters.entry("engine_errors".into()).or_default() += 1;
                    tracing::error!(venue = %venue, "engine_error");
                }
                e.metrics.add("engine_us", picked.elapsed().as_secs_f64() * 1e6);
                e.metrics.add("queue_us", queue_us.max(0.0));
                (std::mem::take(&mut e.intents), std::mem::take(&mut e.hints))
            };
            if intents.is_empty() && hints.is_empty() {
                continue;
            }
            if let Some(tx) = self.exec_tx.lock().as_ref() {
                for h in hints {
                    let _ = tx.send(ExecMsg::Hint(h));
                }
                for i in intents {
                    let _ = tx.send(ExecMsg::Intent(i, picked));
                }
            }
        }
        tracing::info!("engine thread stopped");
    }

    // --- loops ----------------------------------------------------------------------

    pub async fn every(self: Arc<Self>, period: Duration, tick: fn(Arc<Self>) -> futures::BoxFuture) {
        let mut interval = tokio::time::interval(period);
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            interval.tick().await;
            if self.stopping.load(Ordering::Relaxed) {
                return;
            }
            tick(self.clone()).await;
        }
    }

    pub async fn loop_lag(self: Arc<Self>) {
        let every = Duration::from_millis(500);
        loop {
            let t = Instant::now();
            tokio::time::sleep(every).await;
            let lag = (t.elapsed().as_secs_f64() - every.as_secs_f64()) * 1e3;
            self.engine.lock().metrics.add("loop_lag_ms", lag.max(0.0));
        }
    }

    fn targets(&self, now: f64) -> Vec<i64> {
        let current = window_open(now as i64);
        let mut t = vec![current];
        if (current + WINDOW_S) as f64 - now <= self.settings.discover_lead_seconds as f64 {
            t.push(current + WINDOW_S);
        }
        t
    }

    async fn discover(&self, asset: &str, open_unix: i64, now: f64) {
        if self.engine.lock().has_window(asset, open_unix) {
            return;
        }
        let key = (asset.to_owned(), open_unix);
        if self.discover_failed.lock().get(&key).is_some_and(|t| now - t < 5.0) {
            return;
        }
        let window = match self.discovery.discover(asset, open_unix).await {
            Ok(w) => w,
            Err(e) => {
                tracing::warn!(asset, open_unix, error = %e, "discover_failed");
                None
            }
        };
        let Some(window) = window else {
            self.discover_failed.lock().insert(key, now);
            return;
        };
        self.discover_failed.lock().remove(&key);
        tracing::info!(asset, open_unix, kalshi = %window.kalshi_ticker, "window_identified");
        self.control(window.control());
        // Let the engine thread register it before the wanted sets are computed.
        for _ in 0..50 {
            if self.engine.lock().has_window(asset, open_unix) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    }

    pub async fn window_tick(self: Arc<Self>) {
        let now = now_ns() as f64 / 1e9;
        let targets = self.targets(now);
        let todo: Vec<String> = {
            let e = self.engine.lock();
            self.settings
                .assets
                .iter()
                .filter(|a| targets.iter().any(|o| !e.has_window(a, *o)))
                .cloned()
                .collect()
        };
        let jobs = todo.iter().enumerate().map(|(i, asset)| {
            let this = self.clone();
            let targets = targets.clone();
            let asset = asset.clone();
            async move {
                tokio::time::sleep(DISCOVER_STAGGER * i as u32).await;
                for o in targets {
                    this.discover(&asset, o, now).await;
                }
            }
        });
        futures::join_all(jobs).await;
        let live: Vec<Window> = {
            let e = self.engine.lock();
            self.settings
                .assets
                .iter()
                .flat_map(|a| targets.iter().filter_map(|o| e.window(a, *o).cloned()))
                .collect()
        };
        if let Some(k) = &self.kalshi {
            k.set_wanted(live.iter().map(|w| w.kalshi_ticker.clone()).collect());
        }
        if let Some(bridge) = self.exec.lock().clone() {
            bridge.prefetch(live.iter().flat_map(|w| [w.yes_token.clone(), w.no_token.clone()]));
        }
        let sec_into = now - targets[0] as f64;
        if let Some(p) = &self.poly {
            let mut groups: HashMap<String, BTreeSet<String>> = HashMap::new();
            for w in &live {
                groups.entry(w.asset.clone()).or_default().extend([w.yes_token.clone(), w.no_token.clone()]);
            }
            p.set_wanted_groups(&groups);
            if p.removed_since_connect() >= RECYCLE_POLY_AFTER_REMOVED && (1.0..8.0).contains(&sec_into) {
                // Inside entry_early_s: no decision can need these books yet.
                tracing::info!(removed = p.removed_since_connect(), "poly_recycle");
                p.recycle(RECYCLE_POLY_AFTER_REMOVED);
            }
        }
        if self.kalshi_closed.load(Ordering::Relaxed) {
            self.kalshi_is_closed(now).await; // notices the reopening
        }
        if sec_into > 30.0 {
            let missing: Vec<String> = {
                let e = self.engine.lock();
                self.settings.assets.iter().filter(|a| !e.has_window(a, targets[0])).cloned().collect()
            };
            if !missing.is_empty() && !self.kalshi_is_closed(now).await {
                self.alerts.send(
                    "unidentified",
                    "poly-kalshi: window not identified",
                    vec![format!("open_unix={} assets={}", targets[0], missing.join(","))],
                    false,
                );
            }
        }
    }

    /// Kalshi's trading status, asked only when an alert is due; an error reads as open.
    async fn kalshi_is_closed(&self, now: f64) -> bool {
        {
            let mut at = self.kalshi_status_at.lock();
            if at.is_some_and(|t| now - t < KALSHI_STATUS_S) {
                return self.kalshi_closed.load(Ordering::Relaxed);
            }
            *at = Some(now);
        }
        let closed = match self.discovery.kalshi_trading_active().await {
            Ok(active) => !active,
            Err(e) => {
                tracing::warn!(error = %e, "kalshi_status_failed");
                false
            }
        };
        if closed != self.kalshi_closed.swap(closed, Ordering::Relaxed) {
            tracing::info!(closed, "kalshi_trading_status");
            self.alerts.send(
                if closed { "kalshi_closed" } else { "kalshi_reopened" },
                &format!("poly-kalshi: Kalshi {}", if closed { "closed" } else { "reopened" }),
                vec![if closed {
                    "trading_active=false: no windows listed, feed and window alerts held".into()
                } else {
                    "trading_active=true: alerts resume".into()
                }],
                true,
            );
        }
        closed
    }

    pub async fn resolution_tick(self: Arc<Self>) {
        // After a restart, open positions need their window back to be settled.
        // Live winners need theirs to be redeemed.
        let missing: Vec<(String, i64)> = {
            let e = self.engine.lock();
            let horizon = now_ns() / pk_core::NS - REDEEM_HORIZON_S;
            e.ledger
                .positions
                .values()
                .filter(|p| {
                    p.is_open() || (e.live && p.payout.is_some_and(|x| x > 0.0) && p.open_unix > horizon)
                })
                .filter(|p| !e.has_window(&p.asset, p.open_unix))
                .map(|p| (p.asset.clone(), p.open_unix))
                .collect()
        };
        for (asset, open) in missing {
            if let Ok(Some(w)) = self.discovery.discover(&asset, open).await {
                self.control(w.control());
            }
        }
        let now = now_ns() as f64 / 1e9;
        let work: Vec<(Window, &'static str)> = {
            let e = self.engine.lock();
            let mut given_up = self.resolution_given_up.lock();
            let mut work = Vec::new();
            for w in e.all_windows() {
                let age = now - w.close_unix() as f64;
                if age < 20.0 {
                    continue;
                }
                for venue in ["poly", "kalshi"] {
                    let key = (w.asset.clone(), w.open_unix, venue);
                    if e.resolution_known(&w.asset, w.open_unix, venue) || given_up.contains(&key) {
                        continue;
                    }
                    if age > RESOLUTION_GIVE_UP_S {
                        tracing::warn!(asset = %w.asset, open_unix = w.open_unix, venue, "resolution_given_up");
                        given_up.insert(key);
                        continue;
                    }
                    work.push((w.clone(), venue));
                }
            }
            work
        };
        for chunk in work.chunks(8) {
            let polls = chunk.iter().cloned().map(|(w, venue)| {
                let this = self.clone();
                let w = w.clone();
                async move {
                    let w = &w;
                    let outcome = if venue == "poly" {
                        this.discovery.poly_outcome(w).await
                    } else {
                        this.discovery.kalshi_outcome(w).await
                    };
                    match outcome {
                        Ok(Some(o)) => this.control(json!({
                            "type": "resolution", "venue": venue, "asset": w.asset,
                            "open_unix": w.open_unix, "outcome": o,
                        })),
                        Ok(None) => {}
                        Err(e) => tracing::warn!(venue, error = %e, "resolution_poll_failed"),
                    }
                }
            });
            futures::join_all(polls).await;
        }
    }

    fn sample_cpu(&self) {
        let mut c = self.cpu.lock();
        let (wall, cpu) = (Instant::now(), cpu_time::ProcessTime::now().as_duration());
        let dt = wall.duration_since(c.0).as_secs_f64();
        if dt > 0.5 {
            c.2 = Some(((cpu.saturating_sub(c.1)).as_secs_f64() / dt * 1000.0).round() / 10.0);
        }
        c.0 = wall;
        c.1 = cpu;
    }

    pub fn host(&self) -> Value {
        let load1 = std::fs::read_to_string("/proc/loadavg")
            .ok()
            .and_then(|s| s.split_whitespace().next().and_then(|v| v.parse::<f64>().ok()));
        json!({
            "cpu_pct": self.cpu.lock().2,
            "load1": load1,
            "cpus": std::thread::available_parallelism().map(|n| n.get()).ok(),
            "loop": "tokio",
        })
    }

    pub async fn health_tick(self: Arc<Self>) {
        self.sample_cpu();
        self.cancel_orders_for_halt();
        if let Some(link) = &self.link {
            let l = link.clone();
            if let Ok(Some(why)) = tokio::task::spawn_blocking(move || l.check()).await {
                self.alerts.send(
                    "observer_down",
                    "poly-kalshi: the observer process died (tape and statistics paused)",
                    vec![why, "It was restarted; trading is unaffected.".into()],
                    false,
                );
            }
        }
        let now = now_ns();
        let stale = self.settings.stale_feed_seconds;
        let feeds: [(char, Option<String>, bool); 2] = [
            ('k', self.kalshi.as_ref().and_then(FeedHandle::last_error), self.kalshi.is_some()),
            ('p', self.poly.as_ref().and_then(PolyShards::last_error), self.poly.is_some()),
        ];
        for (venue, last_error, present) in feeds {
            if !present {
                continue;
            }
            let last = self.engine.lock().last_by_venue.get(&venue).copied().unwrap_or(self.started_ns);
            let age = (now - last) as f64 / NS as f64;
            if age > stale && !self.kalshi_is_closed(now as f64 / 1e9).await {
                let name = if venue == 'k' { "Kalshi" } else { "Polymarket" };
                self.alerts.send(
                    &format!("stale:{venue}"),
                    &format!("poly-kalshi: {name} feed stale"),
                    vec![format!("no message for {age:.0}s"), format!("last_error={last_error:?}")],
                    false,
                );
            }
        }
        if self.settings.heartbeat_seconds > 0.0 {
            let (books, counters, cash, open) = {
                let e = self.engine.lock();
                let l = e.ledger.snapshot(now);
                (e.books.len(), json!(e.counters), l["cash"].clone(), l["open_positions"].clone())
            };
            tracing::info!(
                lines = self.lines.load(Ordering::Relaxed),
                books,
                counters = %counters,
                host = %self.host(),
                cash = %cash,
                open_positions = %open,
                "heartbeat"
            );
        }
    }

    // --- control and status ---------------------------------------------------------

    pub fn kill(&self, source: &str, reason: &str) -> bool {
        let fired = self.engine.lock().risk.kill(source, reason);
        self.cancel_orders_for_halt();
        if fired {
            self.alerts.send(
                "kill",
                "poly-kalshi HALTED",
                vec![format!("source={source}"), if reason.is_empty() { "-".into() } else { reason.into() }],
                true,
            );
        }
        fired
    }

    /// Once per halt, cancel every order on the account (the kill file halts
    /// inside the engine, so the health tick calls this too).
    fn cancel_orders_for_halt(&self) {
        let halted = self.engine.lock().risk.halted;
        if !halted {
            self.halt_cancelled.store(false, Ordering::Relaxed);
            return;
        }
        if self.halt_cancelled.swap(true, Ordering::Relaxed) {
            return;
        }
        if let Some(bridge) = self.exec.lock().clone() {
            tokio::spawn(async move { bridge.cancel_all().await });
        }
    }

    /// Halt, cancel everything, and sell every open live position.
    pub async fn flatten(&self, source: &str) -> Value {
        self.kill(source, "flatten");
        let bridge = self.exec.lock().clone();
        match bridge {
            Some(b) if b.live.is_some() => {
                let results = b.flatten().await;
                self.alerts.send(
                    "flatten",
                    "poly-kalshi: flattened",
                    results.iter().map(Value::to_string).collect(),
                    true,
                );
                json!({"halted": true, "flattened": results})
            }
            _ => json!({"halted": true, "flattened": [], "note": "not live: nothing to sell"}),
        }
    }

    /// The ledger as the live reconciler needs it.
    pub fn live_view(&self) -> LiveView {
        let now = now_ns();
        let e = self.engine.lock();
        let held = |p: &pk_core::Position| {
            let w = e.window(&p.asset, p.open_unix);
            Held {
                asset: p.asset.clone(),
                open_unix: p.open_unix,
                side: p.side.clone(),
                shares: p.shares,
                token: w.map(|w| if p.side == "yes" { w.yes_token.clone() } else { w.no_token.clone() }),
                condition_id: w.map(|w| w.condition_id.clone()),
                fee_rate: w.map_or(pk_core::fees::DEFAULT_RATE, |w| w.fee_rate),
                fee_exponent: w.map_or(pk_core::fees::DEFAULT_EXPONENT, |w| w.fee_exponent),
            }
        };
        let horizon = now / pk_core::NS - REDEEM_HORIZON_S;
        LiveView {
            open: e.ledger.open_positions().map(held).collect(),
            winners: e
                .ledger
                .positions
                .values()
                .filter(|p| {
                    p.status == "settled" && p.payout.is_some_and(|x| x > 0.0) && p.open_unix > horizon
                })
                .map(held)
                .collect(),
            in_flight: e.ledger.in_flight(),
            day_drawdown: e.ledger.day_drawdown(now),
            daily_loss_limit: e.risk.limits.daily_loss_limit_usd,
        }
    }

    pub fn ready(&self) -> (bool, Value) {
        let feeds = json!({
            "kalshi": self.kalshi.as_ref().map(FeedHandle::connected),
            "poly": self.poly.as_ref().map(PolyShards::connected),
        });
        let e = self.engine.lock();
        let identified: serde_json::Map<String, Value> =
            e.assets.iter().zip(&e.state).map(|(a, s)| (a.clone(), s.window.is_some().into())).collect();
        let feeds_ok = feeds.as_object().is_some_and(|f| f.values().all(|v| v != &Value::Bool(false)));
        let ok = feeds_ok && identified.values().all(|v| v == &Value::Bool(true));
        (ok, json!({"feeds": feeds, "identified": identified}))
    }

    pub fn status(&self) -> Value {
        let s = &self.settings;
        let (engine_status, stamp, recent, notional, latency_ms) = {
            let e = self.engine.lock();
            let recent: Vec<Value> = e.recent.iter().rev().take(50).cloned().collect();
            let latency = e.venue.map(|v| v.latency_ns as f64 / 1e6);
            (e.status(), e.stamp.clone(), recent, e.params.notional_usd, latency)
        };
        json!({
            "profile": s.profile,
            "execution_mode": s.execution_mode.as_str(),
            "epoch": s.results_epoch,
            "build": stamp.get("build"),
            "config": stamp.get("config"),
            "host": s.host_tag,
            "assets": s.assets,
            "params": {
                "gap_threshold": s.params.gap_threshold,
                "max_entry_px": s.params.max_entry_px,
                "notional_usd": notional,
                "order_size_pct": s.order_size_pct,
                "entry_early_s": s.params.entry_early_s,
                "entry_late_s": s.params.entry_late_s,
                "paper_latency_ms": latency_ms.unwrap_or(s.paper_latency_ms as f64),
                "paper_latency_seed_ms": s.paper_latency_ms,
                "limit_offset": s.limit_offset,
                "max_poly_spread": s.params.max_poly_spread,
                "max_poly_lag_ms": s.params.max_poly_lag_ms,
            },
            "started_ns": self.started_ns,
            "host_load": self.host(),
            "telegram": self.telegram.lock().as_ref().map(|t| t.json()),
            "lines": self.lines.load(Ordering::Relaxed),
            "queued": self.ingest.queued(),
            "feeds": {
                "kalshi": self.kalshi.as_ref().map(FeedHandle::stats),
                "poly": self.poly.as_ref().map(PolyShards::stats),
            },
            "recorder": match &self.recorder {
                Some(r) => serde_json::to_value(r.lock().stats()).unwrap_or(Value::Null),
                None => json!({"enabled": self.link.is_some(), "in": "observer"}),
            },
            "observer": self.link.as_ref().map(|l| l.stats()).unwrap_or(json!({"enabled": false})),
            "order_path": self.exec.lock().as_ref().map(|x| x.stats()).unwrap_or(json!({"enabled": false})),
            "alerts": self.alerts.snapshot(),
            "kalshi_closed": self.kalshi_closed.load(Ordering::Relaxed),
            "engine": engine_status,
            "recent": recent,
        })
    }
}

/// Minimal future helpers, so the crate needs no `futures` dependency.
pub mod futures {
    use std::future::Future;
    use std::pin::Pin;

    pub type BoxFuture = Pin<Box<dyn Future<Output = ()> + Send>>;

    pub async fn join_all<F: Future<Output = ()> + Send + 'static>(futs: impl IntoIterator<Item = F>) {
        let handles: Vec<_> = futs.into_iter().map(tokio::spawn).collect();
        for h in handles {
            let _ = h.await;
        }
    }
}
