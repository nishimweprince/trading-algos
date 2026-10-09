//! The order probe: real requests on the real order path that cannot fill.
//!
//! Each probe is a **post-only** BUY at the token's minimum size, priced at
//! least `back_ticks` below the best bid, then cancelled at once. Post-only
//! makes the venue reject it rather than let it take liquidity, and pricing
//! under the best bid means it would rest behind other bids even if post-only
//! were ignored. The guard refuses everything else, so `PK_EXECUTION_MODE=live`
//! stays refused and the probe is not a way around it.
//!
//! What it measures is decision → acknowledgement on a warm HTTP/2 connection,
//! with and without a pre-signed order, which is what the paper venue's
//! `PK_PAPER_LATENCY_MS` has so far had to assume.

use std::collections::HashSet;
use std::sync::Arc;
use std::time::Instant;

use parking_lot::Mutex;
use rust_decimal::Decimal;
use serde::Serialize;

use crate::clob::{ClobExec, OrderTiming, Top, Trigger};
use crate::signing::{OrderSigner, OrderSpec, Side, Tif};

#[derive(Clone, Debug)]
pub struct ProbeLimits {
    /// How far below the best bid, in ticks (≥ 1).
    pub back_ticks: u32,
    /// The most a probe order may be worth at its price, in USD.
    pub max_notional: Decimal,
    /// The most probe orders open at once.
    pub max_open: usize,
}

impl Default for ProbeLimits {
    fn default() -> Self {
        Self { back_ticks: 5, max_notional: Decimal::from(3), max_open: 1 }
    }
}

#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum Refused {
    #[error("no best bid to price under")]
    NoBid,
    #[error("back_ticks must be at least 1")]
    NoBackoff,
    #[error("price {0} is not below the best bid {1}")]
    AtOrAboveBid(Decimal, Decimal),
    #[error("price {0} is not below the best ask {1}")]
    WouldCross(Decimal, Decimal),
    #[error("price {0} is under one tick")]
    BelowTick(Decimal),
    #[error("notional {0} is over the cap {1}")]
    Notional(Decimal, Decimal),
    #[error("{0} probe orders already open")]
    TooManyOpen(usize),
}

/// The probe's order for this book, or why there is none. Pure, so the
/// never-marketable property is tested directly.
pub fn probe_spec(token: &str, top: &Top, limits: &ProbeLimits, open: usize) -> Result<OrderSpec, Refused> {
    if open >= limits.max_open {
        return Err(Refused::TooManyOpen(open));
    }
    if limits.back_ticks == 0 {
        return Err(Refused::NoBackoff);
    }
    let bid = top.best_bid.ok_or(Refused::NoBid)?;
    let price = bid - top.tick * Decimal::from(limits.back_ticks);
    if price < top.tick {
        return Err(Refused::BelowTick(price));
    }
    if price >= bid {
        return Err(Refused::AtOrAboveBid(price, bid));
    }
    if let Some(ask) = top.best_ask.filter(|&ask| price >= ask) {
        return Err(Refused::WouldCross(price, ask));
    }
    let size = top.min_size.max(Decimal::ONE);
    let notional = price * size;
    if notional > limits.max_notional {
        return Err(Refused::Notional(notional, limits.max_notional));
    }
    Ok(OrderSpec { token: token.to_owned(), side: Side::Buy, price, size, tif: Tif::Gtc, post_only: true })
}

#[derive(Debug, Default, Serialize)]
pub struct ProbeCounters {
    pub probes: u64,
    pub refused: u64,
    pub posted: u64,
    pub rejected: u64,
    pub cancelled: u64,
    pub cancel_failed: u64,
    /// Must stay 0. Any fill stops the probe.
    pub fills: u64,
    pub last_refusal: Option<String>,
    pub last_error: Option<String>,
}

pub struct Probe<S: OrderSigner> {
    pub exec: Arc<ClobExec<S>>,
    pub limits: ProbeLimits,
    open: Mutex<HashSet<String>>,
    pub counters: Mutex<ProbeCounters>,
}

/// What one probe produced, for the journal.
#[derive(Debug, Default)]
pub struct ProbeOutcome {
    pub records: Vec<OrderTiming>,
    pub refused: Option<Refused>,
    pub filled: bool,
}

impl<S: OrderSigner> Probe<S> {
    pub fn new(exec: Arc<ClobExec<S>>, limits: ProbeLimits) -> Self {
        Self {
            exec,
            limits,
            open: Mutex::new(HashSet::new()),
            counters: Mutex::new(ProbeCounters::default()),
        }
    }

    pub fn halted(&self) -> bool {
        self.counters.lock().fills > 0
    }

    /// Sign ahead the order a probe on this book would send.
    pub async fn presign(&self, token: &str, top: &Top) -> anyhow::Result<()> {
        if let Ok(spec) = probe_spec(token, top, &self.limits, 0) {
            self.exec.presign.ensure(self.exec.signer.as_ref(), &[spec]).await?;
        }
        Ok(())
    }

