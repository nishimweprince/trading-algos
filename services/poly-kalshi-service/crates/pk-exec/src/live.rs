//! Real orders: a taker buy at or through the ask, sized to the book, and the
//! flatten path's taker sell.
//!
//! An entry is a **FAK BUY** limited at the decision's limit price (rounded
//! down to the tick, never above the cap): it takes what rests at or below the
//! limit now and the venue cancels the rest. Its size is the engine's, which
//! is already cut to the depth at the limit when `PK_SIZE_TO_DEPTH` is on. The
//! pure [`entry_spec`] refuses anything else, and is property-tested.
//!
//! After the post, the result is read from the acknowledgement when the venue
//! reports the match there (making = pUSD paid, taking = shares), else by
//! polling the order until it is no longer live. A FAK that is somehow still
//! resting after the poll is cancelled. The wallet reconciliation corrects any
//! cost the poll could only bound (`cost_basis = "limit"`).

use std::sync::Arc;
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::{Duration, Instant};

use rust_decimal::Decimal;
use rust_decimal::prelude::ToPrimitive;
use serde::Serialize;

use crate::clob::{ClobExec, OrderTiming, PostAck, Top, Trigger};
use crate::signing::{OrderSigner, OrderSpec, Side, Tif};

#[derive(Clone, Debug)]
pub struct LiveLimits {
    /// The strategy's price cap (`PK_MAX_ENTRY_PX`).
    pub max_entry_px: Decimal,
    /// The most one order may cost at its limit (`PK_MAX_OPEN_NOTIONAL_USD`).
    pub max_notional: Decimal,
    /// How far under the best bid a flatten sell may reach, in ticks.
    pub exit_ticks: u32,
    /// How far under the bid the active exit's sell reaches, in ticks.
    pub active_exit_ticks: u32,
    pub poll_every: Duration,
    pub poll_for: Duration,
}

