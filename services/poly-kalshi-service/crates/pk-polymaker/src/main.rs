//! `pk-polymaker`: resting post-only bids on both sides of Polymarket's
//! 15-minute Up/Down markets, priced off Kalshi's YES mid. A process of its
//! own beside the trader: its own Kalshi and Polymarket sockets, state,
//! journal and port. It shares the trader's wallet, so it refuses to go live
//! while the trader is (`PK_EXECUTION_MODE=live`).
//!
//! | Command | |
//! |---|---|
//! | `pk-polymaker --profile dev` | run (shadow unless `PK_PM_MODE=live` and `PK_PM_LIVE_CONFIRM=yes`) |
//! | `touch <state>/polymaker/ACK` | clear today's loss halt (read every 15 s) |
//!
//! | Route (127.0.0.1:`PK_PM_PORT`, `X-API-Key`) | |
//! |---|---|
//! | `GET /v1/pm/status` | engine, coins, fills, feeds, order path |
//! | `GET /v1/pm/series?since=` | realised P&L after each settled window |
//! | `GET /health/live` | process up (no key) |

mod config;
mod engine;
mod live;

use std::collections::BTreeSet;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering};
use std::time::Duration;

use axum::Router;
use axum::extract::{Query, State};
use axum::http::header;
use axum::response::IntoResponse;
use axum::routing::get;
use clap::Parser;
use parking_lot::Mutex;
use pk_core::{NS, window_open};
use pk_feeds::{Discovery, FeedHandle, FeedKind, KalshiSigner, spawn_feed};
use pk_svc::Settings;
use pk_svc::api::{ApiKey, require_key};
use serde::Deserialize;
use serde_json::{Map, Value, json};

use crate::config::{Mode, PmSettings};
use crate::engine::{Action, Engine, Gates};
use crate::live::{Exec, now_ns};

#[derive(Parser)]
#[command(name = "pk-polymaker", about = "Kalshi-priced post-only maker on Polymarket's 15-minute markets")]
struct Args {
    #[arg(long)]
    profile: Option<String>,
}

struct App {
    pm: PmSettings,
    settings: Settings,
    engine: Mutex<Engine>,
    exec: Option<Arc<Exec>>,
    kalshi: Mutex<Option<FeedHandle>>,
    poly: Mutex<Option<FeedHandle>>,
    /// Coinbase `ticker`, only with `PK_PM_SPOT_GUARD=yes`.
    spot: Mutex<Option<FeedHandle>>,
    last_k_ns: AtomicI64,
    last_p_ns: AtomicI64,
    last_tick_ns: AtomicI64,
    killed: AtomicBool,
    deadman_fired: AtomicBool,
    discovery_errors: AtomicU64,
    last_error: Mutex<Option<String>>,
    cash: Mutex<Option<f64>>,
    post_ack_ms: Mutex<pk_stats::Sample>,
    started_ns: i64,
}

impl App {
    fn note_error(&self, e: impl std::fmt::Display) {
        *self.last_error.lock() = Some(e.to_string());
    }

    fn feeds_ok(&self, t: i64) -> bool {
        let stale = self.pm.feed_stale_ms * 1_000_000;
        t - self.last_k_ns.load(Ordering::Relaxed) < stale && t - self.last_p_ns.load(Ordering::Relaxed) < stale
    }

    fn status(&self) -> Value {
        let t = now_ns();
        let mut st = self.engine.lock().status(t);
        st["feeds"] = json!({
            "kalshi": self.kalshi.lock().as_ref().map(FeedHandle::stats),
            "polymarket": self.poly.lock().as_ref().map(FeedHandle::stats),
            "coinbase": self.spot.lock().as_ref().map(FeedHandle::stats),
            "ok": self.feeds_ok(t),
        });
        let ack = self.post_ack_ms.lock();
        st["order_path"] = json!({
            "live": self.pm.mode == Mode::Live,
            "exec": self.exec.as_ref().map(|e| serde_json::to_value(e.clob.stats()).unwrap_or(Value::Null)),
            "post_ack_ms": {"n": ack.n(), "mean": ack.mean().map(|m| (m * 10.0).round() / 10.0)},
            "cash": *self.cash.lock(),
            "deadman_fired": self.deadman_fired.load(Ordering::Relaxed),
        });
        st["kill"] = self.killed.load(Ordering::Relaxed).into();
        st["discovery"] = json!({"errors": self.discovery_errors.load(Ordering::Relaxed), "last_error": self.last_error.lock().clone()});
        st["host"] = self.settings.host_tag.clone().into();
        st["uptime_s"] = ((t - self.started_ns) / NS).into();
        st
    }

