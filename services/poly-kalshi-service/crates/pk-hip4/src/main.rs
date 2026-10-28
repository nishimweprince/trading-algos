//! Isolated Hyperliquid HIP-4 adapter. Kalshi is signal-only; this binary has
//! no Kalshi order code or credentials capable of trading there.

mod client;
mod config;
mod engine;
mod model;
mod sign;

use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::middleware;
use axum::response::IntoResponse;
use axum::routing::{get, post};
use axum::{Json, Router};
use clap::{Parser, Subcommand};
use client::{Client, Gateway};
use config::Config;
use engine::{Batch, Engine, Matched};
use futures_util::{SinkExt, StreamExt};
use parking_lot::Mutex;
use pk_feeds::{FeedHandle, FeedKind, KalshiSigner, spawn_feed};
use pk_svc::api::{ApiKey, require_key};
use pk_venue_core::service::{
    AdapterState, FillRecord, Journal, MarkoutTracker, OrderRecord, now_ms, public_error,
};
use pk_venue_core::{
    Allowlist, MatchDecision, MatchGrade, NormalizedContract, RiskGuard, SideMapping, VenueMode,
};
use serde::Deserialize;
use serde_json::{Value, json};
use sign::{cancel_cloids, order_action, schedule_cancel};
use std::sync::Arc;
use std::time::Duration;
use tokio_tungstenite::tungstenite::Message;

#[derive(Parser)]
#[command(name = "pk-hip4")]
struct Args {
    #[arg(long)]
    profile: Option<String>,
    #[command(subcommand)]
    command: Option<Command>,
}
#[derive(Subcommand)]
enum Command {
    Preflight,
    ProbeOrders {
        #[arg(long)]
        confirm: bool,
    },
    /// Match every upcoming HIP-4 price contract to the Kalshi contract with
    /// the same underlying, strike and decision time; print the allowlist,
    /// or replace the allowlist file with `--write`.
    BuildAllowlist {
        #[arg(long)]
        write: bool,
    },
}
struct App {
    cfg: Config,
    client: Arc<Client>,
    gateway: Gateway,
    engine: Mutex<Engine>,
    state: Mutex<AdapterState>,
    journal: Mutex<Journal>,
    /// Every raw frame, only with `PK_HIP4_RAW=yes`.
    raw: Mutex<Option<Journal>>,
    kalshi: Mutex<Option<FeedHandle>>,
    markouts: Mutex<MarkoutTracker>,
    series_ms: Mutex<i64>,
}
impl App {
    fn raw(&self, kind: &str, value: impl FnOnce() -> Value) {
        if let Some(raw) = self.raw.lock().as_mut() {
            let _ = raw.record(kind, value());
        }
    }

