//! The Kalshi-lead rule (`kg`): the Polymarket US pattern on Polymarket.
//!
//! On Polymarket US, Kalshi's book moved first and the Polymarket US ask
//! followed within ~2 s; buying the side cheaper than Kalshi's mid and selling
//! it into the bids 2 s later paid, holding to settlement did not (pk-pmus
//! epochs 6–7). This rule tries the same on Polymarket's 15-minute markets,
//! whose Kalshi twin is the same window on BRTI (Polymarket settles on
//! Chainlink, so holding carries basis risk; the short exit does not).
//!
//! From `after_open_s` after the open to `before_close_s` before the close,
//! on every Kalshi or Polymarket book change: buy the side whose ask is
//! `min_edge`..=`max_edge` under Kalshi's mid for that side after the taker
//! fee, once per coin-window, 10 shares, a FAK limit at the ask + 1¢ capped at
//! `min_net_edge` of edge. Paper fills land `paper_latency_ms` later against
//! the book then. The position is sold into the bids `exit_ms` after the fill
//! (the traded exit); exits at `probe_ms` and holding to settlement are
//! journaled beside it. Separate from the endgame rule's positions, P&L and halt.
//!
//! | Journal record | |
//! |---|---|
//! | `kg_decision`, `kg_fill`, `kg_no_fill` | an entry and what its paper order got |
//! | `kg_exit` | a sale into the bids at one delay (`traded` = the rule's exit) |
//! | `kg_settle` | Polymarket's outcome: the traded P&L, holding's, and each exit's |

use super::*;

/// A filled Kalshi-lead position, until Polymarket resolves its window.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct KgPos {
    pub id: u64,
    pub asset: String,
    pub open: i64,
    pub up: bool,
    pub token: String,
    pub shares: f64,
    pub usd: f64,
    pub fee: f64,
    pub entry_ns: i64,
    pub edge: f64,
    /// Exit delays still to sell at (ms after the fill).
    pub due: Vec<i64>,
    pub exits: Vec<KgExit>,
    pub epoch: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct KgExit {
    pub delay_ms: i64,
    pub traded: bool,
    pub sold: f64,
    pub proceeds: f64,
    pub fee: f64,
}

/// A Kalshi-lead order between its decision and its paper fill.
#[derive(Clone, Debug)]
pub(super) struct KgOrder {
    id: u64,
    asset: usize,
    open: i64,
    up: bool,
    token: String,
    limit: f64,
    shares: f64,
    edge: f64,
    decision_ns: i64,
    fill_at_ns: i64,
}

/// A sale of `shares` into `book`'s bids: (sold, proceeds, fee).
fn walk_bids(book: &Book, shares: f64, rate: f64, exponent: f64) -> (f64, f64, f64) {
    if book.snapshot_ns.is_none() {
        return (0.0, 0.0, 0.0);
    }
    let (mut sold, mut usd, mut fee) = (0.0, 0.0, 0.0);
    for (&k, &size) in book.bids.iter().rev() {
        let q = size.min(shares - sold);
        if q <= 1e-9 {
            break;
        }
        let px = pk_core::num::key_px(k);
        sold += q;
        usd += q * px;
        fee += taker_fee(px, q, rate, exponent);
    }
    (sold, usd, fee)
}

/// Exits later than this after their time are journaled as missed (a restart).
const LATE_MS: i64 = 5_000;

impl Engine {
    fn kg_open_cost(&self) -> f64 {
        let held: f64 = self.p.kg.iter().filter(|p| !p.due.is_empty()).map(|p| p.usd + p.fee).sum();
        held + self.kg_pending.iter().map(|o| o.limit * o.shares).sum::<f64>()
    }

