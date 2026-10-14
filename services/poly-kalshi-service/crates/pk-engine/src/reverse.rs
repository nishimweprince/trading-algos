//! Reverse direction (research): Polymarket is the signal, Kalshi executes.
//!
//! Same first-crossing / cap / timing / hold-to-settlement rules as the trade:
//!
//! - `gap = poly_yes_mid - kalshi_yes_mid`; `|gap| >= threshold` enters.
//! - `gap > 0` buys Kalshi YES at the YES ask; `gap < 0` buys Kalshi NO at
//!   `1 - YES bid` (Kalshi publishes bids only: NO asks are the mirrored YES
//!   bids, cheapest first).
//! - The order walks the Kalshi ladder after the latency; partial fills kept.
//! - Kalshi's taker fee `0.07 p (1-p)` per share, rounded UP to the cent per
//!   order. Verify the 15-minute schedule before trusting magnitudes.
//! - Settlement is Kalshi's outcome; Polymarket's is recorded alongside.
//!
//! The engine's books, windows and roll are shared exactly; only decision,
//! fill and settlement differ. Markouts are off (they would read the wrong leg).

use pk_core::decision::{Action, Decision, StrategyParams, WINDOW_S};
use pk_core::ledger::Position;
use pk_core::num::{key_px, round_dp};
use pk_core::quote_json;
use pk_core::window::secs_f64;
use pk_core::{Book, Quote};
use serde_json::{Map, Value, json};

use crate::{Engine, Pending};

pub const KALSHI_FEE_RATE: f64 = 0.07;

/// Kalshi taker fee for one order: summed per level, rounded UP to the cent.
pub fn kalshi_fee(fills: &[(f64, f64)]) -> f64 {
    let raw: f64 = fills.iter().map(|(p, s)| s * KALSHI_FEE_RATE * p * (1.0 - p)).sum();
    if raw <= 0.0 { 0.0 } else { (raw * 100.0 - 1e-9).ceil() / 100.0 }
}

/// NO ask levels at or below `limit`, cheapest first: a YES bid at `p` is a NO
/// ask at `1 - p`.
pub fn kalshi_no_asks(book: &Book, limit: f64) -> Vec<(f64, f64)> {
    let mut v: Vec<(f64, f64)> =
        book.bids.iter().map(|(&k, &s)| (1.0 - key_px(k), s)).filter(|(p, _)| *p <= limit + 1e-9).collect();
    v.sort_by(|a, b| a.0.total_cmp(&b.0).then(a.1.total_cmp(&b.1)));
    v
}

/// The mirror of `decide`: Polymarket is the reference, Kalshi executes.
pub fn reverse_decide(k: &Quote, y: &Quote, p: &StrategyParams, sec_into: f64, traded: bool) -> Decision {
    let none = |reason, gap| Decision {
        action: Action::None,
        reason,
        gap,
        buy_yes: None,
        entry_px: None,
        contracts: 0,
        max_px: None,
    };
    if traded {
        return none("traded", None);
    }
    if sec_into < p.entry_early_s as f64 {
        return none("too_early", None);
    }
    if sec_into > (WINDOW_S - p.entry_late_s) as f64 {
        return none("too_late", None);
    }
    if !k.valid || !y.valid {
        return none("invalid_leg", None);
    }
    let (Some(k_mid), Some(p_mid)) = (k.mid(), y.mid()) else { return none("invalid_leg", None) };
    let gap = p_mid - k_mid;
    if gap.abs() < p.gap_threshold {
        return none("below_threshold", Some(gap));
    }
    if p.max_kalshi_spread.is_some_and(|m| k.ask.unwrap_or(0.0) - k.bid.unwrap_or(0.0) > m + 1e-9) {
        return none("kalshi_spread", Some(gap));
    }
    if p.max_poly_spread.is_some_and(|m| y.ask.unwrap_or(0.0) - y.bid.unwrap_or(0.0) > m + 1e-9) {
        return none("poly_spread", Some(gap));
    }
    let buy_yes = gap > 0.0;
    let entry = if buy_yes { k.ask } else { k.bid.map(|b| 1.0 - b) };
    let Some(entry) = entry.filter(|e| *e > 0.0 && *e < 1.0) else { return none("bad_price", Some(gap)) };
    let skip = |reason| Decision {
        action: Action::Skip,
        reason,
        gap: Some(gap),
        buy_yes: Some(buy_yes),
        entry_px: Some(entry),
        contracts: 0,
        max_px: None,
    };
    if entry > p.max_entry_px {
        return skip("above_cap");
    }
    if p.min_entry_px.is_some_and(|f| entry < f - 1e-9) {
        return skip("below_floor");
    }
    let contracts = (p.notional_usd / entry).floor() as i64;
    if contracts < 1 {
        return none("too_small", Some(gap));
    }
    Decision {
        action: Action::Enter,
        reason: "",
        gap: Some(gap),
        buy_yes: Some(buy_yes),
        entry_px: Some(entry),
        contracts,
        max_px: None,
    }
}

