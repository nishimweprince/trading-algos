//! `pk-okx`: Kalshi's 15-minute book as the signal, OKX's event contract on
//! the same window as the leg. Paper only. A process of its own, beside the
//! other services: its own Kalshi socket, its own state, tape and port.
//!
//! | Command | |
//! |---|---|
//! | `pk-okx --profile dev` | run (paper) |
//! | `pk-okx --profile dev check` | the account's fees and permissions, and one post-only order that can't fill, cancelled at once |
//!
//! | Route (127.0.0.1:`PK_OKX_PORT`, `X-API-Key`) | |
//! |---|---|
//! | `GET /v1/okx/status` | engine, feeds, probes, recorder |
//! | `GET /v1/okx/series?since=` | per-second Kalshi and OKX points |
//! | `GET /health/live` | process up (no key) |

mod config;
mod discovery;
mod engine;
mod probes;

use std::collections::{BTreeSet, HashMap};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use axum::Router;
use axum::extract::{Query, State};
use axum::http::header;
use axum::response::IntoResponse;
use axum::routing::get;
use clap::{Parser, Subcommand};
use parking_lot::Mutex;
use pk_core::journal::{JsonlDaily, Sink};
use pk_core::ledger::Ledger;
use pk_core::{NS, window_open};
use pk_exec::okx::{OkxClient, OkxKey, Reply};
use pk_exec::{WarmConfig, WarmHttp};
use pk_feeds::{FeedHandle, FeedKind, KalshiSigner, OkxFeed, spawn_feed, spawn_okx};
use pk_svc::Settings;
use pk_svc::api::{ApiKey, require_key};
use pk_tape::Recorder;
use serde::Deserialize;
use serde_json::{Map, Value, json};

use crate::config::OkxSettings;
use crate::discovery::{OkxDiscovery, OkxWindow};
use crate::engine::{Engine, EngineCfg};
use crate::probes::NetProbe;

/// How long a closed window waits for Kalshi's result before its outcome is recorded without it.
const KALSHI_RESULT_WAIT_S: i64 = 600;

#[derive(Parser)]
#[command(name = "pk-okx", about = "Kalshi-signalled OKX event-contract 15-minute trader (paper)")]
struct Args {
    #[arg(long)]
    profile: Option<String>,
    #[command(subcommand)]
    cmd: Option<Cmd>,
}

#[derive(Subcommand)]
enum Cmd {
    /// Read the account's config, event-contract fees and USDT balance, then post one
    /// post-only buy at the lowest tick on this BTC window and cancel it at once.
    Check {
        /// Contracts in the probe order.
        #[arg(long, default_value_t = 1.0)]
        shares: f64,
        /// The order's `tdMode`.
        #[arg(long, default_value = "isolated")]
        td_mode: String,
        /// Read only: no order.
        #[arg(long)]
        no_order: bool,
    },
}

pub fn now_ns() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos() as i64).unwrap_or(0)
}

struct App {
    okx: OkxSettings,
    settings: Settings,
    engine: Arc<Mutex<Engine>>,
    kalshi: Mutex<Option<FeedHandle>>,
    feed: Mutex<Option<OkxFeed>>,
    recorder: Option<Mutex<Recorder>>,
    probe: Mutex<Option<Arc<NetProbe>>>,
    discovery_errors: AtomicU64,
    last_discovery_error: Mutex<Option<String>>,
    /// Identified windows awaiting their outcome.
    windows: Mutex<HashMap<(String, i64), OkxWindow>>,
    started_ns: i64,
}

impl App {
    fn tape(&self, venue: char, t: i64, text: &str) {
        if let Some(r) = &self.recorder {
            r.lock().write(venue, t, text);
        }
    }

    fn indexes(&self) -> BTreeSet<String> {
        self.okx.assets.iter().map(|a| format!("{a}-USDT")).collect()
    }

    fn resubscribe(&self) {
        let insts = self.engine.lock().instruments();
        if let Some(f) = self.feed.lock().as_ref() {
            f.set_wanted(insts, self.indexes());
        }
    }

