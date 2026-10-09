//! The kpc rule (manual §3), pure: the live engine and research share it.
//!
//! ```text
//! gap = kalshi_yes_mid - poly_yes_mid
//! |gap| >= threshold -> buy the cheap Polymarket leg as a taker, hold to settlement
//! ```
//!
//! Three semantics are load-bearing and must not be "improved" here:
//!
//! 1. The first crossing decides: no mean, no persistence, no confirmation.
//! 2. A crossing whose entry price is above the cap returns `Skip`, and the
//!    caller consumes the window: no retry at a later, cheaper tick.
//! 3. Nothing here exits. Positions are held to settlement.
//!
//! Research filters, all off by default:
//! - `max_kalshi_spread` / `max_poly_spread`: a crossing on a wider book is
//!   `None` (`kalshi_spread` / `poly_spread`) and does not consume the window.
//! - `min_entry_px`: below the floor is `Skip` (`below_floor`) and consumes it.
//! - `kalshi_extreme`: Kalshi's mid no further from 0.5 than Polymarket's is
//!   `Skip` (`kalshi_lags`): Kalshi trailing, not leading.
//! - `skip_hours_utc`: `None` (`hours`) in those UTC hours.
//! - `max_poly_lag_ms`: the stale-frame guard. `None` (`poly_lag`, not
//!   consumed) while the cheap leg's latest frame arrived more than this many
//!   ms after the venue stamped it. It is the transport lag of the book's
//!   latest frame, not the time since the book changed: a quiet Polymarket
//!   book while Kalshi moves is the signal itself.

use serde::{Deserialize, Serialize};

use crate::book::Quote;

pub const WINDOW_S: i64 = 900;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct StrategyParams {
    pub gap_threshold: f64,
    pub max_entry_px: f64,
    pub notional_usd: f64,
    pub entry_early_s: i64,
    pub entry_late_s: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_kalshi_spread: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_poly_spread: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub min_entry_px: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kalshi_extreme: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub skip_hours_utc: Option<Vec<u32>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_poly_lag_ms: Option<f64>,
}

