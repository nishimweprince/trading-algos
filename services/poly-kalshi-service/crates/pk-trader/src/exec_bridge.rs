//! The trader's real order path.
//!
//! - **Probe** (paper with `PK_ORDER_PROBE=on`): every paper entry is mirrored
//!   by a probe on the same token at the same moment (`pk_exec::probe`:
//!   post-only, under the best bid, cancelled at once), and probes also run on
//!   a cadence between decisions. They time the order path, but they do not
//!   feed the paper venue's latency: a post-only order skips the venue's taker
//!   hold, so it measured ~50 ms where a real FAK takes ~250 ms.
//! - **Latency** comes from live entries only: every FAK that got an answer
//!   (filled or killed) is one decision → acknowledgement round trip. Its
//!   changes go back to the engine as `latency` control lines, so the paper
//!   venue simulates the latency a taker actually has.
//! - **Live** (`PK_EXECUTION_MODE=live`): an entry is a real FAK buy
//!   (`pk_exec::live`). Its result goes back as a `live_fill` line. An
//!   [`ExitIntent`] (the active exit) is a FAK sell under the bid; what it sold
//!   goes back as a `live_exit` line with `reason: "fair"`. A
//!   reconciler reads the wallet every [`RECONCILE_EVERY`] and after every
//!   fill or redemption (`balance` lines, which also drive percentage sizing
//!   when flat), checks for stray orders and for positions that don't match
//!   the token balances, redeems settled winners, and halts on the wallet's
//!   daily loss.
//!
//! The engine thread hands over [`OrderIntent`]s with the instant it decided,
//! and [`PresignHint`]s as a gap nears the threshold, so the order is usually
//! signed before the decision and the hot path is one POST.
//!
//! Nothing waits in front of a live entry: [`ExecBridge::run`] only dispatches.
//! Each live entry runs as its own task (assets cross together, and one FAK's
//! post and poll must not hold back the next). Hints go to a worker of their
//! own that keeps only the latest per asset. Probes stay serial on theirs.
//! Token meta is fetched when a window is identified ([`ExecBridge::prefetch`]),
//! so an entry does no REST read before its POST.

use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use parking_lot::Mutex;
use pk_core::fees::taker_fee;
use pk_engine::{ExitIntent, OrderIntent, PresignHint};
use pk_exec::presign::PresignCache;
use pk_exec::{
    ClobExec, LatencyEstimator, LiveExec, LiveLimits, OrderSigner, OrderTiming, Probe, ProbeLimits,
    RedeemReceipt, SdkSigner, Top, Trigger, WalletConfig, WarmConfig, WarmHttp,
};
use rust_decimal::Decimal;
use rust_decimal::prelude::{FromPrimitive, ToPrimitive};
use serde::Serialize;
use serde_json::{Value, json};
use tokio::sync::{Notify, mpsc};

pub const RECONCILE_EVERY: Duration = Duration::from_secs(30);
/// Balance/allowance rejects in a row that halt trading.
pub const BALANCE_REJECTS_HALT: u32 = 3;
/// A redemption still failing after this many tries is alerted (the oracle
/// can take a few minutes to report after Polymarket shows the result).
pub const REDEEM_ALERT_AFTER: u32 = 20;
pub const GAS_LOW_POL: f64 = 0.05;
const GAS_CHECK_EVERY: Duration = Duration::from_secs(600);
const SHARE_DRIFT: f64 = 0.01;

pub enum ExecMsg {
    Intent(OrderIntent, Instant),
    Hint(PresignHint),
    Exit(ExitIntent, Instant),
}

