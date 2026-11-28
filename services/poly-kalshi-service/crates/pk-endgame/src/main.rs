//! `pk-endgame`: in the last minute of a Polymarket 15-minute Up/Down window,
//! most of the settlement average (the Chainlink 60 s TWAP) is already
//! printed. This process prices what is left, and buys the side the book
//! still underprices. Paper by default; a process of its own (own sockets,
//! state, journal), sharing nothing with pkm or the trader.
//!
//! | Command | |
//! |---|---|
//! | `pk-endgame --env FILE run` | run (paper unless `PK_EG_MODE=live` and `PK_EG_LIVE_CONFIRM=yes`) |
//! | `pk-endgame --env FILE check` | live only: authenticate, read the cash, send nothing |
//! | `touch <state>/KILL` | stop new entries (remove the file to resume) |
//! | `touch <state>/ACK` | clear today's loss halt |
//! | `http://<PK_EG_UI_ADDR>/` | the monitoring page (`/api/summary` is its JSON) |
//!
//! The journal is `<state>/journal/YYYY-MM-DD.jsonl`; `scripts/eg_review.py`
//! reads it.

mod config;
mod engine;
mod live;
mod model;
mod rtds;
mod summary;
mod web;

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use clap::{Parser, Subcommand};
use parking_lot::Mutex;
use pk_core::journal::{JsonlDaily, Sink};
use pk_core::window::{Window, poly_slug};
use pk_feeds::{Discovery, FeedHandle, FeedKind, KalshiSigner, spawn_feed};
use serde_json::Value;

use crate::config::{Config, Mode};
use crate::engine::{Action, Engine, NS, Persisted, WINDOW_S, WindowInfo};
use crate::live::Exec;
use crate::rtds::{RtdsStats, now_ns};
use crate::summary::Summary;

#[derive(Parser)]
#[command(name = "pk-endgame", about = "Chainlink-TWAP endgame entries on Polymarket's 15-minute markets")]
struct Args {
    /// Settings file (KEY=VALUE); the process environment wins over it.
    #[arg(long)]
    env: Option<PathBuf>,
    #[command(subcommand)]
    cmd: Option<Cmd>,
}

#[derive(Subcommand, Clone, Copy)]
enum Cmd {
    Run,
    Check,
}

/// A socket frame, queued by the feed's reader and applied by one task, so
/// the readers never wait on the engine lock (Polymarket closes slow
/// consumers with 1013).
enum Frame {
    Poly(char, String, i64),
    Kalshi(char, String, i64),
    Spot(&'static str, String, i64),
}

struct App {
    engine: Mutex<Engine>,
    journal: Mutex<JsonlDaily>,
    exec: Option<Arc<Exec>>,
    discovery: Discovery,
    state_path: PathBuf,
    summary: Mutex<Summary>,
    started_ns: i64,
    poly: Mutex<Option<FeedHandle>>,
    kalshi: Mutex<Option<FeedHandle>>,
    spot: Mutex<Vec<FeedHandle>>,
    rtds: Mutex<Option<Arc<RtdsStats>>>,
}

fn load_state(p: &Path) -> anyhow::Result<Persisted> {
    if !p.is_file() {
        return Ok(Persisted::default());
    }
    Ok(serde_json::from_str(&std::fs::read_to_string(p)?)?)
}

fn save_state(p: &Path, s: &Persisted) {
    let tmp = p.with_extension("json.tmp");
    let r = serde_json::to_vec_pretty(s)
        .map_err(std::io::Error::other)
        .and_then(|b| std::fs::write(&tmp, b))
        .and_then(|()| std::fs::rename(&tmp, p));
    if let Err(e) = r {
        tracing::error!(error = %e, "state_write");
    }
}

/// A Gamma market as this process needs it.
fn window_info(asset: &str, open: i64, m: &Value) -> anyhow::Result<WindowInfo> {
    let list = |k: &str| -> Vec<String> {
        m.get(k)
            .and_then(Value::as_str)
            .and_then(|s| serde_json::from_str::<Vec<Value>>(s).ok())
            .unwrap_or_default()
            .into_iter()
            .map(|v| v.as_str().map(str::to_owned).unwrap_or_else(|| v.to_string()))
            .collect()
    };
    let outcomes: Vec<String> = list("outcomes").iter().map(|o| o.to_lowercase()).collect();
    let tokens = list("clobTokenIds");
    anyhow::ensure!(outcomes == ["up", "down"] && tokens.len() == 2, "{asset} {open}: outcomes {outcomes:?}");
    let fees_enabled = m.get("feesEnabled").and_then(Value::as_bool).unwrap_or(true);
    let fees = m.get("feeSchedule").cloned().unwrap_or(Value::Null);
    let exponent = fees.get("exponent").and_then(Value::as_f64).unwrap_or(pk_core::fees::DEFAULT_EXPONENT);
    let rate = if fees_enabled { fees.get("rate").and_then(Value::as_f64).unwrap_or(pk_core::fees::DEFAULT_RATE) } else { 0.0 };
    Ok(WindowInfo {
        asset: asset.to_owned(),
        open,
        up_token: tokens[0].clone(),
        down_token: tokens[1].clone(),
        condition_id: m.get("conditionId").and_then(Value::as_str).unwrap_or_default().to_owned(),
        slug: poly_slug(asset, open),
        fee_rate: rate,
        fee_exponent: exponent,
        kalshi_ticker: None,
    })
}

impl App {
    fn flush(&self) {
        let (records, dirty) = {
            let mut e = self.engine.lock();
            let d = std::mem::take(&mut e.dirty);
            (e.take_records(), d.then(|| serde_json::to_value(&e.p).ok()).flatten())
        };
        if !records.is_empty() {
            {
                let mut sm = self.summary.lock();
                for r in records.iter().filter(|r| r["type"] != "eg_sec") {
                    sm.ingest(r);
                }
            }
            let mut j = self.journal.lock();
            for r in &records {
                j.write(r.get("t_ns").and_then(Value::as_i64).unwrap_or_else(now_ns), r);
                if matches!(r["type"].as_str(), Some("eg_decision" | "eg_fill" | "eg_no_fill" | "eg_settle" | "eg_halt" | "eg_kill")) {
                    tracing::info!(target: "eg", "{r}");
                }
            }
        }
        if let Some(v) = dirty
            && let Ok(p) = serde_json::from_value::<Persisted>(v)
        {
            save_state(&self.state_path, &p);
        }
    }

