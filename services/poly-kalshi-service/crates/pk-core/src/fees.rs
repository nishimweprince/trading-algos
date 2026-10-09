//! Polymarket's taker fee, always from the fill price (kpc trap 1).
//!
//! Per share it is `rate * (p * (1 - p)) ^ exponent`, in USDC; makers pay
//! nothing. Crypto 15-minute markets carry `rate = 0.07, exponent = 1` in their
//! Gamma `feeSchedule`: `700 * (1 - p)` bps of notional.

pub const DEFAULT_RATE: f64 = 0.07;
pub const DEFAULT_EXPONENT: f64 = 1.0;

pub fn taker_fee(price: f64, shares: f64, rate: f64, exponent: f64) -> f64 {
    if shares <= 0.0 || !(0.0 < price && price < 1.0) {
        return 0.0;
    }
    shares * rate * (price * (1.0 - price)).powf(exponent)
}

/// The fee as basis points of notional.
pub fn fee_bps(price: f64, rate: f64, exponent: f64) -> f64 {
    if 0.0 < price && price < 1.0 { 1e4 * taker_fee(price, 1.0, rate, exponent) / price } else { 0.0 }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fee_shape() {
        assert!((fee_bps(0.5, DEFAULT_RATE, DEFAULT_EXPONENT) - 350.0).abs() < 1e-9);
        assert_eq!(taker_fee(1.0, 10.0, DEFAULT_RATE, DEFAULT_EXPONENT), 0.0);
        assert_eq!(taker_fee(0.5, 0.0, DEFAULT_RATE, DEFAULT_EXPONENT), 0.0);
    }
}