pub type BoxFuture<T> = Pin<Box<dyn Future<Output = T> + Send>>;
/// Writes a record to the journal (through the observer when it is up).
pub type JournalFn = Arc<dyn Fn(i64, Value) + Send + Sync>;
/// Hands a control line to the engine thread (and the tape).
pub type ControlFn = Arc<dyn Fn(Value) + Send + Sync>;
/// `(key, subject, lines, urgent)`.
pub type AlertFn = Arc<dyn Fn(&str, &str, Vec<String>, bool) + Send + Sync>;
/// `(source, reason)`: halts new entries.
pub type KillFn = Arc<dyn Fn(&str, &str) + Send + Sync>;
/// What the ledger holds now, read from the engine.
pub type ViewFn = Arc<dyn Fn() -> LiveView + Send + Sync>;
pub type RedeemFn = Arc<dyn Fn(String) -> BoxFuture<anyhow::Result<RedeemReceipt>> + Send + Sync>;
pub type GasFn = Arc<dyn Fn() -> BoxFuture<anyhow::Result<f64>> + Send + Sync>;

/// One position as the reconciler needs it.
#[derive(Clone, Debug, Default)]
pub struct Held {
    pub asset: String,
    pub open_unix: i64,
    pub side: String,
    pub shares: f64,
    /// From the window, while the engine still knows it.
    pub token: Option<String>,
    pub condition_id: Option<String>,
    pub fee_rate: f64,
    pub fee_exponent: f64,
}

#[derive(Clone, Debug, Default)]
pub struct LiveView {
    pub open: Vec<Held>,
    /// Settled with a payout, recently: candidates for redemption.
    pub winners: Vec<Held>,
    pub in_flight: usize,
    pub day_drawdown: Option<f64>,
    pub daily_loss_limit: f64,
}

/// How the bridge reaches the rest of the trader.
#[derive(Clone)]
pub struct Hooks {
    pub journal: JournalFn,
    pub control: ControlFn,
    pub alert: AlertFn,
    pub kill: KillFn,
    pub view: ViewFn,
}

pub struct LivePart<S: OrderSigner> {
    pub exec: LiveExec<S>,
    pub redeem: RedeemFn,
    pub gas: Option<GasFn>,
    pub signature_type: u8,
}

#[derive(Debug, Default, Serialize)]
pub struct LiveStatus {
    pub balance: Option<f64>,
    pub last_reconcile_ns: Option<i64>,
    pub reconcile_errors: u64,
    pub stray_orders: u64,
    pub position_drift: Option<String>,
    pub redeemed: u64,
    pub redeem_pending: usize,
    pub last_redeem: Option<RedeemReceipt>,
    pub redeem_errors: u64,
    pub gas_pol: Option<f64>,
    pub entries: u64,
    pub filled: u64,
    pub refused: u64,
    pub balance_rejects: u64,
    pub last_error: Option<String>,
    pub exits: u64,
}

pub struct ExecBridge<S: OrderSigner = SdkSigner> {
    pub probe: Arc<Probe<S>>,
    pub live: Option<LivePart<S>>,
    hooks: Hooks,
    meta: Mutex<HashMap<String, (Decimal, Decimal)>>,
    latency: Mutex<LatencyEstimator>,
    /// The last token hinted or entered, for cadence probes.
    last_token: Mutex<Option<String>>,
    /// Hints not yet handled, the latest per asset; `hint_ready` wakes the worker.
    hints: Mutex<HashMap<String, PresignHint>>,
    hint_ready: Notify,
    redeemed: Mutex<HashSet<String>>,
    redeem_tries: Mutex<HashMap<String, u32>>,
    pub status: Mutex<LiveStatus>,
    gas_checked: Mutex<Option<Instant>>,
    pub refused: AtomicU64,
}

fn dec(x: Option<f64>) -> Option<Decimal> {
    x.and_then(Decimal::from_f64).map(|d| d.round_dp(4))
}

fn now_ns() -> i64 {
    crate::now_ns()
}

pub struct BridgeConfig {
    pub latency_seed_ms: f64,
    pub latency_window: usize,
    pub latency_min_samples: usize,
}

/// Where live redemptions are sent from.
pub struct RedeemConfig {
    pub rpc_url: String,
    /// Overrides the exchange's collateral (pUSD).
    pub collateral: Option<String>,
    /// A deposit wallet's relayer key (`POLY_SIGNATURE_TYPE=3`); None redeems from the EOA.
    pub relayer: Option<pk_exec::RelayerConfig>,
}