    /// The page's data: the summary, with the engine and feeds as they are now.
    fn snapshot(&self) -> Value {
        let t = now_ns();
        let (mut live, refit) = {
            let e = self.engine.lock();
            let mut v = e.status_value(t);
            v["host"] = e.cfg.host_tag.clone().into();
            (v, e.cfg.state_dir.join("refit.json"))
        };
        live["uptime_s"] = ((t - self.started_ns) / NS).into();
        live["poly_connected"] = self.poly.lock().as_ref().is_some_and(FeedHandle::connected).into();
        live["kalshi_connected"] = self.kalshi.lock().as_ref().map(FeedHandle::connected).into();
        live["spot_connected"] = self.spot.lock().iter().map(FeedHandle::connected).collect::<Vec<_>>().into();
        live["rtds_connected"] = self
            .rtds
            .lock()
            .as_ref()
            .is_some_and(|r| r.connected.load(std::sync::atomic::Ordering::Relaxed))
            .into();
        let mut out = self.summary.lock().snapshot(live, t);
        out["refit"] =
            std::fs::read_to_string(refit).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or(Value::Null);
        out
    }

    /// Applies queued frames in batches under one lock, then evaluates each
    /// asset whose book changed (`trigger` "book").
    async fn apply_frames(self: Arc<Self>, mut rx: tokio::sync::mpsc::UnboundedReceiver<Frame>) {
        let mut buf = Vec::with_capacity(512);
        while rx.recv_many(&mut buf, 512).await > 0 {
            let actions = {
                let mut e = self.engine.lock();
                let mut touched: BTreeSet<usize> = BTreeSet::new();
                // Assets whose Kalshi book changed (the Kalshi-lead rule's trigger).
                let mut k_touched: BTreeSet<usize> = BTreeSet::new();
                for f in buf.drain(..) {
                    match f {
                        Frame::Poly('p', text, t) => touched.extend(e.on_poly(&text, t).into_iter().map(|w| w.0)),
                        Frame::Poly('c', text, t) if text.contains("\"venue\":\"p\"") => {
                            if text.contains("\"reconnect\"") {
                                e.invalidate_books();
                            }
                            e.on_feed_event("poly", "reconnect", &text, t);
                        }
                        Frame::Kalshi('k', text, t) => k_touched.extend(e.on_kalshi(&text, t)),
                        Frame::Kalshi('c', text, t) if text.contains("\"venue\":\"k\"") => {
                            e.invalidate_kalshi();
                            e.on_feed_event("kalshi", if text.contains("resync") { "resync" } else { "reconnect" }, &text, t);
                        }
                        Frame::Spot(venue, text, t) => {
                            if text.contains("pk_reconnect") {
                                e.on_feed_event(venue, "reconnect", &text, t);
                            } else {
                                e.on_spot_frame(venue, &text, t);
                            }
                        }
                        _ => {}
                    }
                }
                let t = now_ns();
                for &i in &k_touched {
                    e.evaluate_kg(i, t, "kalshi");
                }
                for &i in &touched {
                    e.evaluate_kg(i, t, "book");
                }
                touched.into_iter().filter_map(|i| e.evaluate_now(i, t, "book")).collect::<Vec<_>>()
            };
            self.dispatch(actions);
        }
    }

