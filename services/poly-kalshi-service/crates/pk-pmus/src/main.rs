//! `pk-pmus`: the poly-kalshi gap rule with Polymarket US as the venue, in
//! paper. Kalshi's order book (WebSocket, read-only) is the fair value;
//! Polymarket US's public gateway is polled for the quote, its book for paper
//! fills and its settlement for results. Nothing is ever sent to either venue.
//!
//! | Command | |
//! |---|---|
//! | `pk-pmus --env FILE` | run (paper only) |
//! | `touch <state>/KILL` | stop new entries (remove the file to resume) |
//! | `touch <state>/ACK` | clear today's loss halt |
//! | `http://<PK_PU_UI_ADDR>/` | the monitoring page (`/api/summary` is its JSON) |

mod config;
mod engine;
mod summary;
mod web;

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering::Relaxed};
use std::time::Duration;

use clap::Parser;
use parking_lot::Mutex;
use pk_core::journal::{JsonlDaily, Sink};
use pk_core::window::Window;
use pk_feeds::{Discovery, FeedHandle, FeedKind, KalshiSigner, spawn_feed};
use serde_json::Value;

use crate::config::{Config, slug_prefix};
use crate::engine::{Engine, FetchBook, NS, Persisted, WINDOW_S, WindowInfo};
use crate::summary::Summary;

#[derive(Parser)]
#[command(name = "pk-pmus", about = "Kalshi-priced paper entries on Polymarket US 15-minute crypto markets")]
struct Args {
    /// Settings file (KEY=VALUE); the process environment wins over it.
    #[arg(long)]
    env: Option<PathBuf>,
}

pub fn now_ns() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as i64)
        .unwrap_or(0)
}

struct App {
    engine: Mutex<Engine>,
    journal: Mutex<JsonlDaily>,
    http: reqwest::Client,
    gateway: String,
    discovery: Discovery,
    state_path: PathBuf,
    summary: Mutex<Summary>,
    started_ns: i64,
    kalshi: Mutex<Option<FeedHandle>>,
    poll: PollStats,
    /// Windows with a polling task.
    polling: Mutex<HashSet<(usize, i64)>>,
    poll_ms: u64,
    paper_latency_ms: i64,
    paper_delays_ms: Vec<i64>,
}

/// Book requests by outcome. `not_open` is a market before it trades (404 or
/// a closed state); the rest are errors. `cache_hits` must stay 0: the
/// gateway's CDN caches books for 30 s and every request is cache-busted.
#[derive(Default)]
struct PollStats {
    ok: AtomicU64,
    not_open: AtomicU64,
    http_404: AtomicU64,
    http_429: AtomicU64,
    http_4xx: AtomicU64,
    http_5xx: AtomicU64,
    timeout: AtomicU64,
    other: AtomicU64,
    cache_hits: AtomicU64,
}

impl PollStats {
    fn errors(&self) -> u64 {
        [&self.http_404, &self.http_429, &self.http_4xx, &self.http_5xx, &self.timeout, &self.other]
            .iter()
            .map(|a| a.load(Relaxed))
            .sum()
    }

    fn value(&self) -> Value {
        let g = |a: &AtomicU64| a.load(Relaxed);
        serde_json::json!({
            "ok": g(&self.ok), "not_open": g(&self.not_open), "http_404": g(&self.http_404),
            "http_429": g(&self.http_429), "http_4xx": g(&self.http_4xx), "http_5xx": g(&self.http_5xx),
            "timeout": g(&self.timeout), "other": g(&self.other), "cache_hits": g(&self.cache_hits),
        })
    }
}

#[derive(Debug)]
enum BookErr {
    Status(u16),
    Timeout,
    Other(String),
}

impl std::fmt::Display for BookErr {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            BookErr::Status(c) => write!(f, "HTTP {c}"),
            BookErr::Timeout => write!(f, "timeout"),
            BookErr::Other(e) => write!(f, "{e}"),
        }
    }
}

/// A Polymarket US book as fetched.
struct Book {
    bids: Vec<(f64, f64)>,
    offers: Vec<(f64, f64)>,
    state: Option<String>,
    /// `transactTime`: the book's last change.
    venue_ns: Option<i64>,
    /// The CDN's `Age` when it served a cached copy (should never happen).
    cache_age_s: Option<i64>,
}