    fn record(&self, k: &str, v: Value) {
        if let Err(e) = self.journal.lock().record(k, v) {
            tracing::error!(error=%e,"journal")
        }
    }
    fn mark_open_cancelled(&self) -> usize {
        let mut state = self.state.lock();
        let mut count = 0;
        for order in state
            .orders
            .iter_mut()
            .filter(|order| order.status == "submitted" || order.status == "shadow_open")
        {
            order.status = "cancelled".into();
            order.updated_ms = now_ms();
            count += 1;
        }
        state.recount();
        state.risk.save();
        count
    }
    async fn cancel_all(&self, reason: &str) -> Value {
        self.state.lock().ready = false;
        if self.cfg.mode == VenueMode::Shadow {
            let n = self.mark_open_cancelled();
            self.record("cancel_all", json!({"reason":reason,"shadow":true,"orders":n}));
            return json!({"shadow":true,"cancelled":n});
        }
        let orders = match self.client.open_orders().await {
            Ok(v) => v,
            Err(e) => return json!({"error":public_error(&e)}),
        };
        let mut cancels = vec![];
        for o in orders.as_array().into_iter().flatten() {
            let asset = o
                .get("coin")
                .and_then(Value::as_str)
                .and_then(|c| c.strip_prefix('#'))
                .and_then(|x| x.parse::<u32>().ok())
                .map(|enc| 100_000_000 + enc);
            let cloid = o.get("cloid").and_then(Value::as_str);
            if let (Some(a), Some(c)) = (asset, cloid) {
                cancels.push((a, c.into()))
            }
        }
        if cancels.is_empty() {
            return json!({"cancelled":0});
        }
        match self.gateway.submit(cancel_cloids(&cancels), self.cfg.action_expiry_ms).await {
            Ok(v) => {
                self.mark_open_cancelled();
                self.record("cancel_all", json!({"reason":reason,"count":cancels.len()}));
                v
            }
            Err(e) => {
                self.state.lock().latch(format!("emergency cancellation failed: {}", public_error(&e)));
                json!({"error":public_error(&e)})
            }
        }
    }
    async fn execute(&self, batch: Batch) {
        match batch {
            Batch::Cancel(r) => {
                self.cancel_all(&r).await;
            }
            Batch::Alo(items) => self.place(items, false).await,
            Batch::Ioc(items) => {
                self.cancel_all("suspend ALO before IOC").await;
                let plain = items.into_iter().map(|(m, o, s, _e)| (m, o, s)).collect();
                self.place(plain, true).await
            }
        }
    }
    async fn place(&self, items: Vec<(Matched, sign::OrderSpec, String)>, ioc: bool) {
        if items.is_empty() {
            return;
        }
        let lane = if ioc { "taker" } else { "maker" };
        let mut accepted = vec![];
        // Counters from the orders and positions themselves (an incremental
        // count drifted to 528 of 4 and blocked every other market).
        let (mut provisional, mut active) = {
            let mut s = self.state.lock();
            s.recount();
            let mut active: std::collections::HashSet<String> = s
                .orders
                .iter()
                .filter(|order| order.status == "submitted" || order.status == "shadow_open")
                .map(|order| order.market.clone())
                .collect();
            active.extend(s.positions.iter().filter(|(_, p)| !p.is_flat()).map(|(k, _)| k.clone()));
            (s.risk.clone(), active)
        };
        // A requote replaces this batch's markets' resting quotes, so their
        // notional and order slots are free for the new batch.
        if !ioc {
            let s = self.state.lock();
            let replaced: Vec<&OrderRecord> = s
                .orders
                .iter()
                .filter(|o| o.lane == "maker" && (o.status == "submitted" || o.status == "shadow_open"))
                .collect();
            provisional.state.open_orders = provisional.state.open_orders.saturating_sub(replaced.len());
            provisional.state.open_usd =
                (provisional.state.open_usd - replaced.iter().map(|o| o.price * o.quantity).sum::<f64>()).max(0.0);
        }
        for item in items {
            let cost = item.1.price.parse::<f64>().unwrap_or(0.0) * item.1.size.parse::<f64>().unwrap_or(0.0);
            let market_id = item.0.outcome.outcome.to_string();
            let new_market = !active.contains(&market_id);
            if provisional.check_order(cost, new_market).is_ok() {
                provisional.state.open_orders += 1;
                provisional.state.open_usd += cost;
                if new_market {
                    provisional.state.active_markets += 1;
                    active.insert(market_id);
                }
                accepted.push(item);
            }
        }
        if accepted.is_empty() {
            return;
        }
        // ALO requotes cancel old cloids in their own action before the new
        // ALO-only batch; IOC actions never share that batch.
        if !ioc {
            let matched = self.engine.lock().matched.clone();
            let old: Vec<(u32, String)> = {
                let s = self.state.lock();
                s.orders
                    .iter()
                    .filter(|o| o.lane == "maker" && (o.status == "submitted" || o.status == "shadow_open"))
                    .filter_map(|o| {
                        let m = matched.iter().find(|m| m.outcome.outcome.to_string() == o.market)?;
                        let side = if o.side == "YES" { 0 } else { 1 };
                        Some((m.outcome.side_asset(side), o.client_id.clone()))
                    })
                    .collect()
            };
            if !old.is_empty() && self.cfg.mode == VenueMode::Live {
                if let Err(e) = self.gateway.submit(cancel_cloids(&old), self.cfg.action_expiry_ms).await {
                    self.state.lock().latch(format!("requote cancellation failed: {}", public_error(&e)));
                    return;
                }
            }
            let mut s = self.state.lock();
            for o in s
                .orders
                .iter_mut()
                .filter(|o| o.lane == "maker" && (o.status == "submitted" || o.status == "shadow_open"))
            {
                o.status = "cancelled".into();
                o.updated_ms = now_ms();
            }
            s.recount();
            s.risk.save();
        }
        if self.cfg.mode == VenueMode::Shadow {
            for (m, o, side) in accepted {
                let id = o.cloid.clone().unwrap_or_else(|| format!("ioc-{}", now_ms()));
                let rec = OrderRecord {
                    id: id.clone(),
                    client_id: id,
                    market: m.outcome.outcome.to_string(),
                    lane: lane.into(),
                    side,
                    price: o.price.parse().unwrap_or(0.0),
                    quantity: o.size.parse().unwrap_or(0.0),
                    status: "shadow_open".into(),
                    created_ms: now_ms(),
                    updated_ms: now_ms(),
                    simulated: true,
                };
                let mut state = self.state.lock();
                state.push_order(rec.clone());
                state.recount();
                state.risk.save();
                drop(state);
                self.record("shadow_order", serde_json::to_value(rec).unwrap_or_default())
            }
            self.simulate_shadow_fills();
            return;
        }
        let specs: Vec<_> = accepted.iter().map(|x| x.1.clone()).collect();
        match self.gateway.submit(order_action(&specs), self.cfg.action_expiry_ms).await {
            Ok(reply) => {
                for (m, o, side) in accepted {
                    let id = o.cloid.clone().unwrap_or_else(|| format!("ioc-{}", now_ms()));
                    let rec = OrderRecord {
                        id: id.clone(),
                        client_id: id,
                        market: m.outcome.outcome.to_string(),
                        lane: lane.into(),
                        side,
                        price: o.price.parse().unwrap_or(0.0),
                        quantity: o.size.parse().unwrap_or(0.0),
                        status: "submitted".into(),
                        created_ms: now_ms(),
                        updated_ms: now_ms(),
                        simulated: false,
                    };
                    let mut state = self.state.lock();
                    state.push_order(rec);
                    state.recount();
                    state.risk.save()
                }
                self.record("order_ack", json!({"lane":lane,"reply":reply}));
            }
            Err(e) => {
                let reason = public_error(&e);
                let mut s = self.state.lock();
                s.risk.state.consecutive_rejections += 1;
                let _ = s.risk.check_latches();
                s.risk.save();
                drop(s);
                self.record("order_rejected", json!({"lane":lane,"reason":reason}))
            }
        }
    }

    fn simulate_shadow_fills(&self) {
        if self.cfg.mode != VenueMode::Shadow {
            return;
        }
        let now = now_ms();
        let (matched, books) = {
            let engine = self.engine.lock();
            (engine.matched.clone(), engine.books.clone())
        };
        let mut fills = Vec::new();
        {
            let mut state = self.state.lock();
            for order in state.orders.iter_mut().filter(|order| {
                order.simulated && order.status == "shadow_open" && now - order.created_ms >= 100
            }) {
                let Some(market) =
                    matched.iter().find(|market| market.outcome.outcome.to_string() == order.market)
                else {
                    continue;
                };
                let side = u32::from(order.side != "YES");
                let Some(book) = books.get(&market.outcome.side_asset(side)) else { continue };
                let fills_now =
                    order.lane == "taker" || book.ask.is_some_and(|ask| ask <= order.price + 1e-9);
                if !fills_now {
                    continue;
                }
                order.status = "shadow_filled".into();
                order.updated_ms = now;
                fills.push(FillRecord {
                    id: format!("shadow-fill-{}", order.id),
                    order_id: order.id.clone(),
                    market: order.market.clone(),
                    side: order.side.clone(),
                    price: order.price,
                    quantity: order.quantity,
                    fee: 0.0,
                    time_ms: now,
                    simulated: true,
                });
            }
            for fill in &fills {
                if let Some(m) = matched.iter().find(|m| m.outcome.outcome.to_string() == fill.market) {
                    state.position_meta.entry(fill.market.clone()).or_insert_with(|| position_meta(m, now));
                }
                state.book_fill(fill);
            }
            state.risk.save();
        }
        for fill in fills {
            self.markouts.lock().watch(&fill.market, &fill.side, fill.price, fill.time_ms, None);
            self.record("shadow_fill", serde_json::to_value(fill).unwrap_or_default());
        }
    }
}