impl ExecBridge<SdkSigner> {
    /// Authenticate, open the warm connection, and (live) the redeemer.
    pub async fn connect(
        wallet: &WalletConfig,
        hooks: Hooks,
        cfg: BridgeConfig,
        live: Option<(LiveLimits, RedeemConfig)>,
    ) -> anyhow::Result<Arc<Self>> {
        let signer = Arc::new(SdkSigner::connect(wallet).await?);
        tracing::info!(address = %signer.address(), "order path authenticated");
        let http = WarmHttp::connect(WarmConfig::new(wallet.clob_url.parse()?)).await?;
        http.spawn_maintenance();
        let exec = Arc::new(ClobExec::new(http, signer, PresignCache::new(Duration::from_secs(10), 64)));
        let live = match live {
            None => None,
            Some((limits, rc)) => {
                let (redeem, gas) = match (rc.relayer, &wallet.funder) {
                    // A deposit wallet: the relayer submits, and pays the gas.
                    (Some(relayer), Some(funder)) => {
                        let r = Arc::new(
                            pk_exec::WalletRedeemer::new(
                                relayer,
                                &wallet.private_key,
                                funder,
                                rc.collateral.as_deref(),
                            )?
                            .with_rpc(&rc.rpc_url)
                            .await?,
                        );
                        tracing::info!(wallet = %r.wallet(), "redeemer connected (deposit wallet, relayer)");
                        let redeem: RedeemFn = Arc::new(move |c| {
                            let r = r.clone();
                            Box::pin(async move { pk_exec::Redeem::redeem(r.as_ref(), &c).await })
                        });
                        (redeem, None)
                    }
                    (Some(_), None) => anyhow::bail!("a deposit wallet redeemer needs POLY_FUNDER"),
                    (None, _) => {
                        let r = Arc::new(
                            pk_exec::CtfRedeemer::connect(&rc.rpc_url, &wallet.private_key, rc.collateral.as_deref())
                                .await?,
                        );
                        tracing::info!(address = %r.address(), "redeemer connected");
                        let (r1, r2) = (r.clone(), r);
                        let redeem: RedeemFn = Arc::new(move |c| {
                            let r = r1.clone();
                            Box::pin(async move { pk_exec::Redeem::redeem(r.as_ref(), &c).await })
                        });
                        let gas: GasFn = Arc::new(move || {
                            let r = r2.clone();
                            Box::pin(async move { pk_exec::Redeem::gas_balance(r.as_ref()).await })
                        });
                        (redeem, Some(gas))
                    }
                };
                Some(LivePart {
                    exec: LiveExec::new(exec.clone(), limits),
                    redeem,
                    gas,
                    signature_type: wallet.signature_type,
                })
            }
        };
        Ok(Self::new(exec, hooks, cfg, live))
    }
}

impl<S: OrderSigner> ExecBridge<S> {
    pub fn new(
        exec: Arc<ClobExec<S>>,
        hooks: Hooks,
        cfg: BridgeConfig,
        live: Option<LivePart<S>>,
    ) -> Arc<Self> {
        let probe = Arc::new(Probe::new(exec, ProbeLimits { max_open: 2, ..ProbeLimits::default() }));
        Arc::new(Self {
            probe,
            live,
            hooks,
            meta: Mutex::new(HashMap::new()),
            latency: Mutex::new(LatencyEstimator::new(
                cfg.latency_seed_ms,
                cfg.latency_window,
                cfg.latency_min_samples,
            )),
            last_token: Mutex::new(None),
            hints: Mutex::new(HashMap::new()),
            hint_ready: Notify::new(),
            redeemed: Mutex::new(HashSet::new()),
            redeem_tries: Mutex::new(HashMap::new()),
            status: Mutex::new(LiveStatus::default()),
            gas_checked: Mutex::new(None),
            refused: AtomicU64::new(0),
        })
    }