impl Default for LiveLimits {
    fn default() -> Self {
        Self {
            max_entry_px: Decimal::new(60, 2),
            max_notional: Decimal::from(50),
            exit_ticks: 2,
            active_exit_ticks: 3,
            poll_every: Duration::from_millis(200),
            poll_for: Duration::from_secs(5),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error)]
pub enum Refused {
    #[error("no ask to buy from")]
    NoAsk,
    #[error("no bid to sell to")]
    NoBid,
    #[error("best ask {0} is above the limit {1}")]
    AboveLimit(Decimal, Decimal),
    #[error("price {0} is not within one tick and 1")]
    BadPrice(Decimal),
    #[error("size {0} is under the minimum {1}")]
    BelowMin(Decimal, Decimal),
    #[error("best bid {0} is under the exit floor {1}")]
    BelowFloor(Decimal, Decimal),
    #[error("notional {0} is over the cap {1}")]
    Notional(Decimal, Decimal),
    #[error("a post-only sell at {0} would cross the bid {1}")]
    WouldCross(Decimal, Decimal),
}

fn floor_to(x: Decimal, step: Decimal) -> Decimal {
    if step <= Decimal::ZERO { x } else { (x / step).floor() * step }
}

fn ceil_to(x: Decimal, step: Decimal) -> Decimal {
    if step <= Decimal::ZERO { x } else { (x / step).ceil() * step }
}

/// The entry order for this decision, or why there is none. Pure.
pub fn entry_spec(
    token: &str,
    limit_px: Decimal,
    contracts: Decimal,
    top: &Top,
    limits: &LiveLimits,
) -> Result<OrderSpec, Refused> {
    let price = floor_to(limit_px.min(limits.max_entry_px), top.tick);
    if price < top.tick || price >= Decimal::ONE {
        return Err(Refused::BadPrice(price));
    }
    let ask = top.best_ask.ok_or(Refused::NoAsk)?;
    if ask > price {
        return Err(Refused::AboveLimit(ask, price));
    }
    let size = contracts.floor();
    if size < top.min_size.max(Decimal::ONE) {
        return Err(Refused::BelowMin(size, top.min_size));
    }
    let notional = price * size;
    if notional > limits.max_notional {
        return Err(Refused::Notional(notional, limits.max_notional));
    }
    Ok(OrderSpec { token: token.to_owned(), side: Side::Buy, price, size, tif: Tif::Fak, post_only: false })
}

/// The flatten sell for `shares` held: FAK at `exit_ticks` under the best bid. Pure.
pub fn exit_spec(token: &str, shares: Decimal, top: &Top, limits: &LiveLimits) -> Result<OrderSpec, Refused> {
    exit_spec_ticks(token, shares, top, limits.exit_ticks, None)
}

/// A FAK sell of `shares` at `ticks` under the best bid, and never under
/// `floor` (rounded up to the tick). Pure.
pub fn exit_spec_ticks(
    token: &str,
    shares: Decimal,
    top: &Top,
    ticks: u32,
    floor: Option<Decimal>,
) -> Result<OrderSpec, Refused> {
    let bid = top.best_bid.ok_or(Refused::NoBid)?;
    let mut price = (bid - top.tick * Decimal::from(ticks)).max(top.tick);
    if let Some(floor) = floor.map(|f| ceil_to(f, top.tick)) {
        if floor > bid {
            return Err(Refused::BelowFloor(bid, floor));
        }
        price = price.max(floor);
    }
    if price >= Decimal::ONE {
        return Err(Refused::BadPrice(price));
    }
    let size = floor_to(shares, Decimal::new(1, 2));
    if size <= Decimal::ZERO {
        return Err(Refused::BelowMin(size, Decimal::new(1, 2)));
    }
    Ok(OrderSpec { token: token.to_owned(), side: Side::Sell, price, size, tif: Tif::Fak, post_only: false })
}

/// The resting exit: a post-only GTC sell of `shares` at `px` rounded up to
/// the tick, never under `floor`, and never at or under the bid (it would
/// cross, and post-only is refused). Pure.
pub fn rest_spec(
    token: &str,
    shares: Decimal,
    px: Decimal,
    floor: Option<Decimal>,
    top: &Top,
) -> Result<OrderSpec, Refused> {
    let mut price = ceil_to(px, top.tick);
    if let Some(floor) = floor {
        price = price.max(ceil_to(floor, top.tick));
    }
    if price < top.tick || price >= Decimal::ONE {
        return Err(Refused::BadPrice(price));
    }
    if let Some(bid) = top.best_bid
        && price <= bid
    {
        return Err(Refused::WouldCross(price, bid));
    }
    let size = floor_to(shares, Decimal::new(1, 2));
    if size < top.min_size.max(Decimal::new(1, 2)) {
        return Err(Refused::BelowMin(size, top.min_size));
    }
    Ok(OrderSpec { token: token.to_owned(), side: Side::Sell, price, size, tif: Tif::Gtc, post_only: true })
}

/// The venue refused for lack of funds or approvals.
pub fn is_balance_reject(error: &str) -> bool {
    let e = error.to_lowercase();
    e.contains("balance") || e.contains("allowance")
}

/// What one live order did.
#[derive(Clone, Debug, Default, Serialize)]
pub struct Executed {
    pub order_id: Option<String>,
    pub status: Option<String>,
    /// Shares bought (entry) or sold (exit).
    pub shares: f64,
    /// pUSD paid (entry) or received (exit), before the fee.
    pub usd: f64,
    /// "ack" (the venue's amounts), "limit" (matched size × limit, a bound).
    pub cost_basis: &'static str,
    pub error: Option<String>,
    pub refused: Option<String>,
    pub balance_reject: bool,
    /// This reject's place in the run of balance rejects (1 = the first). Each
    /// concurrent entry gets its own, so a halt at N fires once.
    pub rejects_in_row: u32,
    #[serde(skip)]
    pub records: Vec<OrderTiming>,
    pub decision_to_ack_us: Option<u64>,
}

pub struct LiveExec<S: OrderSigner> {
    pub exec: Arc<ClobExec<S>>,
    pub limits: LiveLimits,
    balance_rejects: AtomicU32,
}

fn amount(s: Option<&str>) -> Option<Decimal> {
    s.and_then(|v| v.parse::<Decimal>().ok()).filter(|v| *v > Decimal::ZERO)
}

fn f(d: Decimal) -> f64 {
    d.to_f64().unwrap_or(0.0)
}

impl<S: OrderSigner> LiveExec<S> {
    pub fn new(exec: Arc<ClobExec<S>>, limits: LiveLimits) -> Self {
        Self { exec, limits, balance_rejects: AtomicU32::new(0) }
    }