    /// Evaluates the Kalshi-lead rule on asset `i`'s live window.
    pub fn evaluate_kg(&mut self, i: usize, t_ns: i64, trigger: &'static str) {
        let kg = &self.cfg.kg;
        if !kg.on || self.killed {
            return;
        }
        let now_s = t_ns / NS;
        let open = now_s - now_s.rem_euclid(WINDOW_S);
        let Some(w) = self.windows.get(&(i, open)).cloned() else { return };
        let since_open = t_ns as f64 / NS as f64 - open as f64;
        let left = (open + WINDOW_S) as f64 - t_ns as f64 / NS as f64;
        if since_open < kg.after_open_s as f64 || left <= kg.before_close_s as f64 {
            return;
        }
        let gate = |e: &mut Self, why: &'static str| e.wstate.entry((i, open)).or_default().kg_gate = Some(why);
        if self.wstate.get(&(i, open)).is_some_and(|s| s.kg_entered) {
            return gate(self, "entries_done");
        }
        let poly_silence_ms = (t_ns - self.poly_last_frame_ns) / 1_000_000;
        if poly_silence_ms > self.cfg.max_poly_silence_ms {
            return gate(self, "poly_silent");
        }
        let Some(kq) = self.kalshi_quote(i, open, t_ns) else { return gate(self, "no_kalshi") };
        if kq.ask - kq.bid > kg.kalshi_max_spread + 1e-9 {
            return gate(self, "kalshi_spread");
        }
        let mut sides: Vec<(bool, f64, f64, f64, f64)> = Vec::new(); // (up, Kalshi side mid, ask, fee, edge)
        let mut stale = false;
        for up in [true, false] {
            let Some(b) = self.books.get(w.token(up)) else { continue };
            if b.snapshot_ns.is_none() {
                continue;
            }
            if b.last_update_ns.is_none_or(|u| t_ns - u > self.cfg.max_book_age_ms * 1_000_000) {
                stale = true;
                continue;
            }
            let Some(ask) = b.best_ask() else { continue };
            if !(kg.min_px..=kg.max_px).contains(&ask) {
                continue;
            }
            let fair = if up { kq.mid } else { 1.0 - kq.mid };
            let fee = taker_fee(ask, 1.0, w.fee_rate, w.fee_exponent);
            sides.push((up, fair, ask, fee, fair - ask - fee));
        }
        if sides.is_empty() {
            return gate(self, if stale { "stale_book" } else { "price_range" });
        }
        {
            let top = sides.iter().map(|x| x.4).fold(f64::NEG_INFINITY, f64::max);
            let ws = self.wstate.entry((i, open)).or_default();
            ws.kg_best_edge = Some(ws.kg_best_edge.map_or(top, |e: f64| e.max(top)));
        }
        let (min_edge, max_edge) = (kg.min_edge, kg.max_edge);
        let Some((up, fair, ask, fee, edge)) = sides
            .iter()
            .filter(|x| x.4 >= min_edge - 1e-12 && x.4 <= max_edge + 1e-12)
            .copied()
            .max_by(|a, b| a.4.total_cmp(&b.4))
        else {
            let over = sides.iter().any(|x| x.4 > max_edge);
            return gate(self, if over { "edge_too_big" } else { "edge" });
        };
        let tick = tick_for(ask);
        let cap = fair - fee - kg.min_net_edge;
        let limit = floor_to((ask + self.cfg.limit_offset).min(cap), tick).max(ask);
        let shares = kg.shares;
        if self.kg_open_cost() + limit * shares > kg.max_open_cost_usd + 1e-9 {
            return gate(self, "max_open_cost");
        }
        let Some(book) = self.books.get(w.token(up)) else { return };
        let instant = walk(book, limit, shares, w.fee_rate, w.fee_exponent);
        let bid = book.best_bid();
        let book_age_ms = book.last_update_ns.map(|u| (t_ns - u) / 1_000_000);
        let paper_latency_ns = self.cfg.paper_latency_ms * 1_000_000;
        {
            let ws = self.wstate.entry((i, open)).or_default();
            ws.kg_entered = true;
            ws.kg_gate = Some("fired");
        }
        self.p.next_kg_id += 1;
        let id = self.p.next_kg_id;
        self.dirty = true;
        self.record(
            t_ns,
            "kg_decision",
            json!({"id": id, "asset": w.asset, "open": open, "side": if up { "up" } else { "down" }, "left": r4(left),
                   "trigger": trigger, "kalshi_bid": r4(kq.bid), "kalshi_ask": r4(kq.ask), "kalshi_side": r4(fair),
                   "kalshi_age_ms": kq.age_ms, "ask": ask, "bid": bid, "fee": r6(fee), "edge": r6(edge),
                   "limit": r4(limit), "shares": shares, "book_age_ms": book_age_ms, "poly_silence_ms": poly_silence_ms,
                   "instant_shares": r4(instant.0), "instant_usd": r4(instant.1), "epoch": self.cfg.epoch, "mode": "paper"}),
        );
        self.kg_pending.push(KgOrder {
            id,
            asset: i,
            open,
            up,
            token: w.token(up).to_owned(),
            limit,
            shares,
            edge,
            decision_ns: t_ns,
            fill_at_ns: t_ns + paper_latency_ns,
        });
    }

