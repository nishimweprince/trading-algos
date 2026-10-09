//! One L2 book per instrument, and the two-sided quote the decision reads.
//!
//! There is deliberately no last-good cache: a side that goes empty makes the
//! quote invalid rather than serving a stale price (kpc trap 6).
//!
//! Kalshi publishes bids only, on YES and on NO. Its book is stored as YES bids
//! plus YES asks, where a NO bid at `p` is a YES ask at `1 - p`, so both venues'
//! books read the same way. Sizes are rounded to 6 dp ([`round_size`]): summed
//! fractional Kalshi deltas otherwise leave ghost levels that cross the book.
//! A crossed book is invalid, never quoted.

use std::collections::BTreeMap;

use serde::Serialize;

use crate::num::{key_px, px_key, round_size};

#[derive(Clone, Copy, Debug, Default, PartialEq, Serialize)]
pub struct Quote {
    pub valid: bool,
    pub bid: Option<f64>,
    pub ask: Option<f64>,
}

impl Quote {
    pub const INVALID: Quote = Quote { valid: false, bid: None, ask: None };

    pub fn mid(&self) -> Option<f64> {
        match (self.valid, self.bid, self.ask) {
            (true, Some(b), Some(a)) => Some((b + a) / 2.0),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum BookState {
    Ok,
    NoSnapshot,
    OneSided,
    Crossed,
    NotUpdated,
}

impl BookState {
    pub fn as_str(self) -> &'static str {
        match self {
            BookState::Ok => "ok",
            BookState::NoSnapshot => "no_snapshot",
            BookState::OneSided => "one_sided",
            BookState::Crossed => "crossed",
            BookState::NotUpdated => "not_updated",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BookSide {
    Bid,
    Ask,
}

#[derive(Clone, Debug, Default)]
pub struct Book {
    pub bids: BTreeMap<i32, f64>,
    pub asks: BTreeMap<i32, f64>,
    /// Any update at all: the ghost-gap guard needs "printed since the switch".
    pub last_update_ns: Option<i64>,
    /// None until a full snapshot arrived: deltas onto nothing are not a book.
    pub snapshot_ns: Option<i64>,
    /// Receive time minus the venue's stamp on the latest frame (ms), when stamped.
    pub lag_ms: Option<f64>,
}

impl Book {
    pub fn snapshot(&mut self, bids: &[(f64, f64)], asks: &[(f64, f64)], recv_ns: i64) {
        self.bids.clear();
        self.asks.clear();
        for &(p, s) in bids {
            if s > 0.0 {
                self.bids.insert(px_key(p), s);
            }
        }
        for &(p, s) in asks {
            if s > 0.0 {
                self.asks.insert(px_key(p), s);
            }
        }
        self.snapshot_ns = Some(recv_ns);
        self.last_update_ns = Some(recv_ns);
    }

    pub fn snapshot_keys(
        &mut self,
        bids: impl Iterator<Item = (i32, f64)>,
        asks: impl Iterator<Item = (i32, f64)>,
        recv_ns: i64,
    ) {
        self.bids.clear();
        self.asks.clear();
        for (k, s) in bids {
            if s > 0.0 {
                self.bids.insert(k, s);
            }
        }
        for (k, s) in asks {
            if s > 0.0 {
                self.asks.insert(k, s);
            }
        }
        self.snapshot_ns = Some(recv_ns);
        self.last_update_ns = Some(recv_ns);
    }

    fn side(&mut self, side: BookSide) -> &mut BTreeMap<i32, f64> {
        match side {
            BookSide::Bid => &mut self.bids,
            BookSide::Ask => &mut self.asks,
        }
    }

    pub fn set_level(&mut self, side: BookSide, key: i32, size: f64, recv_ns: i64) {
        let size = round_size(size);
        let levels = self.side(side);
        if size <= 0.0 {
            levels.remove(&key);
        } else {
            levels.insert(key, size);
        }
        self.last_update_ns = Some(recv_ns);
    }

    pub fn add_level(&mut self, side: BookSide, key: i32, delta: f64, recv_ns: i64) {
        let cur = self.side(side).get(&key).copied().unwrap_or(0.0);
        self.set_level(side, key, cur + delta, recv_ns);
    }

    pub fn invalidate(&mut self) {
        self.bids.clear();
        self.asks.clear();
        self.snapshot_ns = None;
    }

    pub fn best_bid(&self) -> Option<f64> {
        self.bids.keys().next_back().map(|&k| key_px(k))
    }

    pub fn best_ask(&self) -> Option<f64> {
        self.asks.keys().next().map(|&k| key_px(k))
    }

    pub fn bid_size_at(&self, price: f64) -> Option<f64> {
        self.bids.get(&px_key(price)).copied()
    }

    /// `ok`, or why the quote is invalid.
    pub fn state(&self, since_ns: Option<i64>) -> BookState {
        if self.snapshot_ns.is_none() {
            return BookState::NoSnapshot;
        }
        let (Some(&bid), Some(&ask)) = (self.bids.keys().next_back(), self.asks.keys().next()) else {
            return BookState::OneSided;
        };
        if bid >= ask {
            return BookState::Crossed;
        }
        if let Some(since) = since_ns
            && self.last_update_ns.is_none_or(|t| t < since)
        {
            return BookState::NotUpdated;
        }
        BookState::Ok
    }

    /// Both sides present and not crossed, a snapshot seen, and (with `since_ns`)
    /// updated since then. Anything else is invalid, never a stale price.
    pub fn quote(&self, since_ns: Option<i64>) -> Quote {
        if self.state(since_ns) != BookState::Ok {
            return Quote::INVALID;
        }
        Quote { valid: true, bid: self.best_bid(), ask: self.best_ask() }
    }

    /// Ask levels at or below `limit`, cheapest first: what a buy at `limit` can take.
    pub fn asks_upto(&self, limit: f64) -> impl Iterator<Item = (f64, f64)> + '_ {
        self.asks.iter().map(|(&k, &s)| (key_px(k), s)).take_while(move |&(p, _)| p <= limit + 1e-9)
    }

    /// Shares offered at or below `limit` (0 without a snapshot).
    pub fn depth_upto(&self, limit: f64) -> f64 {
        if self.snapshot_ns.is_none() {
            return 0.0;
        }
        self.asks_upto(limit).map(|(_, s)| s).sum()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quotes_and_states() {
        let mut b = Book::default();
        assert_eq!(b.state(None), BookState::NoSnapshot);
        b.snapshot(&[(0.44, 10.0)], &[(0.46, 5.0), (0.47, 7.0)], 10);
        assert_eq!(b.quote(None), Quote { valid: true, bid: Some(0.44), ask: Some(0.46) });
        assert_eq!(b.state(Some(11)), BookState::NotUpdated);
        b.set_level(BookSide::Bid, px_key(0.46), 1.0, 12);
        assert_eq!(b.state(None), BookState::Crossed);
        b.add_level(BookSide::Bid, px_key(0.46), -1.0, 13);
        assert_eq!(b.state(Some(11)), BookState::Ok);
        assert_eq!(b.asks_upto(0.465).collect::<Vec<_>>(), vec![(0.46, 5.0)]);
        assert_eq!(b.depth_upto(0.47), 12.0);
    }

    #[test]
    fn fractional_deltas_leave_no_ghosts() {
        let mut b = Book::default();
        b.snapshot(&[(0.5, 1.0)], &[(0.6, 1.0)], 0);
        let k = px_key(0.55);
        for d in [54.37, 0.1, 0.2, -0.3, -54.37] {
            b.add_level(BookSide::Bid, k, d, 1);
        }
        assert!(!b.bids.contains_key(&k));
    }
}
