//! `pk-limitless`: Kalshi's 15-minute book as the signal, Limitless's mirror
//! market as the leg. A process of its own, beside the Polymarket trader:
//! its own Kalshi socket, its own state, tape and port, no link to the trader.
//!
//! | Command | |
//! |---|---|
//! | `pk-limitless --profile dev` | run (paper unless `PK_LIM_EXECUTION_MODE=live` and `PK_LIM_LIVE_CONFIRM=yes`) |
//! | `pk-limitless --profile dev approve` | one-time Base approvals for the current venue, and balances |
//! | `pk-limitless --profile dev probe-orders` | post-only orders that can't fill, cancelled at once, timed |
//!
//! | Route (127.0.0.1:`PK_LIM_PORT`, `X-API-Key`) | |
//! |---|---|
//! | `GET /v1/lim/status` | engine, feeds, probes, recorder |
//! | `GET /v1/lim/series?since=` | per-second Kalshi and Limitless points |
//! | `GET /health/live` | process up (no key) |

mod config;
mod discovery;
mod engine;
mod live;
mod probes;

use std::collections::{BTreeSet, HashMap};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use axum::Router;
use axum::extract::{Query, State};
use axum::http::header;
use axum::response::IntoResponse;
use axum::routing::get;
use clap::{Parser, Subcommand};
use parking_lot::Mutex;
use pk_core::journal::JsonlDaily;
use pk_core::ledger::Ledger;
use pk_core::{NS, window_open};
use pk_exec::limitless::{Hmac, LimChain, LimClient, LimSigner};
use pk_exec::{WarmConfig, WarmHttp};
use pk_feeds::{FeedHandle, FeedKind, KalshiSigner, LimitlessFeed, spawn_feed, spawn_limitless};
use pk_svc::Settings;
use pk_svc::api::{ApiKey, require_key};
use pk_tape::Recorder;
use serde::Deserialize;
use serde_json::{Map, Value, json};

use crate::config::{LimSettings, Mode};
use crate::discovery::{LimDiscovery, LimWindow};
use crate::engine::{Engine, EngineCfg};
use crate::live::now_ns;
use crate::probes::{NetProbe, OrderProbeStats, TakerStats};

#[derive(Parser)]
#[command(name = "pk-limitless", about = "Kalshi-signalled Limitless 15-minute trader (paper or live)")]
struct Args {
    #[arg(long)]
    profile: Option<String>,
    #[command(subcommand)]
    cmd: Option<Cmd>,
}

#[derive(Subcommand)]
enum Cmd {
    /// Approve USDC and Conditional Tokens for the current BTC venue (Base), then print balances.
    Approve,
    /// Time the order path with post-only buys that can't fill, each cancelled at once.
    ProbeOrders {
        #[arg(long, default_value_t = 10)]
        count: u32,
        #[arg(long, default_value_t = 5.0)]
        every_s: f64,
        /// How far below the best bid (dollars).
        #[arg(long, default_value_t = 0.20)]
        back: f64,
        #[arg(long, default_value_t = 5)]
        shares: u64,
        #[arg(long, default_value_t = 3.0)]
        max_usd: f64,
    },
}

struct App {
    lim: LimSettings,
    settings: Settings,
    engine: Arc<Mutex<Engine>>,
    kalshi: Mutex<Option<FeedHandle>>,
    feed: Mutex<Option<LimitlessFeed>>,
    recorder: Option<Mutex<Recorder>>,
    probe: Mutex<Option<Arc<NetProbe>>>,
    taker: Arc<Mutex<TakerStats>>,
    order_probe: Arc<Mutex<OrderProbeStats>>,
    heartbeat_failures: Arc<AtomicU64>,
    discovery_errors: AtomicU64,
    last_discovery_error: Mutex<Option<String>>,
    windows: Mutex<HashMap<(String, i64), LimWindow>>,
    live: Mutex<Option<Arc<LimClient>>>,
    chain: Mutex<Option<Arc<LimChain>>>,
    redeemed: Mutex<Vec<Value>>,
    started_ns: i64,
}