    /// Hand the engine's actions to the order path (or, in shadow, answer them here).
    fn dispatch(self: &Arc<Self>, actions: Vec<Action>) {
        for a in actions {
            let Some(exec) = self.exec.clone() else {
                let mut e = self.engine.lock();
                match a {
                    Action::Post { asset, up, px, size, .. } => {
                        let id = e.shadow_id();
                        e.on_posted(&asset, up, px, size, Ok(id), now_ns());
                    }
                    Action::Cancel { asset, up, order_id } => e.on_cancelled(&asset, up, &order_id, true, None, now_ns()),
                    Action::CancelAll { .. } => {}
                }
                continue;
            };
            let app = self.clone();
            tokio::spawn(async move {
                match a {
                    Action::Post { asset, up, token, px, size } => {
                        let (r, us) = exec.post(&token, px, size).await;
                        app.post_ack_ms.lock().push(us as f64 / 1000.0);
                        app.engine.lock().on_posted(&asset, up, px, size, r, now_ns());
                    }
                    Action::Cancel { asset, up, order_id } => {
                        let (ok, why) = exec.cancel(&order_id).await;
                        app.engine.lock().on_cancelled(&asset, up, &order_id, ok, why, now_ns());
                    }
                    Action::CancelAll { reason } => match exec.clob.cancel_all().await {
                        Ok(a) => tracing::warn!(reason, cancelled = a.canceled.len(), "pm_cancel_all"),
                        Err(e) => tracing::error!(reason, error = %e, "pm_cancel_all_failed"),
                    },
                }
            });
        }
    }
}

fn stamp(pm: &PmSettings) -> Map<String, Value> {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    pm.config_json().to_string().hash(&mut h);
    let mut m = Map::new();
    m.insert("epoch".into(), pm.epoch.clone().into());
    m.insert("build".into(), pk_svc::build::build_id().into());
    m.insert("config".into(), format!("{:010x}", h.finish() & 0xff_ffff_ffff).into());
    m.insert("mode".into(), pm.mode.as_str().into());
    m
}

fn main() -> anyhow::Result<()> {
    pk_svc::tls::install_default_provider();
    let args = Args::parse();
    let settings = Settings::load(args.profile.as_deref())?;
    let pm = PmSettings::load(&settings)?;
    let events = settings.events_log_path.with_extension("polymaker.jsonl");
    let _guard = pk_svc::logging::init(&settings.log_level, Some(&events));
    let rt = tokio::runtime::Builder::new_multi_thread().worker_threads(2).enable_all().build()?;
    rt.block_on(run(settings, pm))
}