impl Engine {
    fn k_depth(&self, ticker: &str, buy_yes: bool, limit: f64) -> f64 {
        let Some(book) = self.books.get(ticker).filter(|b| b.snapshot_ns.is_some()) else { return 0.0 };
        if buy_yes {
            book.asks_upto(limit).map(|(_, s)| s).sum()
        } else {
            kalshi_no_asks(book, limit).iter().map(|(_, s)| s).sum()
        }
    }

    pub(crate) fn evaluate_reverse(&mut self, idx: usize, recv_ns: i64) {
        let Some(window) = self.state[idx].window.clone() else { return };
        let Some((k, y, _n)) = self.quotes(idx) else { return };
        let sec_into = secs_f64(recv_ns) - window.open_unix as f64;
        if self.observe {
            self.track_gap(idx, &k, &y, sec_into);
        }
        if self.state[idx].traded {
            return;
        }
        let mut d = reverse_decide(&k, &y, &self.params, sec_into, false);
        self.state[idx].evals += 1;
        if self.observe {
            let label = d.label();
            let st = &mut self.state[idx];
            match st.reasons.iter_mut().find(|(r, _)| *r == label) {
                Some((_, n)) => *n += 1,
                None => st.reasons.push((label, 1)),
            }
            if d.reason == "invalid_leg" {
                self.count_invalid_legs(&window);
            }
        }
        if let Some(gap) = d.gap {
            let st = &mut self.state[idx];
            st.last_gap = Some(gap);
            st.max_abs_gap = st.max_abs_gap.max(gap.abs());
        }
        if !d.consumes_window() {
            return;
        }
        self.state[idx].traded = true;
        let buy_yes = d.buy_yes == Some(true);
        if d.action == Action::Enter && self.size_to_depth {
            let offered =
                self.k_depth(&window.kalshi_ticker, buy_yes, self.limit_px(d.entry_px.unwrap_or(0.0), None));
            let capped = d.contracts.min(offered.floor() as i64).max(1);
            if capped < d.contracts {
                d.contracts = capped;
            }
        }
        let age = |id: &str| {
            self.books.get(id).and_then(|b| b.last_update_ns).map(|t| round_dp((recv_ns - t) as f64 / 1e6, 1))
        };
        let ages = [age(&window.kalshi_ticker), age(&window.yes_token), age(&window.no_token)];
        if let Some(a) = ages[0] {
            self.metrics.add("age_ms_kalshi", a);
        }
        let mut r = Map::new();
        r.insert("type".into(), "decision".into());
        r.insert("direction".into(), "reverse".into());
        r.insert("venue".into(), "kalshi".into());
        r.insert("asset".into(), window.asset.clone().into());
        r.insert("open_unix".into(), window.open_unix.into());
        r.insert("t_ns".into(), recv_ns.into());
        r.insert("sec_into".into(), round_dp(sec_into, 3).into());
        r.insert("action".into(), d.action.as_str().into());
        r.insert("reason".into(), d.reason.into());
        r.insert("gap".into(), round_dp(d.gap.unwrap_or(0.0), 4).into());
        r.insert("side".into(), if buy_yes { "yes" } else { "no" }.into());
        r.insert("entry_px".into(), d.entry_px.into());
        r.insert("contracts".into(), d.contracts.into());
        r.insert("kalshi".into(), quote_json(&k));
        r.insert("poly_yes".into(), quote_json(&y));
        r.insert("age_ms".into(), json!({"kalshi": ages[0], "poly_yes": ages[1], "poly_no": ages[2]}));
        r.insert(
            "depth_at_limit".into(),
            d.entry_px.map(|px| round_dp(self.k_depth(&window.kalshi_ticker, buy_yes, px), 2)).into(),
        );
        r.insert(
            "params".into(),
            json!({"gap_threshold": self.params.gap_threshold, "max_entry_px": self.params.max_entry_px,
                   "notional_usd": self.params.notional_usd}),
        );
        if d.action == Action::Enter {
            let blocked = self.submit_reverse(&window, &d, recv_ns);
            r.insert("blocked".into(), blocked.into());
        }
        self.state[idx].decision = Some(r.clone());
        self.write(recv_ns, Value::Object(r), true);
    }