fn main() -> anyhow::Result<()> {
    pk_svc::tls::install_default_provider();
    let a = Args::parse();
    let cfg = Config::load(a.profile.as_deref())?;
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .json()
        .init();
    tokio::runtime::Builder::new_multi_thread().worker_threads(2).enable_all().build()?.block_on(async move {
        let client = Arc::new(Client::new(
            &cfg.api_url,
            &cfg.ws_url,
            cfg.account.as_deref(),
            cfg.agent_key.as_deref(),
        )?);
        match a.command {
            Some(Command::Preflight) => preflight(&cfg, &client).await,
            Some(Command::ProbeOrders { confirm }) => probe(&cfg, client, confirm).await,
            Some(Command::BuildAllowlist { write }) => build_allowlist(&cfg, &client, write).await,
            None => run(cfg, client).await,
        }
    })
}
async fn preflight(cfg: &Config, c: &Client) -> anyhow::Result<()> {
    let account = cfg.account.as_deref().ok_or_else(|| anyhow::anyhow!("HL_ACCOUNT_ADDRESS is required"))?;
    let agent = c.signer_address().ok_or_else(|| anyhow::anyhow!("HL_AGENT_PRIVATE_KEY is required"))?;
    anyhow::ensure!(
        !account.eq_ignore_ascii_case(&agent),
        "dedicated API wallet required; never configure the master key as HL_AGENT_PRIVATE_KEY"
    );
    let meta = c.outcome_meta().await?;
    let templates = c.outcome_templates().await?;
    let orders = c.open_orders().await?;
    let state = c.user_state().await?;
    println!(
        "HIP-4 preflight OK: master={} agent={} outcomes={} templates={} open-orders={} account-state={}",
        account,
        agent,
        model::outcomes(&meta).len(),
        templates.as_array().map(Vec::len).unwrap_or(0),
        orders.as_array().map(Vec::len).unwrap_or(0),
        !state.is_null()
    );
    println!(
        "mode={} maker={} taker={} caps: order=${:.2} open=${:.2} daily-loss=${:.2}",
        cfg.mode.as_str(),
        cfg.maker,
        cfg.taker,
        cfg.risk.order_usd,
        cfg.risk.max_open_usd,
        cfg.risk.daily_loss_usd
    );
    Ok(())
}
async fn probe(cfg: &Config, c: Arc<Client>, confirm: bool) -> anyhow::Result<()> {
    anyhow::ensure!(confirm, "probe-orders places a real ALO; pass --confirm");
    preflight(cfg, &c).await?;
    let outcome = model::outcomes(&c.outcome_meta().await?)
        .into_iter()
        .find(|o| o.name == "template:binaryPrice")
        .ok_or_else(|| anyhow::anyhow!("no HIP-4 binary outcome"))?;
    let gateway = Gateway::spawn(c);
    let cloid = format!("0x{:032x}", now_ms());
    let order = sign::OrderSpec {
        asset: outcome.side_asset(0),
        buy: true,
        price: "0.01".into(),
        size: "1".into(),
        reduce_only: false,
        tif: "Alo",
        cloid: Some(cloid.clone()),
    };
    let t = std::time::Instant::now();
    gateway.submit(order_action(&[order]), cfg.action_expiry_ms).await?;
    gateway.submit(sign::cancel_by_cloid(outcome.side_asset(0), &cloid), cfg.action_expiry_ms).await?;
    println!("HIP-4 ALO probe placed and cancelled in {:.1} ms", t.elapsed().as_secs_f64() * 1e3);
    Ok(())
}

async fn run(cfg: Config, client: Arc<Client>) -> anyhow::Result<()> {
    anyhow::ensure!(cfg.enabled, "PK_HIP4_ENABLED must be yes");
    std::fs::create_dir_all(&cfg.state_dir)?;
    let risk = RiskGuard::open(cfg.risk, cfg.state_dir.join("risk.json"))?;
    let gateway = Gateway::spawn(client.clone());
    let app = Arc::new(App {
        state: Mutex::new({
            let mut s = AdapterState::new("hip4", cfg.mode, cfg.fingerprint.clone(), risk);
            s.pnl = pk_venue_core::service::PnlBook::open(cfg.state_dir.join("pnl.json"), cfg.sim_capital);
            if cfg.mode == VenueMode::Shadow {
                s.restore_open();
            }
            s
        }),
        journal: Mutex::new(Journal::open(cfg.state_dir.join("journal"))?),
        raw: Mutex::new(if cfg.raw { Some(Journal::open(cfg.state_dir.join("raw"))?) } else { None }),
        engine: Mutex::new(Engine::new()),
        kalshi: Mutex::new(None),
        cfg,
        client,
        gateway,
        markouts: Mutex::new(MarkoutTracker::new()),
        series_ms: Mutex::new(0),
    });
    if app.cfg.mode == VenueMode::Live {
        let orders = app.client.open_orders().await?;
        if !orders.as_array().is_none_or(Vec::is_empty) {
            app.cancel_all("startup reconciliation").await;
        }
        let _ = app.client.user_state().await?;
    }
    discover(&app).await?;
    app.state.lock().reconciling = false;
    let pem = std::fs::read_to_string(&app.cfg.kalshi_private_key_path)?;
    let signer = Arc::new(KalshiSigner::from_pem(&app.cfg.kalshi_api_key_id, &pem)?);
    let weak = Arc::downgrade(&app);
    let emit = Arc::new(move |_: char, text: String| {
        if let Some(app) = weak.upgrade() {
            app.raw("kalshi", || json!({"raw":text}));
            if text.contains("\"type\":\"resync\"") {
                let mut s = app.state.lock();
                s.feeds.entry("kalshi".into()).or_default().sequence_gaps += 1;
                // The feed resubscribes for fresh snapshots on a gap. Live
                // positions need a human to reconcile; shadow just waits.
                if app.cfg.mode == VenueMode::Live {
                    s.latch("Kalshi sequence gap requires reconciliation");
                } else {
                    s.ready = false;
                }
            } else if text.contains("\"type\":\"reconnect\"") {
                app.state.lock().ready = false;
            } else if let Some((ticker, fair)) = app.engine.lock().kalshi(&text) {
                let mut s = app.state.lock();
                s.kalshi_fairs.insert(ticker, fair);
                let f = s.feeds.entry("kalshi".into()).or_default();
                f.connected = true;
                f.last_message_ms = now_ms();
            }
        }
    });
    let feed = spawn_feed(app.cfg.kalshi_ws_url.clone(), FeedKind::Kalshi { signer }, emit);
    feed.set_wanted(app.engine.lock().tickers());
    *app.kalshi.lock() = Some(feed);
    tokio::spawn(public_ws(app.clone()));
    tokio::spawn(strategy(app.clone()));
    tokio::spawn(kill_loop(app.clone()));
    tokio::spawn(deadman(app.clone()));
    tokio::spawn(heartbeat_watchdog(app.clone()));
    tokio::spawn(clock_loop(app.clone()));
    tokio::spawn(discovery_loop(app.clone()));
    tokio::spawn(settlement_loop(app.clone()));
    tokio::spawn(shadow_settlement_loop(app.clone()));
    app.state.lock().ready = true;
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", app.cfg.port)).await?;
    tracing::info!(port = app.cfg.port, mode = app.cfg.mode.as_str(), "pk-hip4 ready");
    axum::serve(listener, routes(app.clone())).with_graceful_shutdown(shutdown()).await?;
    app.cancel_all("shutdown").await;
    Ok(())
}