    fn exec(&self) -> &Arc<ClobExec<S>> {
        &self.probe.exec
    }

    /// The tick and minimum size of a token (one REST read, then cached).
    async fn meta(&self, token: &str) -> Option<(Decimal, Decimal)> {
        if let Some(m) = self.meta.lock().get(token) {
            return Some(*m);
        }
        match self.exec().top(token).await {
            Ok(t) => {
                self.meta.lock().insert(token.to_owned(), (t.tick, t.min_size));
                Some((t.tick, t.min_size))
            }
            Err(e) => {
                tracing::warn!(token, error = %e, "order path: book meta");
                None
            }
        }
    }

    async fn top(&self, token: &str, bid: Option<f64>, ask: Option<f64>) -> Option<Top> {
        let (tick, min_size) = self.meta(token).await?;
        Some(Top { best_bid: dec(bid), best_ask: dec(ask), tick, min_size })
    }

    fn record(&self, t: &OrderTiming, asset: &str, open_unix: i64) {
        let mut v = serde_json::to_value(t).unwrap_or(Value::Null);
        if let Some(o) = v.as_object_mut() {
            o.insert("asset".into(), asset.into());
            o.insert("open_unix".into(), open_unix.into());
        }
        (self.hooks.journal)(t.t_ns as i64, v);
    }

    /// One measured round trip; publish the estimate when it moves.
    fn observe_latency(&self, decision_to_ack_us: u64) {
        let published = self.latency.lock().record(decision_to_ack_us);
        if let Some(now) = published {
            (self.hooks.control)(json!({"type": "latency", "ms": now.ms, "n": now.n, "source": now.source}));
        }
    }

    /// Dispatch only: a live entry starts at once as its own task, a hint
    /// replaces the asset's pending one, a probe joins the probe queue.
    pub async fn run(self: Arc<Self>, mut rx: mpsc::UnboundedReceiver<ExecMsg>) {
        let (probe_tx, probe_rx) = mpsc::unbounded_channel();
        tokio::spawn(self.clone().hint_worker());
        tokio::spawn(self.clone().probe_worker(probe_rx));
        while let Some(msg) = rx.recv().await {
            match msg {
                ExecMsg::Hint(h) => {
                    *self.last_token.lock() = Some(h.token.clone());
                    self.hints.lock().insert(h.asset.clone(), h);
                    self.hint_ready.notify_one();
                }
                ExecMsg::Intent(i, t_decision) => {
                    *self.last_token.lock() = Some(i.token.clone());
                    if i.live {
                        let this = self.clone();
                        tokio::spawn(async move { this.enter(&i, t_decision).await });
                    } else {
                        let _ = probe_tx.send((i, t_decision));
                    }
                }
                ExecMsg::Exit(x, t_decision) => {
                    let this = self.clone();
                    tokio::spawn(async move { this.exit_position(&x, t_decision).await });
                }
            }
        }
    }

    /// Signs ahead for the latest hint of each asset; a backlog collapses.
    async fn hint_worker(self: Arc<Self>) {
        loop {
            self.hint_ready.notified().await;
            let hints: Vec<PresignHint> = self.hints.lock().drain().map(|(_, h)| h).collect();
            for h in hints {
                self.presign_hint(&h).await;
            }
            self.exec().presign.prune();
        }
    }

    async fn presign_hint(&self, h: &PresignHint) {
        let Some(top) = self.top(&h.token, h.best_bid, h.best_ask).await else { return };
        match &self.live {
            Some(live) => {
                // The entry a decision at this ask would send.
                if let (Some(limit), Some(n)) = (dec(h.limit_px), h.contracts) {
                    live.exec.presign_entry(&h.token, limit, Decimal::from(n), &top).await;
                }
            }
            None => {
                if let Err(e) = self.probe.presign(&h.token, &top).await {
                    tracing::warn!(error = %e, "order path: presign");
                }
            }
        }
    }