    /// The clock: due paper fills, then due exits.
    pub(super) fn kg_tick(&mut self, t_ns: i64) {
        let (due, keep): (Vec<KgOrder>, Vec<KgOrder>) = self.kg_pending.drain(..).partition(|o| o.fill_at_ns <= t_ns);
        self.kg_pending = keep;
        for o in due {
            self.kg_fill(o, t_ns);
        }
        for n in 0..self.p.kg.len() {
            loop {
                let p = &self.p.kg[n];
                let Some(&d) = p.due.first() else { break };
                let at = p.entry_ns + d * 1_000_000;
                if at > t_ns {
                    break;
                }
                self.kg_exit(n, d, t_ns - at > LATE_MS * 1_000_000, t_ns);
            }
        }
    }

    fn kg_fill(&mut self, o: KgOrder, t_ns: i64) {
        let Some(w) = self.windows.get(&(o.asset, o.open)).cloned() else { return };
        let book = self.books.get(&o.token);
        let ask_now = book.and_then(Book::best_ask);
        let (shares, usd, fee) =
            book.map(|b| walk(b, o.limit, o.shares, w.fee_rate, w.fee_exponent)).unwrap_or((0.0, 0.0, 0.0));
        let body = json!({"id": o.id, "asset": w.asset, "open": o.open, "side": if o.up { "up" } else { "down" },
                          "limit": r4(o.limit), "ask_now": ask_now, "held_ms": (t_ns - o.decision_ns) / 1_000_000,
                          "shares": r4(shares), "usd": r4(usd), "fee": r6(fee),
                          "avg": if shares > 0.0 { r4(usd / shares) } else { Value::Null }, "epoch": self.cfg.epoch});
        if shares <= 1e-9 {
            self.record(t_ns, "kg_no_fill", body);
            return;
        }
        self.record(t_ns, "kg_fill", body);
        let mut due: Vec<i64> = self.cfg.kg.probe_ms.iter().copied().chain([self.cfg.kg.exit_ms]).collect();
        due.sort_unstable();
        due.dedup();
        self.p.kg.push(KgPos {
            id: o.id,
            asset: w.asset.clone(),
            open: o.open,
            up: o.up,
            token: o.token,
            shares,
            usd,
            fee,
            entry_ns: t_ns,
            edge: o.edge,
            due,
            exits: Vec::new(),
            epoch: self.cfg.epoch.clone(),
        });
        self.dirty = true;
    }

