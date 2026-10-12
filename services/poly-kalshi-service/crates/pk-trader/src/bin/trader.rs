//! `poly-kalshi-service`: the trader. Paper by default; `PK_EXECUTION_MODE=live`
//! sends real orders once its guards pass (see `pk_svc::config`). Live, an
//! order path that does not start is fatal: the process exits non-zero so
//! systemd's `OnFailure` alert fires, rather than trading blind.
//!
//! With `PK_FAST_PATH` (live), the Kalshi feed runs on the pinned hot thread
//! (`pk_trader::hot`) and each of its frames goes through the armed trigger
//! (`pk_trader::fast`) before it is queued for the engine.

use std::sync::{Arc, OnceLock};
use std::time::Duration;

use clap::Parser;
use pk_core::journal::JsonlDaily;
use pk_core::{Ledger, RiskLimits, RiskState};
use pk_engine::Role;
use pk_exec::{LiveLimits, RelayerConfig, WalletConfig};
use pk_feeds::{Discovery, Emit, FeedKind, KalshiSigner, PolyShards, spawn_feed};
use pk_svc::{Alerts, Notifier, Settings};
use pk_tape::Recorder;
use pk_trader::exec_bridge::{BridgeConfig, ExecBridge, Hooks, RedeemConfig};
use pk_trader::fast::FastPath;
use pk_trader::hot::HotThread;
use pk_trader::ingest::Ingest;
use pk_trader::link::{LinkSink, ObserverLink};
use pk_trader::observer::SharedJournal;
use pk_trader::runtime::Runtime;
use pk_trader::telegram::{Handler, TelegramCommands};

#[derive(Parser)]
#[command(
    name = "poly-kalshi-service",
    about = "poly-kalshi: Kalshi-signal / Polymarket-leg 15-minute convergence (paper or live)"
)]
struct Args {
    #[arg(long)]
    profile: Option<String>,
}

fn main() -> anyhow::Result<()> {
    pk_svc::tls::install_default_provider();
    let args = Args::parse();
    let settings = match Settings::load(args.profile.as_deref()).and_then(|s| s.check_paths().map(|()| s)) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("poly-kalshi-service: {e:#}");
            std::process::exit(2);
        }
    };
    let _guard = pk_svc::logging::init(&settings.log_level, Some(&settings.events_log_path));
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .thread_name("pk-tokio")
        .build()?
        .block_on(run(settings))
}