    fn submit_reverse(&mut self, w: &pk_core::Window, d: &Decision, recv_ns: i64) -> Option<String> {
        let Some(venue) = self.venue else { return Some("mode_off".into()) };
        let entry = d.entry_px.expect("an entry has a price");
        let est = d.contracts as f64 * entry + kalshi_fee(&[(entry, d.contracts as f64)]);
        if let Some(b) = self.risk.check(&self.ledger, est, recv_ns) {
            self.count(&format!("blocked_{b}"));
            return Some(b.to_owned());
        }
        self.pending.push(Pending {
            due_ns: recv_ns + venue.latency_ns,
            window: w.clone(),
            token: w.kalshi_ticker.clone(),
            side: if d.buy_yes == Some(true) { "yes" } else { "no" },
            decision: *d,
            decision_ns: recv_ns,
        });
        None
    }

    pub(crate) fn fill_reverse(&mut self, order: Pending, recv_ns: i64) {
        let (d, w) = (&order.decision, &order.window);
        let entry = d.entry_px.expect("an entry has a price");
        let limit = self.limit_px(entry, None);
        let buy_yes = order.side == "yes";
        let (levels, best) = match self.books.get(&order.token).filter(|b| b.snapshot_ns.is_some()) {
            Some(book) if buy_yes => (book.asks_upto(limit).collect::<Vec<_>>(), book.best_ask()),
            Some(book) => (kalshi_no_asks(book, limit), book.best_bid().map(|b| round_dp(1.0 - b, 4))),
            None => (Vec::new(), None),
        };
        let mut remaining = d.contracts as f64;
        let mut cost = 0.0;
        let mut taken = Vec::new();
        let mut n_levels = 0u32;
        for (price, size) in &levels {
            if remaining <= 0.0 {
                break;
            }
            let take = size.min(remaining);
            cost += take * price;
            taken.push((*price, take));
            remaining -= take;
            n_levels += 1;
        }
        let filled = d.contracts as f64 - remaining;
        let fee = kalshi_fee(&taken);
        let record = json!({
            "type": "fill", "direction": "reverse", "venue": "kalshi",
            "asset": w.asset, "open_unix": w.open_unix, "t_ns": recv_ns,
            "latency_ms": round_dp((recv_ns - order.decision_ns) as f64 / 1e6, 1),
            "side": order.side, "limit_px": limit, "ordered": d.contracts, "shares": filled,
            "vwap": (filled != 0.0).then(|| round_dp(cost / filled, 4)),
            "cost": round_dp(cost, 4), "fee": round_dp(fee, 4), "levels": n_levels,
            "best_ask_at_fill": best, "touch_px": entry, "touch_shares": d.contracts,
            "touch_fee": round_dp(kalshi_fee(&[(entry, d.contracts as f64)]), 4),
            "depth_at_limit": round_dp(levels.iter().map(|(_, s)| s).sum::<f64>(), 2),
        });
        self.count(if filled > 0.0 { "fills" } else { "no_fills" });
        if filled > 0.0 {
            let position = Position {
                asset: w.asset.clone(),
                open_unix: w.open_unix,
                side: order.side.into(),
                shares: filled,
                cost,
                fee,
                entry_ns: recv_ns,
                status: "open".into(),
                outcome: None,
                payout: None,
                pnl: None,
                settled_ns: None,
            };
            if self.ledger.buy(position).is_err() {
                self.count("engine_errors");
            }
            let known = self
                .resolutions
                .get(&(w.asset.clone(), w.open_unix))
                .and_then(|s| s.iter().find(|(v, _)| v == "kalshi").map(|(_, o)| o.clone()));
            if let Some(outcome) = known {
                self.settle(&w.asset.clone(), w.open_unix, &outcome, recv_ns);
            }
        }
        self.write(recv_ns, record, true);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fee_rounds_up_per_order() {
        assert_eq!(kalshi_fee(&[(0.5, 1.0)]), 0.02); // 0.0175 -> 0.02
        assert_eq!(kalshi_fee(&[(0.5, 100.0)]), 1.75);
        assert_eq!(kalshi_fee(&[]), 0.0);
    }

    #[test]
    fn no_asks_mirror_yes_bids() {
        let mut b = Book::default();
        b.snapshot(&[(0.60, 10.0), (0.55, 5.0)], &[(0.65, 1.0)], 0);
        let v = kalshi_no_asks(&b, 0.42);
        assert_eq!(v.len(), 1);
        assert!((v[0].0 - 0.40).abs() < 1e-12 && v[0].1 == 10.0);
    }
}
