//! Isolated Predict.fun adapter. Kalshi is a read-only signal; every order
//! emitted by this process targets Predict.fun.

mod chain;
mod client;
mod config;
mod engine;
mod model;
mod sign;
mod spot;

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

/// A fill and what Binance, the fairs, and Predict's own mid showed when it came in.
struct PendingTag {
    fill: FillRecord,
    ln_mid: f64,
    move_1s_bps: f64,
    fairs: spot::Fairs,
    /// Predict's mid move over the second before the fill, in cents, signed
    /// in the fill's favour (+ = the market moved our way).
    pmove_c: Option<f64>,
}

/// A market's execution lane.
#[derive(Default)]
struct Lane {
    busy: bool,
    queued: Option<Vec<Action>>,
}

struct App {
    cfg: Config,
    client: Arc<Client>,
    engine: Mutex<Engine>,
    state: Mutex<AdapterState>,
    journal: Mutex<Journal>,
    /// Every raw frame, only with `PK_PRED_RAW=yes` (it writes ~2 GB an hour).
    raw: Mutex<Option<Journal>>,
    kalshi: Mutex<Option<FeedHandle>>,
    binance: Mutex<Option<FeedHandle>>,
    binance_perp: Mutex<Option<FeedHandle>>,
    /// Kalshi vs the Binance-informed fairs, scored per settled window.
    scores: Mutex<spot::FairScores>,
    /// Woken by every Binance, Kalshi and Predict message: the strategy
    /// evaluates on data, not on a timer.
    wake: tokio::sync::Notify,
    /// Per market: one requote in flight, the newest target queued behind it.
    lanes: Mutex<std::collections::HashMap<String, Lane>>,
    /// Cost of orders passed by the risk check but not yet recorded, so lanes
    /// submitting at once cannot together pass the open cap.
    reserved_usd: Mutex<f64>,
    /// Bumped by every cancel-all: a lane that submitted across one removes its bids.
    cancel_epoch: std::sync::atomic::AtomicU64,
    /// Live fills waiting `FILL_TAG_AFTER_MS` for the Binance move after them.
    fill_tags: Mutex<Vec<PendingTag>>,
    /// Submits awaiting their answer, per market: an order the wallet reports
    /// meanwhile is that submit's own (its event beats the HTTP answer by
    /// ~300 ms), not one to adopt.
    submitting: Mutex<std::collections::HashMap<String, u32>>,
    /// The wallet's USDT and when it was read: bids beyond it less resting
    /// bids are skipped here (1,830 venue refusals on 10-27).
    wallet_usdt: Mutex<Option<(f64, i64)>>,
    kalshi_http: reqwest::Client,
    kalshi_signer: Arc<KalshiSigner>,
    markouts: Mutex<MarkoutTracker>,
    series_ms: Mutex<i64>,
    /// Each market's rules as last discovered, for telling a rule change
    /// from the start price Predict sets when the window opens.
    contracts: Mutex<std::collections::HashMap<String, NormalizedContract>>,
    /// Until when new first legs wait after Predict refused an order for
    /// insufficient collateral (free USDT below the total bid).
    collateral_pause_until: Mutex<i64>,
}

impl App {
    fn raw(&self, kind: &str, value: impl FnOnce() -> Value) {
        if let Some(raw) = self.raw.lock().as_mut() {
            let _ = raw.record(kind, value());
        }
    }

    fn record(&self, kind: &str, value: Value) {
        if let Err(error) = self.journal.lock().record(kind, value) {
            tracing::error!(error = %error, "journal_write");
        }
    }

    fn mark_open_cancelled(&self, status: &str) -> usize {
        let mut state = self.state.lock();
        let mut removed = 0;
        for order in state
            .orders
            .iter_mut()
            .filter(|order| order.status == "submitted" || order.status == "shadow_open")
        {
            order.status = status.into();
            order.updated_ms = now_ms();
            removed += 1;
        }
        state.recount();
        state.risk.save();
        removed
    }