impl Book {
    fn open(&self) -> bool {
        self.state.as_deref().is_none_or(|s| s == "MARKET_STATE_OPEN")
    }

    fn top(&self) -> (Option<f64>, Option<f64>) {
        let bid = self.bids.iter().filter(|l| l.1 > 0.0).map(|l| l.0).reduce(f64::max);
        let ask = self.offers.iter().filter(|l| l.1 > 0.0).map(|l| l.0).reduce(f64::min);
        (bid, ask)
    }
}

fn parse_book(v: &Value, cache_age_s: Option<i64>) -> Book {
    let md = v.get("marketData").unwrap_or(v);
    Book {
        bids: levels(md.get("bids").unwrap_or(&Value::Null)),
        offers: levels(md.get("offers").unwrap_or(&Value::Null)),
        state: md.get("state").and_then(Value::as_str).map(str::to_owned),
        venue_ns: md
            .get("transactTime")
            .and_then(Value::as_str)
            .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
            .and_then(|d| d.timestamp_nanos_opt()),
        cache_age_s,
    }
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

/// `cpc-btc-updown-15m-2026-11-25-1415z` for the window opening at 14:15 UTC.
pub fn pm_slug(asset: &str, open: i64) -> Option<String> {
    let p = slug_prefix(asset)?;
    let d = chrono::DateTime::<chrono::Utc>::from_timestamp(open, 0)?;
    Some(format!("{p}-updown-15m-{}", d.format("%Y-%m-%d-%H%Mz")))
}

fn f(v: &Value) -> Option<f64> {
    v.as_f64().or_else(|| v.as_str().and_then(|s| s.parse().ok()))
}

/// `[{"px":{"value":"0.36"},"qty":"72"}, …]` as (price, qty).
fn levels(v: &Value) -> Vec<(f64, f64)> {
    v.as_array()
        .into_iter()
        .flatten()
        .filter_map(|l| Some((f(l.get("px")?.get("value")?)?, f(l.get("qty")?)?)))
        .collect()
}

impl App {
    async fn get(&self, path: &str) -> anyhow::Result<Value> {
        let r = self.http.get(format!("{}{path}", self.gateway)).send().await?;
        let status = r.status();
        anyhow::ensure!(status.is_success(), "{path}: HTTP {status}");
        Ok(r.json().await?)
    }

    fn flush(&self) {
        let (records, dirty) = {
            let mut e = self.engine.lock();
            let d = std::mem::take(&mut e.dirty);
            (e.take_records(), d.then(|| serde_json::to_value(&e.p).ok()).flatten())
        };
        if !records.is_empty() {
            {
                let mut sm = self.summary.lock();
                for r in records.iter().filter(|r| r["type"] != "pu_sec") {
                    sm.ingest(r);
                }
            }
            let mut j = self.journal.lock();
            for r in &records {
                j.write(r.get("t_ns").and_then(Value::as_i64).unwrap_or_else(now_ns), r);
                if matches!(
                    r["type"].as_str(),
                    Some(
                        "pu_decision"
                            | "pu_fill"
                            | "pu_no_fill"
                            | "pu_settle"
                            | "pu_halt"
                            | "pu_kill"
                            | "pu_window"
                    )
                ) {
                    tracing::info!(target: "pu", "{r}");
                }
            }
        }
        if let Some(v) = dirty
            && let Ok(p) = serde_json::from_value::<Persisted>(v)
        {
            save_state(&self.state_path, &p);
        }
    }

    fn snapshot(&self) -> Value {
        let t = now_ns();
        let mut live = {
            let e = self.engine.lock();
            let mut v = e.status_value(t);
            v["host"] = e.cfg.host_tag.clone().into();
            v["poll_ms"] = e.cfg.poll_ms.into();
            v
        };
        live["uptime_s"] = ((t - self.started_ns) / NS).into();
        live["kalshi_connected"] = self.kalshi.lock().as_ref().map(FeedHandle::connected).into();
        live["poll_errors"] = self.poll.errors().into();
        live["poll"] = self.poll.value();
        self.summary.lock().snapshot(live, t)
    }

    async fn discover(&self, assets: &[String]) {
        let now = now_ns() / NS;
        let open = now - now.rem_euclid(WINDOW_S);
        for asset in assets {
            for o in [open, open + WINDOW_S] {
                let known = {
                    let e = self.engine.lock();
                    e.asset_index(asset).is_some_and(|i| e.window(i, o).is_some())
                };
                if known {
                    continue;
                }
                let Some(slug) = pm_slug(asset, o) else { continue };
                let m = match self.get(&format!("/v1/market/slug/{slug}")).await {
                    Ok(v) => v,
                    Err(e) => {
                        tracing::debug!(slug, error = %e, "pu_discovery");
                        continue;
                    }
                };
                let m = m.get("market").unwrap_or(&m);
                let terms = m.get("assetPriceTerms").cloned().unwrap_or(Value::Null);
                let ws = terms
                    .get("windowStart")
                    .and_then(Value::as_str)
                    .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok().map(|d| d.timestamp()));
                if ws.is_some_and(|w| w != o) {
                    tracing::warn!(slug, window_start = ?ws, "pu_window_mismatch");
                    continue;
                }
                let kalshi = match self.discovery.kalshi_market(asset, o).await {
                    Ok(Some(k)) => k.get("ticker").and_then(Value::as_str).map(str::to_owned),
                    Ok(None) => None,
                    Err(e) => {
                        tracing::warn!(asset, open = o, error = %e, "pu_kalshi_discovery");
                        None
                    }
                };
                let Some(kalshi) = kalshi else { continue };
                let w = WindowInfo {
                    asset: asset.clone(),
                    open: o,
                    slug,
                    kalshi_ticker: Some(kalshi),
                    tick: m.get("orderPriceMinTickSize").and_then(f).unwrap_or(0.01),
                    min_qty: m.get("minimumTradeQty").and_then(f).unwrap_or(1.0),
                    index: terms.get("indexSymbol").and_then(Value::as_str).unwrap_or_default().to_owned(),
                    fee_coef: m.get("feeCoefficient").and_then(f),
                };
                self.engine.lock().set_window(w, now_ns());
            }
        }
    }

    /// The book, cache-busted: the gateway's CDN serves `/book` with
    /// `max-age=30` (404s included) unless the URL is new. Not `/bbo`: it was
    /// seen frozen for 10 s while the book moved.
    async fn book(&self, slug: &str) -> Result<Book, BookErr> {
        let url = format!("{}/v1/markets/{slug}/book?_={}", self.gateway, now_ns());
        let r = self
            .http
            .get(url)
            .header("Cache-Control", "no-cache")
            .timeout(Duration::from_secs(1))
            .send()
            .await
            .map_err(|e| if e.is_timeout() { BookErr::Timeout } else { BookErr::Other(format!("{e}")) })?;
        let status = r.status();
        if !status.is_success() {
            return Err(BookErr::Status(status.as_u16()));
        }
        let hit = r
            .headers()
            .get("cf-cache-status")
            .and_then(|v| v.to_str().ok())
            .is_some_and(|v| v.eq_ignore_ascii_case("HIT"));
        let age = r.headers().get("age").and_then(|v| v.to_str().ok()?.trim().parse::<i64>().ok());
        let v: Value = r
            .json()
            .await
            .map_err(|e| if e.is_timeout() { BookErr::Timeout } else { BookErr::Other(format!("{e}")) })?;
        if hit {
            self.poll.cache_hits.fetch_add(1, Relaxed);
        }
        Ok(parse_book(&v, if hit { Some(age.unwrap_or(30)) } else { None }))
    }

    fn count_err(&self, e: &BookErr) {
        let c = match e {
            BookErr::Status(404) => &self.poll.http_404,
            BookErr::Status(429) => &self.poll.http_429,
            BookErr::Status(c) if *c >= 500 => &self.poll.http_5xx,
            BookErr::Status(_) => &self.poll.http_4xx,
            BookErr::Timeout => &self.poll.timeout,
            BookErr::Other(_) => &self.poll.other,
        };
        c.fetch_add(1, Relaxed);
    }

    /// Starts a polling task for each live window that has none.
    fn spawn_pollers(self: &Arc<Self>, now_s: i64) {
        let due = self.engine.lock().to_poll(now_s);
        let mut polling = self.polling.lock();
        polling.retain(|k| k.1 + WINDOW_S > now_s - 60);
        for (i, open, slug) in due {
            if polling.insert((i, open)) {
                tokio::spawn(self.clone().poll_window(i, open, slug));
            }
        }
    }

    /// One window's quote loop, independent of the others: its own interval,
    /// a 1 s request deadline, and back-off while the market is not open yet
    /// (its book is 404 or not `MARKET_STATE_OPEN` until it trades).
    async fn poll_window(self: Arc<Self>, i: usize, open: i64, slug: String) {
        let close = open + WINDOW_S;
        let base = Duration::from_millis(self.poll_ms);
        let mut iv = tokio::time::interval(base);
        iv.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let mut backoff: Option<Duration> = None;
        let mut noted = false;
        let mut errs = 0u64;
        loop {
            match backoff {
                Some(d) => tokio::time::sleep(d).await,
                None => {
                    iv.tick().await;
                }
            }
            let now_s = now_ns() / NS;
            if now_s >= close {
                break;
            }
            let slow = |b: Option<Duration>| Some(b.map_or(base, |d| (d * 2).min(Duration::from_secs(1))));
            match self.book(&slug).await {
                Ok(b) if !b.open() => {
                    self.poll.not_open.fetch_add(1, Relaxed);
                    backoff = slow(backoff);
                }
                Ok(b) => {
                    self.poll.ok.fetch_add(1, Relaxed);
                    backoff = None;
                    let t = now_ns();
                    // A cached copy is as old as the CDN says: the age gate then drops it.
                    let recv = t - b.cache_age_s.unwrap_or(0) * NS;
                    let (bid, ask) = b.top();
                    let fetch = {
                        let mut e = self.engine.lock();
                        e.on_pm_quote(i, open, bid, ask, b.venue_ns, recv);
                        e.evaluate_now(i, open, t, "pm_quote")
                    };
                    if let Some(f) = fetch {
                        self.paper_fill(f);
                    }
                }
                Err(BookErr::Status(404)) if now_s < open + 30 => {
                    self.poll.not_open.fetch_add(1, Relaxed);
                    backoff = slow(backoff);
                }
                Err(e) => {
                    self.count_err(&e);
                    errs += 1;
                    if matches!(e, BookErr::Status(404 | 429)) {
                        backoff = slow(backoff);
                    }
                    if !noted {
                        noted = true;
                        let mut eng = self.engine.lock();
                        let asset = eng.cfg.assets[i].clone();
                        eng.journal(
                            "pu_book_err",
                            serde_json::json!({"asset": asset, "open": open, "slug": slug,
                                               "error": e.to_string(), "since_open_s": now_s - open}),
                            now_ns(),
                        );
                    }
                    if errs.is_power_of_two() {
                        tracing::warn!(slug, error = %e, errors = errs, "pu_book_poll");
                    }
                }
            }
        }
    }

    /// The paper fill at `paper_latency_ms` and the delay-study probes, each
    /// against a book fetched then.
    fn paper_fill(self: &Arc<Self>, a: FetchBook) {
        let mut plan: Vec<(i64, Option<i64>, bool)> = self
            .paper_delays_ms
            .iter()
            .map(|&d| (a.decision_ns + d * 1_000_000, Some(d), d == self.paper_latency_ms))
            .collect();
        if !self.paper_delays_ms.contains(&self.paper_latency_ms) {
            plan.push((a.at_ns, None, true));
        }
        for (at, probe, primary) in plan {
            let app = self.clone();
            let slug = a.slug.clone();
            let id = a.id;
            tokio::spawn(async move {
                let wait = (at - now_ns()).max(0);
                tokio::time::sleep(Duration::from_nanos(wait as u64)).await;
                let r = app.book(&slug).await;
                {
                    let mut e = app.engine.lock();
                    let t = now_ns();
                    match &r {
                        Ok(b) => {
                            if primary {
                                e.on_book(id, &b.bids, &b.offers, t);
                            }
                            if let Some(d) = probe {
                                e.on_probe(id, d, &b.bids, &b.offers, b.venue_ns, b.cache_age_s, t);
                            }
                        }
                        Err(err) => {
                            if primary {
                                e.on_book_error(id, &err.to_string(), t);
                            }
                            if let Some(d) = probe {
                                e.journal(
                                    "pu_fill_probe",
                                    serde_json::json!({"id": id, "delay_ms": d, "error": err.to_string()}),
                                    t,
                                );
                            }
                        }
                    }
                }
                if let Err(err) = &r {
                    app.count_err(err);
                }
                app.flush();
            });
        }
    }

    async fn resolve(&self) {
        let due = self.engine.lock().to_resolve(now_ns() / NS);
        for w in due.into_iter().take(20) {
            let v = match self.get(&format!("/v1/markets/{}/settlement", w.slug)).await {
                Ok(v) => v,
                Err(e) => {
                    tracing::debug!(slug = w.slug, error = %e, "pu_settlement");
                    continue;
                }
            };
            let outcome = match v.get("settlement").and_then(f) {
                Some(x) if x >= 0.5 => "yes",
                Some(_) => "no",
                None => continue,
            };
            let kalshi = match &w.kalshi_ticker {
                Some(t) => {
                    let win = Window {
                        asset: w.asset.clone(),
                        open_unix: w.open,
                        kalshi_ticker: t.clone(),
                        yes_token: String::new(),
                        no_token: String::new(),
                        condition_id: String::new(),
                        fee_rate: 0.0,
                        fee_exponent: 0.0,
                    };
                    self.discovery.kalshi_outcome(&win).await.ok().flatten()
                }
                None => None,
            };
            // Wait a little for Kalshi's result so the comparison is recorded.
            if kalshi.is_none() && now_ns() / NS < w.close() + 300 {
                continue;
            }
            self.engine.lock().on_resolved(&w.asset, w.open, outcome, kalshi, now_ns());
        }
        self.flush();
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .init();
    let _ = rustls::crypto::ring::default_provider().install_default();
    let args = Args::parse();
    let cfg = Config::load(args.env.as_deref())?;
    std::fs::create_dir_all(&cfg.state_dir)?;
    run(cfg).await
}

async fn run(cfg: Config) -> anyhow::Result<()> {
    let state_path = cfg.state_dir.join("state.json");
    let persisted = load_state(&state_path)?;
    tracing::info!("pk-pmus paper: Polymarket US decisions and fills are simulated, nothing is sent");
    let http = reqwest::Client::builder()
        .user_agent(pk_feeds::discovery::USER_AGENT)
        .timeout(Duration::from_secs(5))
        .pool_max_idle_per_host(8)
        .tcp_nodelay(true)
        .build()?;
    let summary = Summary::load(&cfg.state_dir.join("journal"), &cfg.epoch, &cfg.cal_epochs);
    let mut engine = Engine::new(cfg.clone(), persisted);
    for (x, r) in summary.cal_seed() {
        engine.cal.add(x, r);
    }
    tracing::info!(
        n = engine.cal.n,
        beta = engine.beta(),
        epochs = ?cfg.cal_epochs,
        "calibrated Kelly seeded from settled shadows"
    );
    let app = Arc::new(App {
        engine: Mutex::new(engine),
        journal: Mutex::new(JsonlDaily::new(cfg.state_dir.join("journal"))),
        http,
        gateway: cfg.gateway_url.trim_end_matches('/').to_owned(),
        discovery: Discovery::new("https://gamma-api.polymarket.com", &cfg.kalshi_rest_url),
        summary: Mutex::new(summary),
        started_ns: now_ns(),
        state_path,
        kalshi: Mutex::new(None),
        poll: PollStats::default(),
        polling: Mutex::new(HashSet::new()),
        poll_ms: cfg.poll_ms,
        paper_latency_ms: cfg.paper_latency_ms,
        paper_delays_ms: cfg.paper_delays_ms.clone(),
    });
    if let Some(addr) = cfg.ui_addr {
        let a = app.clone();
        let token = cfg.ui_token.clone();
        tokio::spawn(async move {
            if let Err(e) = web::serve(addr, token, Arc::new(move || a.snapshot())).await {
                tracing::error!(error = %e, "pu_ui_failed");
            }
        });
    }

    // Kalshi order books (read-only).
    let (id, path) = (
        cfg.kalshi_key_id.clone().ok_or_else(|| anyhow::anyhow!("KALSHI_API_KEY_ID is required"))?,
        cfg.kalshi_key_path.clone().ok_or_else(|| anyhow::anyhow!("KALSHI_PRIVATE_KEY_PATH is required"))?,
    );
    let pem = std::fs::read_to_string(&path)
        .map_err(|e| anyhow::anyhow!("KALSHI_PRIVATE_KEY_PATH {}: {e}", path.display()))?;
    let signer = Arc::new(KalshiSigner::from_pem(&id, &pem)?);
    let a = app.clone();
    let feed = spawn_feed(
        cfg.kalshi_ws_url.clone(),
        FeedKind::Kalshi { signer },
        Arc::new(move |venue, text| {
            let t = now_ns();
            let fetch = {
                let mut e = a.engine.lock();
                match venue {
                    'k' => e.on_kalshi(&text, t).and_then(|(i, open)| e.evaluate_now(i, open, t, "kalshi")),
                    'c' if text.contains("\"venue\":\"k\"") => {
                        e.invalidate_kalshi();
                        None
                    }
                    _ => None,
                }
            };
            if let Some(f) = fetch {
                a.paper_fill(f);
            }
        }),
    );
    *app.kalshi.lock() = Some(feed);

    // Discovery, every 15 s.
    let a = app.clone();
    let assets = cfg.assets.clone();
    tokio::spawn(async move {
        loop {
            a.discover(&assets).await;
            let wanted = a.engine.lock().wanted_kalshi(now_ns() / NS);
            if let Some(k) = a.kalshi.lock().as_ref() {
                k.set_wanted(wanted);
            }
            a.flush();
            tokio::time::sleep(Duration::from_secs(15)).await;
        }
    });
    // Polymarket US quotes: one task per live window, started by the clock below.
    // Settlements, every 15 s.
    let a = app.clone();
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_secs(15)).await;
            a.resolve().await;
        }
    });

    // The clock: entries, kill and ack files, status.
    let kill = cfg.kill_file.clone();
    let ack = cfg.state_dir.join("ACK");
    let mut step = tokio::time::interval(Duration::from_millis(100));
    step.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut last_status = 0i64;
    let shutdown = tokio::signal::ctrl_c();
    tokio::pin!(shutdown);
    let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    loop {
        tokio::select! {
            _ = &mut shutdown => break,
            _ = term.recv() => break,
            _ = step.tick() => {}
        }
        let t = now_ns();
        app.spawn_pollers(t / NS);
        let fetches = {
            let mut e = app.engine.lock();
            e.set_kill(kill.exists(), t);
            if ack.exists() {
                e.ack(t);
                let _ = std::fs::remove_file(&ack);
            }
            e.tick(t)
        };
        for f in fetches {
            app.paper_fill(f);
        }
        if t - last_status >= 60 * NS {
            last_status = t;
            let mut e = app.engine.lock();
            e.prune(t / NS);
            e.status(t);
            let s = e.status_value(t);
            drop(e);
            tracing::info!(status = %s, "pu_alive");
        }
        app.flush();
    }
    tracing::info!("stopping");
    let p = serde_json::to_value(&app.engine.lock().p)?;
    save_state(&app.state_path, &serde_json::from_value(p)?);
    app.flush();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slugs_and_levels() {
        // 2026-11-25 14:15 UTC
        let open = 1_764_080_100 + 365 * 86_400;
        let s = pm_slug("BTC", open).unwrap();
        assert!(s.starts_with("cpc-btc-updown-15m-") && s.ends_with('z'), "{s}");
        assert_eq!(pm_slug("ETH", 0).unwrap(), "cpceth-eth-updown-15m-1970-01-01-0000z");
        let v: Value =
            serde_json::from_str(r#"[{"px":{"value":"0.36"},"qty":"72"},{"px":{"value":"0.35"},"qty":"5"}]"#)
                .unwrap();
        assert_eq!(levels(&v), vec![(0.36, 72.0), (0.35, 5.0)]);
    }
}