impl Default for StrategyParams {
    fn default() -> Self {
        Self {
            gap_threshold: 0.20,
            max_entry_px: 0.60,
            notional_usd: 50.0,
            entry_early_s: 20,
            entry_late_s: 60,
            max_kalshi_spread: None,
            max_poly_spread: None,
            min_entry_px: None,
            kalshi_extreme: None,
            skip_hours_utc: None,
            max_poly_lag_ms: None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Action {
    None,
    /// Crossed, but outside the price limits: the window is consumed.
    Skip,
    Enter,
}

impl Action {
    pub fn as_str(self) -> &'static str {
        match self {
            Action::None => "none",
            Action::Skip => "skip",
            Action::Enter => "enter",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Decision {
    pub action: Action,
    pub reason: &'static str,
    pub gap: Option<f64>,
    pub buy_yes: Option<bool>,
    pub entry_px: Option<f64>,
    pub contracts: i64,
}

impl Decision {
    pub fn consumes_window(&self) -> bool {
        self.action != Action::None
    }

    /// The reason, or the action when there is none (`enter`).
    pub fn label(&self) -> &'static str {
        if self.reason.is_empty() { self.action.as_str() } else { self.reason }
    }

    fn none(reason: &'static str, gap: Option<f64>) -> Self {
        Self { action: Action::None, reason, gap, buy_yes: None, entry_px: None, contracts: 0 }
    }

    fn skip(reason: &'static str, gap: f64, buy_yes: bool, entry_px: f64) -> Self {
        Self {
            action: Action::Skip,
            reason,
            gap: Some(gap),
            buy_yes: Some(buy_yes),
            entry_px: Some(entry_px),
            contracts: 0,
        }
    }
}

/// `poly_lag_ms`: the transport lag (ms) of the latest frame on the YES and the
/// NO book, read only when `params.max_poly_lag_ms` is set.
#[allow(clippy::too_many_arguments)]
pub fn decide(
    kalshi_yes: &Quote,
    poly_yes: &Quote,
    poly_no: &Quote,
    params: &StrategyParams,
    sec_into: f64,
    already_traded: bool,
    utc_hour: Option<u32>,
    poly_lag_ms: Option<(Option<f64>, Option<f64>)>,
) -> Decision {
    if already_traded {
        return Decision::none("traded", None);
    }
    if let (Some(hours), Some(h)) = (&params.skip_hours_utc, utc_hour)
        && !hours.is_empty()
        && hours.contains(&h)
    {
        return Decision::none("hours", None);
    }
    // Timing before book validity: near expiry the books go one-sided because
    // the outcome is all but decided, and that is "too late", not a data fault.
    if sec_into < params.entry_early_s as f64 {
        return Decision::none("too_early", None);
    }
    if sec_into > (WINDOW_S - params.entry_late_s) as f64 {
        return Decision::none("too_late", None);
    }
    if !kalshi_yes.valid || !poly_yes.valid {
        return Decision::none("invalid_leg", None);
    }
    let (Some(k_mid), Some(p_mid)) = (kalshi_yes.mid(), poly_yes.mid()) else {
        return Decision::none("invalid_leg", None);
    };
    let gap = k_mid - p_mid;
    if gap.abs() < params.gap_threshold {
        return Decision::none("below_threshold", Some(gap));
    }
    if let Some(max) = params.max_kalshi_spread {
        let spread = kalshi_yes.ask.unwrap_or(0.0) - kalshi_yes.bid.unwrap_or(0.0);
        if spread > max + 1e-9 {
            return Decision::none("kalshi_spread", Some(gap));
        }
    }
    if let Some(max) = params.max_poly_spread {
        let spread = poly_yes.ask.unwrap_or(0.0) - poly_yes.bid.unwrap_or(0.0);
        if spread > max + 1e-9 {
            return Decision::none("poly_spread", Some(gap));
        }
    }
    let buy_yes = gap > 0.0;
    if !buy_yes && !poly_no.valid {
        return Decision::none("invalid_leg", Some(gap)); // buying NO needs the NO book
    }
    if let (Some(max), Some((yes_lag, no_lag))) = (params.max_poly_lag_ms, poly_lag_ms) {
        let lag = if buy_yes { yes_lag } else { no_lag };
        if lag.is_some_and(|l| l > max) {
            return Decision::none("poly_lag", Some(gap));
        }
    }
    let entry_px = if buy_yes { poly_yes.ask } else { poly_no.ask };
    let Some(entry_px) = entry_px.filter(|&p| p > 0.0 && p < 1.0) else {
        return Decision::none("bad_price", Some(gap));
    };
    if entry_px > params.max_entry_px {
        return Decision::skip("above_cap", gap, buy_yes, entry_px);
    }
    if params.min_entry_px.is_some_and(|floor| entry_px < floor - 1e-9) {
        return Decision::skip("below_floor", gap, buy_yes, entry_px);
    }
    if params.kalshi_extreme == Some(true) && (k_mid - 0.5).abs() <= (p_mid - 0.5).abs() {
        return Decision::skip("kalshi_lags", gap, buy_yes, entry_px);
    }
    let contracts = (params.notional_usd / entry_px).floor() as i64;
    if contracts < 1 {
        return Decision::none("too_small", Some(gap));
    }
    Decision {
        action: Action::Enter,
        reason: "",
        gap: Some(gap),
        buy_yes: Some(buy_yes),
        entry_px: Some(entry_px),
        contracts,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn q(bid: f64, ask: f64) -> Quote {
        Quote { valid: true, bid: Some(bid), ask: Some(ask) }
    }

    fn params() -> StrategyParams {
        StrategyParams { gap_threshold: 0.15, ..Default::default() }
    }

    #[test]
    fn enters_the_cheap_leg() {
        let d = decide(&q(0.70, 0.72), &q(0.44, 0.46), &q(0.54, 0.56), &params(), 100.0, false, None, None);
        assert_eq!(d.action, Action::Enter);
        assert_eq!(d.buy_yes, Some(true));
        assert_eq!(d.entry_px, Some(0.46));
        assert_eq!(d.contracts, 108);
        let d = decide(&q(0.20, 0.22), &q(0.44, 0.46), &q(0.54, 0.56), &params(), 100.0, false, None, None);
        assert_eq!((d.action, d.buy_yes, d.entry_px), (Action::Enter, Some(false), Some(0.56)));
    }

    #[test]
    fn timing_cap_and_filters() {
        let p = params();
        let (k, y, n) = (q(0.70, 0.72), q(0.44, 0.46), q(0.54, 0.56));
        assert_eq!(decide(&k, &y, &n, &p, 10.0, false, None, None).reason, "too_early");
        assert_eq!(decide(&k, &y, &n, &p, 850.0, false, None, None).reason, "too_late");
        assert_eq!(decide(&k, &y, &n, &p, 100.0, true, None, None).reason, "traded");
        let capped = StrategyParams { max_entry_px: 0.45, ..p.clone() };
        let d = decide(&k, &y, &n, &capped, 100.0, false, None, None);
        assert_eq!((d.action, d.reason), (Action::Skip, "above_cap"));
        let lag = StrategyParams { max_poly_lag_ms: Some(250.0), ..p.clone() };
        let d = decide(&k, &y, &n, &lag, 100.0, false, None, Some((Some(300.0), None)));
        assert_eq!((d.action, d.reason), (Action::None, "poly_lag"));
        let spread = StrategyParams { max_poly_spread: Some(0.01), ..p.clone() };
        assert_eq!(decide(&k, &y, &n, &spread, 100.0, false, None, None).reason, "poly_spread");
        let hours = StrategyParams { skip_hours_utc: Some(vec![5]), ..p };
        assert_eq!(decide(&k, &y, &n, &hours, 100.0, false, Some(5), None).reason, "hours");
    }
}