async fn discover(app: &Arc<App>) -> anyhow::Result<()> {
    let meta = app.client.outcome_meta().await?;
    app.raw("outcome_meta", || json!({"raw":meta.clone()}));
    let outcomes = model::outcomes(&meta);
    let allow = Allowlist::load(&app.cfg.allowlist)?;
    let mut matched = vec![];
    let mut decisions = std::collections::HashMap::new();
    for outcome in outcomes {
        let id = format!("{}:{}", outcome.venue, outcome.outcome);
        let entries: Vec<_> =
            allow.entries.iter().filter(|e| e.venue == "hip4" && e.venue_market_id == id).collect();
        if entries.is_empty() {
            if let Some(venue) = outcome.normalized("") {
                decisions.insert(
                    id.clone(),
                    MatchDecision {
                        grade: MatchGrade::E2,
                        kalshi_ticker: String::new(),
                        venue_market_id: id,
                        kalshi_fingerprint: String::new(),
                        venue_fingerprint: venue.fingerprint(),
                        side_mapping: SideMapping::default(),
                        basis_reason: None,
                        max_basis: 0.0,
                        maker: false,
                        taker: false,
                        rejected: vec!["no exact Kalshi mapping and no versioned allowlist entry".into()],
                    },
                );
            }
            continue;
        }
        for entry in entries {
            let Some(venue) = outcome.normalized(&entry.kalshi_ticker) else { continue };
            let kalshi =
                fetch_kalshi(app, &entry.kalshi_ticker).await.unwrap_or_else(|_| NormalizedContract {
                    venue: "kalshi".into(),
                    venue_market_id: entry.kalshi_ticker.clone(),
                    kalshi_ticker: entry.kalshi_ticker.clone(),
                    ..Default::default()
                });
            let decision = allow.decide(&venue, &kalshi, chrono::Utc::now());
            decisions.insert(id.clone(), decision.clone());
            if decision.grade != MatchGrade::E2 {
                matched.push(Matched {
                    outcome: outcome.clone(),
                    ticker: entry.kalshi_ticker.clone(),
                    decision,
                });
            }
        }
    }
    app.record("discovery",json!({"outcomes_seen":model::outcomes(&meta).len(),"matched":matched.iter().map(|m|json!({"outcome":m.outcome.outcome,"ticker":m.ticker,"grade":m.decision.grade})).collect::<Vec<_>>() }));
    let changed = {
        let s = app.state.lock();
        // A contract whose allowlist entry expired at its decision (Kalshi's
        // rules record its result then, so its fingerprint moves too) or that
        // left the venue's list has simply finished: not a rule change. Only
        // a live contract whose rules changed under it counts (20:00Z latch).
        s.matches.iter().filter(|(_, d)| d.grade != MatchGrade::E2).any(|(id, old)| {
            decisions.get(id).is_some_and(|new| {
                let expired = new.rejected.iter().any(|r| r.contains("expired"));
                !expired
                    && (new.venue_fingerprint != old.venue_fingerprint
                        || new.kalshi_fingerprint != old.kalshi_fingerprint
                        || new.rejected.iter().any(|r| r.contains("fingerprint changed")))
            })
        })
    };
    if changed {
        app.state.lock().latch("HIP-4 rule fingerprint or semantic match changed");
        app.cancel_all("semantic match changed").await;
    }
    {
        let mut s = app.state.lock();
        s.matches = decisions;
        s.degraded.retain(|r| r != "no allowlisted executable HIP-4 contracts");
        if matched.is_empty() {
            s.degraded.push("no allowlisted executable HIP-4 contracts".into());
        }
    }
    app.engine.lock().matched = matched;
    if let Some(f) = app.kalshi.lock().as_ref() {
        f.set_wanted(app.engine.lock().tickers())
    }
    Ok(())
}
async fn discovery_loop(app: Arc<App>) {
    let mut timer = tokio::time::interval(Duration::from_secs(60));
    timer.tick().await;
    loop {
        timer.tick().await;
        app.simulate_shadow_fills();
        if let Err(e) = discover(&app).await {
            let reason = format!("discovery failed: {}", public_error(&e));
            app.state.lock().degraded = vec![reason.clone()];
            app.record("discovery_error", json!({"reason":reason}));
        }
    }
}
async fn fetch_kalshi(app: &Arc<App>, ticker: &str) -> anyhow::Result<NormalizedContract> {
    kalshi_contract(&app.client.http, &app.cfg, ticker).await
}