    fn status(&self) -> Value {
        let t = now_ns();
        let mut st = self.engine.lock().status(t);
        st["feeds"] = json!({
            "kalshi": self.kalshi.lock().as_ref().map(FeedHandle::stats),
            "okx": self.feed.lock().as_ref().map(OkxFeed::stats),
        });
        st["netprobe"] = self.probe.lock().as_ref().map(|p| p.stats()).unwrap_or(Value::Null);
        st["recorder"] = match &self.recorder {
            Some(r) => serde_json::to_value(r.lock().stats()).unwrap_or(Value::Null),
            None => json!({"enabled": false}),
        };
        st["discovery"] = json!({
            "errors": self.discovery_errors.load(Ordering::Relaxed),
            "last_error": self.last_discovery_error.lock().clone(),
            "awaiting_outcome": self.windows.lock().len(),
        });
        st["host"] = self.settings.host_tag.clone().into();
        st["uptime_s"] = ((t - self.started_ns) / NS).into();
        st
    }
}

fn stamp(okx: &OkxSettings) -> Map<String, Value> {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    okx.config_json().to_string().hash(&mut h);
    let mut m = Map::new();
    m.insert("epoch".into(), okx.epoch.clone().into());
    m.insert("build".into(), pk_svc::build::build_id().into());
    m.insert("config".into(), format!("{:010x}", h.finish() & 0xff_ffff_ffff).into());
    m.insert("mode".into(), "paper".into());
    m
}

fn main() -> anyhow::Result<()> {
    pk_svc::tls::install_default_provider();
    let args = Args::parse();
    let settings = Settings::load(args.profile.as_deref())?;
    let okx = OkxSettings::load(&settings)?;
    let events = settings.events_log_path.with_extension("okx.jsonl");
    let _guard = pk_svc::logging::init(&settings.log_level, Some(&events));
    let rt = tokio::runtime::Builder::new_multi_thread().worker_threads(2).enable_all().build()?;
    match args.cmd {
        Some(Cmd::Check { shares, td_mode, no_order }) => rt.block_on(check(&okx, shares, &td_mode, no_order)),
        None => rt.block_on(run(settings, okx)),
    }
}

async fn warm(okx: &OkxSettings) -> anyhow::Result<Arc<WarmHttp>> {
    let mut cfg = WarmConfig::new(url::Url::parse(&okx.api_url)?);
    cfg.warm_path = "/api/v5/public/time".into();
    WarmHttp::connect(cfg).await
}

fn show(step: &str, r: &Reply) {
    println!(
        "{step}: http {} code {} ({:.1} ms) {}",
        r.status,
        r.code(),
        r.body_us as f64 / 1e3,
        r.body.get("msg").and_then(Value::as_str).filter(|m| !m.is_empty()).unwrap_or("")
    );
}

