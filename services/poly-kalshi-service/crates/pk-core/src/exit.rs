//! The active exit, pure: the live engine and the paper venue share it.
//!
//! A held leg is sold (a taker FAK at the bid) once Polymarket has caught up
//! with Kalshi: its best bid is within [`EXIT_MARGIN`] of the Kalshi-implied
//! price of that side and above what the position paid. Kalshi's mid only
//! counts as a price while its book is no wider than [`EXIT_MAX_KALSHI_SPREAD`].
//! On the 2026-10-10/11 tape this beat holding to settlement on every rule
//! and limit tried, with a median of 4–7 s in the trade. Fixed take-profits
//! (+3/+5/+10¢) did worse and are deliberately absent.

use crate::book::Quote;
use crate::fees::taker_fee;

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

/// The lowest price an exit sells at: the lowest cent whose proceeds after the
/// exit's own taker fee still cover the entry, its taker fee and a cent.
pub fn exit_floor(vwap: f64, fee_rate: f64, fee_exponent: f64) -> f64 {
    let need = vwap + taker_fee(vwap, 1.0, fee_rate, fee_exponent) + 0.01;
    let mut cents = ((need - 1e-9) * 100.0).ceil();
    while cents < 99.0 && cents / 100.0 - taker_fee(cents / 100.0, 1.0, fee_rate, fee_exponent) < need - 1e-9 {
        cents += 1.0;
    }
    cents / 100.0
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
    fn floor_nets_a_cent_after_both_fees() {
        for vwap in [0.20, 0.31, 0.50, 0.80] {
            let floor = exit_floor(vwap, 0.07, 1.0);
            let net = floor - taker_fee(floor, 1.0, 0.07, 1.0) - vwap - taker_fee(vwap, 1.0, 0.07, 1.0);
            assert!(net >= 0.01 - 1e-9, "vwap {vwap}: floor {floor} nets {net}");
            let lower = floor - 0.01;
            let net_lower = lower - taker_fee(lower, 1.0, 0.07, 1.0) - vwap - taker_fee(vwap, 1.0, 0.07, 1.0);
            assert!(net_lower < 0.01 - 1e-9, "vwap {vwap}: {floor} is not the lowest");
        }
        assert_eq!(exit_floor(0.50, 0.07, 1.0), 0.55); // 0.50 + 1.75¢ + 1¢, plus ~1.7¢ to sell
    }

    #[test]
    fn exits_when_caught_up_and_in_profit() {
        assert!(should_exit(0.60, 0.61, 0.45));
        assert!(!should_exit(0.59, 0.61, 0.45)); // still 2¢ under Kalshi
        assert!(!should_exit(0.40, 0.40, 0.45)); // caught up, but at a loss: hold
    }
}