async fn kalshi_contract(http: &reqwest::Client, cfg: &Config, ticker: &str) -> anyhow::Result<NormalizedContract> {
    let path = format!("/trade-api/v2/markets/{ticker}");
    let pem = std::fs::read_to_string(&cfg.kalshi_private_key_path)?;
    let signer = KalshiSigner::from_pem(&cfg.kalshi_api_key_id, &pem)?;
    let mut req = http.get(format!("{}/markets/{ticker}", cfg.kalshi_rest_url.trim_end_matches('/')));
    for (k, v) in signer.headers("GET", &path, None) {
        req = req.header(k, v)
    }
    let value = req.send().await?.error_for_status()?.json::<Value>().await?;
    let m = value.get("market").or_else(|| value.pointer("/data/market")).unwrap_or(&value);
    let text = |k: &str| m.get(k).and_then(Value::as_str).unwrap_or_default().to_owned();
    let time = |k: &str| {
        m.get(k)
            .and_then(Value::as_str)
            .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
            .map(|d| d.timestamp_millis())
    };
    Ok(NormalizedContract {
        venue: "kalshi".into(),
        venue_market_id: ticker.into(),
        kalshi_ticker: ticker.into(),
        event_class: if ticker.contains("FED") { "policy_rate" } else { "binary_price" }.into(),
        underlying: ticker.split('-').next().unwrap_or(ticker).into(),
        outcome: "above".into(),
        operator: text("strike_type"),
        threshold: m.get("floor_strike").or_else(|| m.get("functional_strike")).map(Value::to_string),
        close_ms: time("close_time"),
        decision_ms: time("expiration_time").or_else(|| time("close_time")),
        resolution_source: text("settlement_source"),
        averaging_window_s: m.get("settlement_timer_seconds").and_then(Value::as_u64),
        tie_rule: text("expiration_value"),
        correction_rule: text("rules_primary"),
        ..Default::default()
    })
}

/// Kalshi's daily/hourly "above" series per HIP-4 underlying (`perp` with any
/// `xyz:` prefix dropped).
fn kalshi_series(underlying: &str) -> &'static [&'static str] {
    match underlying {
        "BTC" => &["KXBTCD"],
        "ETH" => &["KXETHD"],
        "SOL" => &["KXSOLD"],
        "XRP" => &["KXXRPD"],
        "BNB" => &["KXBNBD"],
        "DOGE" => &["KXDOGED"],
        "HYPE" => &["KXHYPED"],
        "ZEC" => &["KXZECD"],
        "SP500" => &["KXINXU"],
        "XYZ100" => &["KXNASDAQ100U"],
        "GOLD" => &["KXGOLDD"],
        "SILVER" => &["KXSILVERD"],
        "CL" => &["KXWTID", "KXWTI"],
        _ => &[],
    }
}

/// Kalshi "above" markets in `series` that decide at `decision_ms`.
async fn kalshi_above(http: &reqwest::Client, cfg: &Config, series: &str, decision_ms: i64) -> anyhow::Result<Vec<Value>> {
    let s = decision_ms / 1000;
    let pem = std::fs::read_to_string(&cfg.kalshi_private_key_path)?;
    let signer = KalshiSigner::from_pem(&cfg.kalshi_api_key_id, &pem)?;
    let mut req = http
        .get(format!("{}/markets", cfg.kalshi_rest_url.trim_end_matches('/')))
        .query(&[("series_ticker", series.to_owned()), ("min_close_ts", (s - 1).to_string()), ("max_close_ts", (s + 1).to_string()), ("limit", "1000".into())]);
    for (k, v) in signer.headers("GET", "/trade-api/v2/markets", None) {
        req = req.header(k, v)
    }
    let value = req.send().await?.error_for_status()?.json::<Value>().await?;
    Ok(value
        .get("markets")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|m| matches!(m.get("strike_type").and_then(Value::as_str), Some("greater" | "greater_or_equal")))
        .cloned()
        .collect())
}

/// Kalshi writes "above 80,000" as a floor strike of 79,999.99 (`greater`)
/// or 80,000 (`greater_or_equal`): both are HIP-4's `> 80000` within a cent.
pub fn same_strike(kalshi_floor: f64, threshold: f64) -> bool {
    (kalshi_floor - threshold).abs() <= 0.0101
}

async fn build_allowlist(cfg: &Config, client: &Client, write: bool) -> anyhow::Result<()> {
    let meta = client.outcome_meta().await?;
    let now = chrono::Utc::now();
    let mut entries = Vec::new();
    let mut seen = 0;
    let mut unmatched: std::collections::BTreeMap<String, u32> = Default::default();
    let mut lists: std::collections::HashMap<(String, i64), Vec<Value>> = Default::default();
    for outcome in model::outcomes(&meta) {
        if outcome.name != "template:binaryPrice" {
            continue;
        }
        let Some(probe) = outcome.normalized("") else { continue };
        let Some(decision_ms) = probe.decision_ms else { continue };
        if decision_ms <= now.timestamp_millis() + 60_000 {
            continue;
        }
        let Some(threshold) = probe.threshold.as_deref().and_then(|t| t.parse::<f64>().ok()) else { continue };
        let underlying = probe.underlying.trim_start_matches("XYZ:").to_owned();
        seen += 1;
        let mut found = None;
        for series in kalshi_series(&underlying) {
            let key = (series.to_string(), decision_ms);
            if !lists.contains_key(&key) {
                let got = kalshi_above(&client.http, cfg, series, decision_ms).await.unwrap_or_default();
                lists.insert(key.clone(), got);
            }
            found = lists[&key].iter().find(|m| m.get("floor_strike").and_then(Value::as_f64).is_some_and(|f| same_strike(f, threshold))).cloned();
            if found.is_some() {
                break;
            }
        }
        let Some(market) = found else {
            *unmatched.entry(underlying).or_default() += 1;
            continue;
        };
        let ticker = market.get("ticker").and_then(Value::as_str).unwrap_or_default().to_owned();
        let Some(venue) = outcome.normalized(&ticker) else { continue };
        let kalshi = kalshi_contract(&client.http, cfg, &ticker).await?;
        let description = outcome.fields().get("priceDescription").cloned().unwrap_or_default();
        entries.push(pk_venue_core::AllowlistEntry {
            venue: "hip4".into(),
            venue_market_id: venue.venue_market_id.clone(),
            kalshi_ticker: ticker,
            venue_fingerprint: venue.fingerprint(),
            kalshi_fingerprint: kalshi.fingerprint(),
            side_mapping: SideMapping { kalshi_yes_is_venue_side: 0, venue_side_names: ["Yes".into(), "No".into()] },
            max_basis: 0.02,
            expires_at: chrono::DateTime::from_timestamp_millis(decision_ms),
            maker: true,
            taker: false,
            reason: format!(
                "same underlying, strike and decision time; HIP-4 settles on {description} ({}s), Kalshi on {}",
                venue.averaging_window_s.unwrap_or(0),
                if kalshi.resolution_source.is_empty() { "its index" } else { kalshi.resolution_source.as_str() }
            ),
        });
    }
    let list = Allowlist { version: 1, entries };
    println!(
        "HIP-4 price contracts deciding later: {seen}; matched to Kalshi: {}; unmatched by underlying: {unmatched:?}",
        list.entries.len()
    );
    for e in &list.entries {
        println!("  {} -> {} (expires {})", e.venue_market_id, e.kalshi_ticker, e.expires_at.map(|t| t.to_rfc3339()).unwrap_or_default());
    }
    if write {
        let tmp = cfg.allowlist.with_extension("json.tmp");
        std::fs::write(&tmp, serde_json::to_vec_pretty(&list)?)?;
        std::fs::rename(&tmp, &cfg.allowlist)?;
        println!("wrote {}", cfg.allowlist.display());
    }
    Ok(())
}