    /// Sells position `n` into the bids at `delay_ms` (paper; each delay is
    /// scored as if the whole position were sold then).
    fn kg_exit(&mut self, n: usize, delay_ms: i64, late: bool, t_ns: i64) {
        let traded = delay_ms == self.cfg.kg.exit_ms;
        let p = self.p.kg[n].clone();
        let ai = self.asset_index(&p.asset).unwrap_or(0);
        let (rate, exponent) = self.windows.get(&(ai, p.open)).map_or((0.0, 1.0), |w| (w.fee_rate, w.fee_exponent));
        let book = self.books.get(&p.token);
        let fresh = book.is_some_and(|b| {
            b.last_update_ns.is_some_and(|u| t_ns - u <= self.cfg.max_book_age_ms * 1_000_000)
                && t_ns - self.poly_last_frame_ns <= self.cfg.max_poly_silence_ms * 1_000_000
        });
        let (sold, proceeds, fee) = match book {
            Some(b) if !late && fresh => walk_bids(b, p.shares, rate, exponent),
            _ => (0.0, 0.0, 0.0),
        };
        let best_bid = book.and_then(Book::best_bid);
        let entry_cost = p.usd + p.fee;
        let pnl_sold = proceeds - fee - entry_cost * sold / p.shares.max(1e-9);
        self.record(
            t_ns,
            "kg_exit",
            json!({"id": p.id, "asset": p.asset, "open": p.open, "side": if p.up { "up" } else { "down" },
                   "delay_ms": delay_ms, "traded": traded, "held_ms": (t_ns - p.entry_ns) / 1_000_000,
                   "missed": if late { Some("late") } else if !fresh { Some("stale_book") } else { None },
                   "shares": r4(p.shares), "entry_avg": r4(p.usd / p.shares.max(1e-9)), "best_bid": best_bid,
                   "sold": r4(sold), "proceeds": r4(proceeds), "fee": r6(fee),
                   "avg": if sold > 1e-9 { r4(proceeds / sold) } else { Value::Null },
                   "pnl_sold": r4(pnl_sold), "per_share_sold": if sold > 1e-9 { r4(pnl_sold / sold) } else { Value::Null },
                   "epoch": p.epoch}),
        );
        let p = &mut self.p.kg[n];
        p.due.retain(|&d| d != delay_ms);
        p.exits.push(KgExit { delay_ms, traded, sold, proceeds, fee });
        self.dirty = true;
    }

    /// Polymarket resolved `asset`'s window at `open`: settle its Kalshi-lead positions.
    pub(super) fn kg_resolved(&mut self, asset: &str, open: i64, outcome: &str, t_ns: i64) {
        let (done, keep): (Vec<KgPos>, Vec<KgPos>) =
            std::mem::take(&mut self.p.kg).into_iter().partition(|p| p.asset == asset && p.open == open);
        self.p.kg = keep;
        for p in done {
            let won = (outcome == "yes") == p.up;
            let payout = if won { 1.0 } else { 0.0 };
            let cost = p.usd + p.fee;
            let hold = p.shares * payout - cost;
            // Each exit: shares sold at the bid, any remainder held to settlement.
            let exit_pnl = |x: &KgExit| x.proceeds - x.fee + (p.shares - x.sold) * payout - cost;
            let exits: serde_json::Map<String, Value> =
                p.exits.iter().map(|x| (x.delay_ms.to_string(), json!({"pnl": r4(exit_pnl(x)), "sold": r4(x.sold)}))).collect();
            let traded = p.exits.iter().find(|x| x.traded);
            let pnl = traded.map_or(hold, exit_pnl);
            self.dirty = true;
            self.record(
                t_ns,
                "kg_settle",
                json!({"id": p.id, "asset": p.asset, "open": p.open, "side": if p.up { "up" } else { "down" },
                       "outcome": outcome, "won": won, "shares": r4(p.shares), "cost": r4(cost), "edge": r6(p.edge),
                       "pnl": r4(pnl), "per_share": r4(pnl / p.shares.max(1e-9)),
                       "sold": traded.map(|x| r4(x.sold)), "hold_pnl": r4(hold), "exits": exits, "epoch": p.epoch}),
            );
        }
    }

    pub(super) fn kg_prune(&mut self, now_s: i64) {
        let n = self.p.kg.len();
        self.p.kg.retain(|p| p.open + WINDOW_S + RESOLVE_FOR_S + 600 >= now_s);
        if self.p.kg.len() != n {
            self.dirty = true;
        }
    }
}