async fn run(settings: Settings) -> anyhow::Result<()> {
    let notifier = settings.notify.enabled.then(|| Notifier::new(settings.notify.clone()));
    let alerts = Alerts::new(notifier, Duration::from_secs(300));
    let live = settings.execution_mode == pk_svc::ExecutionMode::Live;
    // Live starts from an empty wallet mirror; the first balance read sets it.
    let ledger = Ledger::open_mode(
        Some(&settings.state_dir.join("ledger.json")),
        if live { 0.0 } else { settings.paper_start_cash_usd },
        Some(&settings.results_epoch),
        if live { "live" } else { "paper" },
    )?;
    let risk = RiskState::new(RiskLimits {
        max_open_notional_usd: settings.max_open_notional_usd,
        daily_loss_limit_usd: settings.daily_loss_limit_usd,
        kill_file: Some(settings.kill_file_path.clone()),
    });
    let journal_dir = settings.state_dir.join("journal");
    let (link, recorder, journal_direct, engine) = if settings.observer_enabled {
        let exe = std::env::current_exe()?;
        let observer = exe.with_file_name("pk-observer");
        let mut argv = vec![observer.display().to_string()];
        if let Some(p) = &settings.profile {
            argv.extend(["--profile".into(), p.clone()]);
        }
        let link = Arc::new(ObserverLink::new(argv, JsonlDaily::new(&journal_dir)));
        link.spawn()?;
        let engine =
            pk_trader::build_engine(&settings, Role::Trade, Box::new(LinkSink(link.clone())), ledger, risk);
        (Some(link), None, None, engine)
    } else {
        let journal = SharedJournal(Arc::new(parking_lot::Mutex::new(JsonlDaily::new(&journal_dir))));
        let recorder = settings
            .record_enabled
            .then(|| {
                Recorder::start(
                    &settings.record_dir,
                    &settings.host_tag,
                    (settings.min_free_disk_gb * 1e9) as u64,
                    500_000,
                )
            })
            .transpose()?;
        let engine = pk_trader::build_engine(&settings, Role::Full, Box::new(journal.clone()), ledger, risk);
        (None, recorder, Some(journal), engine)
    };
    let mut engine = engine;
    let a = alerts.clone();
    engine.set_alert(Box::new(move |key, subject, lines| a.send(key, subject, lines.to_vec(), false)));

    let (ingest, rx) = Ingest::new();
    let ingest = Arc::new(ingest);
    let emit: Emit = {
        let ingest = ingest.clone();
        Arc::new(move |venue, text| ingest.push(venue, text))
    };
    // The fast path: the Kalshi feed on its own pinned thread, each frame
    // checked by the armed trigger before it is queued.
    let fast_slot: Arc<OnceLock<Arc<FastPath>>> = Arc::default();
    let hot = if live && settings.fast_path {
        Some(HotThread::start(settings.hot_core, settings.hot_spin)?)
    } else {
        None
    };
    let kalshi_emit: Emit = {
        let (ingest, slot) = (ingest.clone(), fast_slot.clone());
        Arc::new(move |venue, text: String| {
            if let Some(fast) = slot.get() {
                match venue {
                    'k' => fast.on_kalshi(&text),
                    'c' => fast.on_control(&text),
                    _ => {}
                }
            }
            ingest.push(venue, text)
        })
    };
    let pem = std::fs::read_to_string(&settings.kalshi_private_key_path)?;
    let signer = Arc::new(KalshiSigner::from_pem(&settings.kalshi_api_key_id, &pem)?);
    let kalshi = {
        let _on_hot = hot.as_ref().map(|h| h.handle.enter());
        spawn_feed(settings.kalshi_ws_url.clone(), FeedKind::Kalshi { signer }, kalshi_emit)
    };
    let poly = PolyShards::spawn(&settings.poly_ws_url, emit, &settings.assets, settings.poly_sockets);
    let discovery = Discovery::new(&settings.poly_gamma_url, &settings.kalshi_rest_url);

    let rt = Arc::new(Runtime::new(
        settings.clone(),
        engine,
        ingest,
        alerts.clone(),
        discovery,
        Some(kalshi),
        Some(poly),
        link.clone(),
        recorder,
        journal_direct,
    ));
    {
        let rt = rt.clone();
        std::thread::Builder::new().name("pk-engine".into()).spawn(move || rt.engine_loop(rx))?;
    }
    tracing::info!(profile = ?settings.profile, mode = settings.execution_mode.as_str(), "pk_starting");

    // Paper with percentage sizing: seed the size from the ledger, on the tape.
    if !live && settings.order_size_pct > 0.0 {
        let equity = {
            let e = rt.engine.lock();
            e.ledger.cash + e.ledger.open_positions().map(|p| p.cost + p.fee).sum::<f64>()
        };
        rt.control(serde_json::json!({"type": "sizing", "balance": equity}));
    }

    if settings.order_path() {
        let wallet = WalletConfig {
            clob_url: settings.poly_clob_url.clone(),
            private_key: settings.poly_private_key.clone().unwrap_or_default(),
            funder: settings.poly_funder.clone(),
            signature_type: settings.poly_signature_type,
        };
        let hooks = {
            let w = Arc::downgrade(&rt);
            let (w1, w2, w3, w4) = (w.clone(), w.clone(), w.clone(), w.clone());
            Hooks {
                journal: Arc::new(move |t, r| {
                    if let Some(rt) = w.upgrade() {
                        rt.journal(t, r)
                    }
                }),
                control: Arc::new(move |v| {
                    if let Some(rt) = w1.upgrade() {
                        rt.control(v)
                    }
                }),
                alert: Arc::new(move |key, subject, lines, urgent| {
                    if let Some(rt) = w2.upgrade() {
                        rt.alerts.send(key, subject, lines, urgent)
                    }
                }),
                kill: Arc::new(move |source, reason| {
                    if let Some(rt) = w3.upgrade() {
                        rt.kill(source, reason);
                    }
                }),
                view: Arc::new(move || w4.upgrade().map(|rt| rt.live_view()).unwrap_or_default()),
            }
        };
        let cfg = BridgeConfig {
            latency_seed_ms: settings.paper_latency_ms as f64,
            latency_window: settings.latency_window,
            latency_min_samples: settings.latency_min_samples,
        };
        let live_cfg = live.then(|| {
            let d = |x: f64| rust_decimal::Decimal::from_f64_retain(x).unwrap_or_default().round_dp(4);
            (
                LiveLimits {
                    max_entry_px: d(settings.params.max_entry_px),
                    max_notional: d(settings.max_open_notional_usd),
                    ..LiveLimits::default()
                },
                RedeemConfig {
                    rpc_url: settings.polygon_rpc_url.clone().unwrap_or_default(),
                    collateral: settings.poly_redeem_collateral.clone(),
                    relayer: (settings.poly_signature_type == 3).then(|| RelayerConfig {
                        url: settings.poly_relayer_url.clone(),
                        api_key: settings.poly_relayer_api_key.clone().unwrap_or_default(),
                        api_key_address: settings.poly_relayer_api_key_address.clone(),
                    }),
                },
            )
        });
        let cadence =
            (settings.probe_cadence_s > 0.0).then(|| Duration::from_secs_f64(settings.probe_cadence_s));
        match ExecBridge::connect(&wallet, hooks, cfg, live_cfg).await {
            Ok(bridge) => {
                let (tx, rx) = tokio::sync::mpsc::unbounded_channel();
                *rt.exec.lock() = Some(bridge.clone());
                *rt.exec_tx.lock() = Some(tx.clone());
                if let Some(h) = &hot {
                    let rt_handle = tokio::runtime::Handle::current();
                    match FastPath::new(bridge.clone(), settings.fast_conns, tx, rt_handle) {
                        Ok(fast) => {
                            rt.engine.lock().set_fast_gate(fast.clone());
                            *rt.fast.lock() = Some(fast.clone());
                            *rt.hot.lock() = Some(h.stats());
                            tokio::spawn(fast.clone().refresh_loop());
                            let _ = fast_slot.set(fast);
                            tracing::info!(conns = settings.fast_conns, "fast_path_ready");
                        }
                        Err(e) => {
                            tracing::error!(error = %format!("{e:#}"), "fast path did not start");
                            rt.alerts.send(
                                "fast_path",
                                "poly-kalshi: the fast path did not start; the normal order path trades",
                                vec![format!("{e:#}")],
                                true,
                            );
                        }
                    }
                }
                if live {
                    // Nothing of ours should rest; then read the wallet before trading.
                    bridge.cancel_all().await;
                    bridge.reconcile().await;
                }
                tokio::spawn(bridge.clone().run(rx));
                tokio::spawn(bridge.run_background(cadence));
            }
            Err(e) if live => {
                tracing::error!(error = %format!("{e:#}"), "live order path did not start");
                rt.alerts.send(
                    "order_path",
                    "poly-kalshi: the LIVE order path did not start; exiting",
                    vec![format!("{e:#}")],
                    true,
                );
                tokio::time::sleep(Duration::from_millis(500)).await;
                std::process::exit(3);
            }
            Err(e) => rt.alerts.send(
                "order_path",
                "poly-kalshi: the order path did not start",
                vec![format!("{e:#}"), "Paper trading continues without the probe.".into()],
                true,
            ),
        }
    }

    tokio::spawn(rt.clone().every(pk_trader::runtime::TICK, |r| Box::pin(r.window_tick())));
    tokio::spawn(rt.clone().every(pk_trader::runtime::RESOLUTION_POLL, |r| Box::pin(r.resolution_tick())));
    tokio::spawn(
        rt.clone().every(Duration::from_secs_f64(settings.heartbeat_seconds.max(5.0)), |r| {
            Box::pin(r.health_tick())
        }),
    );
    tokio::spawn(rt.clone().loop_lag());

    if let Some(token) = settings.telegram_bot_token.clone() {
        let hrt = rt.clone();
        let mode = settings.execution_mode.as_str();
        let handler: Handler = Arc::new(move |command, user| {
            let rt = hrt.clone();
            Box::pin(async move {
                if command == "flatten" {
                    let out = rt.flatten(&format!("telegram user {user}")).await;
                    return format!("HALTED and flattened: {}", out["flattened"]);
                }
                if command == "kill" {
                    let fired = rt.kill("telegram", &format!("user {user}"));
                    return if fired {
                        "HALTED. Ack via POST /v1/kill/ack.".into()
                    } else {
                        "Already halted.".into()
                    };
                }
                let (halted, l) = {
                    let e = rt.engine.lock();
                    (e.risk.halted, e.ledger.snapshot(e.last_ns.max(1)))
                };
                format!(
                    "mode={mode} halted={halted} cash={} open={} today={} total={}",
                    l["cash"], l["open_positions"], l["realised_today"], l["realised_total"]
                )
            })
        });
        let ca = alerts.clone();
        let bot = TelegramCommands::new(
            token,
            settings.telegram_admin_user_ids.clone(),
            handler,
            Box::new(move |_e| {
                ca.send(
                    "telegram_conflict",
                    "poly-kalshi: Telegram /kill is not reaching this service",
                    vec![
                        "Another process polls the same bot token (getUpdates Conflict).".into(),
                        "Give PK_TELEGRAM_BOT_TOKEN a bot of its own; until then use POST /v1/kill or the kill file."
                            .into(),
                    ],
                    true,
                )
            }),
        );
        *rt.telegram.lock() = Some(bot.stats.clone());
        tokio::spawn(bot.run());
    }

    if !pk_svc::api::is_loopback(&settings.host) {
        tracing::error!(host = %settings.host, port = settings.port, "api_exposed");
        alerts.send(
            "api_exposed",
            "poly-kalshi: API bound to a non-loopback address",
            vec![
                format!("HOST={} port {}: reachable beyond this host.", settings.host, settings.port),
                "Set HOST=127.0.0.1 and reach it over SSH; check the firewall too.".into(),
            ],
            true,
        );
    }
    alerts.send(
        "start",
        "poly-kalshi started",
        vec![
            format!("profile={:?} mode={}", settings.profile, settings.execution_mode.as_str()),
            format!("assets={} host={}", settings.assets.join(","), settings.host_tag),
        ],
        false,
    );

    let listener = tokio::net::TcpListener::bind((settings.host.as_str(), settings.port)).await?;
    tracing::info!(addr = %listener.local_addr()?, "api listening");
    axum::serve(listener, pk_trader::api::router(rt.clone()))
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    tracing::info!("pk_stopping");
    rt.stopping.store(true, std::sync::atomic::Ordering::Relaxed);
    let bridge = rt.exec.lock().clone();
    if let Some(b) = bridge {
        b.shutdown().await;
    }
    if let Some(link) = link {
        tokio::task::spawn_blocking(move || link.close()).await?;
    }
    if let Some(r) = &rt.recorder {
        r.lock().stop();
    }
    tokio::time::sleep(Duration::from_millis(200)).await; // let the last alerts go out
    Ok(())
}

async fn shutdown_signal() {
    let ctrl_c = tokio::signal::ctrl_c();
    let mut term =
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("SIGTERM handler");
    tokio::select! {
        _ = ctrl_c => {}
        _ = term.recv() => {}
    }
}
