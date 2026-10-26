//! Isolated Predict.fun adapter. Kalshi is a read-only signal; every order
//! emitted by this process targets Predict.fun.

mod chain;
mod client;
mod config;
mod engine;
mod model;
mod sign;

use std::sync::Arc;
use std::time::Duration;

use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::middleware;
use axum::response::IntoResponse;
use axum::routing::{get, post};
use axum::{Json, Router};
use clap::{Parser, Subcommand};
use client::{Client, Submit};
use config::Config;
use engine::{Action, Engine};
use futures_util::{SinkExt, StreamExt};
use parking_lot::Mutex;
use pk_feeds::{FeedHandle, FeedKind, KalshiSigner, spawn_feed};
use pk_svc::api::{ApiKey, require_key};
use pk_venue_core::service::{
    AdapterState, FillRecord, Journal, MarkoutTracker, OrderRecord, now_ms, public_error,
};
use pk_venue_core::{MatchGrade, NormalizedContract, RiskGuard, VenueMode};
use serde::Deserialize;
use serde_json::{Value, json};
use sign::{PredictSigner, Side};
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;

#[derive(Parser)]
#[command(name = "pk-predict")]
struct Args {
    #[arg(long)]
    profile: Option<String>,
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    Preflight,
    Approve {
        #[arg(long)]
        confirm: bool,
    },
    ProbeOrders {
        #[arg(long)]
        confirm: bool,
    },
}

struct App {
    cfg: Config,
    client: Arc<Client>,
    engine: Mutex<Engine>,
    state: Mutex<AdapterState>,
    journal: Mutex<Journal>,
    raw: Mutex<Journal>,
    kalshi: Mutex<Option<FeedHandle>>,
    kalshi_http: reqwest::Client,
    kalshi_signer: Arc<KalshiSigner>,
    markouts: Mutex<MarkoutTracker>,
    series_ms: Mutex<i64>,
}

impl App {
    fn record(&self, kind: &str, value: Value) {
        if let Err(error) = self.journal.lock().record(kind, value) {
            tracing::error!(error = %error, "journal_write");
        }
    }

    fn mark_open_cancelled(&self, status: &str) -> usize {
        let mut state = self.state.lock();
        let mut removed = 0;
        let mut released = 0.0;
        for order in state
            .orders
            .iter_mut()
            .filter(|order| order.status == "submitted" || order.status == "shadow_open")
        {
            order.status = status.into();
            order.updated_ms = now_ms();
            removed += 1;
            released += order.price * order.quantity;
        }
        state.risk.state.open_orders = state.risk.state.open_orders.saturating_sub(removed);
        state.risk.state.open_usd = (state.risk.state.open_usd - released).max(0.0);
        state.risk.state.active_markets =
            state.inventory.values().filter(|quantity| quantity.abs() > 1e-9).count();
        state.risk.save();
        removed
    }

    async fn cancel_all(&self, reason: &str) -> Value {
        self.state.lock().ready = false;
        let result = if self.cfg.mode == VenueMode::Live {
            let orders = self.client.orders().await;
            match (orders, self.client.cancel_all_off_book().await) {
                (Ok(orders), Ok(value)) => {
                    self.state.lock().onchain_cancel_pending = true;
                    match self.invalidate_orders(order_rows(&orders)).await {
                        Ok(receipts) => {
                            self.state.lock().onchain_cancel_pending = false;
                            self.mark_open_cancelled("cancelled_onchain");
                            json!({"off_book": value, "onchain_receipts": receipts})
                        }
                        Err(error) => {
                            self.state
                                .lock()
                                .latch(format!("on-chain cancellation failed: {}", public_error(&error)));
                            json!({"off_book": value, "error": public_error(&error)})
                        }
                    }
                }
                (Err(error), _) | (_, Err(error)) => {
                    self.state.lock().latch(format!("cancel-all failed: {}", public_error(&error)));
                    json!({"error": public_error(&error)})
                }
            }
        } else {
            json!({"shadow": true, "cancelled": self.mark_open_cancelled("cancelled")})
        };
        self.record("cancel_all", json!({"reason": reason, "result": result}));
        result
    }

    async fn invalidate_orders(&self, orders: &[Value]) -> anyhow::Result<Vec<String>> {
        if orders.is_empty() {
            return Ok(Vec::new());
        }
        let rpc = self
            .cfg
            .bnb_rpc_url
            .as_deref()
            .ok_or_else(|| anyhow::anyhow!("PREDICT_BNB_RPC_URL is required for cancellation"))?;
        let key = self
            .cfg
            .privy_private_key
            .as_deref()
            .ok_or_else(|| anyhow::anyhow!("PREDICT_PRIVY_PRIVATE_KEY is required for cancellation"))?;
        let account = self
            .cfg
            .account_address
            .as_deref()
            .ok_or_else(|| anyhow::anyhow!("PREDICT_ACCOUNT_ADDRESS is required for cancellation"))?;
        let markets = self.engine.lock().markets();
        let mut groups: std::collections::HashMap<(bool, bool), Vec<Value>> = Default::default();
        for order in orders {
            let raw = order.get("order").unwrap_or(order);
            let token = raw
                .get("tokenId")
                .map(|value| value.as_str().map(str::to_owned).unwrap_or_else(|| value.to_string()))
                .ok_or_else(|| anyhow::anyhow!("open Predict order is missing tokenId"))?;
            let market = markets
                .iter()
                .find(|market| market.outcomes.iter().any(|outcome| outcome.token_id == token))
                .ok_or_else(|| anyhow::anyhow!("open Predict order has an unknown market/token"))?;
            groups.entry((market.is_yield_bearing, market.is_neg_risk)).or_default().push(raw.clone());
        }
        let chain = chain::PredictChain::connect(rpc, key, account, self.cfg.chain_id).await?;
        let mut receipts = Vec::new();
        for ((yield_bearing, neg_risk), orders) in groups {
            if let Some(receipt) = chain.cancel_orders(&orders, yield_bearing, neg_risk).await? {
                receipts.push(receipt);
            }
        }
        Ok(receipts)
    }