impl App {
    fn tape(&self, venue: char, t: i64, text: &str) {
        if let Some(r) = &self.recorder {
            r.lock().write(venue, t, text);
        }
    }

    fn status(&self) -> Value {
        let t = now_ns();
        let mut st = self.engine.lock().status(t);
        let feeds = json!({
            "kalshi": self.kalshi.lock().as_ref().map(FeedHandle::stats),
            "limitless": self.feed.lock().as_ref().map(LimitlessFeed::stats),
        });
        st["feeds"] = feeds;
        st["netprobe"] = self.probe.lock().as_ref().map(|p| p.stats()).unwrap_or(Value::Null);
        st["order_path"] = json!({
            "live": self.lim.mode == Mode::Live,
            "credentials": self.lim.creds().is_some(),
            "connected": self.live.lock().is_some(),
            "taker": self.taker.lock().json(),
            "probe": self.order_probe.lock().json(),
            "heartbeat_failures": self.heartbeat_failures.load(Ordering::Relaxed),
            "redeemed": self.redeemed.lock().len(),
        });
        st["recorder"] = match &self.recorder {
            Some(r) => serde_json::to_value(r.lock().stats()).unwrap_or(Value::Null),
            None => json!({"enabled": false}),
        };
        st["discovery"] = json!({
            "errors": self.discovery_errors.load(Ordering::Relaxed),
            "last_error": self.last_discovery_error.lock().clone(),
        });
        st["host"] = self.settings.host_tag.clone().into();
        st["uptime_s"] = ((t - self.started_ns) / NS).into();
        st
    }
}

fn stamp(lim: &LimSettings) -> Map<String, Value> {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    lim.config_json().to_string().hash(&mut h);
    let mut m = Map::new();
    m.insert("epoch".into(), lim.epoch.clone().into());
    m.insert("build".into(), pk_svc::build::build_id().into());
    m.insert("config".into(), format!("{:010x}", h.finish() & 0xff_ffff_ffff).into());
    m.insert("mode".into(), lim.mode.as_str().into());
    m
}

fn main() -> anyhow::Result<()> {
    pk_svc::tls::install_default_provider();
    let args = Args::parse();
    let settings = Settings::load(args.profile.as_deref())?;
    let lim = LimSettings::load(&settings)?;
    let events = settings.events_log_path.with_extension("limitless.jsonl");
    let _guard = pk_svc::logging::init(&settings.log_level, Some(&events));
    let rt = tokio::runtime::Builder::new_multi_thread().worker_threads(2).enable_all().build()?;
    match args.cmd {
        Some(Cmd::Approve) => rt.block_on(approve(&settings, &lim)),
        Some(Cmd::ProbeOrders { count, every_s, back, shares, max_usd }) => {
            rt.block_on(probe_orders(&settings, &lim, count, every_s, back, shares, max_usd))
        }
        None => rt.block_on(run(settings, lim)),
    }
}

async fn connect_client(lim: &LimSettings) -> anyhow::Result<Arc<LimClient>> {
    let (token, secret, key) = lim.creds().ok_or_else(|| {
        anyhow::anyhow!("LIMITLESS_API_KEY, LIMITLESS_API_SECRET and a private key are needed for the order path")
    })?;
    let mut cfg = WarmConfig::new(url::Url::parse(&lim.api_url)?);
    cfg.warm_path = "/markets/btc-15min-price".into();
    let http = WarmHttp::connect(cfg).await?;
    http.spawn_maintenance();
    Ok(Arc::new(LimClient::connect(http, Hmac::new(&token, &secret)?, LimSigner::new(&key)?).await?))
}

async fn current_window(settings: &Settings, lim: &LimSettings, asset: &str) -> anyhow::Result<LimWindow> {
    let d = LimDiscovery::new(&lim.api_url, &settings.poly_gamma_url, &settings.kalshi_rest_url);
    let open = window_open(now_ns() / NS);
    d.discover(asset, open).await?.ok_or_else(|| anyhow::anyhow!("{asset} {open}: not listed on both venues yet"))
}