    /// Probes one at a time, so `ProbeLimits` hold as before.
    async fn probe_worker(self: Arc<Self>, mut rx: mpsc::UnboundedReceiver<(OrderIntent, Instant)>) {
        while let Some((i, t_decision)) = rx.recv().await {
            self.probe_intent(&i, t_decision).await;
        }
    }

    /// Read the tick and minimum size of tokens not cached yet, in the
    /// background, so an entry on them does no REST read before its POST.
    pub fn prefetch(self: &Arc<Self>, tokens: impl IntoIterator<Item = String>) {
        let missing: Vec<String> = {
            let meta = self.meta.lock();
            tokens.into_iter().filter(|t| !meta.contains_key(t)).collect()
        };
        for token in missing {
            let this = self.clone();
            tokio::spawn(async move { this.meta(&token).await });
        }
    }

    async fn probe_intent(&self, i: &OrderIntent, t_decision: Instant) {
        let Some(top) = self.top(&i.token, i.best_bid, i.best_ask).await else { return };
        let out = self.probe.run_once(&i.token, &top, t_decision, Trigger::Decision).await;
        if out.refused.is_some() {
            self.refused.fetch_add(1, Ordering::Relaxed);
        }
        for t in &out.records {
            self.record(t, &i.asset, i.open_unix);
        }
    }

    /// A live entry: post, read the result, hand it to the engine.
    async fn enter(&self, i: &OrderIntent, t_decision: Instant) {
        let Some(live) = &self.live else { return };
        let top = self.top(&i.token, i.best_bid, i.best_ask).await;
        let limit = dec(Some(i.limit_px)).unwrap_or_default();
        let ex = match &top {
            Some(top) => live.exec.enter(&i.token, limit, Decimal::from(i.contracts), top, t_decision).await,
            None => pk_exec::Executed { refused: Some("no book meta".into()), ..Default::default() },
        };
        for t in &ex.records {
            self.record(t, &i.asset, i.open_unix);
            // A taker's round trip, filled or killed: an HTTP answer came back.
            if t.kind_is_post() && t.status > 0 {
                self.observe_latency(t.decision_to_ack_us);
            }
        }
        {
            let mut s = self.status.lock();
            s.entries += 1;
            if ex.shares > 0.0 {
                s.filled += 1;
            }
            if ex.refused.is_some() {
                s.refused += 1;
            }
            if ex.balance_reject {
                s.balance_rejects += 1;
            }
            if let Some(e) = ex.error.clone().or(ex.refused.clone()) {
                s.last_error = Some(e);
            }
        }
        (self.hooks.control)(json!({
            "type": "live_fill",
            "asset": i.asset,
            "open_unix": i.open_unix,
            "side": i.side,
            "token": i.token,
            "order_id": ex.order_id,
            "status": ex.status,
            "ordered": i.contracts,
            "shares": ex.shares,
            "cost": ex.usd,
            "cost_basis": ex.cost_basis,
            "entry_px": i.entry_px,
            "limit_px": i.limit_px,
            "decision_ns": i.decision_ns,
            "decision_to_ack_ms": ex.decision_to_ack_us.map(|u| (u as f64 / 100.0).round() / 10.0),
            "error": ex.error.clone().or(ex.refused.clone()),
        }));
        if ex.balance_reject {
            let n = ex.rejects_in_row;
            (self.hooks.alert)(
                "balance_reject",
                "poly-kalshi: live order rejected for balance or allowance",
                vec![
                    format!("{} {} {}", i.asset, i.side, ex.error.clone().unwrap_or_default()),
                    format!("{n} in a row; {BALANCE_REJECTS_HALT} halt trading"),
                ],
                true,
            );
            if n >= BALANCE_REJECTS_HALT {
                (self.hooks.kill)("balance_rejects", &format!("{n} balance/allowance rejects in a row"));
            }
        }
    }