async fn run(settings: Settings, pm: PmSettings) -> anyhow::Result<()> {
    let shadow = pm.mode == Mode::Shadow;
    let engine = Engine::new(pm.cfg.clone(), &pm.assets, &pm.state_dir, stamp(&pm), shadow);
    // Live: the order path must come up, and nothing of ours may rest from before.
    let exec = if shadow {
        None
    } else {
        let exec = Arc::new(Exec::connect(&settings).await?);
        let a = exec.clob.cancel_all().await?;
        tracing::warn!(cancelled = a.canceled.len(), "pm LIVE: cancel-all at start");
        Some(exec)
    };
    let app = Arc::new(App {
        pm: pm.clone(),
        settings: settings.clone(),
        engine: Mutex::new(engine),
        exec,
        kalshi: Mutex::new(None),
        poly: Mutex::new(None),
        spot: Mutex::new(None),
        last_k_ns: AtomicI64::new(0),
        last_p_ns: AtomicI64::new(0),
        last_tick_ns: AtomicI64::new(now_ns()),
        killed: AtomicBool::new(false),
        deadman_fired: AtomicBool::new(false),
        discovery_errors: AtomicU64::new(0),
        last_error: Mutex::new(None),
        cash: Mutex::new(None),
        post_ack_ms: Mutex::new(pk_stats::Sample::default()),
        started_ns: now_ns(),
    });

    // Feeds: a reconnect invalidates that venue's books (and so pulls the quotes).
    let pem = std::fs::read_to_string(&settings.kalshi_private_key_path)?;
    let signer = Arc::new(KalshiSigner::from_pem(&settings.kalshi_api_key_id, &pem)?);
    {
        let a = app.clone();
        let emit: pk_feeds::Emit = Arc::new(move |venue, text: String| {
            let t = now_ns();
            let actions = {
                let mut e = a.engine.lock();
                match venue {
                    'k' => {
                        a.last_k_ns.store(t, Ordering::Relaxed);
                        e.on_kalshi(&text, t);
                    }
                    'c' if text.contains(r#""venue":"k""#) => e.invalidate_kalshi(),
                    _ => {}
                }
                e.take_actions()
            };
            a.dispatch(actions);
        });
        *app.kalshi.lock() = Some(spawn_feed(settings.kalshi_ws_url.clone(), FeedKind::Kalshi { signer }, emit));
    }
    {
        let a = app.clone();
        let emit: pk_feeds::Emit = Arc::new(move |venue, text: String| {
            let t = now_ns();
            let mut e = a.engine.lock();
            match venue {
                'p' => {
                    a.last_p_ns.store(t, Ordering::Relaxed);
                    e.on_poly(&text, t);
                }
                'c' if text.contains(r#""venue":"p""#) => e.invalidate_poly(),
                _ => {}
            }
        });
        let kind = FeedKind::Poly { shard: None, silence: Some(Duration::from_secs(30)) };
        *app.poly.lock() = Some(spawn_feed(settings.poly_ws_url.clone(), kind, emit));
    }
    // Spot for the directional guard. A stale spot only lifts the guard (as
    // pk-maker-cpp); it does not stop the quotes.
    if app.pm.cfg.spot_guard {
        let a = app.clone();
        let emit: pk_feeds::Emit = Arc::new(move |venue, text: String| {
            if venue == 'o' {
                a.engine.lock().on_spot(&text, now_ns());
            }
        });
        let feed = spawn_feed(settings.coinbase_ws_url.clone(), FeedKind::Coinbase, emit);
        feed.set_wanted(app.pm.assets.iter().filter_map(|a| pk_feeds::ws::coinbase_product(a)).collect());
        *app.spot.lock() = Some(feed);
    }

    // Discovery: this window on both venues, every 2 s until found.
    {
        let a = app.clone();
        tokio::spawn(async move {
            let d = Discovery::new(&a.settings.poly_gamma_url, &a.settings.kalshi_rest_url);
            let mut tick = tokio::time::interval(Duration::from_secs(2));
            loop {
                tick.tick().await;
                let open = window_open(now_ns() / NS);
                let mut changed = false;
                for asset in &a.pm.assets {
                    if a.engine.lock().window(asset).is_some_and(|w| w.open_unix == open) {
                        continue;
                    }
                    match d.discover(asset, open).await {
                        Ok(Some(w)) => {
                            a.engine.lock().set_window(w, now_ns());
                            changed = true;
                        }
                        Ok(None) => {}
                        Err(e) => {
                            a.discovery_errors.fetch_add(1, Ordering::Relaxed);
                            a.note_error(format!("discover {asset}: {e}"));
                        }
                    }
                }
                if changed {
                    let (tickers, tokens) = {
                        let e = a.engine.lock();
                        (e.tickers().into_iter().collect::<BTreeSet<_>>(), e.tokens())
                    };
                    if let Some(k) = a.kalshi.lock().as_ref() {
                        k.set_wanted(tickers);
                    }
                    if let Some(p) = a.poly.lock().as_ref() {
                        p.set_wanted(tokens);
                    }
                }
            }
        });
    }

    // The quote loop, every 50 ms; the KILL file pulls everything.
    {
        let a = app.clone();
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(Duration::from_millis(50));
            tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            loop {
                tick.tick().await;
                let t = now_ns();
                let kill = a.settings.kill_file_path.exists();
                let was = a.killed.swap(kill, Ordering::Relaxed);
                let actions = {
                    let mut e = a.engine.lock();
                    e.tick(t, &Gates { kill, feeds_ok: a.feeds_ok(t) });
                    let mut v = e.take_actions();
                    if kill && !was {
                        e.record(t, "pm_kill", json!({"file": a.settings.kill_file_path}));
                        v.push(Action::CancelAll { reason: "KILL file".into() });
                    }
                    v
                };
                a.dispatch(actions);
                a.last_tick_ns.store(now_ns(), Ordering::Relaxed);
            }
        });
    }

    if let Some(exec) = app.exec.clone() {
        // Dead man: a quote loop stalled for 2 s cancels everything.
        let a = app.clone();
        let x = exec.clone();
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(Duration::from_millis(500));
            loop {
                tick.tick().await;
                let stalled = now_ns() - a.last_tick_ns.load(Ordering::Relaxed) > 2 * NS;
                if stalled && !a.deadman_fired.swap(true, Ordering::Relaxed) {
                    tracing::error!("pm_deadman: quote loop stalled, cancel-all");
                    let _ = x.clob.cancel_all().await;
                } else if !stalled {
                    a.deadman_fired.store(false, Ordering::Relaxed);
                }
            }
        });

        // Fills: matched shares of every tracked order, each second.
        let a = app.clone();
        let x = exec.clone();
        let every = Duration::from_millis(a.pm.fills_poll_ms.max(250));
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(every);
            tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            loop {
                tick.tick().await;
                let tracked = a.engine.lock().tracked();
                if tracked.is_empty() {
                    continue;
                }
                let open = match x.open_matched().await {
                    Ok(m) => m,
                    Err(e) => {
                        a.note_error(format!("open orders: {e}"));
                        continue;
                    }
                };
                for id in tracked {
                    if let Some(&m) = open.get(&id) {
                        a.engine.lock().on_order(&id, m, false, now_ns());
                        continue;
                    }
                    match x.final_state(&id).await {
                        Ok(Some((m, live))) => a.engine.lock().on_order(&id, m, !live, now_ns()),
                        Ok(None) => a.engine.lock().on_unknown(&id, now_ns()),
                        Err(e) => {
                            a.note_error(format!("order {id}: {e}"));
                            a.engine.lock().record(now_ns(), "pm_state_error", json!({"id": id, "error": e.to_string()}));
                        }
                    }
                }
            }
        });

        // Wallet: cash, and each held token against the booked inventory.
        let a = app.clone();
        let x = exec.clone();
        let every = Duration::from_secs(a.pm.reconcile_s.max(5));
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(every);
            loop {
                tick.tick().await;
                match x.cash().await {
                    Ok(c) => *a.cash.lock() = Some(c),
                    Err(e) => a.note_error(format!("cash: {e}")),
                }
                let held = a.engine.lock().held_tokens(now_ns());
                for token in held.keys() {
                    match x.token_balance(token).await {
                        Ok(b) => a.engine.lock().on_balance(token, b, now_ns()),
                        Err(e) => a.note_error(format!("token balance: {e}")),
                    }
                }
            }
        });
    }

    // Settlement, redemption and the halt ack, every 15 s.
    {
        let a = app.clone();
        tokio::spawn(async move {
            let d = Discovery::new(&a.settings.poly_gamma_url, &a.settings.kalshi_rest_url);
            let ack = a.pm.state_dir.join("ACK");
            let mut tick = tokio::time::interval(Duration::from_secs(15));
            loop {
                tick.tick().await;
                if ack.exists() {
                    let _ = std::fs::remove_file(&ack);
                    a.engine.lock().ack(now_ns());
                    tracing::warn!("pm_ack: halt cleared");
                }
                let due = a.engine.lock().unsettled(now_ns());
                for w in due {
                    match d.poly_outcome(&w).await {
                        Ok(Some(outcome)) => a.engine.lock().settle(&w.asset, w.open_unix, outcome, now_ns()),
                        Ok(None) => {}
                        Err(e) => a.note_error(format!("outcome {} {}: {e}", w.asset, w.open_unix)),
                    }
                }
                let Some(exec) = a.exec.clone() else { continue };
                let due = a.engine.lock().redeem_due();
                for (key, cond) in due {
                    let r = exec.redeemer.redeem(&cond).await.map_err(|e| format!("{e:#}"));
                    a.engine.lock().on_redeemed(&key, r, now_ns());
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
            "/v1/pm/status",
            get(|State(a): State<Arc<App>>| async move {
                ([(header::CONTENT_TYPE, "application/json")], a.status().to_string()).into_response()
            }),
        )
        .route(
            "/v1/pm/series",
            get(|State(a): State<Arc<App>>, Query(q): Query<Since>| async move {
                let body = a.engine.lock().series(q.since.unwrap_or(0));
                ([(header::CONTENT_TYPE, "application/json")], body.to_string()).into_response()
            }),
        )
        .layer(axum::middleware::from_fn_with_state(key, require_key))
        .route("/health/live", get(|| async { axum::Json(json!({"status": "ok"})) }))
        .with_state(app.clone());
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", pm.port)).await?;
    tracing::info!(port = pm.port, mode = pm.mode.as_str(), assets = ?pm.assets, "pk_polymaker_running");
    axum::serve(listener, api).with_graceful_shutdown(shutdown()).await?;

    // Pull everything before leaving.
    app.engine.lock().stop();
    if let Some(exec) = &app.exec {
        match exec.clob.cancel_all().await {
            Ok(a) => tracing::warn!(cancelled = a.canceled.len(), "pm shutdown: cancel-all"),
            Err(e) => tracing::error!(error = %e, "pm shutdown: cancel-all failed"),
        }
        tokio::time::sleep(Duration::from_millis(1500)).await;
        // The final states of what was resting, so late fills are booked.
        let tracked = app.engine.lock().tracked();
        for id in tracked {
            if let Ok(Some((m, live))) = exec.final_state(&id).await {
                app.engine.lock().on_order(&id, m, !live, now_ns());
            }
        }
    }
    app.engine.lock().save();
    tracing::info!("pk_polymaker_stopped");
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