async fn approve(settings: &Settings, lim: &LimSettings) -> anyhow::Result<()> {
    let rpc = lim.base_rpc_url.clone().ok_or_else(|| anyhow::anyhow!("BASE_RPC_URL is not set"))?;
    let key = lim.private_key.clone().ok_or_else(|| anyhow::anyhow!("no private key"))?;
    let chain = LimChain::connect(&rpc, &key).await?;
    let w = current_window(settings, lim, "BTC").await?;
    let exchange = w.exchange.parse()?;
    let fee_module = w.fee_module.as_deref().map(str::parse).transpose()?;
    println!("wallet {}", chain.address());
    let sent = chain.approve(exchange, fee_module).await?;
    if sent.is_empty() {
        println!("approvals already in place for exchange {exchange}");
    }
    for s in sent {
        println!("sent {s}");
    }
    let (usdc, eth) = chain.balances().await?;
    println!("balances: {usdc:.2} USDC, {eth:.6} ETH (gas)");
    Ok(())
}

async fn probe_orders(
    settings: &Settings,
    lim: &LimSettings,
    count: u32,
    every_s: f64,
    back: f64,
    shares: u64,
    max_usd: f64,
) -> anyhow::Result<()> {
    let client = connect_client(lim).await?;
    let stats = Mutex::new(OrderProbeStats::default());
    let mut journal = JsonlDaily::new(lim.state_dir.join("probe"));
    let http = reqwest::Client::new();
    for n in 0..count {
        let w = current_window(settings, lim, "BTC").await?;
        let book: Value =
            http.get(format!("{}/markets/{}/orderbook", lim.api_url, w.slug)).send().await?.json().await?;
        let Some(bid) = book.pointer("/bids/0/price").and_then(Value::as_f64) else {
            println!("{n}: no bid on {}; skipped", w.slug);
            continue;
        };
        match live::probe_once(&client, &w, bid, back, shares, max_usd).await {
            Ok(r) => {
                live::record_probe(&stats, &r);
                pk_core::journal::Sink::write(&mut journal, now_ns(), &r);
                println!("{n}: {r}");
            }
            Err(e) => println!("{n}: refused: {e}"),
        }
        tokio::time::sleep(Duration::from_secs_f64(every_s)).await;
    }
    println!("summary {}", stats.lock().json());
    Ok(())
}