async fn public_ws(app: Arc<App>) {
    loop {
        match tokio_tungstenite::connect_async(&app.cfg.ws_url).await {
            Ok((mut ws, _)) => {
                // Coins join as allowlist entries match (discovery reloads it
                // every 60 s), so the book subscriptions follow the engine.
                let mut subscribed = std::collections::HashSet::new();
                let follow = |subscribed: &mut std::collections::HashSet<String>| -> Vec<String> {
                    app.engine.lock().coins().into_iter().map(|(c, _)| c).filter(|c| subscribed.insert(c.clone())).collect()
                };
                for coin in follow(&mut subscribed) {
                    let _ = ws
                        .send(Message::Text(
                            json!({"method":"subscribe","subscription":{"type":"l2Book","coin":coin}})
                                .to_string()
                                .into(),
                        ))
                        .await;
                }
                if !app.client.account.is_empty() {
                    for kind in ["orderUpdates", "userFills"] {
                        let _=ws.send(Message::Text(json!({"method":"subscribe","subscription":{"type":kind,"user":app.client.account}}).to_string().into())).await;
                    }
                }
                // Hyperliquid closes a socket idle for 60 s; ping every 30 s.
                let mut tick = tokio::time::interval(Duration::from_secs(30));
                tick.tick().await;
                loop {
                    let frame = tokio::select! {
                        frame = ws.next() => frame,
                        _ = tick.tick() => {
                            if ws.send(Message::Text(json!({"method":"ping"}).to_string().into())).await.is_err() {
                                break;
                            }
                            for coin in follow(&mut subscribed) {
                                let _ = ws
                                    .send(Message::Text(
                                        json!({"method":"subscribe","subscription":{"type":"l2Book","coin":coin}})
                                            .to_string()
                                            .into(),
                                    ))
                                    .await;
                            }
                            continue;
                        }
                    };
                    let Some(frame) = frame else { break };
                    match frame {
                        Ok(frame) if frame.is_ping() => {
                            let _ = ws.send(Message::Pong(frame.into_data())).await;
                        }
                        Ok(frame) if frame.is_text() => {
                            let text = frame.to_text().unwrap_or("{}");
                            if let Ok(v) = serde_json::from_str::<Value>(text) {
                                app.raw("hip4", || json!({"raw":v.clone()}));
                                match v.get("channel").and_then(Value::as_str) {
                                    Some("l2Book") => {
                                        let coin =
                                            v.pointer("/data/coin").and_then(Value::as_str).unwrap_or("");
                                        if let (Some(enc), Some(book)) = (
                                            coin.strip_prefix('#').and_then(|x| x.parse::<u32>().ok()),
                                            model::book(&v),
                                        ) {
                                            let asset = 100_000_000 + enc;
                                            app.engine.lock().books.insert(asset, book);
                                            let mut s = app.state.lock();
                                            s.books.insert(asset.to_string(), book);
                                            let f = s.feeds.entry("hip4".into()).or_default();
                                            f.connected = true;
                                            f.last_message_ms = now_ms();
                                        }
                                    }
                                    Some("userFills") => handle_fills(&app, &v),
                                    Some("orderUpdates") => {
                                        if !v.get("data").is_some_and(Value::is_array) {
                                            app.state.lock().latch("malformed private order update");
                                        }
                                    }
                                    _ => {}
                                }
                            }
                        }
                        Ok(frame) if frame.is_close() => break,
                        Ok(_) => {}
                        Err(_) => break,
                    }
                }
            }
            Err(e) => {
                let mut s = app.state.lock();
                let f = s.feeds.entry("hip4".into()).or_default();
                f.errors += 1;
                f.last_error = Some(public_error(&e));
            }
        }
        tokio::time::sleep(Duration::from_secs(1)).await
    }
}

