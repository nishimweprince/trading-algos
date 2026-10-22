//! `pk-observer`: the trader's child. Lines in on stdin, tape and statistics out.

use std::sync::Arc;

use axum::Router;
use axum::extract::{Query, State};
use axum::http::header;
use axum::response::IntoResponse;
use axum::routing::get;
use clap::Parser;
use parking_lot::Mutex;
use pk_core::journal::JsonlDaily;
use pk_core::{Ledger, RiskLimits, RiskState};
use pk_engine::Role;
use pk_feeds::ws::{POLYBOLT_SYMBOLS, binance_stream, bitstamp_channel, kraken_symbol};
use pk_feeds::{FeedKind, spawn_feed};
use pk_svc::Settings;
use pk_svc::api::{ApiKey, require_key};
use pk_tape::Recorder;
use pk_trader::now_ns;
use pk_trader::observer::{Observer, SharedJournal};
use serde::Deserialize;
use serde_json::json;

#[derive(Parser)]
#[command(name = "pk-observer")]
struct Args {
    #[arg(long)]
    profile: Option<String>,
}

#[derive(Deserialize)]
struct Since {
    since: Option<i64>,
}

fn main() -> anyhow::Result<()> {
    pk_svc::tls::install_default_provider();
    let args = Args::parse();
    let settings = Settings::load(args.profile.as_deref())?;
    let _guard = pk_svc::logging::init(&settings.log_level, Some(&settings.events_log_path));
    let journal = SharedJournal(Arc::new(Mutex::new(JsonlDaily::new(settings.state_dir.join("journal")))));
    // In memory only: the trader's ledger is the one that counts.
    let ledger = Ledger::new(settings.paper_start_cash_usd);
    let risk = RiskState::new(RiskLimits {
        max_open_notional_usd: settings.max_open_notional_usd,
        daily_loss_limit_usd: settings.daily_loss_limit_usd,
        kill_file: None,
    });
    let engine = pk_trader::build_engine(&settings, Role::Observe, Box::new(journal.clone()), ledger, risk);
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
    let observer = Observer::new(engine, journal, recorder);
    let references = settings.binance_enabled || settings.kraken_enabled || settings.bitstamp_enabled;
    let observer = Arc::new(if references { observer.with_leadlag() } else { observer });

    let rt = tokio::runtime::Builder::new_multi_thread().worker_threads(2).enable_all().build()?;
    {
        let observer = observer.clone();
        let settings = settings.clone();
        rt.spawn(async move {
            if let (Some(creds), true) = (settings.polybolt_credentials.clone(), observer.recorder.is_some())
            {
                let o = observer.clone();
                let emit: pk_feeds::Emit = Arc::new(move |venue, text: String| {
                    let text =
                        if text.contains(['\n', '\r']) { text.replace(['\r', '\n'], " ") } else { text };
                    if let Some(r) = &o.recorder {
                        r.lock().write(venue, now_ns(), text.trim());
                    }
                });
                let feed = spawn_feed(
                    settings.polybolt_url.clone(),
                    FeedKind::PolyBolt { credentials: creds },
                    emit,
                );
                let symbols = settings
                    .assets
                    .iter()
                    .filter_map(|a| POLYBOLT_SYMBOLS.iter().find(|(x, _)| x == a).map(|(_, s)| s.to_string()))
                    .collect();
                feed.set_wanted(symbols);
                *observer.polybolt.lock() = Some(feed);
            }
            if settings.binance_enabled {
                for (futures, url) in
                    [(false, &settings.binance_spot_ws_url), (true, &settings.binance_perp_ws_url)]
                {
                    let o = observer.clone();
                    let emit: pk_feeds::Emit = Arc::new(move |venue, text: String| o.binance(venue, &text));
                    let feed = spawn_feed(url.clone(), FeedKind::Binance { futures }, emit);
                    let streams = settings.assets.iter().filter_map(|a| binance_stream(a, futures)).collect();
                    feed.set_wanted(streams);
                    observer.binance.lock().push(feed);
                }
            }
            if settings.kraken_enabled {
                let o = observer.clone();
                let emit: pk_feeds::Emit = Arc::new(move |_, text: String| o.kraken(&text));
                let feed = spawn_feed(settings.kraken_ws_url.clone(), FeedKind::Kraken, emit);
                feed.set_wanted(settings.assets.iter().filter_map(|a| kraken_symbol(a)).map(str::to_owned).collect());
                *observer.kraken.lock() = Some(feed);
            }
            if settings.bitstamp_enabled {
                let o = observer.clone();
                let emit: pk_feeds::Emit = Arc::new(move |_, text: String| o.bitstamp(&text));
                let feed = spawn_feed(settings.bitstamp_ws_url.clone(), FeedKind::Bitstamp, emit);
                feed.set_wanted(settings.assets.iter().filter_map(|a| bitstamp_channel(a)).collect());
                *observer.bitstamp.lock() = Some(feed);
            }
            let key = ApiKey(Arc::new(settings.api_key.clone()));
            let app = Router::new()
                .route(
                    "/v1/obs/status",
                    get(|State(o): State<Arc<Observer>>| async move {
                        ([(header::CONTENT_TYPE, "application/json")], o.status().to_string()).into_response()
                    }),
                )
                .route(
                    "/v1/series",
                    get(|State(o): State<Arc<Observer>>, Query(q): Query<Since>| async move {
                        let points = o.engine.lock().series_since(q.since.unwrap_or(0));
                        ([(header::CONTENT_TYPE, "application/json")], json!({"points": points}).to_string())
                            .into_response()
                    }),
                )
                .layer(axum::middleware::from_fn_with_state(key, require_key))
                .route("/health/live", get(|| async { axum::Json(json!({"status": "ok"})) }))
                .with_state(observer.clone());
            match tokio::net::TcpListener::bind(("127.0.0.1", settings.observer_port)).await {
                Ok(l) => {
                    let _ = axum::serve(l, app).await;
                }
                Err(e) => tracing::error!(error = %e, "observer api did not start"),
            }
        });
    }
    tracing::info!(port = settings.observer_port, "observer_running");
    observer.read_stdin(); // until the trader closes the pipe
    if let Some(r) = &observer.recorder {
        r.lock().stop();
    }
    tracing::info!(
        lines = observer.lines.load(std::sync::atomic::Ordering::Relaxed),
        records = observer.records.load(std::sync::atomic::Ordering::Relaxed),
        "observer_stopped"
    );
    rt.shutdown_timeout(std::time::Duration::from_secs(2));
    Ok(())
}