    /// Balance/allowance rejects in a row (reset by any accepted post).
    pub fn balance_rejects(&self) -> u32 {
        self.balance_rejects.load(Ordering::Relaxed)
    }

    /// Sign ahead the entry a decision on this book would most likely send.
    pub async fn presign_entry(&self, token: &str, limit_px: Decimal, contracts: Decimal, top: &Top) {
        if let Ok(spec) = entry_spec(token, limit_px, contracts, top, &self.limits)
            && let Err(e) = self.exec.presign.ensure(self.exec.signer.as_ref(), &[spec]).await
        {
            tracing::warn!(error = %e, "live presign");
        }
    }

    pub async fn enter(
        &self,
        token: &str,
        limit_px: Decimal,
        contracts: Decimal,
        top: &Top,
        t_decision: Instant,
    ) -> Executed {
        match entry_spec(token, limit_px, contracts, top, &self.limits) {
            Ok(spec) => self.execute(&spec, t_decision).await,
            Err(r) => Executed { refused: Some(r.to_string()), cost_basis: "none", ..Default::default() },
        }
    }

    pub async fn exit(&self, token: &str, shares: Decimal, top: &Top) -> Executed {
        self.exit_ticks(token, shares, top, self.limits.exit_ticks, None, Instant::now()).await
    }

    /// The active exit's sell: a FAK `ticks` under the bid, not under `floor`.
    pub async fn exit_ticks(
        &self,
        token: &str,
        shares: Decimal,
        top: &Top,
        ticks: u32,
        floor: Option<Decimal>,
        t_decision: Instant,
    ) -> Executed {
        match exit_spec_ticks(token, shares, top, ticks, floor) {
            Ok(spec) => self.execute(&spec, t_decision).await,
            Err(r) => Executed { refused: Some(r.to_string()), cost_basis: "none", ..Default::default() },
        }
    }

    /// Post the resting exit; it rests, so nothing is polled here.
    pub async fn rest(&self, spec: &OrderSpec) -> Executed {
        let (ack, timing) = self.exec.post(spec, Instant::now(), Trigger::Rest).await;
        let mut out = Executed {
            order_id: timing.order_id.clone(),
            status: timing.venue_status.clone(),
            error: timing.error.clone(),
            cost_basis: "none",
            ..Default::default()
        };
        if timing.ok {
            if let Some((shares, usd)) = ack.as_ref().and_then(|a| from_ack(a, spec.side)) {
                (out.shares, out.usd, out.cost_basis) = (f(shares), f(usd), "ack");
            }
        } else {
            out.balance_reject = out.error.as_deref().is_some_and(is_balance_reject);
        }
        out.records.push(timing);
        out
    }

    async fn execute(&self, spec: &OrderSpec, t_decision: Instant) -> Executed {
        let (ack, timing) = self.exec.post(spec, t_decision, Trigger::Decision).await;
        self.finish_post(spec, ack, timing).await
    }