    async fn fast_remove_quotes(&self, reason: &str) {
        if self.cfg.mode == VenueMode::Live {
            if let Err(error) = self.client.cancel_all_off_book().await {
                self.state.lock().latch(format!("fast quote removal failed: {}", public_error(&error)));
            }
        }
        let now = now_ms();
        let mut state = self.state.lock();
        let mut removed = 0_usize;
        for order in state.orders.iter_mut().filter(|o| o.status == "submitted" || o.status == "shadow_open")
        {
            order.status = "removed_off_book".into();
            order.updated_ms = now;
            removed += 1;
        }
        state.risk.state.open_orders = state.risk.state.open_orders.saturating_sub(removed);
        // Orders removed before fills release their reserved notional. Wallet
        // reconciliation remains the authority after any ambiguous ack.
        state.risk.state.open_usd = 0.0;
        state.risk.state.active_markets =
            state.inventory.values().filter(|quantity| quantity.abs() > 1e-9).count();
        state.risk.save();
        drop(state);
        self.record("fast_remove", json!({"reason":reason}));
    }

    async fn execute(&self, action: Action) {
        match action {
            Action::CancelAll { reason } => {
                self.cancel_all(&reason).await;
            }
            Action::Maker { market, token, outcome, price, quantity } => {
                self.place(&market, &token, &outcome, price, quantity, "maker", 0.0).await;
            }
            Action::Taker { market, token, outcome, price, quantity, edge } => {
                // Remove maker quotes before a marketable order to avoid self-trading.
                self.fast_remove_quotes("suspend maker before taker").await;
                self.place(&market, &token, &outcome, price, quantity, "taker", edge).await;
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    async fn place(
        &self,
        market: &model::PredictMarket,
        token: &str,
        outcome: &str,
        price: f64,
        quantity: f64,
        lane: &str,
        edge: f64,
    ) {
        let cost = price * quantity;
        let market_id = market.id.to_string();
        let new_market = {
            let state = self.state.lock();
            !state.orders.iter().any(|order| {
                order.market == market_id && (order.status == "submitted" || order.status == "shadow_open")
            }) && state.inventory.get(&market_id).is_none_or(|quantity| quantity.abs() <= 1e-9)
        };
        if let Err(reason) = self.state.lock().risk.check_order(cost, new_market) {
            self.record("risk_reject", json!({"market":market.id,"lane":lane,"reason":reason}));
            return;
        }
        let client_id = format!("pred-{}-{}", market.id, now_ms());
        if self.cfg.mode == VenueMode::Shadow {
            let record = OrderRecord {
                id: client_id.clone(),
                client_id,
                market: market.id.to_string(),
                lane: lane.into(),
                side: outcome.into(),
                price,
                quantity,
                status: "shadow_open".into(),
                created_ms: now_ms(),
                updated_ms: now_ms(),
                simulated: true,
            };
            {
                let mut state = self.state.lock();
                state.risk.state.open_orders += 1;
                state.risk.state.open_usd += cost;
                if new_market {
                    state.risk.state.active_markets += 1;
                }
                state.risk.save();
                state.push_order(record.clone());
            }
            self.record("shadow_order", serde_json::to_value(record).unwrap_or_default());
            if lane == "taker" {
                self.simulate_shadow_fills();
            }
            return;
        }
        let expiration = (now_ms() / 1000 + self.cfg.quote_ttl_s as i64) as u64;
        let strategy = if lane == "maker" { "LIMIT" } else { "MARKET" };
        let result = self
            .client
            .submit(Submit {
                market,
                token_id: token,
                side: Side::Buy,
                price,
                quantity,
                strategy,
                expiration,
                slippage_bps: (lane == "taker").then_some((self.cfg.taker_slippage * 10_000.0) as u64),
                post_only: lane == "maker",
            })
            .await;
        match result {
            Ok(reply) => {
                let id = reply
                    .response
                    .pointer("/data/orderId")
                    .or_else(|| reply.response.pointer("/data/id"))
                    .and_then(Value::as_str)
                    .unwrap_or(&client_id)
                    .to_owned();
                let record = OrderRecord {
                    id,
                    client_id,
                    market: market.id.to_string(),
                    lane: lane.into(),
                    side: outcome.into(),
                    price,
                    quantity,
                    status: "submitted".into(),
                    created_ms: now_ms(),
                    updated_ms: now_ms(),
                    simulated: false,
                };
                let mut state = self.state.lock();
                state.risk.state.open_orders += 1;
                state.risk.state.open_usd += cost;
                if new_market {
                    state.risk.state.active_markets += 1;
                }
                state.risk.save();
                state.push_order(record.clone());
                drop(state);
                self.record("order_submitted", json!({"order":record,"edge":edge}));
            }
            Err(error) => {
                let reason = public_error(&error);
                let mut state = self.state.lock();
                state.risk.state.consecutive_rejections += 1;
                let _ = state.risk.check_latches();
                state.risk.save();
                drop(state);
                self.record("order_rejected", json!({"market":market.id,"lane":lane,"reason":reason}));
            }
        }
    }

    fn simulate_shadow_fills(&self) {
        if self.cfg.mode != VenueMode::Shadow {
            return;
        }
        let now = now_ms();
        let mut fills = Vec::new();
        {
            let precision: std::collections::HashMap<String, u32> = self
                .engine
                .lock()
                .markets()
                .into_iter()
                .map(|m| (m.id.to_string(), m.decimal_precision))
                .collect();
            let mut state = self.state.lock();
            let books = state.books.clone();
            for order in state.orders.iter_mut().filter(|o| o.simulated && o.status == "shadow_open") {
                // A small measured-latency floor prevents a shadow taker from
                // seeing and filling the exact same instantaneous update.
                if now - order.created_ms < 100 {
                    continue;
                }
                let Some(book) = books.get(&order.market) else { continue };
                let dp = precision.get(&order.market).copied().unwrap_or(2);
                let executable = if order.lane == "taker" {
                    true
                } else if order.side == "YES" {
                    book.ask.is_some_and(|ask| ask <= order.price + 1e-9) && book.ask_size >= order.quantity
                } else {
                    book.bid.is_some_and(|bid| model::complement_price(bid, dp) <= order.price + 1e-9)
                        && book.bid_size >= order.quantity
                };
                if executable {
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
            }
            for fill in &fills {
                *state.inventory.entry(fill.market.clone()).or_default() +=
                    if fill.side == "YES" { fill.quantity } else { -fill.quantity };
                state.risk.state.open_orders = state.risk.state.open_orders.saturating_sub(1);
                state.risk.state.open_usd = (state.risk.state.open_usd - fill.price * fill.quantity).max(0.0);
                state.push_fill(fill.clone());
            }
            state.risk.save();
        }
        for fill in fills {
            self.markouts.lock().watch(&fill.market, &fill.side, fill.price, fill.time_ms);
            self.record("shadow_fill", serde_json::to_value(fill).unwrap_or_default());
        }
    }
}

fn main() -> anyhow::Result<()> {
    pk_svc::tls::install_default_provider();
    let args = Args::parse();
    let cfg = Config::load(args.profile.as_deref())?;
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .json()
        .init();
    let runtime = tokio::runtime::Builder::new_multi_thread().worker_threads(2).enable_all().build()?;
    runtime.block_on(async move {
        let signer = match (&cfg.privy_private_key, &cfg.account_address) {
            (Some(key), Some(account)) => Some(Arc::new(PredictSigner::new(key, account, cfg.chain_id)?)),
            _ => None,
        };
        let api_key = cfg.predict_api_key.clone().unwrap_or_default();
        let client = Arc::new(Client::new(&cfg.predict_api_url, &api_key, signer)?);
        match args.command {
            Some(Command::Preflight) => preflight(&cfg, &client).await,
            Some(Command::Approve { confirm }) => approve(&cfg, &client, confirm).await,
            Some(Command::ProbeOrders { confirm }) => probe(&cfg, &client, confirm).await,
            None => run(cfg, client).await,
        }
    })
}

async fn preflight(cfg: &Config, client: &Client) -> anyhow::Result<()> {
    anyhow::ensure!(cfg.predict_api_key.is_some(), "PREDICT_API_KEY is required");
    client.authenticate().await?;
    let markets = client.markets().await?;
    let supported: Vec<_> = markets.iter().filter(|m| m.supported(&cfg.assets)).collect();
    anyhow::ensure!(!supported.is_empty(), "no open BTC/ETH 15-minute direct-linked markets");
    let book = client.orderbook(supported[0].id).await?;
    let orders = client.orders().await?;
    let positions = client.positions().await?;
    println!(
        "Predict preflight OK: account={} markets={} book={} open-orders={} positions={}",
        cfg.account_address.as_deref().unwrap_or("unset"),
        supported.len(),
        !book.is_null(),
        count_rows(&orders),
        count_rows(&positions)
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

async fn approve(cfg: &Config, client: &Client, confirm: bool) -> anyhow::Result<()> {
    anyhow::ensure!(confirm, "approve is state-changing; pass --confirm");
    client.authenticate().await?;
    let rpc = cfg.bnb_rpc_url.as_deref().ok_or_else(|| anyhow::anyhow!("PREDICT_BNB_RPC_URL is required"))?;
    let key = cfg
        .privy_private_key
        .as_deref()
        .ok_or_else(|| anyhow::anyhow!("PREDICT_PRIVY_PRIVATE_KEY is required"))?;
    let account = cfg
        .account_address
        .as_deref()
        .ok_or_else(|| anyhow::anyhow!("PREDICT_ACCOUNT_ADDRESS is required"))?;
    let chain = chain::PredictChain::connect(rpc, key, account, cfg.chain_id).await?;
    let (usdt, gas) = chain.balances().await?;
    anyhow::ensure!(gas > 0.0, "Privy signer has no BNB for approval gas");
    println!("smart account USDT {usdt:.6}, signer BNB {gas:.6}");
    let receipts = chain.approve_all().await?;
    if receipts.is_empty() {
        println!("all Predict allowances are already present");
    }
    for receipt in receipts {
        println!("approved {receipt}");
    }
    Ok(())
}

async fn probe(cfg: &Config, client: &Client, confirm: bool) -> anyhow::Result<()> {
    anyhow::ensure!(confirm, "probe-orders places a real order; pass --confirm");
    client.authenticate().await?;
    let market = client
        .markets()
        .await?
        .into_iter()
        .find(|m| m.supported(&cfg.assets))
        .ok_or_else(|| anyhow::anyhow!("no eligible market"))?;
    let token = market.yes_outcome().ok_or_else(|| anyhow::anyhow!("YES token missing"))?.token_id.clone();
    let expiration = (now_ms() / 1000 + 20) as u64;
    let started = std::time::Instant::now();
    let submitted = client
        .submit(Submit {
            market: &market,
            token_id: &token,
            side: Side::Buy,
            price: 0.01,
            quantity: 1.0,
            strategy: "LIMIT",
            expiration,
            slippage_bps: None,
            post_only: true,
        })
        .await?;
    let id = submitted
        .response
        .pointer("/data/order/id")
        .or_else(|| submitted.response.pointer("/data/id"))
        .map(|id| id.as_str().map(str::to_owned).unwrap_or_else(|| id.to_string()));
    if let Some(id) = id {
        let _ = client.remove(&[id]).await?;
    } else {
        let _ = client.cancel_all_off_book().await?;
    }
    let rpc = cfg.bnb_rpc_url.as_deref().ok_or_else(|| anyhow::anyhow!("PREDICT_BNB_RPC_URL is required"))?;
    let key = cfg
        .privy_private_key
        .as_deref()
        .ok_or_else(|| anyhow::anyhow!("PREDICT_PRIVY_PRIVATE_KEY is required"))?;
    let account = cfg
        .account_address
        .as_deref()
        .ok_or_else(|| anyhow::anyhow!("PREDICT_ACCOUNT_ADDRESS is required"))?;
    let chain = chain::PredictChain::connect(rpc, key, account, cfg.chain_id).await?;
    let receipt =
        chain.cancel_orders(&[submitted.order], market.is_yield_bearing, market.is_neg_risk).await?;
    println!(
        "Predict probe submitted, removed off-book, and cancelled on-chain in {:.1} ms; receipt={}",
        started.elapsed().as_secs_f64() * 1e3,
        receipt.as_deref().unwrap_or("none")
    );
    Ok(())
}

async fn run(cfg: Config, client: Arc<Client>) -> anyhow::Result<()> {
    anyhow::ensure!(cfg.enabled, "PK_PRED_ENABLED must be yes");
    std::fs::create_dir_all(&cfg.state_dir)?;
    let risk = RiskGuard::open(cfg.risk, cfg.state_dir.join("risk.json"))?;
    let state = AdapterState::new("predict", cfg.mode, cfg.fingerprint.clone(), risk);
    let pem = std::fs::read_to_string(&cfg.kalshi_private_key_path)?;
    let kalshi_signer = Arc::new(KalshiSigner::from_pem(&cfg.kalshi_api_key_id, &pem)?);
    let app = Arc::new(App {
        journal: Mutex::new(Journal::open(cfg.state_dir.join("journal"))?),
        raw: Mutex::new(Journal::open(cfg.state_dir.join("raw"))?),
        engine: Mutex::new(Engine::new()),
        state: Mutex::new(state),
        kalshi: Mutex::new(None),
        kalshi_http: reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(3))
            .timeout(Duration::from_secs(8))
            .build()?,
        kalshi_signer,
        cfg,
        client,
        markouts: Mutex::new(MarkoutTracker::new()),
        series_ms: Mutex::new(0),
    });

    // Reconcile before readiness. Discovery runs before cancellation so every
    // open token can be routed to its exact standard/yield/neg-risk exchange.
    let mut startup_orders = None;
    if app.cfg.predict_api_key.is_some() && app.cfg.privy_private_key.is_some() {
        app.client.authenticate().await?;
        let orders = app.client.orders().await?;
        let positions = app.client.positions().await?;
        app.record("reconcile", json!({"orders":count_rows(&orders),"positions":count_rows(&positions)}));
        startup_orders = Some(orders);
    }

    discover(&app).await?;
    if startup_orders.as_ref().is_some_and(|orders| count_rows(orders) > 0) {
        app.cancel_all("startup reconciliation").await;
        anyhow::ensure!(
            !app.state.lock().onchain_cancel_pending,
            "startup cancellation did not complete on-chain"
        );
    }
    app.state.lock().reconciling = false;
    let weak = Arc::downgrade(&app);
    let emit = Arc::new(move |_venue: char, text: String| {
        if let Some(app) = weak.upgrade() {
            let now = now_ms();
            let _ = app.raw.lock().record("kalshi", json!({"raw":text}));
            if text.contains("\"type\":\"reconnect\"") || text.contains("\"type\":\"resync\"") {
                let mut state = app.state.lock();
                state.ready = false;
                state.feeds.entry("kalshi".into()).or_default().sequence_gaps +=
                    text.contains("resync") as u64;
            } else if let Some((ticker, fair)) = app.engine.lock().kalshi_frame(&text) {
                let mut state = app.state.lock();
                state.kalshi_fairs.insert(ticker, fair);
                let feed = state.feeds.entry("kalshi".into()).or_default();
                feed.connected = true;
                feed.last_message_ms = now;
            }
        }
    });
    let kalshi = spawn_feed(
        app.cfg.kalshi_ws_url.clone(),
        FeedKind::Kalshi { signer: app.kalshi_signer.clone() },
        emit,
    );
    kalshi.set_wanted(app.engine.lock().tickers());
    *app.kalshi.lock() = Some(kalshi);

    tokio::spawn(predict_ws(app.clone()));
    tokio::spawn(discovery_loop(app.clone()));
    tokio::spawn(strategy_loop(app.clone()));
    tokio::spawn(kill_loop(app.clone()));
    tokio::spawn(clock_loop(app.clone()));
    tokio::spawn(settlement_loop(app.clone()));

    app.state.lock().ready = true;
    let routes = routes(app.clone());
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", app.cfg.port)).await?;
    tracing::info!(port = app.cfg.port, mode = app.cfg.mode.as_str(), "pk-predict ready");
    let server = axum::serve(listener, routes).with_graceful_shutdown(shutdown());
    server.await?;
    app.cancel_all("shutdown").await;
    Ok(())
}

async fn discover(app: &Arc<App>) -> anyhow::Result<()> {
    let markets: Vec<_> =
        app.client.markets().await?.into_iter().filter(|m| m.supported(&app.cfg.assets)).collect();
    anyhow::ensure!(
        !markets.is_empty(),
        "Predict discovery returned no eligible direct-linked BTC/ETH markets"
    );
    let mut decisions = std::collections::HashMap::new();
    let mut executable = Vec::new();
    for mut market in markets {
        let ticker = market.kalshi_ticker.clone().unwrap_or_default();
        let kalshi = fetch_kalshi_for(app, &market).await.unwrap_or_else(|error| NormalizedContract {
            venue: "kalshi".into(),
            venue_market_id: ticker.clone(),
            kalshi_ticker: ticker,
            correction_rule: format!("unavailable:{}", public_error(&error)),
            ..Default::default()
        });
        let decision = market.match_decision(app.cfg.basis_buffer, &kalshi);
        decisions.insert(market.id.to_string(), decision.clone());
        if decision.grade != MatchGrade::E2 {
            // Engine/feed are keyed by Kalshi ticker; store the resolved
            // (explicit or timing-derived) ticker on the executable market.
            market.kalshi_ticker = Some(decision.kalshi_ticker.clone());
            executable.push(market);
        }
    }
    let changed = {
        let state = app.state.lock();
        state.matches.iter().any(|(id, old)| {
            decisions.get(id).is_none_or(|new| {
                old.venue_fingerprint != new.venue_fingerprint || old.kalshi_ticker != new.kalshi_ticker
            })
        })
    };
    if changed {
        app.state.lock().latch("Predict market rules, ticker link, or outcome mapping changed");
        app.cancel_all("semantic match changed").await;
    }
    {
        let mut state = app.state.lock();
        state.matches = decisions.clone();
    }
    app.record("discovery", json!({"markets":executable,"decisions":decisions}));
    app.engine.lock().set_markets(executable);
    if let Some(feed) = app.kalshi.lock().as_ref() {
        feed.set_wanted(app.engine.lock().tickers());
    }
    Ok(())
}

fn kalshi_contract(ticker: &str, market: &Value) -> anyhow::Result<NormalizedContract> {
    let text = |key: &str| market.get(key).and_then(Value::as_str).unwrap_or_default().to_owned();
    let time = |key: &str| {
        market.get(key).and_then(|value| {
            value.as_i64().or_else(|| {
                value
                    .as_str()
                    .and_then(|raw| chrono::DateTime::parse_from_rfc3339(raw).ok())
                    .map(|date| date.timestamp_millis())
            })
        })
    };
    let returned = text("ticker");
    anyhow::ensure!(returned.is_empty() || returned == ticker, "Kalshi returned a different ticker");
    let underlying = ["BTC", "ETH"].into_iter().find(|asset| ticker.contains(asset)).unwrap_or("");
    anyhow::ensure!(!underlying.is_empty(), "unsupported Kalshi underlying");
    Ok(NormalizedContract {
        venue: "kalshi".into(),
        venue_market_id: ticker.into(),
        kalshi_ticker: ticker.into(),
        event_class: "crypto_up_down_15m".into(),
        underlying: underlying.into(),
        outcome: "up".into(),
        operator: text("strike_type"),
        threshold: market
            .get("floor_strike")
            .or_else(|| market.get("functional_strike"))
            .map(Value::to_string),
        open_ms: time("open_time"),
        close_ms: time("close_time"),
        decision_ms: time("expiration_time").or_else(|| time("close_time")),
        resolution_source: text("settlement_source"),
        averaging_window_s: market.get("settlement_timer_seconds").and_then(Value::as_u64),
        tie_rule: text("expiration_value"),
        correction_rule: text("rules_primary"),
    })
}

async fn fetch_kalshi(app: &Arc<App>, ticker: &str) -> anyhow::Result<NormalizedContract> {
    anyhow::ensure!(!ticker.is_empty(), "Predict market has no Kalshi ticker");
    let path = format!("/trade-api/v2/markets/{ticker}");
    let mut request =
        app.kalshi_http.get(format!("{}/markets/{ticker}", app.cfg.kalshi_rest_url.trim_end_matches('/')));
    for (key, value) in app.kalshi_signer.headers("GET", &path, None) {
        request = request.header(key, value);
    }
    let value = request.send().await?.error_for_status()?.json::<Value>().await?;
    let market = value.get("market").or_else(|| value.pointer("/data/market")).unwrap_or(&value);
    kalshi_contract(ticker, market)
}

/// Resolve the Kalshi contract for a Predict market: explicit ticker when
/// the venue supplies one, else the `KX<ASSET>15M` market whose
/// open/close match the Predict window from `seriesSlug`/`categorySlug`.
async fn fetch_kalshi_for(
    app: &Arc<App>,
    market: &crate::model::PredictMarket,
) -> anyhow::Result<NormalizedContract> {
    if let Some(ticker) = market.kalshi_ticker.as_deref().filter(|t| !t.is_empty()) {
        return fetch_kalshi(app, ticker).await;
    }
    let asset = market.series_asset().ok_or_else(|| anyhow::anyhow!("Predict market has no series asset"))?;
    let open_s = market.window_open_s().ok_or_else(|| anyhow::anyhow!("Predict market has no 15m window"))?;
    let close_s = open_s + 900;
    let mut request = app
        .kalshi_http
        .get(format!("{}/markets", app.cfg.kalshi_rest_url.trim_end_matches('/')))
        .query(&[
            ("series_ticker", format!("KX{asset}15M")),
            ("min_close_ts", (close_s - 1).to_string()),
            ("max_close_ts", (close_s + 901).to_string()),
        ]);
    for (key, value) in app.kalshi_signer.headers("GET", "/trade-api/v2/markets", None) {
        request = request.header(key, value);
    }
    let value = request.send().await?.error_for_status()?.json::<Value>().await?;
    let markets = value.get("markets").and_then(Value::as_array).map(Vec::as_slice).unwrap_or_default();
    let parse_s = |m: &Value, key: &str| {
        m.get(key).and_then(|v| {
            v.as_i64().map(|s| s).or_else(|| {
                v.as_str().and_then(|raw| chrono::DateTime::parse_from_rfc3339(raw).ok()).map(|d| d.timestamp())
            })
        })
    };
    let found = markets.iter().find(|m| {
        parse_s(m, "open_time") == Some(open_s) && parse_s(m, "close_time") == Some(close_s)
    });
    let found = found.ok_or_else(|| anyhow::anyhow!("no Kalshi KX{asset}15M market for window {open_s}"))?;
    let ticker = found.get("ticker").and_then(Value::as_str).unwrap_or_default();
    kalshi_contract(ticker, found)
}

async fn discovery_loop(app: Arc<App>) {
    let mut interval = tokio::time::interval(Duration::from_secs(30));
    interval.tick().await;
    loop {
        interval.tick().await;
        if let Err(error) = discover(&app).await {
            let reason = public_error(&error);
            app.state.lock().degraded = vec![reason.clone()];
            app.record("discovery_error", json!({"error":reason}));
        }
    }
}

async fn strategy_loop(app: Arc<App>) {
    let mut interval = tokio::time::interval(Duration::from_millis(100));
    loop {
        interval.tick().await;
        app.simulate_shadow_fills();
        let inventory = app.state.lock().inventory.clone();
        let actions = app.engine.lock().evaluate(&app.cfg, &inventory);
        if actions.iter().any(|a| matches!(a, Action::Maker { .. })) {
            app.fast_remove_quotes("requote").await;
        }
        for action in actions {
            app.execute(action).await;
        }
        let now = now_ms();
        {
            let markets = app.engine.lock().markets();
            let state = app.state.lock();
            let mut mids = std::collections::HashMap::new();
            for market in &markets {
                let id = market.id.to_string();
                if let Some(mid) = state.books.get(&id).copied().and_then(pk_venue_core::Book::mid) {
                    mids.insert((id.clone(), "YES".into()), mid);
                    mids.insert((id, "NO".into()), model::complement_price(mid, market.decimal_precision));
                }
            }
            drop(state);
            for markout in app.markouts.lock().poll(now, &mids) {
                app.record("markout", markout.clone());
                app.state.lock().push_markout(markout);
            }
            if now - *app.series_ms.lock() >= 1000 {
                *app.series_ms.lock() = now;
                let mut state = app.state.lock();
                for market in &markets {
                    let id = market.id.to_string();
                    let fair =
                        market.kalshi_ticker.as_deref().and_then(|t| state.kalshi_fairs.get(t).copied());
                    let mid = state.books.get(&id).copied().and_then(pk_venue_core::Book::mid);
                    state.series.push(&id, json!([now, fair, mid]));
                }
            }
        }
        let mut state = app.state.lock();
        state.last_heartbeat_ms = now;
        state.ready = state.latched.is_empty()
            && state.degraded.is_empty()
            && !state.reconciling
            && !state.onchain_cancel_pending;
    }
}

async fn kill_loop(app: Arc<App>) {
    let mut interval = tokio::time::interval(Duration::from_millis(250));
    loop {
        interval.tick().await;
        if app.cfg.kill_file.exists() {
            app.state.lock().latch("kill file present");
            app.cancel_all("kill file").await;
        }
    }
}

async fn clock_loop(app: Arc<App>) {
    let mut interval = tokio::time::interval(Duration::from_secs(5));
    loop {
        interval.tick().await;
        match pk_venue_core::service::chrony_offset_ms() {
            Ok(offset) => {
                app.state.lock().clock_offset_ms = Some(offset);
                if offset > 100 {
                    app.state.lock().latch(format!("clock offset {offset} ms exceeds 100 ms"));
                    app.cancel_all("clock offset").await;
                }
            }
            Err(error) if cfg!(target_os = "linux") => {
                app.state.lock().latch(format!("clock check failed: {}", public_error(&error)));
                app.cancel_all("clock check failed").await;
            }
            Err(_) => {}
        }
    }
}

async fn settlement_loop(app: Arc<App>) {
    if app.cfg.privy_private_key.is_none() {
        return;
    }
    let mut interval = tokio::time::interval(Duration::from_secs(30));
    loop {
        interval.tick().await;
        match app.client.positions().await {
            Ok(positions) => {
                app.record("position_reconcile", json!({"positions": positions}));
                settle_positions(&app, &positions).await;
            }
            Err(error) => {
                let reason = format!("position reconciliation failed: {}", public_error(&error));
                app.state.lock().latch(reason.clone());
                app.record("reconcile_error", json!({"reason": reason}));
                app.cancel_all("position reconciliation failed").await;
            }
        }
    }
}

/// Redeem WON legs and merge balanced YES/NO pairs. Candidates are always
/// journalled; execution needs live mode plus its explicit flag, runs in this
/// background task, and never blocks the strategy loop.
async fn settle_positions(app: &Arc<App>, positions: &Value) {
    let (redeems, merges) = model::settle_candidates(positions);
    if redeems.is_empty() && merges.is_empty() {
        return;
    }
    app.record(
        "settle_candidates",
        json!({"redeems": redeems, "merges": merges, "auto": {
            "redeem": app.cfg.auto_redeem, "merge": app.cfg.auto_merge,
        }}),
    );
    if app.cfg.mode != VenueMode::Live || !(app.cfg.auto_redeem || app.cfg.auto_merge) {
        return;
    }
    let mut chain: Option<chain::PredictChain> = None;
    let markets = app.engine.lock().markets();
    let flags = |market_id: &str| {
        markets.iter().find(|m| m.id.to_string() == market_id).map(|m| (m.is_yield_bearing, m.is_neg_risk))
    };
    if app.cfg.auto_redeem {
        for candidate in &redeems {
            let (condition, amount) =
                (parse_b256(&candidate.condition_id), parse_u256(&candidate.amount_wei));
            let (Some((is_yield, is_neg)), Some(condition), Some(amount)) =
                (flags(&candidate.market_id), condition, amount)
            else {
                continue;
            };
            if chain.is_none() {
                chain = match settle_chain(app).await {
                    Ok(chain) => Some(chain),
                    Err(error) => {
                        app.record("settle_error", json!({"error": public_error(&error)}));
                        break;
                    }
                };
            }
            let receipt = chain
                .as_ref()
                .unwrap()
                .redeem_positions(condition, candidate.index_set, amount, is_yield, is_neg)
                .await;
            match receipt {
                Ok(receipt) => {
                    app.record("redeemed", json!({"candidate": candidate, "receipt": receipt}));
                    let mut state = app.state.lock();
                    state.inventory.remove(&candidate.market_id);
                    state.risk.state.active_markets =
                        state.inventory.values().filter(|q| q.abs() > 1e-9).count();
                    state.risk.save();
                }
                Err(error) => {
                    app.record(
                        "settle_error",
                        json!({"candidate": candidate, "error": public_error(&error)}),
                    );
                }
            }
        }
    }
    if app.cfg.auto_merge {
        for candidate in &merges {
            let (condition, amount) =
                (parse_b256(&candidate.condition_id), parse_u256(&candidate.amount_wei));
            let (Some((is_yield, is_neg)), Some(condition), Some(amount)) =
                (flags(&candidate.market_id), condition, amount)
            else {
                continue;
            };
            if chain.is_none() {
                chain = match settle_chain(app).await {
                    Ok(chain) => Some(chain),
                    Err(error) => {
                        app.record("settle_error", json!({"error": public_error(&error)}));
                        break;
                    }
                };
            }
            let receipt = chain.as_ref().unwrap().merge_positions(condition, amount, is_yield, is_neg).await;
            match receipt {
                Ok(receipt) => {
                    app.record("merged", json!({"candidate": candidate, "receipt": receipt}));
                }
                Err(error) => {
                    app.record(
                        "settle_error",
                        json!({"candidate": candidate, "error": public_error(&error)}),
                    );
                }
            }
        }
    }
}

async fn settle_chain(app: &Arc<App>) -> anyhow::Result<chain::PredictChain> {
    let rpc =
        app.cfg.bnb_rpc_url.as_deref().ok_or_else(|| anyhow::anyhow!("PREDICT_BNB_RPC_URL is required"))?;
    let key = app
        .cfg
        .privy_private_key
        .as_deref()
        .ok_or_else(|| anyhow::anyhow!("PREDICT_PRIVY_PRIVATE_KEY is required"))?;
    let account = app
        .cfg
        .account_address
        .as_deref()
        .ok_or_else(|| anyhow::anyhow!("PREDICT_ACCOUNT_ADDRESS is required"))?;
    chain::PredictChain::connect(rpc, key, account, app.cfg.chain_id).await
}

fn parse_b256(raw: &str) -> Option<alloy::primitives::B256> {
    use std::str::FromStr;
    alloy::primitives::B256::from_str(raw).ok()
}

fn parse_u256(raw: &str) -> Option<alloy::primitives::U256> {
    use std::str::FromStr;
    alloy::primitives::U256::from_str(raw).ok()
}

async fn predict_ws(app: Arc<App>) {
    loop {
        if let Err(error) = predict_ws_session(&app).await {
            let reason = public_error(&error);
            let mut state = app.state.lock();
            let feed = state.feeds.entry("predict".into()).or_default();
            feed.connected = false;
            feed.errors += 1;
            feed.last_error = Some(reason);
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
}

async fn predict_ws_session(app: &Arc<App>) -> anyhow::Result<()> {
    let mut request = app.cfg.predict_ws_url.clone().into_client_request()?;
    let api_key = app
        .cfg
        .predict_api_key
        .as_deref()
        .ok_or_else(|| anyhow::anyhow!("PREDICT_API_KEY is required for WebSocket"))?;
    request.headers_mut().insert("x-api-key", api_key.parse()?);
    let (mut ws, _) = tokio_tungstenite::connect_async(request).await?;
    {
        let mut state = app.state.lock();
        let feed = state.feeds.entry("predict".into()).or_default();
        feed.connected = true;
        feed.reconnects += 1;
    }
    let markets = app.engine.lock().markets();
    for market in &markets {
        let id = app.client.next_request_id();
        ws.send(Message::Text(
            json!({"method":"subscribe","requestId":id,"params":[format!("predictOrderbook/{}",market.id)]})
                .to_string()
                .into(),
        ))
        .await?;
    }
    if let Some(jwt) = app.client.jwt() {
        let id = app.client.next_request_id();
        ws.send(Message::Text(
            json!({"method":"subscribe","requestId":id,"params":[format!("predictWalletEvents/{jwt}")]})
                .to_string()
                .into(),
        ))
        .await?;
    }
    while let Some(frame) = ws.next().await {
        let frame = frame?;
        if frame.is_ping() {
            ws.send(Message::Pong(frame.into_data())).await?;
            continue;
        }
        let Some(text) = frame.to_text().ok() else { continue };
        let value: Value = serde_json::from_str(text)?;
        if value.get("type").and_then(Value::as_str) == Some("H")
            || value.get("method").and_then(Value::as_str) == Some("heartbeat")
        {
            let data = value.get("data").cloned().unwrap_or(Value::Null);
            ws.send(Message::Text(json!({"method":"heartbeat","data":data}).to_string().into())).await?;
            continue;
        }
        app.raw.lock().record("predict", json!({"raw":value.clone()}))?;
        if let Some((id, book)) = model::book_from_ws(&value) {
            if book.valid() {
                app.engine.lock().venue_book(id.clone(), book);
                let mut state = app.state.lock();
                state.books.insert(id, book);
                let feed = state.feeds.entry("predict".into()).or_default();
                feed.last_message_ms = now_ms();
            }
        }
        handle_wallet_event(app, &value);
    }
    anyhow::bail!("Predict WebSocket closed")
}

fn handle_wallet_event(app: &Arc<App>, value: &Value) {
    let data = value.get("data").or_else(|| value.get("payload")).unwrap_or(value);
    let Some(kind) = data.get("type").and_then(Value::as_str) else { return };
    if !kind.starts_with("order") {
        return;
    }
    app.record("wallet_event", data.clone());
    let order_id = data.get("orderId").and_then(Value::as_str).unwrap_or_default().to_owned();
    match kind {
        "orderTransactionSuccess" => {
            let fill = data.get("fill").or_else(|| data.pointer("/details/fill"));
            let wei = |key: &str| {
                fill.and_then(|f| f.get(key))
                    .and_then(Value::as_str)
                    .and_then(|x| x.parse::<f64>().ok())
                    .unwrap_or(0.0)
                    / 1e18
            };
            let fee = data
                .get("fee")
                .and_then(|f| f.get("amountWei"))
                .and_then(Value::as_str)
                .and_then(|x| x.parse::<f64>().ok())
                .unwrap_or(0.0)
                / 1e18;
            let record = FillRecord {
                id: data.get("settlementId").and_then(Value::as_str).unwrap_or("unknown").into(),
                order_id: if order_id.is_empty() { "unknown".into() } else { order_id.clone() },
                market: data.pointer("/details/marketId").map(Value::to_string).unwrap_or_default(),
                side: data.pointer("/details/outcome").and_then(Value::as_str).unwrap_or("").into(),
                price: wei("executedPriceWei"),
                quantity: wei("executedSizeWei"),
                fee,
                time_ms: now_ms(),
                simulated: false,
            };
            if app.cfg.mode == VenueMode::Live
                && !app.state.lock().orders.iter().any(|o| o.id == record.order_id)
            {
                app.state.lock().latch(format!("unknown fill {}", record.order_id));
            }
            let mut state = app.state.lock();
            if let Some(order) = state.orders.iter_mut().find(|order| order.id == record.order_id) {
                order.status = "filled".into();
                order.updated_ms = record.time_ms;
            }
            *state.inventory.entry(record.market.clone()).or_default() +=
                if record.side.eq_ignore_ascii_case("yes") { record.quantity } else { -record.quantity };
            state.risk.state.open_orders = state.risk.state.open_orders.saturating_sub(1);
            state.risk.state.open_usd = (state.risk.state.open_usd - record.price * record.quantity).max(0.0);
            state.risk.state.active_markets =
                state.inventory.values().filter(|quantity| quantity.abs() > 1e-9).count();
            state.risk.save();
            state.push_fill(record.clone());
            drop(state);
            app.markouts.lock().watch(&record.market, &record.side, record.price, record.time_ms);
        }
        "orderNotAccepted" => {
            let reason = data.get("reason").and_then(Value::as_str).unwrap_or("not_accepted").to_owned();
            let mut state = app.state.lock();
            let released = state.orders.iter_mut().find(|order| order.id == order_id).map(|order| {
                order.status = format!("rejected:{reason}");
                order.updated_ms = now_ms();
                order.price * order.quantity
            });
            if let Some(released) = released {
                state.risk.state.open_orders = state.risk.state.open_orders.saturating_sub(1);
                state.risk.state.open_usd = (state.risk.state.open_usd - released).max(0.0);
            }
            state.risk.state.consecutive_rejections += 1;
            let _ = state.risk.check_latches();
            state.risk.save();
        }
        "orderExpired" | "orderCancelled" | "orderTransactionFailed" => {
            let status = if kind == "orderTransactionFailed" { "settlement_failed" } else { kind };
            let mut state = app.state.lock();
            let released = state.orders.iter_mut().find(|order| order.id == order_id).map(|order| {
                order.status = status.into();
                order.updated_ms = now_ms();
                order.price * order.quantity
            });
            if let Some(released) = released {
                state.risk.state.open_orders = state.risk.state.open_orders.saturating_sub(1);
                state.risk.state.open_usd = (state.risk.state.open_usd - released).max(0.0);
                state.risk.save();
            }
        }
        _ => {}
    }
}

fn routes(app: Arc<App>) -> Router {
    let protected = Router::new()
        .route("/v1/predict/status", get(api_status))
        .route("/v1/predict/markets", get(api_markets))
        .route("/v1/predict/series", get(api_series))
        .route("/v1/predict/orders", get(api_orders))
        .route("/v1/predict/fills", get(api_fills))
        .route("/v1/predict/kill", post(api_kill))
        .route("/v1/predict/kill/ack", post(api_ack))
        .route("/v1/predict/cancel-all", post(api_cancel))
        .route("/v1/predict/flatten", post(api_flatten))
        .layer(middleware::from_fn_with_state(ApiKey(Arc::new(app.cfg.api_key.clone())), require_key));
    Router::new()
        .route("/health/live", get(|| async { Json(json!({"live":true})) }))
        .route("/health/ready", get(api_ready))
        .merge(protected)
        .with_state(app)
}

async fn api_status(State(app): State<Arc<App>>) -> Json<Value> {
    Json(app.state.lock().status())
}
async fn api_markets(State(app): State<Arc<App>>) -> Json<Value> {
    Json(json!(app.engine.lock().markets()))
}
#[derive(Deserialize)]
struct Since {
    #[serde(default)]
    since: i64,
}
async fn api_series(State(app): State<Arc<App>>, Query(q): Query<Since>) -> Json<Value> {
    Json(app.state.lock().series.since(q.since))
}
async fn api_orders(State(app): State<Arc<App>>) -> Json<Value> {
    Json(json!(app.state.lock().orders))
}
async fn api_fills(State(app): State<Arc<App>>) -> Json<Value> {
    Json(json!(app.state.lock().fills))
}
async fn api_ready(State(app): State<Arc<App>>) -> impl IntoResponse {
    let ready = app.state.lock().status()["ready"].as_bool().unwrap_or(false);
    (if ready { StatusCode::OK } else { StatusCode::SERVICE_UNAVAILABLE }, Json(json!({"ready":ready})))
}
async fn api_kill(State(app): State<Arc<App>>) -> Json<Value> {
    let result = std::fs::write(&app.cfg.kill_file, b"http kill\n")
        .map(|_| json!({"halted":true}))
        .unwrap_or_else(|e| json!({"error":public_error(&e)}));
    app.state.lock().latch("HTTP kill");
    app.cancel_all("HTTP kill").await;
    Json(result)
}
async fn api_ack(State(app): State<Arc<App>>) -> Json<Value> {
    let _ = std::fs::remove_file(&app.cfg.kill_file);
    app.state.lock().acknowledge();
    Json(json!({"acknowledged":true}))
}
async fn api_cancel(State(app): State<Arc<App>>) -> Json<Value> {
    Json(app.cancel_all("HTTP cancel-all").await)
}
async fn api_flatten(State(app): State<Arc<App>>) -> Json<Value> {
    let cancelled = app.cancel_all("HTTP flatten").await;
    let inventory = app.state.lock().inventory.clone();
    let remaining: Vec<_> = inventory.iter().filter(|(_, q)| q.abs() > 1e-9).collect();
    if !remaining.is_empty() {
        app.state.lock().latch("flatten requires liquidity within slippage cap");
    }
    Json(json!({"cancelled":cancelled,"remaining_inventory":inventory,"flattened":remaining.is_empty()}))
}

async fn shutdown() {
    #[cfg(unix)]
    {
        let mut term =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("SIGTERM");
        tokio::select! { _ = tokio::signal::ctrl_c() => {}, _ = term.recv() => {} }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}

fn count_rows(value: &Value) -> usize {
    value
        .pointer("/data/orders")
        .or_else(|| value.pointer("/data/positions"))
        .or_else(|| value.get("data"))
        .or_else(|| value.get("orders"))
        .or_else(|| value.get("positions"))
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0)
}

fn order_rows(value: &Value) -> &[Value] {
    value
        .pointer("/data/orders")
        .or_else(|| value.get("orders"))
        .or_else(|| value.get("data"))
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or_default()
}