/// The go/no-go for this account on this host: can it trade event contracts at all?
async fn check(okx: &OkxSettings, shares: f64, td_mode: &str, no_order: bool) -> anyhow::Result<()> {
    let (k, s, p) = okx.creds().ok_or_else(|| anyhow::anyhow!("check needs OKX_API_KEY, OKX_API_SECRET and OKX_API_PASSPHRASE"))?;
    let client = OkxClient::new(warm(okx).await?, Some(OkxKey::new(&k, &s, &p)));
    let mut journal = JsonlDaily::new(okx.state_dir.join("probe"));
    let mut rec = json!({"type": "okx_check", "t_ns": now_ns(), "host_ip_bound": null});

    let r = client.get("/api/v5/account/config").await?;
    show("account config", &r);
    if let Some(c) = r.data().first() {
        let pick = |k: &str| c.get(k).cloned().unwrap_or(Value::Null);
        let cfg = json!({"uid": pick("uid"), "acctLv": pick("acctLv"), "perm": pick("perm"), "ip": pick("ip"),
                         "label": pick("label"), "level": pick("level"), "kycLv": pick("kycLv"),
                         "posMode": pick("posMode"), "type": pick("type")});
        println!("  {cfg}");
        rec["account"] = cfg;
    }
    anyhow::ensure!(r.ok(), "the key is refused: {}", r.body);

    let r = client.get("/api/v5/account/trade-fee?instType=EVENTS").await?;
    show("trade fee (EVENTS)", &r);
    println!("  {}", Value::Array(r.data().to_vec()));
    rec["trade_fee"] = r.body.clone();

    let r = client.get("/api/v5/account/balance?ccy=USDT").await?;
    show("balance (USDT)", &r);
    let avail = r.data().first().and_then(|b| b.pointer("/details/0/availBal")).and_then(Value::as_str).unwrap_or("0").to_owned();
    println!("  available USDT {avail}");
    rec["usdt_available"] = avail.clone().into();

    // This BTC window: its instrument and book.
    let d = OkxDiscovery::new(&okx.api_url, &okx.series, "https://gamma-api.polymarket.com", "https://api.elections.kalshi.com/trade-api/v2");
    let open = window_open(now_ns() / NS);
    let list = d.instruments("BTC").await?;
    let w = discovery::pick_instrument("BTC", open, "", &list)
        .map_err(|e| anyhow::anyhow!(e))?
        .ok_or_else(|| anyhow::anyhow!("the BTC window opening at {open} is not live on OKX yet; try again in a minute"))?;
    println!("window {} strike {} tick {} min size {}", w.inst_id, w.strike, w.tick, w.min_size);
    let book = client.get(&format!("/api/v5/market/books?instId={}&sz=5", w.inst_id)).await?;
    let best_bid = book.data().first().and_then(|b| b.pointer("/bids/0/0")).and_then(Value::as_str).and_then(|x| x.parse::<f64>().ok());
    let best_ask = book.data().first().and_then(|b| b.pointer("/asks/0/0")).and_then(Value::as_str).and_then(|x| x.parse::<f64>().ok());
    println!("  book bid {best_bid:?} ask {best_ask:?}");
    rec["window"] = serde_json::to_value(&w)?;

    if no_order {
        journal.write(now_ns(), &rec);
        println!("read-only check done (--no-order)");
        return Ok(());
    }
    // One post-only buy at the lowest tick: below any bid, never crossing an ask.
    let px = w.tick;
    anyhow::ensure!(best_bid.is_none_or(|b| px < b - 1e-9), "the bid is at the lowest tick; no price below it");
    anyhow::ensure!(best_ask.is_none_or(|a| px < a - 1e-9), "an ask at the lowest tick would cross");
    anyhow::ensure!(px * shares <= 1.0 + 1e-9, "probe notional above $1");
    let cl = format!("pkokx{}", now_ns() / 1_000_000);
    // Event contracts: `outcome` yes (Up) / no (Down), tdMode isolated; post-only needs no speedBump.
    let order = json!({"instId": w.inst_id, "tdMode": td_mode, "side": "buy", "outcome": "yes", "ordType": "post_only",
                       "px": format!("{px}"), "sz": format!("{shares}"), "clOrdId": cl});
    let t0 = Instant::now();
    let r = client.post("/api/v5/trade/order", &order).await?;
    let ack_ms = t0.elapsed().as_secs_f64() * 1e3;
    show("post-only order", &r);
    let d0 = r.data().first().cloned().unwrap_or(Value::Null);
    let s_code = d0.get("sCode").and_then(Value::as_str).unwrap_or("?");
    println!("  sCode {s_code} {} ack {ack_ms:.1} ms", d0.get("sMsg").and_then(Value::as_str).unwrap_or(""));
    rec["order"] = json!({"request": order, "reply": r.body, "ack_ms": ack_ms});
    let ord_id = d0.get("ordId").and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_owned);
    if let (true, Some(id)) = (r.ok() && s_code == "0", ord_id) {
        let t0 = Instant::now();
        let c = client.post("/api/v5/trade/cancel-order", &json!({"instId": w.inst_id, "ordId": id})).await?;
        let cancel_ms = t0.elapsed().as_secs_f64() * 1e3;
        show("cancel", &c);
        let st = client.get(&format!("/api/v5/trade/order?instId={}&ordId={id}", w.inst_id)).await?;
        let o = st.data().first().cloned().unwrap_or(Value::Null);
        let filled = o.get("accFillSz").and_then(Value::as_str).and_then(|x| x.parse::<f64>().ok()).unwrap_or(0.0);
        println!("  state {} filled {filled} cancel {cancel_ms:.1} ms", o.get("state").and_then(Value::as_str).unwrap_or("?"));
        rec["cancel"] = json!({"reply": c.body, "cancel_ms": cancel_ms, "order": o});
        journal.write(now_ns(), &rec);
        anyhow::ensure!(filled == 0.0, "the probe filled {filled}: check the account");
        println!("GO: this account can post and cancel event-contract orders from this host.");
        println!("Put the fee rates above into PK_OKX_FEE_RATE / PK_OKX_FEE_EXPONENT / PK_OKX_SETTLE_FEE.");
    } else {
        journal.write(now_ns(), &rec);
        anyhow::bail!("NO-GO: OKX refused the order (code {}, sCode {s_code}); see the reply above", r.code());
    }
    Ok(())
}

