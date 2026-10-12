//! The active exit, pure: the live engine and the shadow share it.
//!
//! A held leg is sold (a taker FAK at the bid) once Polymarket has caught up
//! with Kalshi: its best bid is within [`EXIT_MARGIN`] of the Kalshi-implied
//! price of that side and above what the position paid. Kalshi's mid only
//! counts as a price while its book is no wider than [`EXIT_MAX_KALSHI_SPREAD`].
//! On the 2026-10-10/11 tape this beat holding to settlement on every rule
//! and limit tried, with a median of 4–7 s in the trade. Fixed take-profits
//! (+3/+5/+10¢) did worse and are deliberately absent.

use crate::book::Quote;

pub const EXIT_MARGIN: f64 = 0.01;
pub const EXIT_MAX_KALSHI_SPREAD: f64 = 0.02;
/// Positions smaller than this are left to settle (dust after a partial exit).
pub const EXIT_MIN_SHARES: f64 = 1.0;

/// Kalshi's implied price of the YES (`yes`) or NO side, while its book is tight.
pub fn kalshi_fair(k: &Quote, yes: bool) -> Option<f64> {
    let (bid, ask) = (k.bid?, k.ask?);
    if !k.valid || ask < bid || ask - bid > EXIT_MAX_KALSHI_SPREAD + 1e-9 {
        return None;
    }
    let mid = (bid + ask) / 2.0;
    Some(if yes { mid } else { 1.0 - mid })
}

/// Sell now: the bid has reached Kalshi's price and is above the entry.
pub fn should_exit(bid: f64, fair: f64, entry_vwap: f64) -> bool {
    bid >= fair - EXIT_MARGIN - 1e-9 && bid > entry_vwap + 1e-9
}

#[cfg(test)]
mod tests {
    use super::*;

    fn q(bid: f64, ask: f64) -> Quote {
        Quote { valid: true, bid: Some(bid), ask: Some(ask) }
    }

    #[test]
    fn fair_only_on_a_tight_book() {
        assert_eq!(kalshi_fair(&q(0.60, 0.62), true), Some(0.61));
        assert!((kalshi_fair(&q(0.60, 0.62), false).unwrap() - 0.39).abs() < 1e-12);
        assert_eq!(kalshi_fair(&q(0.55, 0.60), true), None);
        assert_eq!(kalshi_fair(&Quote::INVALID, true), None);
    }

    #[test]
    fn exits_when_caught_up_and_in_profit() {
        assert!(should_exit(0.60, 0.61, 0.45));
        assert!(!should_exit(0.59, 0.61, 0.45)); // still 2¢ under Kalshi
        assert!(!should_exit(0.40, 0.40, 0.45)); // caught up, but at a loss: hold
    }
}