    async fn cancel_all(&self, reason: &str) -> Value {
        self.state.lock().ready = false;
        self.cancel_epoch.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        // Quote from scratch afterwards, not at the next refresh.
        self.engine.lock().forget_quotes();
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

    /// Removes resting quotes off book: those of `only`'s markets, by the order
    /// ids we tracked (one request, no listing), or every quote on the account
    /// (lists, then removes). Removal is idempotent, so it is retried on a
    /// transport error; only `REMOVE_ATTEMPTS` failures in a row latch (one
    /// dropped request latched the live adapter for six hours on 10-27).
    /// Orders we never learned an id for are left to `orphan_sweep_loop`.
    async fn fast_remove_quotes(&self, reason: &str, only: Option<&[model::PredictMarket]>) {
        let markets: Option<std::collections::HashSet<String>> =
            only.map(|markets| markets.iter().map(|m| m.id.to_string()).collect());
        let ids: Option<Vec<String>> = markets.as_ref().map(|markets| {
            self.state
                .lock()
                .orders
                .iter()
                .filter(|o| o.status == "submitted" && !o.simulated && markets.contains(&o.market))
                .map(|o| o.id.clone())
                .collect()
        });
        let mut attempts = 0;
        if self.cfg.mode == VenueMode::Live {
            let mut failure = None;
            for attempt in 0..REMOVE_ATTEMPTS {
                if attempt > 0 {
                    tokio::time::sleep(Duration::from_millis(REMOVE_RETRY_MS * attempt as u64)).await;
                }
                attempts = attempt + 1;
                let removed = match &ids {
                    Some(ids) if ids.is_empty() => Ok(json!({"removed": 0})),
                    Some(ids) => self.client.remove(ids).await,
                    None => self.client.cancel_all_off_book().await,
                };
                match removed {
                    Ok(_) => {
                        failure = None;
                        break;
                    }
                    Err(error) => failure = Some(error),
                }
            }
            if let Some(error) = failure {
                self.state.lock().latch(format!("fast quote removal failed: {}", public_error(&error)));
            }
        }
        let now = now_ms();
        let mut state = self.state.lock();
        for order in state.orders.iter_mut().filter(|o| {
            (o.status == "submitted" || o.status == "shadow_open")
                && markets.as_ref().is_none_or(|markets| markets.contains(&o.market))
        }) {
            order.status = "removed_off_book".into();
            order.updated_ms = now;
        }
        // Wallet reconciliation remains the authority after an ambiguous ack.
        state.recount();
        state.risk.save();
        drop(state);
        self.record("fast_remove", json!({"reason":reason,"markets":markets,"attempts":attempts}));
    }

    /// One market's actions: a requote removes its bids, then submits the new
    /// ones together; a withdrawal only removes.
    async fn run_batch(&self, batch: Vec<Action>) {
        let epoch = self.cancel_epoch.load(std::sync::atomic::Ordering::SeqCst);
        let requoted: Option<model::PredictMarket> = batch.iter().find_map(|a| match a {
            Action::Maker { market, .. } => Some(market.clone()),
            _ => None,
        });
        let Some(requoted) = requoted else {
            for action in batch {
                self.execute(action).await;
            }
            return;
        };
        self.fast_remove_quotes("requote", Some(std::slice::from_ref(&requoted))).await;
        let makers = batch.into_iter().filter(|a| matches!(a, Action::Maker { .. }));
        futures_util::future::join_all(makers.map(|action| self.execute(action))).await;
        // A cancel-all ran meanwhile: what this lane just placed goes too.
        if self.cancel_epoch.load(std::sync::atomic::Ordering::SeqCst) != epoch {
            self.fast_remove_quotes("cancel-all crossed a requote", Some(std::slice::from_ref(&requoted))).await;
        }
    }

    async fn execute(&self, action: Action) {
        match action {
            Action::CancelAll { reason } => {
                self.cancel_all(&reason).await;
            }
            Action::Maker { market, token, outcome, price, quantity } => {
                self.place(&market, &token, &outcome, price, quantity, "maker", 0.0).await;
            }
            Action::Withdraw { market } => {
                self.fast_remove_quotes("withdraw", Some(std::slice::from_ref(&market))).await;
            }
            Action::Taker { market, token, outcome, price, quantity, edge } => {
                // Remove maker quotes before a marketable order to avoid self-trading.
                self.fast_remove_quotes("suspend maker before taker", None).await;
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
        let (new_market, pairing) = {
            let mut state = self.state.lock();
            state.recount();
            let held = state.positions.get(&market_id).cloned().unwrap_or_default();
            let new_market = !state.orders.iter().any(|order| {
                order.market == market_id && (order.status == "submitted" || order.status == "shadow_open")
            }) && held.is_flat();
            // A maker bid on the side opposite a one-sided position pairs it.
            let pairing = lane == "maker"
                && if outcome == "YES" { held.no_qty > held.yes_qty + 1e-9 } else { held.yes_qty > held.no_qty + 1e-9 };
            (new_market, pairing)
        };
        if !pairing && now_ms() < *self.collateral_pause_until.lock() {
            self.record("risk_reject", json!({"market":market.id,"lane":lane,"reason":"collateral pause"}));
            return;
        }
        // The check counts orders other lanes have passed but not recorded,
        // and reserves this one, in one step.
        let checked = {
            let mut state = self.state.lock();
            let mut reserved = self.reserved_usd.lock();
            state.risk.state.open_usd += *reserved;
            let mut checked =
                if pairing { state.risk.check_pairing_order(cost) } else { state.risk.check_order(cost, new_market) };
            state.risk.state.open_usd -= *reserved;
            // Predict refuses a bid when the wallet's USDT is under every
            // resting bid plus it: refuse it here first.
            if checked.is_ok()
                && self.cfg.mode == VenueMode::Live
                && let Some((usdt, read_ms)) = *self.wallet_usdt.lock()
                && now_ms() - read_ms <= WALLET_STALE_MS
            {
                let resting: f64 =
                    state.orders.iter().filter(|o| o.status == "submitted").map(|o| o.price * o.quantity).sum();
                if resting + *reserved + cost > usdt - self.cfg.collateral_margin_usd {
                    checked = Err(format!("free collateral: {:.2} USDT, {:.2} resting", usdt, resting + *reserved));
                }
            }
            if checked.is_ok() {
                *reserved += cost;
            }
            checked
        };
        if let Err(reason) = checked {
            self.record("risk_reject", json!({"market":market.id,"lane":lane,"reason":reason}));
            return;
        }
        // Released however the submit ends (recorded orders count via recount).
        struct Release<'a>(&'a Mutex<f64>, f64);
        impl Drop for Release<'_> {
            fn drop(&mut self) {
                let mut reserved = self.0.lock();
                *reserved = (*reserved - self.1).max(0.0);
            }
        }
        let _release = Release(&self.reserved_usd, cost);
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
                state.push_order(record.clone());
                state.recount();
                state.risk.save();
            }
            self.record("shadow_order", serde_json::to_value(record).unwrap_or_default());
            if lane == "taker" {
                self.simulate_shadow_fills();
            }
            return;
        }
        // In flight until this returns, so its own wallet event is not adopted.
        struct Submitting<'a>(&'a Mutex<std::collections::HashMap<String, u32>>, String);
        impl Drop for Submitting<'_> {
            fn drop(&mut self) {
                let mut map = self.0.lock();
                if let Some(n) = map.get_mut(&self.1) {
                    *n = n.saturating_sub(1);
                    if *n == 0 {
                        map.remove(&self.1);
                    }
                }
            }
        }
        *self.submitting.lock().entry(market_id.clone()).or_default() += 1;
        let _submitting = Submitting(&self.submitting, market_id.clone());
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
                // Adopted from its wallet event already (the event can beat
                // this answer): replace that record rather than add a second.
                match state.orders.iter_mut().find(|o| o.id == record.id) {
                    Some(existing) => *existing = record.clone(),
                    None => state.push_order(record.clone()),
                }
                state.recount();
                // An accepted order ends a run of rejections.
                state.risk.state.consecutive_rejections = 0;
                state.risk.save();
                drop(state);
                self.record("order_submitted", json!({"order":record,"edge":edge}));
            }
            Err(error) => {
                // With its causes ("{:#}"): "error sending request" alone hides why.
                let reason = public_error(&format!("{error:#}"));
                let mut state = self.state.lock();
                // Not enough free USDT for the bids resting plus this one is the
                // venue's own cap, not a broken order path: pause first legs and
                // let funds free up, without counting toward the latch.
                if is_collateral_refusal(&reason) {
                    *self.collateral_pause_until.lock() = now_ms() + COLLATERAL_PAUSE_MS;
                } else if reason.contains("HTTP 429") {
                    // The venue's rate limit, not a refused order: the client
                    // backs off every call; it does not count toward the latch.
                } else {
                    state.risk.state.consecutive_rejections += 1;
                    let _ = state.risk.check_latches();
                }
                state.risk.save();
                drop(state);
                self.record("order_rejected", json!({"market":market.id,"lane":lane,"reason":reason}));
            }
        }
    }

    /// Keeps a fill with Binance's move and Predict's mid move before it, to
    /// journal with the move after. The mid move is captured now: 5 s on, the
    /// trail no longer reaches back to the second before the fill.
    fn tag_fill(&self, fill: FillRecord) {
        let mut resting = self.state.lock().orders.clone();
        let (ctx, pmove_c) = {
            let engine = self.engine.lock();
            let Some(ctx) =
                engine.fill_context(&fill.market, fill.time_ms, self.cfg.blend_w, resting.make_contiguous())
            else {
                return;
            };
            let sign = if fill.side.eq_ignore_ascii_case("YES") { 1.0 } else { -1.0 };
            let pmove_c = engine.predict_move(&fill.market, fill.time_ms, 1_000).map(|m| sign * m * 100.0);
            (ctx, pmove_c)
        };
        self.fill_tags.lock().push(PendingTag {
            fill,
            ln_mid: ctx.ln_mid,
            move_1s_bps: ctx.move_1s_bps,
            fairs: ctx.fairs,
            pmove_c,
        });
    }

    /// Predict's YES mid excluding our resting orders, in the fill's side
    /// terms (raw mid when the exclusion empties a side).
    fn fill_side_mid(&self, market: &str, side: &str) -> Option<f64> {
        let mut resting = self.state.lock().orders.clone();
        let (ex_own, precision) = {
            let engine = self.engine.lock();
            let precision = engine
                .markets()
                .iter()
                .find(|m| m.id.to_string() == market)
                .map(|m| m.decimal_precision);
            (engine.mid_ex_own(market, resting.make_contiguous()), precision)
        };
        let mid = ex_own.or_else(|| {
            self.state.lock().books.get(market).copied().and_then(pk_venue_core::Book::mid)
        })?;
        if side.eq_ignore_ascii_case("YES") {
            Some(mid)
        } else {
            precision.map(|dp| model::complement_price(mid, dp))
        }
    }

    /// Journals each fill `FILL_TAG_AFTER_MS` on as `fill_tag`: Binance's
    /// move in the second before and the 5 s after, Predict's own mid move in
    /// the second before, all signed so that positive is in the fill's favour
    /// (rising for YES), and the fairs. Fills against a falling (YES) or
    /// rising (NO) spot are the adverse ones.
    fn resolve_fill_tags(&self, now: i64) {
        let due: Vec<PendingTag> = {
            let mut tags = self.fill_tags.lock();
            let (due, keep): (Vec<_>, Vec<_>) = tags.drain(..).partition(|t| now - t.fill.time_ms >= FILL_TAG_AFTER_MS);
            *tags = keep;
            due
        };
        for tag in due {
            let after = self.engine.lock().spot_ln(&tag.fill.market, now).map(|ln| (ln - tag.ln_mid) * 1e4);
            let sign = if tag.fill.side.eq_ignore_ascii_case("YES") { 1.0 } else { -1.0 };
            self.record(
                "fill_tag",
                json!({
                    "order_id": tag.fill.order_id, "fill_id": tag.fill.id, "market": tag.fill.market,
                    "side": tag.fill.side, "price": tag.fill.price, "quantity": tag.fill.quantity,
                    "fill_ms": tag.fill.time_ms,
                    "spot_before_bps": sign * tag.move_1s_bps,
                    "spot_after_5s_bps": after.map(|a| sign * a),
                    "pmove_c": tag.pmove_c,
                    "kalshi": tag.fairs.kalshi, "adjusted": tag.fairs.adjusted, "binance": tag.fairs.spot,
                    "blend": tag.fairs.blend, "anchored": tag.fairs.anchored,
                }),
            );
        }
    }

    /// One scorecard sample per live market with the established fairs and
    /// a Predict mid (paired, so the sources are compared on the same
    /// moments). The blend and anchored candidates score when available.
    fn sample_fairs(&self, now: i64) {
        let mut resting = self.state.lock().orders.clone();
        let rows = self.engine.lock().signal_rows(now, self.cfg.blend_w, resting.make_contiguous());
        let mut scores = self.scores.lock();
        for row in rows {
            let (Some(adjusted), Some(spot), Some(mid)) = (row.fairs.adjusted, row.fairs.spot, row.predict_mid) else {
                continue;
            };
            scores.sample(&row.market, row.close_ms, spot::Sample {
                ms: now,
                fairs: [
                    row.fairs.kalshi,
                    adjusted,
                    spot,
                    row.fairs.blend.unwrap_or(f64::NAN),
                    row.fairs.anchored.unwrap_or(f64::NAN),
                    mid,
                ],
            });
        }
    }

    /// The Binance signal for the status page: the guard per asset, every
    /// live market's fairs, and the scorecard.
    fn signal_status(&self) -> Value {
        let now = now_ms();
        let mut resting = self.state.lock().orders.clone();
        let engine = self.engine.lock();
        let rows: Vec<Value> = engine
            .signal_rows(now, self.cfg.blend_w, resting.make_contiguous())
            .into_iter()
            .map(|r| {
                json!({
                    "market": r.market, "label": r.label, "asset": r.asset, "tau_s": r.tau_s,
                    "strike": r.strike, "spot": r.spot, "kalshi": r.fairs.kalshi,
                    "adjusted": r.fairs.adjusted, "binance": r.fairs.spot,
                    "blend": r.fairs.blend, "anchored": r.fairs.anchored,
                    "predict_mid": r.predict_mid, "pmove_1s_c": r.pmove_1s_c,
                    "up_off": r.guarded.0, "down_off": r.guarded.1,
                    "book_up_off": r.book_guarded.0, "book_down_off": r.book_guarded.1,
                })
            })
            .collect();
        json!({
            "config": {
                // The lanes this process sends (a contract's grade only allows them).
                "maker": self.cfg.maker, "taker": self.cfg.taker,
                "guard": self.cfg.binance_guard, "pause_bps": self.cfg.binance_pause_bps,
                "pause_ms": self.cfg.binance_pause_ms, "pause_gap": self.cfg.binance_pause_gap,
                "book_guard": self.cfg.book_guard, "book_pause_c": self.cfg.book_pause_c,
                "book_pause_window_ms": self.cfg.book_pause_window_ms,
                "book_pause_ms": self.cfg.book_pause_ms, "blend_w": self.cfg.blend_w,
                "regime_start": self.cfg.regime_start,
                "open_fair": self.cfg.open_fair, "pair_fair": self.cfg.pair_fair,
            },
            "assets": engine.spot.status(now),
            "sigma": engine.sigma_status(now),
            "guard_pauses": engine.guard_pauses,
            "book_pauses": engine.book_pauses,
            "markets": rows,
            "scores": self.scores.lock().status(),
        })
    }

    fn simulate_shadow_fills(&self) {
        if self.cfg.mode != VenueMode::Shadow {
            return;
        }
        let now = now_ms();
        let mut fills = Vec::new();
        {
            let markets: std::collections::HashMap<String, model::PredictMarket> =
                self.engine.lock().markets().into_iter().map(|m| (m.id.to_string(), m)).collect();
            let precision: std::collections::HashMap<String, u32> =
                markets.iter().map(|(k, m)| (k.clone(), m.decimal_precision)).collect();
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
                if let Some(m) = markets.get(&fill.market) {
                    state.position_meta.entry(fill.market.clone()).or_insert_with(|| position_meta(m, now));
                }
                state.book_fill(fill);
            }
            state.risk.save();
        }
        for fill in fills {
            let mid = self.fill_side_mid(&fill.market, &fill.side);
            self.markouts.lock().watch(&fill.market, &fill.side, fill.price, fill.time_ms, mid);
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
        client.set_rate_limit(cfg.max_requests_per_s);
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
    let expiration = (now_ms() / 1000) as u64 + config::MIN_QUOTE_TTL_S;
    let started = std::time::Instant::now();
    let submitted = client
        .submit(Submit {
            market: &market,
            token_id: &token,
            side: Side::Buy,
            price: 0.01,
            quantity: engine::shares_for(0.01, cfg.min_order_usd),
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
    let mut state = AdapterState::new("predict", cfg.mode, cfg.fingerprint.clone(), risk);
    state.pnl = pk_venue_core::service::PnlBook::open(cfg.state_dir.join("pnl.json"), cfg.sim_capital);
    // Positions held before a restart (live, the wallet still holds them and
    // redemption pays them; this keeps their cost basis in the P&L).
    state.restore_open();
    let pem = std::fs::read_to_string(&cfg.kalshi_private_key_path)?;
    let kalshi_signer = Arc::new(KalshiSigner::from_pem(&cfg.kalshi_api_key_id, &pem)?);
    let app = Arc::new(App {
        journal: Mutex::new(Journal::open(cfg.state_dir.join("journal"))?),
        raw: Mutex::new(if cfg.raw { Some(Journal::open(cfg.state_dir.join("raw"))?) } else { None }),
        engine: Mutex::new({
            let mut engine = Engine::new();
            engine.spot.perp_primary = cfg.binance_perp_ws_url.is_some();
            engine.sigma_floor = cfg.sigma_floor;
            engine.restore_implied(load_implied(&cfg.state_dir.join(IMPLIED_SIGMA_FILE)));
            engine
        }),
        state: Mutex::new(state),
        kalshi: Mutex::new(None),
        binance: Mutex::new(None),
        binance_perp: Mutex::new(None),
        scores: Mutex::new(spot::FairScores::load(&cfg.state_dir.join(FAIR_SCORES_FILE))),
        wake: tokio::sync::Notify::new(),
        lanes: Mutex::new(Default::default()),
        reserved_usd: Mutex::new(0.0),
        cancel_epoch: std::sync::atomic::AtomicU64::new(0),
        fill_tags: Mutex::new(Vec::new()),
        submitting: Mutex::new(Default::default()),
        wallet_usdt: Mutex::new(None),
        kalshi_http: reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(3))
            .timeout(Duration::from_secs(8))
            .build()?,
        kalshi_signer,
        cfg,
        client,
        markouts: Mutex::new(MarkoutTracker::new()),
        series_ms: Mutex::new(0),
        contracts: Mutex::new(Default::default()),
        collateral_pause_until: Mutex::new(0),
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
            app.raw("kalshi", || json!({"raw":text}));
            if text.contains("\"type\":\"reconnect\"") || text.contains("\"type\":\"resync\"") {
                let mut state = app.state.lock();
                state.ready = false;
                state.feeds.entry("kalshi".into()).or_default().sequence_gaps +=
                    text.contains("resync") as u64;
            } else if let Some((ticker, fair)) = app.engine.lock().kalshi_frame(&text) {
                {
                    let mut state = app.state.lock();
                    state.kalshi_fairs.insert(ticker, fair);
                    let feed = state.feeds.entry("kalshi".into()).or_default();
                    feed.connected = true;
                    feed.last_message_ms = now;
                }
                app.wake.notify_one();
            }
        }
    });
    let kalshi = spawn_feed(
        app.cfg.kalshi_ws_url.clone(),
        FeedKind::Kalshi { signer: app.kalshi_signer.clone() },
        emit,
    );
    kalshi.set_wanted(kalshi_wanted(&app));
    *app.kalshi.lock() = Some(kalshi);

    // Binance: the guard and the Binance-informed fairs (see `spot`); the
    // perp drives when connected, spot sets the level.
    let weak = Arc::downgrade(&app);
    let emit = Arc::new(move |venue: char, text: String| {
        let Some(app) = weak.upgrade() else { return };
        let perp = venue == 'f';
        let updated = app.engine.lock().binance_frame(&app.cfg, &text, perp);
        {
            let mut state = app.state.lock();
            let feed = state.feeds.entry(if perp { "binance_perp" } else { "binance" }.into()).or_default();
            feed.connected = true;
            feed.last_message_ms = now_ms();
        }
        if updated {
            app.wake.notify_one();
        }
    });
    let wanted: std::collections::BTreeSet<String> = app.cfg.assets.iter().map(|a| format!("{}usdt@bookTicker", a.to_lowercase())).collect();
    let binance = spawn_feed(app.cfg.binance_ws_url.clone(), FeedKind::Binance { futures: false }, emit.clone());
    binance.set_wanted(wanted.clone());
    *app.binance.lock() = Some(binance);
    if let Some(url) = app.cfg.binance_perp_ws_url.clone() {
        let perp = spawn_feed(url, FeedKind::Binance { futures: true }, emit);
        perp.set_wanted(wanted);
        *app.binance_perp.lock() = Some(perp);
    }

    tokio::spawn(predict_ws(app.clone()));
    tokio::spawn(discovery_loop(app.clone()));
    tokio::spawn(strategy_loop(app.clone()));
    tokio::spawn(housekeeping_loop(app.clone()));
    tokio::spawn(kill_loop(app.clone()));
    tokio::spawn(clock_loop(app.clone()));
    tokio::spawn(settlement_loop(app.clone()));
    tokio::spawn(settlement_accounting_loop(app.clone()));
    tokio::spawn(orphan_sweep_loop(app.clone()));
    tokio::spawn(fair_score_loop(app.clone()));
    tokio::spawn(wallet_loop(app.clone()));

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
    let mut contracts = std::collections::HashMap::new();
    let mut executable = Vec::new();
    for mut market in markets {
        contracts.insert(market.id.to_string(), market.normalized());
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
    // A market missing from this pass has closed (15-minute windows roll off
    // the OPEN list); only one present in both passes with different rules,
    // or a different Kalshi link, is a semantic change.
    // Predict sets the window's start price (`threshold`) when it opens;
    // that first value is not a rule change, a later different one is.
    let changed = {
        let state = app.state.lock();
        let before = app.contracts.lock();
        // Only a market executable in both passes counts: one that drops to
        // E2 (a transient Kalshi lookup failure, an empty link) is simply not
        // quoted, which is already the safe outcome (19:30Z latch, 10-26).
        state.matches.iter().filter(|(_, old)| old.grade != MatchGrade::E2).any(|(id, old)| {
            decisions.get(id).filter(|new| new.grade != MatchGrade::E2).is_some_and(|new| {
                old.kalshi_ticker != new.kalshi_ticker
                    || (old.venue_fingerprint != new.venue_fingerprint
                        && match (before.get(id), contracts.get(id)) {
                            (Some(a), Some(b)) => rules_changed(a, b),
                            _ => true,
                        })
            })
        })
    };
    *app.contracts.lock() = contracts;
    if changed {
        app.state.lock().latch("Predict market rules, ticker link, or outcome mapping changed");
        app.cancel_all("semantic match changed").await;
    }
    {
        let mut state = app.state.lock();
        state.matches = decisions.clone();
        // A pass that succeeded clears an earlier failed one.
        state.degraded.clear();
    }
    app.record("discovery", json!({"markets":executable,"decisions":decisions}));
    app.engine.lock().set_markets(executable);
    if let Some(feed) = app.kalshi.lock().as_ref() {
        feed.set_wanted(kalshi_wanted(app));
    }
    Ok(())
}

/// A rule change between two passes over one market, ignoring the start
/// price appearing for the first time (it is set at the window's open).
fn rules_changed(before: &NormalizedContract, after: &NormalizedContract) -> bool {
    if before.threshold.is_some() && before.threshold != after.threshold {
        return true;
    }
    let mut a = before.clone();
    let mut b = after.clone();
    a.threshold = None;
    b.threshold = None;
    a != b
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
    let underlying = crate::model::ASSETS.into_iter().find(|asset| ticker.contains(asset)).unwrap_or("");
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

/// How far ahead the Kalshi and Predict sockets follow upcoming windows.
const FOLLOW_AHEAD_MS: i64 = 30 * 60 * 1000;

/// Kalshi tickers of windows open now or within `FOLLOW_AHEAD_MS` (Kalshi lists
/// the whole day ahead; following every one floods the socket with deltas).
fn kalshi_wanted(app: &Arc<App>) -> std::collections::BTreeSet<String> {
    app.engine
        .lock()
        .upcoming(now_ms(), FOLLOW_AHEAD_MS)
        .into_iter()
        .filter_map(|m| m.kalshi_ticker)
        .collect()
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
    // Evaluate on every Binance, Kalshi and Predict message; the timer only
    // covers time-driven rules (window edges, pauses ending, refreshes).
    let mut fallback = tokio::time::interval(Duration::from_millis(100));
    loop {
        tokio::select! {
            _ = app.wake.notified() => {}
            _ = fallback.tick() => {}
        }
        let positions = app.state.lock().positions.clone();
        let mut resting = app.state.lock().orders.clone();
        let actions = app.engine.lock().evaluate(&app.cfg, &positions, resting.make_contiguous());
        dispatch(&app, actions);
    }
}

/// Hands each market's actions to its lane; cancel-all and taker actions run
/// on their own. A busy lane keeps only the newest target queued.
fn dispatch(app: &Arc<App>, actions: Vec<Action>) {
    let mut batches: Vec<(String, Vec<Action>)> = Vec::new();
    for action in actions {
        let market = match &action {
            Action::Maker { market, .. } | Action::Withdraw { market } => market.id.to_string(),
            Action::CancelAll { .. } | Action::Taker { .. } => {
                let app = app.clone();
                tokio::spawn(async move { app.execute(action).await });
                continue;
            }
        };
        match batches.iter_mut().find(|(m, _)| *m == market) {
            Some((_, batch)) => batch.push(action),
            None => batches.push((market, vec![action])),
        }
    }
    for (market, batch) in batches {
        let mut lanes = app.lanes.lock();
        let lane = lanes.entry(market.clone()).or_default();
        if lane.busy {
            lane.queued = Some(batch);
            continue;
        }
        lane.busy = true;
        drop(lanes);
        tokio::spawn(run_lane(app.clone(), market, batch));
    }
}

async fn run_lane(app: Arc<App>, market: String, mut batch: Vec<Action>) {
    loop {
        app.run_batch(batch).await;
        let mut lanes = app.lanes.lock();
        let lane = lanes.entry(market.clone()).or_default();
        match lane.queued.take() {
            Some(next) => batch = next,
            None => {
                lane.busy = false;
                return;
            }
        }
    }
}

/// Book-keeping off the hot path: shadow fills, markouts, marks, the series,
/// the scorecard, readiness and the heartbeat.
async fn housekeeping_loop(app: Arc<App>) {
    let mut interval = tokio::time::interval(Duration::from_millis(100));
    let (mut last_score_ms, mut last_implied_ms) = (0, now_ms());
    loop {
        interval.tick().await;
        if now_ms() - last_score_ms >= SCORE_SAMPLE_MS {
            last_score_ms = now_ms();
            app.sample_fairs(last_score_ms);
        }
        if now_ms() - last_implied_ms >= IMPLIED_SAVE_MS {
            last_implied_ms = now_ms();
            save_implied(&app.cfg.state_dir.join(IMPLIED_SIGMA_FILE), &app.engine.lock().implied_snapshot());
        }
        app.simulate_shadow_fills();
        let now = now_ms();
        app.resolve_fill_tags(now);
        {
            // Markouts compare against the venue mid excluding our own resting
            // orders (raw mid when the exclusion empties a side).
            let mut resting = app.state.lock().orders.clone();
            let books = app.state.lock().books.clone();
            let engine = app.engine.lock();
            let markets = engine.markets();
            let mut mids = std::collections::HashMap::new();
            for market in &markets {
                let id = market.id.to_string();
                let mid = engine.mid_ex_own(&id, resting.make_contiguous()).or_else(|| {
                    books.get(&id).copied().and_then(pk_venue_core::Book::mid)
                });
                if let Some(mid) = mid {
                    mids.insert((id.clone(), "YES".into()), mid);
                    mids.insert((id, "NO".into()), model::complement_price(mid, market.decimal_precision));
                }
            }
            drop(engine);
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

const RECONCILE_FAILURES_TO_LATCH: u32 = 3;

fn position_meta(m: &model::PredictMarket, now: i64) -> pk_venue_core::service::PositionMeta {
    pk_venue_core::service::PositionMeta {
        close_ms: m.close_ms(),
        kalshi_ticker: m.kalshi_ticker.clone(),
        label: m.category_slug.clone().unwrap_or_else(|| m.id.to_string()),
        opened_ms: now,
    }
}

/// Settle the books once a window closes: read its market from Predict
/// (`RESOLVED`, the Up/Yes outcome `WON` or `LOST`) and realise at $1 / $0.
/// Kalshi's result for the mirrored contract is the fallback when Predict has
/// not resolved after 20 min. Runs live too: a losing leg is never redeemed,
/// so redemption alone would leave it on the books (and its slot taken);
/// redemption only collects what won.
async fn settlement_accounting_loop(app: Arc<App>) {
    let mut interval = tokio::time::interval(Duration::from_secs(20));
    loop {
        interval.tick().await;
        let due = app.state.lock().due_settlement(now_ms(), 15_000);
        for (market, meta) in due {
            let outcome = match app.client.market(&market).await {
                Ok(value) => model::resolved_yes(&value).map(|won| (won, "predict")),
                Err(error) => {
                    app.record("settle_error", json!({"market": market, "error": public_error(&error)}));
                    None
                }
            };
            let stale = meta.close_ms.is_some_and(|c| now_ms() - c > 20 * 60_000);
            let outcome = match (outcome, stale, meta.kalshi_ticker.as_deref()) {
                (Some(o), _, _) => Some(o),
                (None, true, Some(ticker)) => kalshi_result(&app, ticker).await.map(|won| (won, "kalshi (fallback)")),
                _ => None,
            };
            let Some((yes_won, source)) = outcome else { continue };
            let settled = app.state.lock().settle_position(&market, yes_won, source);
            if let Some(record) = settled {
                app.record("position_settled", record);
            }
        }
    }
}

/// Kalshi's `result` for a settled contract: Some(true) for "yes".
/// Reads the wallet's USDT every `WALLET_POLL_S` for the free-collateral check.
async fn wallet_loop(app: Arc<App>) {
    if app.cfg.mode != VenueMode::Live || app.cfg.privy_private_key.is_none() {
        return;
    }
    let mut interval = tokio::time::interval(Duration::from_secs(WALLET_POLL_S));
    let mut chain: Option<chain::PredictChain> = None;
    loop {
        interval.tick().await;
        if chain.is_none() {
            chain = settle_chain(&app).await.ok();
        }
        let Some(c) = chain.as_ref() else { continue };
        match c.balances().await {
            Ok((usdt, _gas)) => *app.wallet_usdt.lock() = Some((usdt, now_ms())),
            Err(error) => {
                app.record("wallet_error", json!({"error": public_error(&error)}));
                chain = None;
            }
        }
    }
}

/// Requotes remove only the order ids they tracked. An order the venue holds
/// but we have no id for (a submit whose answer was lost) is removed once two
/// sweeps in a row, `ORPHAN_SWEEP_S` apart, saw it untracked, so a submit
/// still awaiting its answer is safe.
async fn orphan_sweep_loop(app: Arc<App>) {
    if app.cfg.mode != VenueMode::Live {
        return;
    }
    let mut interval = tokio::time::interval(Duration::from_secs(ORPHAN_SWEEP_S));
    let mut seen: std::collections::HashSet<String> = Default::default();
    loop {
        interval.tick().await;
        let Ok(open) = app.client.open_order_ids().await else { continue };
        let tracked: std::collections::HashSet<String> = app
            .state
            .lock()
            .orders
            .iter()
            .filter(|o| o.status == "submitted")
            .map(|o| o.id.clone())
            .collect();
        let untracked: std::collections::HashSet<String> =
            open.into_iter().filter(|id| !tracked.contains(id)).collect();
        let orphans: Vec<String> = untracked.intersection(&seen).cloned().collect();
        if !orphans.is_empty() {
            let result = app.client.remove(&orphans).await.map_err(|e| public_error(&e));
            app.record("orphans_removed", json!({"ids": orphans, "ok": result.is_ok()}));
        }
        seen = untracked;
    }
}

/// Scores the fair samples of closed windows at Predict's resolution; a
/// window unresolved `SCORE_GIVE_UP_MS` after its close is dropped unscored.
async fn fair_score_loop(app: Arc<App>) {
    let mut interval = tokio::time::interval(Duration::from_secs(60));
    loop {
        interval.tick().await;
        let now = now_ms();
        let due = app.scores.lock().due(now, 90_000);
        if due.is_empty() {
            continue;
        }
        for (market, close_ms) in due {
            let resolved = match app.client.market(&market).await {
                Ok(value) => model::resolved_yes(&value),
                Err(_) => None,
            };
            let mut scores = app.scores.lock();
            match resolved {
                Some(yes_won) => scores.resolve(&market, yes_won),
                None if now - close_ms > SCORE_GIVE_UP_MS => scores.drop_market(&market),
                None => {}
            }
        }
        app.scores.lock().save(&app.cfg.state_dir.join(FAIR_SCORES_FILE));
    }
}

async fn kalshi_result(app: &Arc<App>, ticker: &str) -> Option<bool> {
    let path = format!("/trade-api/v2/markets/{ticker}");
    let mut request =
        app.kalshi_http.get(format!("{}/markets/{ticker}", app.cfg.kalshi_rest_url.trim_end_matches('/')));
    for (key, value) in app.kalshi_signer.headers("GET", &path, None) {
        request = request.header(key, value);
    }
    let value = request.send().await.ok()?.error_for_status().ok()?.json::<Value>().await.ok()?;
    match value.pointer("/market/result").and_then(Value::as_str) {
        Some("yes") => Some(true),
        Some("no") => Some(false),
        _ => None,
    }
}

async fn settlement_loop(app: Arc<App>) {
    if app.cfg.privy_private_key.is_none() {
        return;
    }
    let mut interval = tokio::time::interval(Duration::from_secs(30));
    let mut failures = 0u32;
    loop {
        interval.tick().await;
        match app.client.positions().await {
            Ok(positions) => {
                failures = 0;
                app.record("position_reconcile", json!({"positions": positions}));
                settle_positions(&app, &positions).await;
            }
            Err(error) => {
                failures += 1;
                let reason = format!("position reconciliation failed: {}", public_error(&error));
                app.record("reconcile_error", json!({"reason": reason, "consecutive": failures}));
                // One failed request is a network blip. Live latches after
                // three in a row (90 s blind to the wallet); shadow holds no
                // real positions and only records.
                if app.cfg.mode == VenueMode::Live && failures >= RECONCILE_FAILURES_TO_LATCH {
                    app.state.lock().latch(reason);
                    app.cancel_all("position reconciliation failed").await;
                }
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
                    // Usually already settled by the accounting loop; if not,
                    // the redeemed (winning) leg settles it here, once.
                    let settled =
                        app.state.lock().settle_position(&candidate.market_id, candidate.index_set == 1, "predict (redeemed)");
                    app.record("redeemed", json!({"candidate": candidate, "receipt": receipt, "settled": settled}));
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
                    let shares = candidate
                        .amount_wei
                        .parse::<f64>()
                        .ok()
                        .map(|wei| wei / 1e18)
                        .unwrap_or(0.0);
                    let realised = app.state.lock().realise_merge(&candidate.market_id, shares);
                    app.record(
                        "merged",
                        json!({"candidate": candidate, "receipt": receipt, "realised": realised}),
                    );
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
    let mut subscribed = std::collections::HashSet::new();
    subscribe_new(app, &mut ws, &mut subscribed).await?;
    if let Some(jwt) = app.client.jwt() {
        let id = app.client.next_request_id();
        ws.send(Message::Text(
            json!({"method":"subscribe","requestId":id,"params":[format!("predictWalletEvents/{jwt}")]})
                .to_string()
                .into(),
        ))
        .await?;
    }
    let mut follow = tokio::time::interval(Duration::from_secs(5));
    loop {
        let frame = tokio::select! {
            frame = ws.next() => frame,
            _ = follow.tick() => {
                subscribe_new(app, &mut ws, &mut subscribed).await?;
                continue;
            }
        };
        let Some(frame) = frame else { break };
        let frame = frame?;
        if frame.is_ping() {
            ws.send(Message::Pong(frame.into_data())).await?;
            continue;
        }
        if frame.is_close() {
            break;
        }
        let Some(text) = frame.to_text().ok() else { continue };
        // Empty and non-JSON frames (keep-alives) are skipped, not fatal:
        // failing the session on them reconnected every ~30 s.
        let Ok(value) = serde_json::from_str::<Value>(text) else {
            if !text.trim().is_empty() {
                let mut state = app.state.lock();
                state.feeds.entry("predict".into()).or_default().errors += 1;
            }
            continue;
        };
        // Predict's keep-alive is `{"type":"M","topic":"heartbeat","data":<ms>}`
        // about every 31 s; unanswered, the server closes the socket.
        if value.get("type").and_then(Value::as_str) == Some("H")
            || value.get("topic").and_then(Value::as_str) == Some("heartbeat")
            || value.get("method").and_then(Value::as_str) == Some("heartbeat")
        {
            let data = value.get("data").cloned().unwrap_or(Value::Null);
            ws.send(Message::Text(json!({"method":"heartbeat","data":data}).to_string().into())).await?;
            continue;
        }
        app.raw("predict", || json!({"raw":value.clone()}));
        if let Some((id, depth)) = model::depth_from_ws(&value) {
            if depth.top.valid() {
                let mut resting = app.state.lock().orders.clone();
                let top = depth.top;
                app.engine.lock().venue_book(id.clone(), depth, resting.make_contiguous());
                let mut state = app.state.lock();
                state.books.insert(id, top);
                let feed = state.feeds.entry("predict".into()).or_default();
                feed.last_message_ms = now_ms();
            }
        }
        handle_wallet_event(app, &value);
        // A book or a fill changes what to quote.
        app.wake.notify_one();
    }
    anyhow::bail!("Predict WebSocket closed")
}

/// Subscribe the Predict socket to every window open now or within
/// `FOLLOW_AHEAD_MS` that it does not follow yet (windows roll every 15 min).
async fn subscribe_new<S>(app: &Arc<App>, ws: &mut S, subscribed: &mut std::collections::HashSet<String>) -> anyhow::Result<()>
where
    S: futures_util::Sink<Message> + Unpin,
    S::Error: std::error::Error + Send + Sync + 'static,
{
    let markets = app.engine.lock().upcoming(now_ms(), FOLLOW_AHEAD_MS);
    for market in markets {
        let id = market.id.to_string();
        if !subscribed.insert(id.clone()) {
            continue;
        }
        let request = app.client.next_request_id();
        ws.send(Message::Text(
            json!({"method":"subscribe","requestId":request,"params":[format!("predictOrderbook/{id}")]})
                .to_string()
                .into(),
        ))
        .await?;
    }
    Ok(())
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
            let meta = app.engine.lock().markets().into_iter().find(|m| m.id.to_string() == record.market);
            let mut state = app.state.lock();
            if let Some(m) = meta {
                state.position_meta.entry(record.market.clone()).or_insert_with(|| position_meta(&m, record.time_ms));
            }
            if let Some(order) = state.orders.iter_mut().find(|order| order.id == record.order_id) {
                order.status = "filled".into();
                order.updated_ms = record.time_ms;
            }
            state.book_fill(&record);
            state.risk.save();
            drop(state);
            let mid = app.fill_side_mid(&record.market, &record.side);
            app.markouts.lock().watch(&record.market, &record.side, record.price, record.time_ms, mid);
            app.tag_fill(record);
        }
        "orderNotAccepted" => {
            let reason = data.get("reason").and_then(Value::as_str).unwrap_or("not_accepted").to_owned();
            let mut state = app.state.lock();
            if let Some(order) = state.orders.iter_mut().find(|order| order.id == order_id) {
                order.status = format!("rejected:{reason}");
                order.updated_ms = now_ms();
            }
            state.recount();
            // A maker quote the book moved through is refused post-only: the
            // venue doing its job, not a failing order path. Only other
            // refusals count toward the consecutive-rejection latch.
            if !is_post_only_refusal(&reason) {
                state.risk.state.consecutive_rejections += 1;
                let _ = state.risk.check_latches();
            }
            state.risk.save();
        }
        "orderExpired" | "orderCancelled" | "orderTransactionFailed" => {
            let status = if kind == "orderTransactionFailed" { "settlement_failed" } else { kind };
            let mut state = app.state.lock();
            let found = state.orders.iter_mut().find(|order| order.id == order_id).map(|order| {
                order.status = status.into();
                order.updated_ms = now_ms();
            });
            if found.is_some() {
                state.recount();
                state.risk.save();
            }
        }
        // Placement and other updates: an order of ours this process never
        // recorded is adopted (fills are not: a fill on an order never seen
        // still latches as unknown).
        _ => adopt_order(app, &order_id, data),
    }
}

/// The record for an order our wallet reports that this process has none
/// for: a submit whose answer was lost in transit though the venue accepted
/// it (10-27 14:16Z: "error sending request", accepted 80 ms later, filled
/// 13 s on and latched as unknown). Adopted, the next requote removes it by id
/// like any of ours, and its fill is known.
fn adopted_record(order_id: &str, data: &Value) -> Option<OrderRecord> {
    if order_id.is_empty() {
        return None;
    }
    let d = data.get("details").unwrap_or(data);
    let market = d.get("marketId").map(|v| v.as_str().map(str::to_owned).unwrap_or_else(|| v.to_string()))?;
    let number = |key: &str| {
        d.get(key).and_then(|v| v.as_str().and_then(|s| s.parse::<f64>().ok()).or_else(|| v.as_f64())).unwrap_or(0.0)
    };
    let now = now_ms();
    Some(OrderRecord {
        id: order_id.into(),
        client_id: "adopted".into(),
        market,
        lane: "maker".into(),
        side: d.get("outcome").and_then(Value::as_str).unwrap_or_default().into(),
        price: number("price"),
        quantity: number("quantity"),
        status: "submitted".into(),
        created_ms: now,
        updated_ms: now,
        simulated: false,
    })
}

fn adopt_order(app: &Arc<App>, order_id: &str, data: &Value) {
    if app.cfg.mode != VenueMode::Live {
        return;
    }
    let Some(record) = adopted_record(order_id, data) else { return };
    // A submit to this market awaiting its answer owns the order.
    if app.submitting.lock().contains_key(&record.market) {
        return;
    }
    let mut state = app.state.lock();
    if state.orders.iter().any(|o| o.id == order_id) {
        return;
    }
    state.push_order(record.clone());
    state.recount();
    state.risk.save();
    drop(state);
    app.record("order_adopted", json!({"order": record}));
}

const COLLATERAL_PAUSE_MS: i64 = 30_000;
/// The wallet's USDT is read this often, and trusted this long.
const WALLET_POLL_S: u64 = 15;
const WALLET_STALE_MS: i64 = 60_000;

/// Off-book quote removal: tries in all, and the backoff step between them.
const REMOVE_ATTEMPTS: u32 = 3;
const REMOVE_RETRY_MS: u64 = 250;
/// Seconds between orphan sweeps (an untracked order needs two in a row).
const ORPHAN_SWEEP_S: u64 = 15;

/// The fair scorecard: a sample every 5 s, kept in the state directory.
const SCORE_SAMPLE_MS: i64 = 5_000;
const SCORE_GIVE_UP_MS: i64 = 45 * 60_000;
const FAIR_SCORES_FILE: &str = "fair_scores.json";
/// The learned Kalshi-implied σ per asset, kept across restarts.
const IMPLIED_SIGMA_FILE: &str = "implied_sigma.json";
const IMPLIED_SAVE_MS: i64 = 30_000;
/// A fill's tag records Binance's move over this long after it.
const FILL_TAG_AFTER_MS: i64 = 5_000;

fn load_implied(path: &std::path::Path) -> std::collections::HashMap<String, (f64, i64)> {
    std::fs::read(path).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default()
}

fn save_implied(path: &std::path::Path, implied: &std::collections::HashMap<String, (f64, i64)>) {
    if let Ok(bytes) = serde_json::to_vec(implied) {
        let _ = std::fs::write(path, bytes);
    }
}

/// Predict's refusal for free USDT below the total bid amount.
fn is_collateral_refusal(reason: &str) -> bool {
    let r = reason.to_ascii_lowercase();
    r.contains("insufficient collateral") || r.contains("available balance is less")
}

/// Predict's refusal of a post-only order that would have crossed
/// (`rejectedPostOnly`).
fn is_post_only_refusal(reason: &str) -> bool {
    reason.to_ascii_lowercase().contains("postonly")
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
    let mut status = app.state.lock().status();
    status["signal"] = app.signal_status();
    Json(status)
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_order_our_wallet_reports_is_adopted_with_its_terms() {
        // The 10-27 14:16Z event: accepted though the submit errored in transit.
        let event = json!({"details": {"categorySlug": "bnb-updown-15m-1793110500", "marketId": 3872843,
            "outcome": "YES", "price": "0.400", "quantity": "3.000", "quantityFilled": "0.000"},
            "orderId": "6427353463"});
        let r = adopted_record("6427353463", &event).unwrap();
        assert_eq!((r.market.as_str(), r.side.as_str(), r.status.as_str()), ("3872843", "YES", "submitted"));
        assert!((r.price - 0.40).abs() < 1e-9 && (r.quantity - 3.0).abs() < 1e-9);
        assert!(adopted_record("", &event).is_none(), "no id");
        assert!(adopted_record("1", &json!({"details": {}})).is_none(), "no market");
    }

    #[test]
    fn post_only_refusals_do_not_count_as_rejections() {
        assert!(is_post_only_refusal("rejectedPostOnly"));
        assert!(!is_post_only_refusal("insufficientBalance"));
        assert!(is_collateral_refusal(
            "Predict HTTP 400: Insufficient collateral: available balance is less than the total bid amount."
        ));
        assert!(!is_collateral_refusal("Predict HTTP 400: invalid signature"));
    }

    #[test]
    fn start_price_set_at_the_open_is_not_a_rule_change() {
        let before = NormalizedContract { venue_market_id: "1".into(), close_ms: Some(900_000), ..Default::default() };
        let mut after = before.clone();
        after.threshold = Some("80064.715".into());
        assert!(!rules_changed(&before, &after), "first start price");
        let mut moved = after.clone();
        moved.threshold = Some("80100.0".into());
        assert!(rules_changed(&after, &moved), "a start price that changes");
        let mut other = after.clone();
        other.resolution_source = "PYTH".into();
        assert!(rules_changed(&after, &other), "any other field");
    }
}