    fn dispatch(self: &Arc<Self>, actions: Vec<Action>) {
        for a in actions {
            let Some(exec) = self.exec.clone() else { continue };
            let app = self.clone();
            tokio::spawn(async move {
                match a {
                    Action::Enter { id, asset, token, limit, shares } => {
                        let r = exec.enter(&token, limit, shares).await;
                        tracing::info!(id, asset, shares = r.shares, usd = r.usd, error = ?r.error, "eg_live_ack");
                        app.engine.lock().on_live(id, r, now_ns());
                    }
                    Action::Redeem { condition_id } => {
                        let r = exec.redeem(&condition_id).await.map_err(|e| format!("{e:#}"));
                        app.engine.lock().on_redeemed(&condition_id, r, now_ns());
                    }
                }
                app.flush();
            });
        }
    }

    async fn discover(&self, assets: &[String]) {
        let now = now_ns() / NS;
        let open = now - now.rem_euclid(WINDOW_S);
        for asset in assets {
            for o in [open, open + WINDOW_S] {
                let known = {
                    let e = self.engine.lock();
                    let Some(i) = e.asset_index(asset) else { continue };
                    e.has_window(i, o)
                };
                if known {
                    continue;
                }
                match self.discovery.poly_market(asset, o).await {
                    Ok(Some(m)) => match window_info(asset, o, &m) {
                        Ok(w) => self.engine.lock().set_window(w, now_ns()),
                        Err(e) => tracing::warn!(error = %e, "eg_window_parse"),
                    },
                    Ok(None) => {}
                    Err(e) => tracing::warn!(asset, open = o, error = %e, "eg_discovery"),
                }
            }
        }
        // The Kalshi market on each window (series KX<ASSET>15M, closing at the same instant).
        if self.kalshi.lock().is_some() {
            let missing = self.engine.lock().missing_kalshi(now);
            for (asset, o) in missing {
                match self.discovery.kalshi_market(&asset, o).await {
                    Ok(Some(m)) => {
                        if let Some(t) = m.get("ticker").and_then(Value::as_str) {
                            self.engine.lock().set_kalshi_ticker(&asset, o, t, now_ns());
                        }
                    }
                    Ok(None) => {}
                    Err(e) => tracing::warn!(asset, open = o, error = %e, "eg_kalshi_discovery"),
                }
            }
        }
    }

