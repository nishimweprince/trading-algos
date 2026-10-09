//! `poly-kalshi-service`: the trader. Paper only; `PK_EXECUTION_MODE=live` is refused.

use std::sync::Arc;
use std::time::Duration;

use clap::Parser;
use pk_core::journal::JsonlDaily;
use pk_core::{Ledger, RiskLimits, RiskState};
use pk_engine::Role;
use pk_exec::WalletConfig;
use pk_feeds::{Discovery, Emit, FeedKind, KalshiSigner, PolyShards, spawn_feed};
use pk_svc::{Alerts, Notifier, Settings};
use pk_tape::Recorder;
use pk_trader::exec_bridge::ExecBridge;
use pk_trader::ingest::Ingest;
use pk_trader::link::{LinkSink, ObserverLink};
use pk_trader::observer::SharedJournal;
use pk_trader::runtime::Runtime;
use pk_trader::telegram::{Handler, TelegramCommands};

#[derive(Parser)]
#[command(
    name = "poly-kalshi-service",
    about = "poly-kalshi: Kalshi-signal / Polymarket-leg 15-minute convergence (paper)"
)]
struct Args {
    #[arg(long)]
    profile: Option<String>,
}

fn main() -> anyhow::Result<()> {
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
    let ledger = Ledger::open(
        Some(&settings.state_dir.join("ledger.json")),
        settings.paper_start_cash_usd,
        Some(&settings.results_epoch),
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
    let pem = std::fs::read_to_string(&settings.kalshi_private_key_path)?;
    let signer = Arc::new(KalshiSigner::from_pem(&settings.kalshi_api_key_id, &pem)?);
    let kalshi = spawn_feed(settings.kalshi_ws_url.clone(), FeedKind::Kalshi { signer }, emit.clone());
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

    if settings.order_probe {
        let rt2 = rt.clone();
        let wallet = WalletConfig {
            clob_url: settings.poly_clob_url.clone(),
            private_key: settings.poly_private_key.clone().unwrap_or_default(),
            funder: settings.poly_funder.clone(),
            signature_type: settings.poly_signature_type,
        };
        tokio::spawn(async move {
            let journal_rt = rt2.clone();
            let journal: pk_trader::exec_bridge::JournalFn = Arc::new(move |t, r| journal_rt.journal(t, r));
            match ExecBridge::connect(&wallet, journal).await {
                Ok(bridge) => {
                    let (tx, rx) = tokio::sync::mpsc::unbounded_channel();
                    *rt2.exec.lock() = Some(bridge.clone());
                    *rt2.exec_tx.lock() = Some(tx);
                    bridge.run(rx).await;
                }
                Err(e) => rt2.alerts.send(
                    "order_path",
                    "poly-kalshi: the order path did not start",
                    vec![format!("{e:#}"), "Paper trading continues without the probe.".into()],
                    true,
                ),
            }
        });
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
