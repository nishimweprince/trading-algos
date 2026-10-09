//! The paper venue: a marketable FAK limit at the decision's entry price.
//!
//! - It meets the book `latency_ms` **after** the decision (the engine resolves
//!   it on the first line at or past that time, against the book as it stood).
//!   If the ask moved above the limit, nothing fills.
//! - It walks the asks up to the limit and takes what is there: partial fills
//!   are normal, the rest is cancelled.
//! - The fee is computed per level from that level's price.
//!
//! The naive kpc assumption (100% at the recorded touch, zero latency) is
//! logged next to it, so the gap between the two is measured (kpc trap 2).

use pk_core::Book;
use pk_core::fees::taker_fee;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct FillResult {
    pub shares: f64,
    pub cost: f64,
    pub fee: f64,
    pub levels: u32,
    pub best_ask_at_fill: Option<f64>,
}

impl FillResult {
    pub fn vwap(&self) -> Option<f64> {
        (self.shares != 0.0).then(|| self.cost / self.shares)
    }
}

#[derive(Clone, Copy, Debug)]
pub struct PaperVenue {
    pub latency_ns: i64,
}

impl PaperVenue {
    pub fn new(latency_ms: i64) -> Self {
        Self { latency_ns: latency_ms * 1_000_000 }
    }

    pub fn fill(
        &self,
        book: &Book,
        limit_px: f64,
        shares: i64,
        fee_rate: f64,
        fee_exponent: f64,
    ) -> FillResult {
        let mut remaining = shares as f64;
        let (mut cost, mut fee, mut levels) = (0.0, 0.0, 0);
        let mut best = None;
        if book.snapshot_ns.is_some() {
            best = book.best_ask();
            for (price, size) in book.asks_upto(limit_px) {
                if remaining <= 0.0 {
                    break;
                }
                let take = size.min(remaining);
                cost += take * price;
                fee += taker_fee(price, take, fee_rate, fee_exponent);
                remaining -= take;
                levels += 1;
            }
        }
        FillResult { shares: shares as f64 - remaining, cost, fee, levels, best_ask_at_fill: best }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn walks_the_asks_to_the_limit() {
        let mut b = Book::default();
        b.snapshot(&[(0.44, 10.0)], &[(0.46, 60.0), (0.47, 80.0), (0.50, 100.0)], 0);
        let r = PaperVenue::new(150).fill(&b, 0.47, 100, 0.07, 1.0);
        assert_eq!(r.shares, 100.0);
        assert_eq!(r.levels, 2);
        assert!((r.cost - (60.0 * 0.46 + 40.0 * 0.47)).abs() < 1e-9);
        assert_eq!(r.best_ask_at_fill, Some(0.46));
        let none = PaperVenue::new(150).fill(&b, 0.45, 100, 0.07, 1.0);
        assert_eq!((none.shares, none.vwap()), (0.0, None));
        let empty = PaperVenue::new(0).fill(&Book::default(), 0.5, 10, 0.07, 1.0);
        assert_eq!((empty.shares, empty.best_ask_at_fill), (0.0, None));
    }
}