    /// The active exit: sell the held leg with a FAK under the bid, and hand
    /// what it sold to the engine (`live_exit`, `reason: "fair"`).
    async fn exit_position(&self, x: &ExitIntent, t_decision: Instant) {
        let Some(live) = &self.live else { return };
        let Some(top) = self.top(&x.token, Some(x.bid), None).await else { return };
        let shares = Decimal::from_f64(x.shares).unwrap_or_default();
        let ticks = live.exec.limits.active_exit_ticks;
        let ex = live.exec.exit_ticks(&x.token, shares, &top, ticks, t_decision).await;
        for t in &ex.records {
            self.record(t, &x.asset, x.open_unix);
        }
        if let Some(e) = ex.error.clone().or(ex.refused.clone()) {
            self.status.lock().last_error = Some(format!("exit: {e}"));
        }
        if ex.shares <= 0.0 {
            return;
        }
        let px = ex.usd / ex.shares;
        let fee = taker_fee(px, ex.shares, x.fee_rate, x.fee_exponent);
        (self.hooks.control)(json!({
            "type": "live_exit", "asset": x.asset, "open_unix": x.open_unix, "reason": "fair",
            "shares": ex.shares, "proceeds": ex.usd, "fee": fee, "order_id": ex.order_id,
        }));
        self.status.lock().exits += 1;
    }

    // --- live: reconciliation ----------------------------------------------------------

    /// Read the wallet and publish it, unless an order is in flight (its cost
    /// would be counted twice until the next read).
    async fn publish_balance(&self, live: &LivePart<S>) -> Option<f64> {
        if (self.hooks.view)().in_flight > 0 {
            return None;
        }
        match self.exec().balance(live.signature_type).await {
            Ok(b) => {
                let b = b.to_f64().unwrap_or(0.0);
                if (self.hooks.view)().in_flight > 0 {
                    return None;
                }
                (self.hooks.control)(json!({"type": "balance", "balance": b}));
                let mut s = self.status.lock();
                s.balance = Some(b);
                s.last_reconcile_ns = Some(now_ns());
                Some(b)
            }
            Err(e) => {
                let mut s = self.status.lock();
                s.reconcile_errors += 1;
                s.last_error = Some(format!("balance: {e:#}"));
                None
            }
        }
    }

    /// One reconciliation pass (live only).
    pub async fn reconcile(&self) {
        let Some(live) = &self.live else { return };
        self.publish_balance(live).await;
        match self.exec().open_orders().await {
            Ok(orders) if !orders.is_empty() => {
                // Entries are FAK: nothing should ever rest.
                self.status.lock().stray_orders += orders.len() as u64;
                let cancelled = self.exec().cancel_all().await.map(|a| a.canceled.len());
                (self.hooks.alert)(
                    "stray_orders",
                    "poly-kalshi: resting orders found on the live account",
                    vec![format!("{} open; cancel-all: {cancelled:?}", orders.len())],
                    true,
                );
            }
            Ok(_) => {}
            Err(e) => {
                let mut s = self.status.lock();
                s.reconcile_errors += 1;
                s.last_error = Some(format!("open orders: {e:#}"));
            }
        }
        let view = (self.hooks.view)();
        let mut drift = Vec::new();
        for p in &view.open {
            let Some(token) = &p.token else { continue };
            if let Ok(held) = self.exec().token_balance(token, live.signature_type).await {
                let held = held.to_f64().unwrap_or(0.0);
                if (held - p.shares).abs() > SHARE_DRIFT {
                    drift.push(format!(
                        "{}:{} {} ledger={} wallet={held}",
                        p.asset, p.open_unix, p.side, p.shares
                    ));
                }
            }
        }
        self.status.lock().position_drift = (!drift.is_empty()).then(|| drift.join("; "));
        if !drift.is_empty() {
            (self.hooks.alert)("position_drift", "poly-kalshi: ledger and wallet disagree", drift, true);
        }
        self.redeem_winners(live, &view).await;
        if let (Some(dd), limit) = (view.day_drawdown, view.daily_loss_limit)
            && dd >= limit
        {
            (self.hooks.kill)("daily_loss", &format!("wallet down {dd:.2} today (limit {limit:.2})"));
        }
        self.check_gas(live).await;
    }