fn handle_fills(app: &Arc<App>, value: &Value) {
    for fill in value.get("data").and_then(Value::as_array).into_iter().flatten() {
        let oid = fill.get("oid").map(Value::to_string).unwrap_or_default();
        let known =
            app.state.lock().orders.iter().any(|o| {
                o.id == oid || o.client_id == fill.get("cloid").and_then(Value::as_str).unwrap_or("")
            });
        if !known && app.cfg.mode == VenueMode::Live {
            app.state.lock().latch(format!("unknown fill {oid}"));
        }
        let mut record = FillRecord {
            id: fill.get("tid").map(Value::to_string).unwrap_or_else(|| format!("fill-{}", now_ms())),
            order_id: oid,
            market: fill.get("coin").and_then(Value::as_str).unwrap_or("").into(),
            side: fill.get("side").and_then(Value::as_str).unwrap_or("").into(),
            price: fill.get("px").and_then(Value::as_str).and_then(|x| x.parse().ok()).unwrap_or(0.0),
            quantity: fill.get("sz").and_then(Value::as_str).and_then(|x| x.parse().ok()).unwrap_or(0.0),
            fee: fill.get("fee").and_then(Value::as_str).and_then(|x| x.parse().ok()).unwrap_or(0.0),
            time_ms: fill.get("time").and_then(Value::as_i64).unwrap_or_else(now_ms),
            simulated: false,
        };
        let cloid = fill.get("cloid").and_then(Value::as_str).unwrap_or("");
        let mut state = app.state.lock();
        if let Some(order) = state
            .orders
            .iter_mut()
            .find(|order| order.id == record.order_id || (!cloid.is_empty() && order.client_id == cloid))
        {
            record.market = order.market.clone();
            record.side = order.side.clone();
            order.status = "filled".into();
            order.updated_ms = record.time_ms;
        }
        let meta = app.engine.lock().matched.iter().find(|m| m.outcome.outcome.to_string() == record.market).cloned();
        if let Some(m) = meta {
            state.position_meta.entry(record.market.clone()).or_insert_with(|| position_meta(&m, record.time_ms));
        }
        state.book_fill(&record);
        state.risk.save();
        drop(state);
        app.markouts.lock().watch(&record.market, &record.side, record.price, record.time_ms, None);
        app.record("fill", serde_json::to_value(record).unwrap_or_default());
    }
}
async fn strategy(app: Arc<App>) {
    let mut timer = tokio::time::interval(Duration::from_millis(app.cfg.ioc_batch_ms.clamp(1, 10)));
    let mut alo_pending = Vec::new();
    let mut last_alo = std::time::Instant::now();
    loop {
        timer.tick().await;
        let batches = { app.engine.lock().evaluate(&app.cfg) };
        for batch in batches {
            match batch {
                Batch::Alo(items) => alo_pending.extend(items),
                other => app.execute(other).await,
            }
        }
        if !alo_pending.is_empty() && last_alo.elapsed() >= Duration::from_millis(app.cfg.alo_batch_ms) {
            let items = std::mem::take(&mut alo_pending);
            app.execute(Batch::Alo(items)).await;
            last_alo = std::time::Instant::now();
        }
        let now = now_ms();
        {
            let engine = app.engine.lock();
            let state = app.state.lock();
            let mut mids = std::collections::HashMap::new();
            for m in &engine.matched {
                let id = m.outcome.outcome.to_string();
                for (side, label) in [(0, "YES"), (1, "NO")] {
                    let asset = m.outcome.side_asset(side);
                    if let Some(mid) = engine.books.get(&asset).copied().and_then(pk_venue_core::Book::mid) {
                        mids.insert((id.clone(), label.into()), mid);
                    }
                }
            }
            drop(engine);
            drop(state);
            for markout in app.markouts.lock().poll(now, &mids) {
                app.record("markout", markout.clone());
                app.state.lock().push_markout(markout);
            }
            {
                let yes_mids: std::collections::HashMap<String, f64> = mids
                    .iter()
                    .filter(|((_, side), _)| side.eq_ignore_ascii_case("YES"))
                    .map(|((market, _), mid)| (market.clone(), *mid))
                    .collect();
                app.state.lock().mark_to_market(&yes_mids);
            }
            if now - *app.series_ms.lock() >= 1000 {
                *app.series_ms.lock() = now;
                let engine = app.engine.lock();
                let mut state = app.state.lock();
                for m in &engine.matched {
                    let id = m.outcome.outcome.to_string();
                    let fair = state.kalshi_fairs.get(&m.ticker).copied();
                    let mid = engine
                        .books
                        .get(&m.outcome.side_asset(0))
                        .copied()
                        .and_then(pk_venue_core::Book::mid);
                    state.series.push(&id, json!([now, fair, mid]));
                }
            }
        }
        let mut s = app.state.lock();
        s.last_heartbeat_ms = now;
        s.ready = s.latched.is_empty() && s.degraded.is_empty() && !s.reconciling;
    }
}
async fn deadman(app: Arc<App>) {
    if app.cfg.mode != VenueMode::Live {
        return;
    }
    let mut timer = tokio::time::interval(Duration::from_secs(30));
    loop {
        timer.tick().await;
        let when = (now_ms() + 60_000) as u64;
        match app.gateway.submit(schedule_cancel(Some(when)), app.cfg.action_expiry_ms).await {
            Ok(_) => app.state.lock().deadman_armed = true,
            Err(e) => {
                let reason = format!("scheduleCancel unavailable: {}", public_error(&e));
                let mut s = app.state.lock();
                s.deadman_armed = false;
                s.degraded = vec![reason];
            }
        }
    }
}
/// When scheduleCancel is unavailable, a stalled strategy loop must still
/// pull quotes: explicit cancellation on a stale heartbeat.
async fn heartbeat_watchdog(app: Arc<App>) {
    if app.cfg.mode != VenueMode::Live {
        return;
    }
    let mut timer = tokio::time::interval(Duration::from_secs(5));
    loop {
        timer.tick().await;
        let (armed, age_ms) = {
            let state = app.state.lock();
            (state.deadman_armed, now_ms() - state.last_heartbeat_ms)
        };
        if !armed && age_ms > 10_000 {
            app.state.lock().latch(format!("strategy heartbeat stale {age_ms} ms without dead-man"));
            app.cancel_all("stale heartbeat without dead-man").await;
        }
    }
}
async fn kill_loop(app: Arc<App>) {
    let mut t = tokio::time::interval(Duration::from_millis(250));
    loop {
        t.tick().await;
        if app.cfg.kill_file.exists() {
            app.state.lock().latch("kill file present");
            app.cancel_all("kill file").await;
        }
    }
}
async fn clock_loop(app: Arc<App>) {
    let mut t = tokio::time::interval(Duration::from_secs(5));
    loop {
        t.tick().await;
        match pk_venue_core::service::chrony_offset_ms() {
            Ok(offset) => {
                app.state.lock().clock_offset_ms = Some(offset);
                if offset > 100 {
                    app.state.lock().latch(format!("clock offset {offset} ms exceeds 100 ms"));
                    app.cancel_all("clock offset").await;
                }
            }
            Err(e) if cfg!(target_os = "linux") => {
                app.state.lock().latch(format!("clock check failed: {}", public_error(&e)));
                app.cancel_all("clock check failed").await;
            }
            Err(_) => {}
        }
    }
}
fn position_meta(m: &Matched, now: i64) -> pk_venue_core::service::PositionMeta {
    pk_venue_core::service::PositionMeta {
        close_ms: m.outcome.normalized(&m.ticker).and_then(|n| n.decision_ms),
        kalshi_ticker: Some(m.ticker.clone()),
        label: format!("{} {}", m.decision.venue_market_id, m.ticker),
        opened_ms: now,
    }
}