async fn run(settings: Settings, lim: LimSettings) -> anyhow::Result<()> {
    let ledger = Ledger::open_mode(
        Some(&lim.state_dir.join("ledger.json")),
        lim.paper_cash,
        Some(&lim.epoch),
        lim.mode.as_str(),
    )?;
    let journal = JsonlDaily::new(lim.state_dir.join("journal"));
    let cfg = EngineCfg {
        params: lim.params.clone(),
        shares: lim.shares as f64,
        limit_offset: lim.limit_offset,
        exit_slip: lim.exit_slip,
        active_exit: lim.active_exit,
        fee_rate: lim.fee_rate,
        paper_extra_ms: lim.paper_extra_ms,
        live: lim.mode == Mode::Live,
        max_open_usd: lim.max_open_usd,
        daily_loss_usd: lim.daily_loss_usd,
    };
    let engine = Arc::new(Mutex::new(Engine::new(cfg, &lim.assets, ledger, journal, stamp(&lim))));
    let recorder = if lim.record {
        Some(Mutex::new(Recorder::start(
            &lim.record_dir,
            &settings.host_tag,
            (settings.min_free_disk_gb * 1e9) as u64,
            200_000,
        )?))
    } else {
        None
    };
    let app = Arc::new(App {
        lim: lim.clone(),
        settings: settings.clone(),
        engine: engine.clone(),
        kalshi: Mutex::new(None),
        feed: Mutex::new(None),
        recorder,
        probe: Mutex::new(None),
        taker: Arc::new(Mutex::new(TakerStats::default())),
        order_probe: Arc::new(Mutex::new(OrderProbeStats::default())),
        heartbeat_failures: Arc::new(AtomicU64::new(0)),
        discovery_errors: AtomicU64::new(0),
        last_discovery_error: Mutex::new(None),
        windows: Mutex::new(HashMap::new()),
        live: Mutex::new(None),
        chain: Mutex::new(None),
        redeemed: Mutex::new(Vec::new()),
        started_ns: now_ns(),
    });

    // Live: the order path must come up, or nothing trades.
    let (intent_tx, mut intent_rx) = tokio::sync::mpsc::unbounded_channel::<engine::Intent>();
    if lim.mode == Mode::Live {
        let client = connect_client(&lim).await?;
        let rpc = lim.base_rpc_url.clone().expect("checked in settings");
        let chain = Arc::new(LimChain::connect(&rpc, lim.private_key.as_deref().expect("checked")).await?);
        let (usdc, _) = chain.balances().await?;
        engine.lock().ledger.set_cash(usdc, now_ns());
        tokio::spawn(live::heartbeat(client.clone(), app.heartbeat_failures.clone()));
        *app.live.lock() = Some(client.clone());
        *app.chain.lock() = Some(chain);
        let (engine, taker) = (engine.clone(), app.taker.clone());
        tokio::spawn(async move {
            while let Some(i) = intent_rx.recv().await {
                tokio::spawn(live::execute(client.clone(), engine.clone(), taker.clone(), i));
            }
        });
        tracing::warn!("pk-limitless LIVE");
    }
    let send_intents = {
        let tx = intent_tx.clone();
        move |e: &mut Engine| {
            for i in e.take_intents() {
                let _ = tx.send(i);
            }
        }
    };

    // Feeds.
    let pem = std::fs::read_to_string(&settings.kalshi_private_key_path)?;
    let signer = Arc::new(KalshiSigner::from_pem(&settings.kalshi_api_key_id, &pem)?);
    {
        let (a, si) = (app.clone(), send_intents.clone());
        let emit: pk_feeds::Emit = Arc::new(move |venue, text: String| {
            let t = now_ns();
            a.tape(venue, t, &text);
            let mut e = a.engine.lock();
            match venue {
                'k' => e.on_kalshi(&text, t),
                'c' if text.contains(r#""venue":"k""#) => e.invalidate_kalshi(),
                _ => {}
            }
            si(&mut e);
        });
        *app.kalshi.lock() = Some(spawn_feed(settings.kalshi_ws_url.clone(), FeedKind::Kalshi { signer }, emit));
    }
    {
        let (a, si) = (app.clone(), send_intents.clone());
        let emit: pk_feeds::Emit = Arc::new(move |_, text: String| {
            let t = now_ns();
            a.tape('l', t, &text);
            let mut e = a.engine.lock();
            e.on_limitless(&text, t);
            si(&mut e);
        });
        *app.feed.lock() = Some(spawn_limitless(lim.ws_url.clone(), emit));
    }

    // Discovery: this window, on both venues, every 2 s until found.
    {
        let a = app.clone();
        tokio::spawn(async move {
            let d = LimDiscovery::new(&a.lim.api_url, &a.settings.poly_gamma_url, &a.settings.kalshi_rest_url);
            let mut tick = tokio::time::interval(Duration::from_secs(2));
            loop {
                tick.tick().await;
                let open = window_open(now_ns() / NS);
                let mut changed = false;
                for asset in &a.lim.assets {
                    if a.engine.lock().window(asset).is_some_and(|w| w.open_unix == open) {
                        continue;
                    }
                    match d.discover(asset, open).await {
                        Ok(Some(w)) => {
                            let t = now_ns();
                            let mut ctl = serde_json::to_value(&w).unwrap_or(Value::Null);
                            ctl["type"] = "lim_window".into();
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
                    let (tickers, slugs) = {
                        let e = a.engine.lock();
                        (e.tickers(), e.slugs())
                    };
                    if let Some(k) = a.kalshi.lock().as_ref() {
                        k.set_wanted(tickers.into_iter().collect::<BTreeSet<_>>());
                    }
                    if let Some(l) = a.feed.lock().as_ref() {
                        l.set_wanted(slugs);
                    }
                    let cutoff = open - 6 * 3600;
                    a.windows.lock().retain(|k, _| k.1 >= cutoff);
                }
            }
        });
    }

    // Timers: held paper orders, probes, markouts, series (every 50 ms).
    {
        let (a, si) = (app.clone(), send_intents.clone());
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(Duration::from_millis(50));
            tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            loop {
                tick.tick().await;
                let mut e = a.engine.lock();
                e.tick(now_ns());
                si(&mut e);
            }
        });
    }

    // Settlement (and, live, redemption) every 15 s.
    {
        let a = app.clone();
        tokio::spawn(async move {
            let d = LimDiscovery::new(&a.lim.api_url, &a.settings.poly_gamma_url, &a.settings.kalshi_rest_url);
            let mut tick = tokio::time::interval(Duration::from_secs(15));
            loop {
                tick.tick().await;
                let due = a.engine.lock().unsettled(now_ns());
                for (asset, open) in due {
                    let slug = discovery::lim_slug(&asset, open);
                    let Ok(Some(outcome)) = d.outcome(&slug).await else { continue };
                    a.engine.lock().settle(&asset, open, outcome, now_ns());
                    let chain = a.chain.lock().clone();
                    let cond = a.windows.lock().get(&(asset.clone(), open)).map(|w| w.condition_id.clone());
                    if let (Some(chain), Some(cond)) = (chain, cond) {
                        let r = match chain.redeem(&cond).await {
                            Ok(tx) => json!({"asset": asset, "open_unix": open, "tx": tx}),
                            Err(e) => json!({"asset": asset, "open_unix": open, "error": e.to_string()}),
                        };
                        a.engine.lock().note(now_ns(), "lim_redeem", r.clone());
                        a.redeemed.lock().push(r);
                    }
                }
                let chain = a.chain.lock().clone();
                if let Some(chain) = chain
                    && let Ok((usdc, _)) = chain.balances().await
                {
                    let mut e = a.engine.lock();
                    if e.ledger.open_positions().count() == 0 {
                        e.ledger.set_cash(usdc, now_ns());
                    }
                }
            }
        });
    }

    // Network probes on their own warm connection (never the live order's).
    {
        let a = app.clone();
        let every = Duration::from_secs(a.lim.netprobe_every_s.max(1));
        tokio::spawn(async move {
            let mut cfg = match url::Url::parse(&a.lim.api_url) {
                Ok(u) => WarmConfig::new(u),
                Err(_) => return,
            };
            cfg.warm_path = "/markets/btc-15min-price".into();
            let warm = loop {
                match WarmHttp::connect(cfg.clone()).await {
                    Ok(w) => break w,
                    Err(e) => {
                        tracing::warn!(error = %e, "lim_netprobe_connect");
                        tokio::time::sleep(Duration::from_secs(10)).await;
                    }
                }
            };
            warm.spawn_maintenance();
            let probe = Arc::new(NetProbe::new(warm, &a.lim.api_url));
            *a.probe.lock() = Some(probe.clone());
            let mut tick = tokio::time::interval(every);
            let mut n: u64 = 0;
            let mut last_summary = now_ns();
            loop {
                tick.tick().await;
                let slug = a.engine.lock().slugs().into_iter().next();
                let Some(slug) = slug else { continue };
                probe.round(&slug, n % 12 == 0).await;
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
                    a.engine.lock().note(last_summary, "lim_probe", s);
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
            "/v1/lim/status",
            get(|State(a): State<Arc<App>>| async move {
                ([(header::CONTENT_TYPE, "application/json")], a.status().to_string()).into_response()
            }),
        )
        .route(
            "/v1/lim/series",
            get(|State(a): State<Arc<App>>, Query(q): Query<Since>| async move {
                let body = a.engine.lock().series_since(q.since.unwrap_or(0));
                ([(header::CONTENT_TYPE, "application/json")], body.to_string()).into_response()
            }),
        )
        .layer(axum::middleware::from_fn_with_state(key, require_key))
        .route("/health/live", get(|| async { axum::Json(json!({"status": "ok"})) }))
        .with_state(app.clone());
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", lim.port)).await?;
    tracing::info!(port = lim.port, mode = lim.mode.as_str(), assets = ?lim.assets, "pk_limitless_running");
    axum::serve(listener, api).with_graceful_shutdown(shutdown()).await?;
    if let Some(r) = &app.recorder {
        r.lock().stop();
    }
    tracing::info!("pk_limitless_stopped");
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