    async fn redeem_winners(&self, live: &LivePart<S>, view: &LiveView) {
        let todo: Vec<&Held> = view
            .winners
            .iter()
            .filter(|w| w.condition_id.as_ref().is_some_and(|c| !self.redeemed.lock().contains(c)))
            .collect();
        self.status.lock().redeem_pending = todo.len();
        let mut redeemed_any = false;
        for w in todo {
            let condition = w.condition_id.clone().unwrap_or_default();
            // Already redeemed (e.g. before a restart): nothing left to claim.
            if let Some(token) = &w.token
                && let Ok(held) = self.exec().token_balance(token, live.signature_type).await
                && held <= Decimal::ZERO
            {
                self.redeemed.lock().insert(condition);
                continue;
            }
            match (live.redeem)(condition.clone()).await {
                Ok(receipt) => {
                    self.redeemed.lock().insert(condition.clone());
                    self.redeem_tries.lock().remove(&condition);
                    let mut v = serde_json::to_value(&receipt).unwrap_or(Value::Null);
                    if let Some(o) = v.as_object_mut() {
                        o.insert("type".into(), "redeem".into());
                        o.insert("asset".into(), w.asset.clone().into());
                        o.insert("open_unix".into(), w.open_unix.into());
                        o.insert("shares".into(), w.shares.into());
                    }
                    (self.hooks.journal)(now_ns(), v);
                    {
                        let mut s = self.status.lock();
                        s.redeemed += 1;
                        s.last_redeem = Some(receipt);
                    }
                    redeemed_any = true;
                    // Shares still there: the redeem claimed nothing.
                    if let Some(token) = &w.token
                        && let Ok(held) = self.exec().token_balance(token, live.signature_type).await
                        && held > Decimal::ZERO
                    {
                        (self.hooks.alert)(
                            "redeem_no_effect",
                            "poly-kalshi: redemption left the shares in the wallet",
                            vec![
                                format!("{}:{} condition {condition}", w.asset, w.open_unix),
                                "Check the collateral (POLY_REDEEM_COLLATERAL); redeem manually.".into(),
                            ],
                            true,
                        );
                    }
                }
                Err(e) => {
                    let tries = {
                        let mut t = self.redeem_tries.lock();
                        let n = t.entry(condition.clone()).or_default();
                        *n += 1;
                        *n
                    };
                    {
                        let mut s = self.status.lock();
                        s.redeem_errors += 1;
                        s.last_error = Some(format!("redeem: {e:#}"));
                    }
                    if tries == REDEEM_ALERT_AFTER {
                        (self.hooks.alert)(
                            "redeem_failed",
                            "poly-kalshi: a settled position could not be redeemed",
                            vec![format!("{}:{} after {tries} tries: {e:#}", w.asset, w.open_unix)],
                            true,
                        );
                    }
                }
            }
        }
        if redeemed_any {
            self.publish_balance(live).await;
        }
    }

    async fn check_gas(&self, live: &LivePart<S>) {
        let Some(gas) = &live.gas else { return };
        {
            let mut at = self.gas_checked.lock();
            if at.is_some_and(|t| t.elapsed() < GAS_CHECK_EVERY) {
                return;
            }
            *at = Some(Instant::now());
        }
        match gas().await {
            Ok(pol) => {
                self.status.lock().gas_pol = Some(pol);
                if pol < GAS_LOW_POL {
                    (self.hooks.alert)(
                        "gas_low",
                        "poly-kalshi: the wallet is low on POL for redemptions",
                        vec![format!("{pol:.4} POL (alert under {GAS_LOW_POL})")],
                        true,
                    );
                }
            }
            Err(e) => self.status.lock().last_error = Some(format!("gas: {e:#}")),
        }
    }