/// Settle simulated positions at the decision: the allowlist only admits a
/// HIP-4 contract whose Kalshi twin has the same underlying, strike and
/// decision time, so Kalshi's `result` decides it (the settlement indexes
/// differ, which is the entry's stated basis).
async fn shadow_settlement_loop(app: Arc<App>) {
    if app.cfg.mode != VenueMode::Shadow {
        return;
    }
    let mut interval = tokio::time::interval(Duration::from_secs(30));
    loop {
        interval.tick().await;
        let due = app.state.lock().due_settlement(now_ms(), 60_000);
        for (market, meta) in due {
            let Some(ticker) = meta.kalshi_ticker.as_deref() else { continue };
            let Some(yes_won) = kalshi_result(&app, ticker).await else { continue };
            let settled = app.state.lock().settle_position(&market, yes_won, "kalshi (same strike and time)");
            if let Some(record) = settled {
                app.record("shadow_settlement", record);
            }
        }
    }
}

async fn kalshi_result(app: &Arc<App>, ticker: &str) -> Option<bool> {
    let path = format!("/trade-api/v2/markets/{ticker}");
    let pem = std::fs::read_to_string(&app.cfg.kalshi_private_key_path).ok()?;
    let signer = KalshiSigner::from_pem(&app.cfg.kalshi_api_key_id, &pem).ok()?;
    let mut req = app.client.http.get(format!("{}/markets/{ticker}", app.cfg.kalshi_rest_url.trim_end_matches('/')));
    for (k, v) in signer.headers("GET", &path, None) {
        req = req.header(k, v)
    }
    let value = req.send().await.ok()?.error_for_status().ok()?.json::<Value>().await.ok()?;
    match value.pointer("/market/result").and_then(Value::as_str) {
        Some("yes") => Some(true),
        Some("no") => Some(false),
        _ => None,
    }
}

async fn settlement_loop(app: Arc<App>) {
    if app.client.account.is_empty() {
        return;
    }
    let mut timer = tokio::time::interval(Duration::from_secs(30));
    loop {
        timer.tick().await;
        match app.client.user_state().await {
            Ok(state) => app.record("balance_reconcile", json!({"state":state})),
            Err(e) => {
                let reason = format!("balance reconciliation failed: {}", public_error(&e));
                app.state.lock().latch(reason.clone());
                app.record("reconcile_error", json!({"reason":reason}));
                app.cancel_all("balance reconciliation failed").await;
            }
        }
    }
}

fn routes(app: Arc<App>) -> Router {
    let protected = Router::new()
        .route("/v1/hip4/status", get(status))
        .route("/v1/hip4/markets", get(markets))
        .route("/v1/hip4/series", get(series))
        .route("/v1/hip4/orders", get(orders))
        .route("/v1/hip4/fills", get(fills))
        .route("/v1/hip4/kill", post(kill))
        .route("/v1/hip4/kill/ack", post(ack))
        .route("/v1/hip4/cancel-all", post(cancel))
        .route("/v1/hip4/flatten", post(flatten))
        .layer(middleware::from_fn_with_state(ApiKey(Arc::new(app.cfg.api_key.clone())), require_key));
    Router::new()
        .route("/health/live", get(|| async { Json(json!({"live":true})) }))
        .route("/health/ready", get(ready))
        .merge(protected)
        .with_state(app)
}
async fn status(State(a): State<Arc<App>>) -> Json<Value> {
    Json(a.state.lock().status())
}
async fn markets(State(a): State<Arc<App>>) -> Json<Value> {
    Json(json!(
        a.engine
            .lock()
            .matched
            .iter()
            .map(|m| json!({"outcome":m.outcome,"ticker":m.ticker,"match":m.decision}))
            .collect::<Vec<_>>()
    ))
}
#[derive(Deserialize)]
struct Since {
    #[serde(default)]
    since: i64,
}
async fn series(State(a): State<Arc<App>>, Query(q): Query<Since>) -> Json<Value> {
    Json(a.state.lock().series.since(q.since))
}
async fn orders(State(a): State<Arc<App>>) -> Json<Value> {
    Json(json!(a.state.lock().orders))
}
async fn fills(State(a): State<Arc<App>>) -> Json<Value> {
    Json(json!(a.state.lock().fills))
}
async fn ready(State(a): State<Arc<App>>) -> impl IntoResponse {
    let r = a.state.lock().status()["ready"].as_bool().unwrap_or(false);
    (if r { StatusCode::OK } else { StatusCode::SERVICE_UNAVAILABLE }, Json(json!({"ready":r})))
}
async fn kill(State(a): State<Arc<App>>) -> Json<Value> {
    let _ = std::fs::write(&a.cfg.kill_file, b"http kill\n");
    a.state.lock().latch("HTTP kill");
    Json(a.cancel_all("HTTP kill").await)
}
async fn ack(State(a): State<Arc<App>>) -> Json<Value> {
    let _ = std::fs::remove_file(&a.cfg.kill_file);
    a.state.lock().acknowledge();
    Json(json!({"acknowledged":true}))
}
async fn cancel(State(a): State<Arc<App>>) -> Json<Value> {
    Json(a.cancel_all("HTTP cancel-all").await)
}
async fn flatten(State(a): State<Arc<App>>) -> Json<Value> {
    let c = a.cancel_all("HTTP flatten").await;
    let inv = a.state.lock().inventory.clone();
    if inv.values().any(|x| x.abs() > 1e-9) {
        a.state.lock().latch("flatten requires liquidity within slippage cap")
    }
    Json(json!({"cancelled":c,"remaining_inventory":inv}))
}
async fn shutdown() {
    #[cfg(unix)]
    {
        let mut term =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("SIGTERM");
        tokio::select! {_=tokio::signal::ctrl_c()=>{},_=term.recv()=>{}}
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}

#[cfg(test)]
mod allowlist_tests {
    use super::*;

    #[test]
    fn kalshi_above_strikes_match_hip4_thresholds() {
        assert!(same_strike(79_999.99, 80_000.0), "greater at a cent below");
        assert!(same_strike(29_400.0, 29_400.0), "greater_or_equal at the strike");
        assert!(!same_strike(80_199.99, 80_152.0));
        assert_eq!(kalshi_series("BTC"), ["KXBTCD"]);
        assert_eq!(kalshi_series("SP500"), ["KXINXU"]);
        assert!(kalshi_series("SPCX").is_empty());
    }
}