async fn kalshi_result(http: &reqwest::Client, base: &str, ticker: &str) -> Option<&'static str> {
    let v: Value = http.get(format!("{}/markets/{ticker}", base.trim_end_matches('/'))).send().await.ok()?.json().await.ok()?;
    match v.pointer("/market/result").and_then(Value::as_str) {
        Some("yes") => Some("yes"),
        Some("no") => Some("no"),
        _ => None,
    }
}

async fn run(settings: Settings, okx: OkxSettings) -> anyhow::Result<()> {
    let ledger = Ledger::open_mode(Some(&okx.state_dir.join("ledger.json")), okx.paper_cash, Some(&okx.epoch), "paper")?;
    let journal = JsonlDaily::new(okx.state_dir.join("journal"));
    let cfg = EngineCfg {
        params: okx.params.clone(),
        shares: okx.shares,
        limit_offset: okx.limit_offset,
        exit_slip: okx.exit_slip,
        active_exit: okx.active_exit,
        fee_rate: okx.fee_rate,
        fee_exponent: okx.fee_exponent,
        fee_notional: okx.fee_notional,
        settle_fee: okx.settle_fee,
        paper_latency_ms: okx.paper_latency_ms,
        paper_extra_ms: okx.paper_extra_ms,
        max_open_usd: okx.max_open_usd,
        daily_loss_usd: okx.daily_loss_usd,
    };
    let engine = Arc::new(Mutex::new(Engine::new(cfg, &okx.assets, ledger, journal, stamp(&okx))));
    let recorder = if okx.record {
        Some(Mutex::new(Recorder::start(&okx.record_dir, &settings.host_tag, (settings.min_free_disk_gb * 1e9) as u64, 200_000)?))
    } else {
        None
    };
    let app = Arc::new(App {
        okx: okx.clone(),
        settings: settings.clone(),
        engine: engine.clone(),
        kalshi: Mutex::new(None),
        feed: Mutex::new(None),
        recorder,
        probe: Mutex::new(None),
        discovery_errors: AtomicU64::new(0),
        last_discovery_error: Mutex::new(None),
        windows: Mutex::new(HashMap::new()),
        started_ns: now_ns(),
    });

    // Feeds.
    let pem = std::fs::read_to_string(&settings.kalshi_private_key_path)?;
    let signer = Arc::new(KalshiSigner::from_pem(&settings.kalshi_api_key_id, &pem)?);
    {
        let a = app.clone();
        let emit: pk_feeds::Emit = Arc::new(move |venue, text: String| {
            let t = now_ns();
            a.tape(venue, t, &text);
            let mut e = a.engine.lock();
            match venue {
                'k' => e.on_kalshi(&text, t),
                'c' if text.contains(r#""venue":"k""#) => e.invalidate_kalshi(),
                _ => {}
            }
        });
        *app.kalshi.lock() = Some(spawn_feed(settings.kalshi_ws_url.clone(), FeedKind::Kalshi { signer }, emit));
    }
    {
        let a = app.clone();
        let emit: pk_feeds::Emit = Arc::new(move |_, text: String| {
            let t = now_ns();
            a.tape('o', t, &text);
            a.engine.lock().on_okx(&text, t);
        });
        *app.feed.lock() = Some(spawn_okx(okx.ws_url.clone(), emit));
        app.resubscribe();
    }

    // Discovery: this window, on both venues, every 2 s until found.
    {
        let a = app.clone();
        tokio::spawn(async move {
            let d = OkxDiscovery::new(&a.okx.api_url, &a.okx.series, &a.settings.poly_gamma_url, &a.settings.kalshi_rest_url);
            let mut tick = tokio::time::interval(Duration::from_secs(2));
            loop {
                tick.tick().await;
                let open = window_open(now_ns() / NS);
                let mut changed = false;
                for asset in &a.okx.assets {
                    if a.engine.lock().window(asset).is_some_and(|w| w.open_unix == open) {
                        continue;
                    }
                    match d.discover(asset, open).await {
                        Ok(Some(w)) => {
                            let t = now_ns();
                            let mut ctl = serde_json::to_value(&w).unwrap_or(Value::Null);
                            ctl["type"] = "okx_window".into();
                            a.tape('c', t, &ctl.to_string());
                            a.windows.lock().insert((w.asset.clone(), w.open_unix), w.clone());
                            a.engine.lock().set_window(w, t);
                            changed = true;
                        }
                        Ok(None) => {}
                        Err(e) => {
                            a.discovery_errors.fetch_add(1, Ordering::Relaxed);
                            *a.last_discovery_error.lock() = Some(e.to_string());
                        }
                    }
                }
                if changed {
                    let tickers = a.engine.lock().tickers();
                    if let Some(k) = a.kalshi.lock().as_ref() {
                        k.set_wanted(tickers.into_iter().collect::<BTreeSet<_>>());
                    }
                    a.resubscribe();
                }
            }
        });
    }

    // Timers: paper orders in flight, probes, markouts, series (every 50 ms).
    {
        let a = app.clone();
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(Duration::from_millis(50));
            tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            loop {
                tick.tick().await;
                a.engine.lock().tick(now_ns());
            }
        });
    }

    // Outcomes every 15 s: OKX's settlement, Kalshi's result, OKX's index; then the paper position.
    {
        let a = app.clone();
        tokio::spawn(async move {
            let d = OkxDiscovery::new(&a.okx.api_url, &a.okx.series, &a.settings.poly_gamma_url, &a.settings.kalshi_rest_url);
            let http = reqwest::Client::builder().timeout(Duration::from_secs(10)).build().expect("http client");
            let mut tick = tokio::time::interval(Duration::from_secs(15));
            loop {
                tick.tick().await;
                let now_s = now_ns() / NS;
                let due: Vec<OkxWindow> =
                    a.windows.lock().values().filter(|w| now_s > w.exp_ms / 1000 + 20).cloned().collect();
                for w in due {
                    let okx = match d.outcome(&w).await {
                        Ok(Some(o)) => o,
                        Ok(None) => continue,
                        Err(e) => {
                            a.discovery_errors.fetch_add(1, Ordering::Relaxed);
                            *a.last_discovery_error.lock() = Some(format!("outcome {}: {e}", w.inst_id));
                            continue;
                        }
                    };
                    let kalshi = if w.kalshi_ticker.is_empty() {
                        None
                    } else {
                        kalshi_result(&http, &a.settings.kalshi_rest_url, &w.kalshi_ticker).await
                    };
                    if kalshi.is_none() && !w.kalshi_ticker.is_empty() && now_s < w.exp_ms / 1000 + KALSHI_RESULT_WAIT_S {
                        continue;
                    }
                    let mut e = a.engine.lock();
                    let index = if w.strike > 0.0 { e.index_close(&w) } else { None };
                    e.outcome(&w, okx, kalshi, index, now_ns());
                    drop(e);
                    a.windows.lock().remove(&(w.asset.clone(), w.open_unix));
                }
                // A position whose window this process never identified (a restart) still
                // settles: its instrument id follows from the open, its outcome from the candles.
                let orphans = a.engine.lock().unsettled(now_ns());
                for (asset, open) in orphans {
                    if !a.windows.lock().contains_key(&(asset.clone(), open)) {
                        let w = discovery::bare_window(&asset, &a.okx.series, open);
                        a.windows.lock().insert((asset, open), w);
                    }
                }
            }
        });
    }

    // Network probe on its own warm connection.
    {
        let a = app.clone();
        let every = Duration::from_secs(a.okx.netprobe_every_s.max(1));
        tokio::spawn(async move {
            let w = loop {
                match warm(&a.okx).await {
                    Ok(w) => break w,
                    Err(e) => {
                        tracing::warn!(error = %e, "okx_netprobe_connect");
                        tokio::time::sleep(Duration::from_secs(10)).await;
                    }
                }
            };
            w.spawn_maintenance();
            let probe = Arc::new(NetProbe::new(w, &a.okx.api_url));
            *a.probe.lock() = Some(probe.clone());
            let mut tick = tokio::time::interval(every);
            let mut n: u64 = 0;
            let mut last_summary = now_ns();
            loop {
                tick.tick().await;
                let inst = a.engine.lock().instruments().into_iter().next();
                let Some(inst) = inst else { continue };
                probe.round(&inst, n % 12 == 0).await;
                n += 1;
                if let Some(p50) = probe.warm_p50() {
                    a.engine.lock().set_rtt(p50);
                }
                if now_ns() - last_summary >= 60 * NS {
                    last_summary = now_ns();
                    let mut s = probe.summary();
                    if let Some(f) = a.feed.lock().as_ref() {
                        s["ws"] = f.stats();
                    }
                    a.engine.lock().note(last_summary, "okx_probe", s);
                }
            }
        });
    }

    // API.
    let key = ApiKey(Arc::new(settings.api_key.clone()));
    #[derive(Deserialize)]
    struct Since {
        since: Option<i64>,
    }
    let api = Router::new()
        .route(
            "/v1/okx/status",
            get(|State(a): State<Arc<App>>| async move {
                ([(header::CONTENT_TYPE, "application/json")], a.status().to_string()).into_response()
            }),
        )
        .route(
            "/v1/okx/series",
            get(|State(a): State<Arc<App>>, Query(q): Query<Since>| async move {
                let body = a.engine.lock().series_since(q.since.unwrap_or(0));
                ([(header::CONTENT_TYPE, "application/json")], body.to_string()).into_response()
            }),
        )
        .layer(axum::middleware::from_fn_with_state(key, require_key))
        .route("/health/live", get(|| async { axum::Json(json!({"status": "ok"})) }))
        .with_state(app.clone());
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", okx.port)).await?;
    tracing::info!(port = okx.port, assets = ?okx.assets, "pk_okx_running");
    axum::serve(listener, api).with_graceful_shutdown(shutdown()).await?;
    if let Some(r) = &app.recorder {
        r.lock().stop();
    }
    tracing::info!("pk_okx_stopped");
    Ok(())
}

async fn shutdown() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    #[cfg(unix)]
    let term = async {
        if let Ok(mut s) = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
            s.recv().await;
        }
    };
    #[cfg(not(unix))]
    let term = std::future::pending::<()>();
    tokio::select! {
        _ = ctrl_c => {}
        _ = term => {}
    }
}