    /// Reconcile on a timer (live), and probe on a cadence (both modes).
    pub async fn run_background(self: Arc<Self>, probe_cadence: Option<Duration>) {
        let mut reconcile = tokio::time::interval(RECONCILE_EVERY);
        reconcile.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        let cadence_every = probe_cadence.unwrap_or(Duration::from_secs(3600));
        let mut cadence = tokio::time::interval(cadence_every);
        cadence.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        cadence.tick().await;
        loop {
            tokio::select! {
                _ = reconcile.tick(), if self.live.is_some() => self.reconcile().await,
                _ = cadence.tick(), if probe_cadence.is_some() => self.cadence_probe().await,
            }
        }
    }

    /// A probe between decisions on the last token seen, against its REST book.
    pub async fn cadence_probe(&self) {
        let Some(token) = self.last_token.lock().clone() else { return };
        if self.probe.halted() {
            return;
        }
        let Ok(top) = self.exec().top(&token).await else { return };
        let out = self.probe.run_once(&token, &top, Instant::now(), Trigger::Cadence).await;
        for t in &out.records {
            self.record(t, "", 0);
        }
    }

    // --- control ------------------------------------------------------------------

    /// The kill switch's order side: cancel everything on the account.
    pub async fn cancel_all(&self) {
        for t in self.probe.cancel_everything().await {
            self.record(&t, "", 0);
        }
    }

    pub async fn shutdown(&self) {
        self.cancel_all().await;
    }

    /// Sell every open live position now (the caller has halted entries).
    pub async fn flatten(&self) -> Vec<Value> {
        let Some(live) = &self.live else { return vec![json!({"error": "not live"})] };
        self.cancel_all().await;
        let mut results = Vec::new();
        for p in (self.hooks.view)().open {
            let Some(token) = p.token.clone() else {
                results.push(json!({"asset": p.asset, "open_unix": p.open_unix, "error": "window unknown"}));
                continue;
            };
            let top = match self.exec().top(&token).await {
                Ok(t) => t,
                Err(e) => {
                    results
                        .push(json!({"asset": p.asset, "open_unix": p.open_unix, "error": format!("{e:#}")}));
                    continue;
                }
            };
            let shares = Decimal::from_f64(p.shares).unwrap_or_default();
            let ex = live.exec.exit(&token, shares, &top).await;
            for t in &ex.records {
                self.record(t, &p.asset, p.open_unix);
            }
            if ex.shares > 0.0 {
                let px = ex.usd / ex.shares;
                let fee = taker_fee(px, ex.shares, p.fee_rate, p.fee_exponent);
                (self.hooks.control)(json!({
                    "type": "live_exit", "asset": p.asset, "open_unix": p.open_unix,
                    "shares": ex.shares, "proceeds": ex.usd, "fee": fee, "order_id": ex.order_id,
                }));
                self.status.lock().exits += 1;
            }
            results.push(json!({
                "asset": p.asset, "open_unix": p.open_unix, "sold": ex.shares, "proceeds": ex.usd,
                "error": ex.error.or(ex.refused),
            }));
        }
        self.publish_balance(live).await;
        results
    }

    pub fn latency_now(&self) -> pk_exec::LatencyNow {
        self.latency.lock().current()
    }

    pub fn stats(&self) -> Value {
        json!({
            "enabled": true,
            "mode": if self.live.is_some() { "live" } else { "probe" },
            "exec": self.exec().stats(),
            "probe": &*self.probe.counters.lock(),
            "open_orders": self.probe.open_orders(),
            "refused": self.refused.load(Ordering::Relaxed),
            "latency": self.latency_now(),
            "live": self.live.as_ref().map(|_| serde_json::to_value(&*self.status.lock()).unwrap_or(Value::Null)),
        })
    }
}

trait PostKind {
    fn kind_is_post(&self) -> bool;
}

impl PostKind for OrderTiming {
    fn kind_is_post(&self) -> bool {
        matches!(self.kind, pk_exec::clob::Kind::Post)
    }
}