    async fn resolve(self: &Arc<Self>) {
        let due = self.engine.lock().to_resolve(now_ns() / NS);
        for w in due.into_iter().take(30) {
            let win = Window {
                asset: w.asset.clone(),
                open_unix: w.open,
                kalshi_ticker: String::new(),
                yes_token: w.up_token.clone(),
                no_token: w.down_token.clone(),
                condition_id: w.condition_id.clone(),
                fee_rate: w.fee_rate,
                fee_exponent: w.fee_exponent,
            };
            match self.discovery.poly_outcome(&win).await {
                Ok(Some(outcome)) => {
                    let acts = self.engine.lock().on_resolved(&w.asset, w.open, outcome, now_ns());
                    self.dispatch(acts);
                }
                Ok(None) => {}
                Err(e) => tracing::warn!(asset = w.asset, open = w.open, error = %e, "eg_outcome"),
            }
        }
        self.flush();
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .init();
    // Both rustls providers are compiled in through the dependencies: pick one.
    let _ = rustls::crypto::ring::default_provider().install_default();
    let args = Args::parse();
    let cfg = Config::load(args.env.as_deref())?;
    std::fs::create_dir_all(&cfg.state_dir)?;
    match args.cmd.unwrap_or(Cmd::Run) {
        Cmd::Check => check(cfg).await,
        Cmd::Run => run(cfg).await,
    }
}

/// Live trading only where Polymarket allows it from this host: its own
/// geoblock answer, failing closed. France, the UK, the US and others are
/// blocked or close-only; paper mode never asks.
async fn geoblock_ok() -> anyhow::Result<String> {
    let v: Value = reqwest::Client::builder()
        .user_agent(pk_feeds::discovery::USER_AGENT)
        .timeout(Duration::from_secs(10))
        .build()?
        .get("https://polymarket.com/api/geoblock")
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    let country = v.get("country").and_then(Value::as_str).unwrap_or("?").to_owned();
    let flagged = |k: &str| v.get(k).and_then(Value::as_bool) == Some(true);
    anyhow::ensure!(
        !flagged("blocked") && !flagged("closeOnly") && !flagged("close_only"),
        "Polymarket does not allow opening positions from this host ({country}): {v}"
    );
    Ok(country)
}

async fn check(cfg: Config) -> anyhow::Result<()> {
    anyhow::ensure!(cfg.mode == Mode::Live, "check is for live settings (PK_EG_MODE=live, PK_EG_LIVE_CONFIRM=yes)");
    println!("geoblock: allowed from {}", geoblock_ok().await?);
    let exec = Exec::connect(&cfg).await?;
    println!("authenticated; cash {:.2} pUSD; nothing was sent", exec.cash().await?);
    Ok(())
}

async fn run(cfg: Config) -> anyhow::Result<()> {
    let state_path = cfg.state_dir.join("state.json");
    let persisted = load_state(&state_path)?;
    let exec = if cfg.mode == Mode::Live {
        let country = geoblock_ok().await?;
        tracing::info!(country, "geoblock: allowed");
        let e = Arc::new(Exec::connect(&cfg).await?);
        tracing::warn!(cash = ?e.cash().await.ok(), "pk-endgame LIVE: real orders");
        Some(e)
    } else {
        tracing::info!("pk-endgame paper: decisions and fills are simulated, nothing is sent");
        None
    };
    let app = Arc::new(App {
        engine: Mutex::new(Engine::new(cfg.clone(), persisted)),
        journal: Mutex::new(JsonlDaily::new(cfg.state_dir.join("journal"))),
        exec,
        discovery: Discovery::new(&cfg.gamma_url, &cfg.kalshi_rest_url),
        summary: Mutex::new(Summary::load(&cfg.state_dir.join("journal"), &cfg.epoch)),
        started_ns: now_ns(),
        state_path,
        poly: Mutex::new(None),
        kalshi: Mutex::new(None),
        spot: Mutex::new(Vec::new()),
        rtds: Mutex::new(None),
    });
    if let Some(addr) = cfg.ui_addr {
        let a = app.clone();
        let token = cfg.ui_token.clone();
        tokio::spawn(async move {
            if let Err(e) = web::serve(addr, token, Arc::new(move || a.snapshot())).await {
                tracing::error!(error = %e, "eg_ui_failed");
            }
        });
    }

    // Socket frames go through one queue to one applier task.
    let (ftx, frx) = tokio::sync::mpsc::unbounded_channel::<Frame>();
    tokio::spawn(app.clone().apply_frames(frx));
    // Polymarket books.
    let tx = ftx.clone();
    let poly = spawn_feed(
        cfg.poly_ws_url.clone(),
        FeedKind::Poly { shard: None, silence: Some(Duration::from_secs(30)) },
        Arc::new(move |venue, text| {
            let _ = tx.send(Frame::Poly(venue, text, now_ns()));
        }),
    );
    *app.poly.lock() = Some(poly.clone());
    // Chainlink.
    let a = app.clone();
    let rtds = rtds::spawn(
        cfg.rtds_url.clone(),
        Arc::new(move |asset, sec, px, t| {
            let action = {
                let mut e = a.engine.lock();
                e.asset_index(asset).and_then(|i| {
                    e.on_tick(i, sec, px, t);
                    e.evaluate_now(i, t, "chainlink")
                })
            };
            if let Some(act) = action {
                a.dispatch(vec![act]);
            }
        }),
    );

    *app.rtds.lock() = Some(rtds.clone());
    // Spot: Coinbase and Binance best bid/ask, recorded beside the model (no rule reads it).
    let mut spot_feeds: Vec<FeedHandle> = Vec::new();
    if cfg.spot {
        for (venue, url, kind, ids) in [
            ("cb", cfg.coinbase_ws_url.clone(), FeedKind::Coinbase,
             cfg.assets.iter().map(|a| format!("{a}-USD")).collect::<std::collections::BTreeSet<_>>()),
            ("bn", cfg.binance_ws_url.clone(), FeedKind::Binance { futures: false },
             cfg.assets.iter().map(|a| format!("{}usdt@bookTicker", a.to_lowercase())).collect()),
        ] {
            let tx = ftx.clone();
            let h = spawn_feed(
                url,
                kind,
                Arc::new(move |_venue, text| {
                    let _ = tx.send(Frame::Spot(venue, text, now_ns()));
                }),
            );
            h.set_wanted(ids);
            spot_feeds.push(h);
        }
        tracing::info!("spot feeds on (recorded only)");
    }
    *app.spot.lock() = spot_feeds;
    // Kalshi order books (read-only), when a key is configured.
    if let (Some(id), Some(path)) = (&cfg.kalshi_key_id, &cfg.kalshi_key_path) {
        let pem = std::fs::read_to_string(path)
            .map_err(|e| anyhow::anyhow!("KALSHI_PRIVATE_KEY_PATH {}: {e}", path.display()))?;
        let signer = Arc::new(KalshiSigner::from_pem(id, &pem)?);
        let tx = ftx.clone();
        let feed = spawn_feed(
            cfg.kalshi_ws_url.clone(),
            FeedKind::Kalshi { signer },
            Arc::new(move |venue, text| {
                let _ = tx.send(Frame::Kalshi(venue, text, now_ns()));
            }),
        );
        *app.kalshi.lock() = Some(feed);
        tracing::info!("kalshi feed on");
    } else {
        tracing::warn!("no Kalshi key: the Kalshi filter cannot pass (PK_EG_KALSHI_FILTER=no to run without it)");
    }
    // Discovery, every 15 s.
    let a = app.clone();
    let assets = cfg.assets.clone();
    tokio::spawn(async move {
        loop {
            a.discover(&assets).await;
            let (wanted, wanted_k) = {
                let e = a.engine.lock();
                (e.wanted_tokens(now_ns() / NS), e.wanted_kalshi(now_ns() / NS))
            };
            poly.set_wanted(wanted);
            if let Some(k) = a.kalshi.lock().as_ref() {
                k.set_wanted(wanted_k);
            }
            a.flush();
            tokio::time::sleep(Duration::from_secs(15)).await;
        }
    });
    // Resolutions, every 15 s; redemption retries every 5 min.
    let a = app.clone();
    tokio::spawn(async move {
        let mut n = 0u64;
        loop {
            tokio::time::sleep(Duration::from_secs(15)).await;
            a.resolve().await;
            n += 1;
            if n.is_multiple_of(20) {
                let due = a.engine.lock().redeem_due();
                a.dispatch(due.into_iter().map(|condition_id| Action::Redeem { condition_id }).collect());
            }
        }
    });

    // The clock: entries, paper fills, kill and ack files, status.
    let kill = cfg.kill_file.clone();
    let ack = cfg.state_dir.join("ACK");
    let mut step = tokio::time::interval(Duration::from_millis(100));
    step.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut last_status = 0i64;
    let (mut rtds_connects, mut rtds_errors) = (0u64, 0u64);
    let shutdown = tokio::signal::ctrl_c();
    tokio::pin!(shutdown);
    loop {
        tokio::select! {
            _ = &mut shutdown => break,
            _ = step.tick() => {}
        }
        let t = now_ns();
        let actions = {
            let mut e = app.engine.lock();
            e.set_kill(kill.exists(), t);
            if ack.exists() {
                e.ack(t);
                let _ = std::fs::remove_file(&ack);
            }
            e.tick(t)
        };
        app.dispatch(actions);
        {
            use std::sync::atomic::Ordering::Relaxed;
            let (c, er) = (rtds.connects.load(Relaxed), rtds.errors.load(Relaxed));
            if er > rtds_errors || (c > rtds_connects && rtds_connects > 0) {
                let mut e = app.engine.lock();
                if er > rtds_errors {
                    e.on_feed_event("chainlink", "error", &format!("errors {er}"), t);
                }
                if c > rtds_connects && rtds_connects > 0 {
                    e.on_feed_event("chainlink", "reconnect", &format!("connects {c}"), t);
                }
            }
            (rtds_connects, rtds_errors) = (c, er);
        }
        if t - last_status >= 60 * NS {
            last_status = t;
            let mut e = app.engine.lock();
            e.prune(t / NS);
            e.status(t);
            drop(e);
            tracing::info!(
                rtds_connected = rtds.connected.load(std::sync::atomic::Ordering::Relaxed),
                rtds_ticks = rtds.ticks.load(std::sync::atomic::Ordering::Relaxed),
                "eg_alive"
            );
        }
        app.flush();
    }
    tracing::info!("stopping");
    let p = serde_json::to_value(&app.engine.lock().p)?;
    save_state(&app.state_path, &serde_json::from_value(p)?);
    app.flush();
    Ok(())
}