    /// A post's result, from its acknowledgement or by polling the order: shared
    /// by the normal path and the fast path's own connection.
    pub async fn finish_post(&self, spec: &OrderSpec, ack: Option<PostAck>, timing: OrderTiming) -> Executed {
        let mut out = Executed {
            order_id: timing.order_id.clone(),
            status: timing.venue_status.clone(),
            error: timing.error.clone(),
            cost_basis: "none",
            decision_to_ack_us: timing.ok.then_some(timing.decision_to_ack_us),
            ..Default::default()
        };
        let ok = timing.ok;
        out.records.push(timing);
        if !ok {
            out.balance_reject = out.error.as_deref().is_some_and(is_balance_reject);
            // Only buys count toward the halt: a sell seconds after its entry is
            // refused until the shares are credited, which is no funding fault.
            if out.balance_reject && spec.side == Side::Buy {
                out.rejects_in_row = self.balance_rejects.fetch_add(1, Ordering::Relaxed) + 1;
            }
            return out;
        }
        if spec.side == Side::Buy {
            self.balance_rejects.store(0, Ordering::Relaxed);
        }
        if let Some((shares, usd)) = ack.as_ref().and_then(|a| from_ack(a, spec.side)) {
            (out.shares, out.usd, out.cost_basis) = (f(shares), f(usd), "ack");
            return out;
        }
        let Some(id) = out.order_id.clone() else { return out };
        // Not reported on the ack: poll until the order is no longer live.
        let deadline = Instant::now() + self.limits.poll_for;
        let mut last = None;
        loop {
            match self.exec.order_state(&id).await {
                Ok(state) => {
                    let live = matches!(state.status.to_lowercase().as_str(), "live" | "delayed");
                    last = Some(state);
                    if !live {
                        break;
                    }
                }
                Err(e) => out.error = Some(format!("order state: {e:#}")),
            }
            if Instant::now() >= deadline {
                // A FAK should never rest; if it does, take it off the book.
                let (_, cancel) = self.exec.cancel(&id, Trigger::Cleanup).await;
                out.records.push(cancel);
                if let Ok(state) = self.exec.order_state(&id).await {
                    last = Some(state);
                }
                break;
            }
            tokio::time::sleep(self.limits.poll_every).await;
        }
        if let Some(state) = last {
            out.status = Some(state.status.to_lowercase());
            let px = state.price.unwrap_or(spec.price);
            (out.shares, out.usd, out.cost_basis) =
                (f(state.size_matched), f(state.size_matched * px), "limit");
        }
        out
    }
}

/// (shares, pUSD) from the acknowledgement, when the venue reported a match.
/// A buy makes pUSD and takes shares; a sell makes shares and takes pUSD.
fn from_ack(a: &PostAck, side: Side) -> Option<(Decimal, Decimal)> {
    let making = amount(a.making_amount.as_deref())?;
    let taking = amount(a.taking_amount.as_deref())?;
    Some(match side {
        Side::Buy => (taking, making),
        Side::Sell => (making, taking),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d(s: &str) -> Decimal {
        s.parse().unwrap()
    }

    fn top(bid: Option<&str>, ask: Option<&str>) -> Top {
        Top { best_bid: bid.map(d), best_ask: ask.map(d), tick: d("0.01"), min_size: d("5") }
    }

    #[test]
    fn buys_at_the_limit_rounded_down() {
        let l = LiveLimits::default();
        let s = entry_spec("t", d("0.4675"), d("10.9"), &top(Some("0.44"), Some("0.46")), &l).unwrap();
        assert_eq!(
            (s.price, s.size, s.side, s.tif, s.post_only),
            (d("0.46"), d("10"), Side::Buy, Tif::Fak, false)
        );
        // Capped at the strategy's maximum.
        let s = entry_spec("t", d("0.75"), d("10"), &top(None, Some("0.55")), &l).unwrap();
        assert_eq!(s.price, d("0.60"));
    }

    #[test]
    fn rests_post_only_above_the_bid_and_floor() {
        let t = top(Some("0.52"), Some("0.55"));
        let s = rest_spec("t", d("18.199"), d("0.531"), Some(d("0.48")), &t).unwrap();
        assert_eq!(
            (s.price, s.size, s.side, s.tif, s.post_only),
            (d("0.54"), d("18.19"), Side::Sell, Tif::Gtc, true)
        );
        // The floor wins over a lower fair.
        assert_eq!(rest_spec("t", d("18"), d("0.40"), Some(d("0.561")), &t).unwrap().price, d("0.57"));
        assert_eq!(rest_spec("t", d("18"), d("0.52"), None, &t), Err(Refused::WouldCross(d("0.52"), d("0.52"))));
        assert_eq!(rest_spec("t", d("4"), d("0.60"), None, &t), Err(Refused::BelowMin(d("4"), d("5"))));
        assert_eq!(rest_spec("t", d("18"), d("1.2"), None, &t), Err(Refused::BadPrice(d("1.2"))));
    }

    #[test]
    fn refuses() {
        let l = LiveLimits { max_notional: d("5"), ..LiveLimits::default() };
        let t = top(Some("0.44"), Some("0.46"));
        assert_eq!(entry_spec("t", d("0.46"), d("10"), &top(Some("0.4"), None), &l), Err(Refused::NoAsk));
        assert_eq!(
            entry_spec("t", d("0.45"), d("10"), &t, &l),
            Err(Refused::AboveLimit(d("0.46"), d("0.45")))
        );
        assert_eq!(entry_spec("t", d("0.46"), d("4.9"), &t, &l), Err(Refused::BelowMin(d("4"), d("5"))));
        assert!(matches!(entry_spec("t", d("0.46"), d("20"), &t, &l), Err(Refused::Notional(..))));
        assert!(matches!(entry_spec("t", d("0.004"), d("20"), &t, &l), Err(Refused::BadPrice(_))));
    }

    /// Over every book, limit and size it can see, an entry is a buy no
    /// higher than the limit or the cap, and no costlier than the cap.
    #[test]
    fn never_above_the_cap() {
        let l = LiveLimits { max_notional: d("20"), ..LiveLimits::default() };
        for tick in ["0.01", "0.001"] {
            let tick = d(tick);
            for ask_cents in (1..=99u32).step_by(7) {
                for limit_ticks in (0..=120u32).step_by(5) {
                    for size in [0u32, 4, 5, 13, 40, 100] {
                        let ask = Decimal::from(ask_cents) / d("100");
                        let t = Top { best_bid: None, best_ask: Some(ask), tick, min_size: d("5") };
                        let limit = Decimal::from(limit_ticks) / d("100");
                        if let Ok(s) = entry_spec("t", limit, Decimal::from(size), &t, &l) {
                            assert_eq!((s.side, s.tif), (Side::Buy, Tif::Fak));
                            assert!(s.price <= limit && s.price <= l.max_entry_px && s.price >= ask);
                            assert!(s.price * s.size <= l.max_notional);
                        }
                    }
                }
            }
        }
    }

    #[test]
    fn exits_under_the_bid() {
        let l = LiveLimits::default();
        let s = exit_spec("t", d("10.555"), &top(Some("0.44"), None), &l).unwrap();
        assert_eq!((s.side, s.price, s.size), (Side::Sell, d("0.42"), d("10.55")));
        assert_eq!(exit_spec("t", d("10"), &top(None, Some("0.5")), &l), Err(Refused::NoBid));
        assert_eq!(exit_spec("t", d("10"), &top(Some("0.01"), None), &l).unwrap().price, d("0.01"));
        // The active exit: three ticks under the bid, never under the floor.
        let t = top(Some("0.70"), None);
        assert_eq!(exit_spec_ticks("t", d("10"), &t, 3, None).unwrap().price, d("0.67"));
        assert_eq!(exit_spec_ticks("t", d("10"), &t, 3, Some(d("0.6812"))).unwrap().price, d("0.69"));
        assert_eq!(
            exit_spec_ticks("t", d("10"), &t, 3, Some(d("0.705"))),
            Err(Refused::BelowFloor(d("0.70"), d("0.71")))
        );
    }

    #[test]
    fn reads_the_ack() {
        let ack = |m: &str, t: &str| PostAck {
            success: true,
            error_msg: None,
            order_id: "x".into(),
            status: Some("matched".into()),
            making_amount: Some(m.into()),
            taking_amount: Some(t.into()),
        };
        assert_eq!(from_ack(&ack("4.6", "10"), Side::Buy), Some((d("10"), d("4.6"))));
        assert_eq!(from_ack(&ack("10", "4.2"), Side::Sell), Some((d("10"), d("4.2"))));
        assert_eq!(from_ack(&ack("", ""), Side::Buy), None);
        assert!(is_balance_reject("not enough balance / allowance"));
        assert!(!is_balance_reject("order crosses book"));
    }
}