    /// One probe: post at `t_decision` against the book `top`, then cancel.
    pub async fn run_once(
        &self,
        token: &str,
        top: &Top,
        t_decision: Instant,
        trigger: Trigger,
    ) -> ProbeOutcome {
        let mut out = ProbeOutcome::default();
        if self.halted() {
            out.refused = Some(Refused::TooManyOpen(usize::MAX));
            return out;
        }
        self.counters.lock().probes += 1;
        let open = self.open.lock().len();
        let spec = match probe_spec(token, top, &self.limits, open) {
            Ok(s) => s,
            Err(r) => {
                let mut c = self.counters.lock();
                c.refused += 1;
                c.last_refusal = Some(r.to_string());
                out.refused = Some(r);
                return out;
            }
        };
        let (ack, timing) = self.exec.post(&spec, t_decision, trigger).await;
        let order_id = timing.order_id.clone().filter(|_| timing.ok);
        let matched_on_post = ack.as_ref().is_some_and(|a| {
            a.status.as_deref() == Some("matched")
                || a.taking_amount
                    .as_deref()
                    .is_some_and(|t| t.parse::<Decimal>().is_ok_and(|v| v > Decimal::ZERO))
        });
        {
            let mut c = self.counters.lock();
            if timing.ok {
                c.posted += 1;
            } else {
                c.rejected += 1;
                c.last_error = timing.error.clone();
            }
        }
        out.records.push(timing);
        if let Some(id) = order_id {
            self.open.lock().insert(id.clone());
            let (_, cancel) = self.exec.cancel(&id, Trigger::Cleanup).await;
            let cancelled = cancel.ok;
            out.records.push(cancel);
            let mut filled = matched_on_post;
            if cancelled {
                self.open.lock().remove(&id);
                self.counters.lock().cancelled += 1;
            } else {
                self.counters.lock().cancel_failed += 1;
                // Find out why: an order that can't be cancelled may have filled.
                match self.exec.order_state(&id).await {
                    Ok(state) => {
                        filled |= state.size_matched > Decimal::ZERO;
                        if state.status.to_lowercase() != "live" {
                            self.open.lock().remove(&id);
                        }
                    }
                    Err(e) => self.counters.lock().last_error = Some(format!("order state: {e:#}")),
                }
            }
            if filled {
                out.filled = true;
                self.counters.lock().fills += 1;
                tracing::error!(order_id = %id, "probe order filled: probe halted");
            }
        }
        out
    }

    /// The kill switch: cancel everything still open, then the whole account.
    pub async fn cancel_everything(&self) -> Vec<OrderTiming> {
        let ids: Vec<String> = self.open.lock().iter().cloned().collect();
        let mut records = Vec::new();
        for id in ids {
            let (_, t) = self.exec.cancel(&id, Trigger::Cleanup).await;
            if t.ok {
                self.open.lock().remove(&id);
            }
            records.push(t);
        }
        if let Err(e) = self.exec.cancel_all().await {
            tracing::warn!(error = %e, "cancel-all failed");
        }
        records
    }

    pub fn open_orders(&self) -> usize {
        self.open.lock().len()
    }
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
    fn prices_under_the_bid() {
        let s = probe_spec("t", &top(Some("0.45"), Some("0.47")), &ProbeLimits::default(), 0).unwrap();
        assert_eq!(s.price, d("0.40"));
        assert_eq!(s.size, d("5"));
        assert!(s.post_only);
        assert_eq!(s.side, Side::Buy);
    }

    #[test]
    fn refuses() {
        let l = ProbeLimits::default();
        assert_eq!(probe_spec("t", &top(None, Some("0.5")), &l, 0), Err(Refused::NoBid));
        assert!(matches!(probe_spec("t", &top(Some("0.03"), None), &l, 0), Err(Refused::BelowTick(_))));
        assert_eq!(probe_spec("t", &top(Some("0.5"), None), &l, 1), Err(Refused::TooManyOpen(1)));
        let zero = ProbeLimits { back_ticks: 0, ..l.clone() };
        assert_eq!(probe_spec("t", &top(Some("0.5"), None), &zero, 0), Err(Refused::NoBackoff));
        let cheap = ProbeLimits { max_notional: d("0.5"), ..l };
        assert!(matches!(probe_spec("t", &top(Some("0.5"), None), &cheap, 0), Err(Refused::Notional(..))));
    }

    /// The property the probe rests on: over every bid/ask/tick/backoff it can
    /// see, an order it sends is below both the best bid and the best ask.
    #[test]
    fn never_marketable() {
        for tick in ["0.01", "0.001"] {
            let tick = d(tick);
            for bid_ticks in 0..=100u32 {
                for spread in 0..=5u32 {
                    for back in 0..=7u32 {
                        let bid = tick * Decimal::from(bid_ticks);
                        let ask = bid + tick * Decimal::from(spread);
                        let t = Top { best_bid: Some(bid), best_ask: Some(ask), tick, min_size: d("5") };
                        let l = ProbeLimits { back_ticks: back, max_notional: d("100"), max_open: 1 };
                        if let Ok(s) = probe_spec("t", &t, &l, 0) {
                            assert!(s.price < bid && s.price < ask && s.price >= tick && s.post_only);
                        }
                    }
                }
            }
        }
    }
}
