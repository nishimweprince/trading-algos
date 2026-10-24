//! The maker arm (paper): resting quotes on Limitless around Polymarket's mid.
//!
//! Why a maker: Limitless holds every taker order for `takerDelayMs` (500 ms
//! on the 15-minute markets) while resting post-only orders and cancels are
//! never held. A maker that pulls a stale quote within a round trip is gone
//! before a held taker can reach it. The taker arm showed the other side of
//! that: every entry whose book repriced in the hold missed, and the fills
//! that remained lost.
//!
//! - **Fair value:** the mirrored Polymarket market's Up mid (same rules,
//!   same oracle), when its spread is at most `max_poly_spread` and its book
//!   is fresh. No fair value, no quotes.
//! - **Quotes:** buy YES at `fair − h − skew` and buy NO at `1 − (fair + h − skew)`
//!   (Limitless sells YES only from inventory, so the ask is a NO bid).
//!   `skew = skew_k · net / max_pos`, net = YES − NO shares. Prices are kept
//!   post-only against Limitless's book, the side that would add inventory
//!   past `max_pos` is not quoted, and nothing is quoted in the last `stop_s`
//!   of the market or while a pause is on.
//! - **Pauses:** a Kalshi YES-mid jump (15-minute BTC/ETH) or a Polymarket mid
//!   jump of `pause_jump` in 1 s pulls both quotes for `pause_ms`.
//! - The fair value also needs Polymarket's book to have been seen since the
//!   last reconnect, and the market's own Limitless book.
//! - **Order life (paper):** a post or cancel takes effect half the measured
//!   round trip after it is decided. Requotes need a move of `requote_ticks`
//!   and `min_requote_ms` since the last change; cancels are never delayed.
//! - **Fills come from Limitless's own trade prints**, polled seconds after
//!   the fact, against the quote that was live at the print's time: a taker
//!   selling YES below our bid, or buying YES above our ask, fills us; at
//!   exactly our price the size resting there when we joined trades first.
//!   Size is the smaller of the print and what remains of the quote.
//! - **Inventory:** YES and NO shares per market. A YES + NO pair always pays
//!   $1, so matched pairs are locked P&L; the net settles on Limitless's
//!   outcome. Makers pay no fee; the rebate (`makerRebateMult` × the taker's
//!   fee) is estimated and journaled, never added to P&L.
//! - **Measured:** spread captured against fair at each fill, markouts at 5 s
//!   and 30 s against Polymarket's fair (adverse selection), quote uptime per
//!   market, fills, net ¢ per share filled.

use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};

use pk_core::book::Book;
use pk_core::journal::{JsonlDaily, Sink};
use pk_core::ledger::utc_day;
use pk_core::num::{px_key, round_dp, round_opt};
use pk_stats::Sample;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

use super::markets::{Kind, MakerMarket, Print};

const TICK: f64 = 0.001;
/// Polymarket sends nothing while a book is unchanged (quiet hourly and daily
/// markets go minutes between updates), so age alone cannot mean stale: a
/// reconnect invalidates every book, and this only bounds a silent feed.
const FAIR_MAX_AGE_MS: i64 = 300_000;
const TRAIL_MS: i64 = 60_000;
const SPAN_KEEP_MS: i64 = 180_000;
const SEEN_KEEP: usize = 2_000;
const RECENT: usize = 60;

#[derive(Clone, Debug)]
pub struct MakerCfg {
    pub series: Vec<String>,
    pub shares: f64,
    pub half_spread: f64,
    pub skew_k: f64,
    pub max_pos: f64,
    pub max_poly_spread: f64,
    pub pause_jump: f64,
    pub pause_ms: i64,
    pub requote_ticks: f64,
    pub min_requote_ms: i64,
    pub stop_s_15m: i64,
    pub stop_s_1h: i64,
    pub stop_s_1d: i64,
    pub max_open_usd: f64,
    pub daily_loss_usd: f64,
    pub paper_cash: f64,
    pub trades_poll_15m_ms: u64,
    pub trades_poll_other_ms: u64,
}

impl MakerCfg {
    fn stop_s(&self, k: Kind) -> i64 {
        match k {
            Kind::M15 => self.stop_s_15m,
            Kind::H1 => self.stop_s_1h,
            Kind::D1 => self.stop_s_1d,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Side {
    /// Buy YES at the bid.
    Bid,
    /// Buy NO at 1 − ask (sells YES exposure at the ask).
    Ask,
}

/// One resting quote's life (paper), in venue milliseconds.
#[derive(Clone, Debug)]
pub struct Span {
    pub px: f64,
    pub remaining: f64,
    pub live_ms: i64,
    pub dead_ms: Option<i64>,
    /// Shares resting at this exact price on Limitless when we joined.
    pub queue: f64,
}

impl Span {
    fn live_at(&self, t: i64) -> bool {
        t >= self.live_ms && self.dead_ms.is_none_or(|d| t < d)
    }
}

/// Does a print fill this side's span, and for how many shares (consuming the queue)?
pub fn fill_from_print(side: Side, span: &mut Span, p: &Print) -> f64 {
    if span.remaining <= 1e-9 || !span.live_at(p.t_ms) {
        return 0.0;
    }
    let (through, at) = match side {
        Side::Bid => (!p.taker_buys_yes && p.yes_px < span.px - 1e-9, !p.taker_buys_yes && (p.yes_px - span.px).abs() <= 1e-9),
        Side::Ask => (p.taker_buys_yes && p.yes_px > span.px + 1e-9, p.taker_buys_yes && (p.yes_px - span.px).abs() <= 1e-9),
    };
    let mut avail = if through || at { p.shares } else { 0.0 };
    if at {
        let used = avail.min(span.queue);
        span.queue -= used;
        avail -= used;
    }
    let got = avail.min(span.remaining);
    span.remaining -= got;
    got
}

/// The prices to quote, or None for a side that must not be quoted.
#[allow(clippy::too_many_arguments)]
pub fn desired(
    fair: f64,
    half: f64,
    skew_k: f64,
    net: f64,
    max_pos: f64,
    lim_bid: Option<f64>,
    lim_ask: Option<f64>,
) -> (Option<f64>, Option<f64>) {
    let skew = if max_pos > 0.0 { skew_k * net / max_pos } else { 0.0 };
    let mut bid = ((fair - half - skew) / TICK + 1e-6).floor() * TICK;
    let mut ask = ((fair + half - skew) / TICK - 1e-6).ceil() * TICK;
    if let Some(a) = lim_ask
        && bid >= a - 1e-9
    {
        bid = a - TICK;
    }
    if let Some(b) = lim_bid
        && ask <= b + 1e-9
    {
        ask = b + TICK;
    }
    let ok = |p: f64| (0.01 - 1e-9..=0.99 + 1e-9).contains(&p);
    let bid = (ok(bid) && net < max_pos - 1e-9).then(|| round_dp(bid, 3));
    let ask = (ok(ask) && net > -max_pos + 1e-9).then(|| round_dp(ask, 3));
    match (bid, ask) {
        (Some(b), Some(a)) if b >= a - 1e-9 => (None, None),
        x => x,
    }
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Inventory {
    pub yes: f64,
    pub yes_cost: f64,
    pub no: f64,
    pub no_cost: f64,
    pub fills: u64,
    pub shares_filled: f64,
    pub rebate_est: f64,
    pub close_ms: i64,
    pub asset: String,
    pub series: String,
    pub settled: Option<String>,
    pub pnl: Option<f64>,
    pub settled_ms: Option<i64>,
}

impl Inventory {
    pub fn net(&self) -> f64 {
        self.yes - self.no
    }

    pub fn cost(&self) -> f64 {
        self.yes_cost + self.no_cost
    }

    /// P&L if settled on `outcome`.
    pub fn settle_pnl(&self, outcome: &str) -> f64 {
        let payout = if outcome == "yes" { self.yes } else { self.no };
        payout - self.cost()
    }

    /// Matched pairs pay $1 whatever happens: their P&L is locked.
    pub fn locked_pnl(&self) -> f64 {
        let pairs = self.yes.min(self.no);
        if pairs <= 0.0 {
            return 0.0;
        }
        let y = if self.yes > 0.0 { self.yes_cost / self.yes } else { 0.0 };
        let n = if self.no > 0.0 { self.no_cost / self.no } else { 0.0 };
        pairs * (1.0 - y - n)
    }
}

#[derive(Default, Serialize, Deserialize)]
struct Persist {
    start_cash: f64,
    inventories: BTreeMap<String, Inventory>,
    realised_by_day: BTreeMap<String, f64>,
}

struct Mkt {
    m: MakerMarket,
    lbook: Book,
    l_version: Option<i64>,
    up: Book,
    down: Book,
    fair_trail: VecDeque<(i64, f64)>,
    pause_until_ms: i64,
    bid: Option<Span>,
    ask: Option<Span>,
    spans: Vec<(Side, Span)>,
    last_change_ms: i64,
    seen: HashSet<String>,
    seen_order: VecDeque<String>,
    prints: u64,
    print_shares: f64,
    quoting_ms: i64,
    window_ms: i64,
    last_tick_ms: Option<i64>,
    pauses: u64,
    requotes: u64,
}

impl Mkt {
    fn new(m: MakerMarket) -> Self {
        Self {
            m,
            lbook: Book::default(),
            l_version: None,
            up: Book::default(),
            down: Book::default(),
            fair_trail: VecDeque::new(),
            pause_until_ms: 0,
            bid: None,
            ask: None,
            spans: Vec::new(),
            last_change_ms: 0,
            seen: HashSet::new(),
            seen_order: VecDeque::new(),
            prints: 0,
            print_shares: 0.0,
            quoting_ms: 0,
            window_ms: 0,
            last_tick_ms: None,
            pauses: 0,
            requotes: 0,
        }
    }

    /// Polymarket's Up mid, if its book is tight and fresh (else 1 − Down's).
    fn fair(&self, now_ms: i64, max_spread: f64) -> Option<f64> {
        let pick = |b: &Book, up: bool| -> Option<f64> {
            let q = b.quote(None);
            let (bid, ask) = (q.bid?, q.ask?);
            let fresh = b.last_update_ns.is_some_and(|u| now_ms - u / 1_000_000 <= FAIR_MAX_AGE_MS);
            (q.valid && ask >= bid && ask - bid <= max_spread + 1e-9 && fresh)
                .then(|| if up { (bid + ask) / 2.0 } else { 1.0 - (bid + ask) / 2.0 })
        };
        pick(&self.up, true).or_else(|| pick(&self.down, false))
    }

    fn fair_at(&self, t_ms: i64) -> Option<f64> {
        self.fair_trail.iter().take_while(|(t, _)| *t <= t_ms).last().map(|(_, f)| *f)
    }
}

#[derive(Default)]
struct Totals {
    fills: u64,
    shares: f64,
    capture: Sample,
    markout5: Sample,
    markout30: Sample,
    net_per_share: Sample,
}

pub struct Maker {
    pub cfg: MakerCfg,
    mkts: BTreeMap<String, Mkt>,
    /// Polymarket token → (slug, is Up).
    poly_owner: HashMap<String, (String, bool)>,
    inv: BTreeMap<String, Inventory>,
    realised_by_day: BTreeMap<String, f64>,
    start_cash: f64,
    state_path: std::path::PathBuf,
    journal: JsonlDaily,
    stamp: Map<String, Value>,
    rtt_ms: f64,
    totals: Totals,
    /// Markouts waiting for their time: (due venue ms, slug, side, price).
    markouts: Vec<(i64, i64, String, Side, f64)>,
    counters: BTreeMap<String, u64>,
    recent: VecDeque<Value>,
    kalshi_trail: HashMap<String, VecDeque<(i64, f64)>>,
}

impl Maker {
    pub fn new(cfg: MakerCfg, dir: &std::path::Path, stamp: Map<String, Value>) -> Self {
        let state_path = dir.join("state.json");
        let p: Persist = std::fs::read(&state_path).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default();
        let start_cash = if p.start_cash > 0.0 { p.start_cash } else { cfg.paper_cash };
        Self {
            mkts: BTreeMap::new(),
            poly_owner: HashMap::new(),
            inv: p.inventories,
            realised_by_day: p.realised_by_day,
            start_cash,
            state_path,
            journal: JsonlDaily::new(dir.join("journal")),
            stamp,
            rtt_ms: 50.0,
            totals: Totals::default(),
            markouts: Vec::new(),
            counters: BTreeMap::new(),
            recent: VecDeque::new(),
            kalshi_trail: HashMap::new(),
            cfg,
        }
    }

    fn save(&self) {
        let p = Persist { start_cash: self.start_cash, inventories: self.inv.clone(), realised_by_day: self.realised_by_day.clone() };
        if let Ok(b) = serde_json::to_vec_pretty(&p) {
            if let Some(d) = self.state_path.parent() {
                let _ = std::fs::create_dir_all(d);
            }
            let tmp = self.state_path.with_extension("json.tmp");
            if std::fs::write(&tmp, b).is_ok() {
                let _ = std::fs::rename(&tmp, &self.state_path);
            }
        }
    }

    fn count(&mut self, k: &str) {
        *self.counters.entry(k.to_owned()).or_insert(0) += 1;
    }

    fn record(&mut self, t_ns: i64, kind: &str, mut r: Value) {
        if let Some(o) = r.as_object_mut() {
            o.insert("type".into(), kind.into());
            o.insert("t_ns".into(), t_ns.into());
            for (k, v) in &self.stamp {
                o.insert(k.clone(), v.clone());
            }
        }
        self.journal.write(t_ns, &r);
        if kind != "limm_quote" {
            if self.recent.len() == RECENT {
                self.recent.pop_back();
            }
            self.recent.push_front(r);
        }
    }

    pub fn set_rtt(&mut self, ms: f64) {
        self.rtt_ms = ms;
    }

    pub fn has(&self, slug: &str) -> bool {
        self.mkts.contains_key(slug)
    }

    pub fn add(&mut self, m: MakerMarket, t_ns: i64) {
        if self.mkts.contains_key(&m.slug) {
            return;
        }
        self.poly_owner.insert(m.poly_up.clone(), (m.slug.clone(), true));
        self.poly_owner.insert(m.poly_down.clone(), (m.slug.clone(), false));
        self.record(t_ns, "limm_market", serde_json::to_value(&m).unwrap_or(Value::Null));
        self.count("markets");
        self.mkts.insert(m.slug.clone(), Mkt::new(m));
    }

    /// Markets to subscribe, and drop those long closed.
    pub fn slugs(&self) -> Vec<String> {
        self.mkts.keys().cloned().collect()
    }

    pub fn poly_tokens(&self) -> std::collections::BTreeSet<String> {
        self.poly_owner.keys().cloned().collect()
    }

    pub fn prune(&mut self, now_ms: i64) {
        let gone: Vec<String> = self.mkts.iter().filter(|(_, m)| now_ms > m.m.close_ms + 300_000).map(|(k, _)| k.clone()).collect();
        for s in gone {
            if let Some(m) = self.mkts.remove(&s) {
                self.poly_owner.remove(&m.m.poly_up);
                self.poly_owner.remove(&m.m.poly_down);
            }
        }
    }

    /// Markets whose trades are due a poll: (slug, yes token, no token).
    pub fn poll_due(&self, now_ms: i64, last: &HashMap<String, i64>) -> Vec<(String, String, String)> {
        self.mkts
            .values()
            .filter(|m| now_ms < m.m.close_ms + 60_000)
            .filter(|m| {
                let every = if m.m.kind == Kind::M15 { self.cfg.trades_poll_15m_ms } else { self.cfg.trades_poll_other_ms } as i64;
                last.get(&m.m.slug).is_none_or(|t| now_ms - t >= every)
            })
            .map(|m| (m.m.slug.clone(), m.m.yes_token.clone(), m.m.no_token.clone()))
            .collect()
    }

    /// A Limitless `orderbookUpdate` (the shared Limitless socket's line).
    pub fn on_limitless(&mut self, text: &str, t_ns: i64) {
        let Ok(v) = serde_json::from_str::<Value>(text) else { return };
        if v.get("type").and_then(Value::as_str) == Some("pk_reconnect") {
            for m in self.mkts.values_mut() {
                m.lbook.invalidate();
                m.l_version = None;
            }
            return;
        }
        if v.get("ev").and_then(Value::as_str) != Some("orderbookUpdate") {
            return;
        }
        let d = &v["d"];
        let slug = d.get("marketSlug").and_then(Value::as_str).unwrap_or_default();
        let Some(m) = self.mkts.get_mut(slug) else { return };
        let version = d.get("version").and_then(Value::as_i64).unwrap_or(0);
        if m.l_version.is_some_and(|last| version < last && version != 0 || version == 0 && last > 0) {
            return;
        }
        m.l_version = Some(version);
        let side = |k: &str| -> Vec<(f64, f64)> {
            d.pointer(&format!("/orderbook/{k}"))
                .and_then(Value::as_array)
                .map(|l| l.iter().filter_map(|x| Some((x.get("price")?.as_f64()?, x.get("size")?.as_f64()? / 1e6))).collect())
                .unwrap_or_default()
        };
        m.lbook.snapshot(&side("bids"), &side("asks"), t_ns);
        let slug = slug.to_owned();
        self.requote(&slug, t_ns);
    }

    /// A Polymarket frame (`p` line).
    pub fn on_poly(&mut self, text: &str, t_ns: i64) {
        let Ok(msgs) = pk_engine::parse::poly_msgs(text) else { return };
        let mut touched: Vec<String> = Vec::new();
        for msg in msgs.iter().flatten() {
            match msg.kind() {
                Some("book") => {
                    let Some(id) = msg.asset_id.as_ref().map(|i| i.as_str()) else { continue };
                    let Some((slug, up)) = self.poly_owner.get(id).cloned() else { continue };
                    let Some(m) = self.mkts.get_mut(&slug) else { continue };
                    let lv = |v: &Option<Vec<pk_engine::parse::Level>>| {
                        v.as_deref().unwrap_or_default().iter().map(|l| (px_key(l.price.0), l.size.0)).collect::<Vec<_>>()
                    };
                    let b = if up { &mut m.up } else { &mut m.down };
                    b.snapshot_keys(lv(&msg.bids).into_iter(), lv(&msg.asks).into_iter(), t_ns);
                    if !touched.contains(&slug) {
                        touched.push(slug);
                    }
                }
                Some("price_change") => {
                    for c in msg.price_changes.as_deref().unwrap_or_default() {
                        let Some(id) = c.asset_id.as_ref().map(|i| i.as_str()) else { continue };
                        let Some((slug, up)) = self.poly_owner.get(id).cloned() else { continue };
                        let Some(m) = self.mkts.get_mut(&slug) else { continue };
                        let b = if up { &mut m.up } else { &mut m.down };
                        if b.snapshot_ns.is_none() {
                            continue;
                        }
                        let side = if c.side.as_deref() == Some("BUY") { pk_core::BookSide::Bid } else { pk_core::BookSide::Ask };
                        b.set_level(side, px_key(c.price.0), c.size.0, t_ns);
                        if !touched.contains(&slug) {
                            touched.push(slug);
                        }
                    }
                }
                _ => {}
            }
        }
        for slug in touched {
            self.on_fair(&slug, t_ns);
            self.requote(&slug, t_ns);
        }
    }

    /// Polymarket reconnect: every fair value is unknown until fresh books.
    pub fn invalidate_poly(&mut self) {
        for m in self.mkts.values_mut() {
            m.up.invalidate();
            m.down.invalidate();
        }
        self.count("poly_resets");
    }

    fn on_fair(&mut self, slug: &str, t_ns: i64) {
        let now_ms = t_ns / 1_000_000;
        let (jump, max_spread) = (self.cfg.pause_jump, self.cfg.max_poly_spread);
        let pause_ms = self.cfg.pause_ms;
        let Some(m) = self.mkts.get_mut(slug) else { return };
        let Some(f) = m.fair(now_ms, max_spread) else { return };
        let prev = m.fair_trail.iter().take_while(|(t, _)| *t <= now_ms - 1000).last().map(|(_, v)| *v);
        m.fair_trail.push_back((now_ms, f));
        while m.fair_trail.front().is_some_and(|(t, _)| *t < now_ms - TRAIL_MS) {
            m.fair_trail.pop_front();
        }
        if prev.is_some_and(|p| (f - p).abs() >= jump - 1e-9) && now_ms >= m.pause_until_ms {
            m.pause_until_ms = now_ms + pause_ms;
            m.pauses += 1;
            let r = json!({"slug": slug, "source": "polymarket", "from": prev, "to": f});
            self.count("pauses_poly");
            self.record(t_ns, "limm_pause", r);
            self.requote(slug, t_ns);
        }
    }

    /// The taker arm's Kalshi YES mid for a 15-minute asset: a jump pauses that asset's 15-minute quotes.
    pub fn on_kalshi_mid(&mut self, asset: &str, mid: f64, t_ns: i64) {
        let now_ms = t_ns / 1_000_000;
        let tr = self.kalshi_trail.entry(asset.to_owned()).or_default();
        if tr.back().is_some_and(|(_, v)| (*v - mid).abs() < 1e-9) {
            return; // the mid did not move: nothing to record
        }
        let prev = tr.iter().take_while(|(t, _)| *t <= now_ms - 1000).last().map(|(_, v)| *v);
        tr.push_back((now_ms, mid));
        while tr.len() > 1 && tr[1].0 <= now_ms - 2000 {
            tr.pop_front();
        }
        if !prev.is_some_and(|p| (mid - p).abs() >= self.cfg.pause_jump - 1e-9) {
            return;
        }
        let pause_ms = self.cfg.pause_ms;
        let slugs: Vec<String> = self
            .mkts
            .values_mut()
            .filter(|m| m.m.kind == Kind::M15 && m.m.asset == asset && now_ms >= m.pause_until_ms && now_ms < m.m.close_ms)
            .map(|m| {
                m.pause_until_ms = now_ms + pause_ms;
                m.pauses += 1;
                m.m.slug.clone()
            })
            .collect();
        for s in slugs {
            self.count("pauses_kalshi");
            self.record(t_ns, "limm_pause", json!({"slug": s, "source": "kalshi", "from": prev, "to": mid}));
            self.requote(&s, t_ns);
        }
    }

    fn open_cost(&self) -> f64 {
        self.inv.values().filter(|i| i.settled.is_none()).map(Inventory::cost).sum()
    }

    fn realised_today(&self, t_ns: i64) -> f64 {
        self.realised_by_day.get(&utc_day(t_ns)).copied().unwrap_or(0.0)
    }

    /// Bring a market's paper quotes to what they should be now.
    fn requote(&mut self, slug: &str, t_ns: i64) {
        let now_ms = t_ns / 1_000_000;
        let half_rtt = (self.rtt_ms / 2.0) as i64;
        let blocked_risk = self.open_cost() >= self.cfg.max_open_usd - 1e-9 || -self.realised_today(t_ns) >= self.cfg.daily_loss_usd;
        let net = self.inv.get(slug).map_or(0.0, Inventory::net);
        let cfg = self.cfg.clone();
        let Some(m) = self.mkts.get_mut(slug) else { return };
        let stop = m.m.close_ms - cfg.stop_s(m.m.kind) * 1000;
        let fair = m.fair(now_ms, cfg.max_poly_spread);
        let lq = m.lbook.quote(None);
        let can = now_ms >= m.m.open_ms && now_ms < stop && now_ms >= m.pause_until_ms && m.lbook.snapshot_ns.is_some();
        let (want_bid, want_ask) = match (can, fair) {
            (true, Some(f)) => {
                let h = m.m.max_spread.map_or(cfg.half_spread, |s| cfg.half_spread.min(s));
                desired(f, h, cfg.skew_k, net, cfg.max_pos, lq.bid, lq.ask)
            }
            _ => (None, None),
        };
        // Risk: no side that adds cost once a cap is hit (both sides add cost).
        let (want_bid, want_ask) = if blocked_risk { (None, None) } else { (want_bid, want_ask) };
        let mut changed = false;
        for side in [Side::Bid, Side::Ask] {
            let want = if side == Side::Bid { want_bid } else { want_ask };
            let cur = if side == Side::Bid { m.bid.as_ref().map(|s| s.px) } else { m.ask.as_ref().map(|s| s.px) };
            let replace = match (cur, want) {
                (None, None) => false,
                (Some(_), None) => true, // cancels are never delayed
                (None, Some(_)) => now_ms - m.last_change_ms >= cfg.min_requote_ms,
                (Some(c), Some(w)) => (c - w).abs() >= cfg.requote_ticks * TICK - 1e-9 && now_ms - m.last_change_ms >= cfg.min_requote_ms,
            };
            if !replace {
                continue;
            }
            let slot = if side == Side::Bid { &mut m.bid } else { &mut m.ask };
            if let Some(mut old) = slot.take() {
                old.dead_ms = Some(now_ms + half_rtt);
                m.spans.push((side, old));
            }
            if let Some(px) = want {
                // Queue ahead: Limitless's size resting at exactly this price (YES bids for the bid,
                // YES asks for the ask, i.e. NO bids at 1 − px).
                let book_side = if side == Side::Bid { &m.lbook.bids } else { &m.lbook.asks };
                let queue = book_side.get(&px_key(px)).copied().unwrap_or(0.0);
                *slot = Some(Span { px, remaining: cfg.shares, live_ms: now_ms + half_rtt, dead_ms: None, queue });
            }
            changed = true;
        }
        if changed {
            m.last_change_ms = now_ms;
            m.requotes += 1;
            let r = json!({"slug": slug, "fair": fair.map(|f| round_dp(f, 4)),
                           "bid": m.bid.as_ref().map(|s| s.px), "ask": m.ask.as_ref().map(|s| s.px),
                           "lim_bid": lq.bid, "lim_ask": lq.ask, "net": net});
            self.record(t_ns, "limm_quote", r);
        }
    }

    /// New trade prints for a market (polled). Returns the ones not seen before (for the tape).
    pub fn on_prints(&mut self, slug: &str, prints: Vec<Print>, t_ns: i64) -> Vec<Print> {
        let Some(m) = self.mkts.get_mut(slug) else { return Vec::new() };
        let mut fresh: Vec<Print> = prints.into_iter().filter(|p| !m.seen.contains(&p.key)).collect();
        fresh.sort_by_key(|p| p.t_ms);
        for p in &fresh {
            m.seen.insert(p.key.clone());
            m.seen_order.push_back(p.key.clone());
            if m.seen_order.len() > SEEN_KEEP
                && let Some(old) = m.seen_order.pop_front()
            {
                m.seen.remove(&old);
            }
            m.prints += 1;
            m.print_shares += p.shares;
        }
        let mut fills: Vec<(Side, f64, f64, i64)> = Vec::new();
        for p in &fresh {
            for (side, span) in m.spans.iter_mut().map(|(s, sp)| (*s, sp)).chain(
                [(Side::Bid, m.bid.as_mut()), (Side::Ask, m.ask.as_mut())].into_iter().filter_map(|(s, o)| o.map(|sp| (s, sp))),
            ) {
                let got = fill_from_print(side, span, p);
                if got > 1e-9 {
                    fills.push((side, span.px, got, p.t_ms));
                }
            }
        }
        // A quote filled in full is gone; the next requote replaces it.
        if m.bid.as_ref().is_some_and(|s| s.remaining <= 1e-9) {
            m.bid = None;
        }
        if m.ask.as_ref().is_some_and(|s| s.remaining <= 1e-9) {
            m.ask = None;
        }
        let now_ms = t_ns / 1_000_000;
        m.spans.retain(|(_, s)| s.dead_ms.is_none_or(|d| now_ms - d < SPAN_KEEP_MS));
        let market = m.m.clone();
        let fair_at: Vec<Option<f64>> = fills.iter().map(|f| m.fair_at(f.3)).collect();
        for ((side, px, shares, at_ms), fair) in fills.into_iter().zip(fair_at) {
            self.book_fill(&market, side, px, shares, at_ms, fair, t_ns);
        }
        if !fresh.is_empty() {
            self.requote(slug, t_ns);
        }
        fresh
    }

    #[allow(clippy::too_many_arguments)]
    fn book_fill(&mut self, m: &MakerMarket, side: Side, px: f64, shares: f64, at_ms: i64, fair: Option<f64>, t_ns: i64) {
        let inv = self.inv.entry(m.slug.clone()).or_insert_with(|| Inventory {
            close_ms: m.close_ms,
            asset: m.asset.clone(),
            series: m.series.clone(),
            ..Default::default()
        });
        // Bid: bought YES at px. Ask: bought NO at 1 − px.
        let (token_px, label) = match side {
            Side::Bid => {
                inv.yes += shares;
                inv.yes_cost += shares * px;
                (px, "yes")
            }
            Side::Ask => {
                inv.no += shares;
                inv.no_cost += shares * (1.0 - px);
                (1.0 - px, "no")
            }
        };
        inv.fills += 1;
        inv.shares_filled += shares;
        // What the taker paid in fee (Limitless: 0.05 · p(1 − p) per share on the taker's token).
        let rebate = m.rebate_mult.unwrap_or(0.0) * 0.05 * token_px * (1.0 - token_px) * shares;
        inv.rebate_est += rebate;
        let net = inv.net();
        // Spread captured against fair, per share (positive = bought below fair).
        let capture = fair.map(|f| match side {
            Side::Bid => f - px,
            Side::Ask => px - f,
        });
        self.totals.fills += 1;
        self.totals.shares += shares;
        if let Some(c) = capture {
            self.totals.capture.push(c * 100.0);
        }
        self.markouts.push((at_ms + 5_000, 5, m.slug.clone(), side, px));
        self.markouts.push((at_ms + 30_000, 30, m.slug.clone(), side, px));
        self.count("fills");
        let lag_ms = t_ns / 1_000_000 - at_ms;
        let r = json!({"slug": m.slug, "series": m.series, "asset": m.asset, "side": label, "quote_px": px,
                       "token_px": round_dp(token_px, 4), "shares": round_dp(shares, 4), "at_ms": at_ms,
                       "seen_after_ms": lag_ms, "fair": fair.map(|f| round_dp(f, 4)),
                       "capture_c": capture.map(|c| round_dp(c * 100.0, 2)), "net_after": round_dp(net, 4),
                       "rebate_est": round_dp(rebate, 5)});
        self.record(t_ns, "limm_fill", r);
        self.save();
    }

    /// Timers: markouts, uptime, quote housekeeping.
    pub fn tick(&mut self, t_ns: i64) {
        let now_ms = t_ns / 1_000_000;
        let due: Vec<_> = self.markouts.iter().filter(|m| now_ms >= m.0).cloned().collect();
        self.markouts.retain(|m| now_ms < m.0);
        for (at, horizon, slug, side, px) in due {
            let Some(f) = self.mkts.get(&slug).and_then(|m| m.fair_at(at)) else { continue };
            let mark = match side {
                Side::Bid => f - px,
                Side::Ask => px - f,
            } * 100.0;
            if horizon == 5 {
                self.totals.markout5.push(mark);
            } else {
                self.totals.markout30.push(mark);
            }
        }
        let slugs: Vec<String> = self.mkts.keys().cloned().collect();
        for s in slugs {
            if let Some(m) = self.mkts.get_mut(&s) {
                let stop = m.m.close_ms - self.cfg.stop_s(m.m.kind) * 1000;
                if let Some(last) = m.last_tick_ms
                    && now_ms >= m.m.open_ms
                    && now_ms < stop
                {
                    let dt = (now_ms - last).clamp(0, 1000);
                    m.window_ms += dt;
                    if m.bid.is_some() && m.ask.is_some() {
                        m.quoting_ms += dt;
                    }
                }
                m.last_tick_ms = Some(now_ms);
            }
            self.requote(&s, t_ns);
        }
    }

    /// Closed markets with an open inventory: (slug).
    pub fn unsettled(&self, t_ns: i64) -> Vec<String> {
        let now_ms = t_ns / 1_000_000;
        self.inv.iter().filter(|(_, i)| i.settled.is_none() && now_ms > i.close_ms + 20_000).map(|(k, _)| k.clone()).collect()
    }

    pub fn settle(&mut self, slug: &str, outcome: &str, t_ns: i64) {
        let Some(i) = self.inv.get_mut(slug) else { return };
        if i.settled.is_some() {
            return;
        }
        let pnl = i.settle_pnl(outcome);
        i.settled = Some(outcome.to_owned());
        i.pnl = Some(pnl);
        i.settled_ms = Some(t_ns / 1_000_000);
        let r = json!({"slug": slug, "series": i.series, "asset": i.asset, "outcome": outcome,
                       "yes": round_dp(i.yes, 4), "no": round_dp(i.no, 4), "cost": round_dp(i.cost(), 4),
                       "locked": round_dp(i.locked_pnl(), 4), "pnl": round_dp(pnl, 4), "fills": i.fills,
                       "shares_filled": round_dp(i.shares_filled, 4), "rebate_est": round_dp(i.rebate_est, 5)});
        let per_share = (i.shares_filled > 0.0).then(|| pnl * 100.0 / i.shares_filled);
        *self.realised_by_day.entry(utc_day(t_ns)).or_insert(0.0) += pnl;
        if let Some(c) = per_share {
            self.totals.net_per_share.push(c);
        }
        self.count("settled");
        self.record(t_ns, "limm_settle", r);
        self.save();
    }

    pub fn status(&self, t_ns: i64) -> Value {
        let now_ms = t_ns / 1_000_000;
        let markets: Vec<Value> = self
            .mkts
            .values()
            .map(|m| {
                let lq = m.lbook.quote(None);
                let inv = self.inv.get(&m.m.slug);
                json!({
                    "slug": m.m.slug, "series": m.m.series, "asset": m.m.asset, "kind": m.m.kind.as_str(),
                    "to_close_s": (m.m.close_ms - now_ms) / 1000, "open": now_ms >= m.m.open_ms,
                    "fair": round_opt(m.fair(now_ms, self.cfg.max_poly_spread), 4),
                    "bid": m.bid.as_ref().map(|s| s.px), "ask": m.ask.as_ref().map(|s| s.px),
                    "lim_bid": lq.bid, "lim_ask": lq.ask,
                    "paused": now_ms < m.pause_until_ms, "pauses": m.pauses, "requotes": m.requotes,
                    "prints": m.prints, "print_shares": round_dp(m.print_shares, 2),
                    "uptime": (m.window_ms > 0).then(|| round_dp(m.quoting_ms as f64 / m.window_ms as f64, 3)),
                    "net": inv.map(|i| round_dp(i.net(), 3)), "fills": inv.map(|i| i.fills),
                    "cost": inv.map(|i| round_dp(i.cost(), 3)), "locked": inv.map(|i| round_dp(i.locked_pnl(), 3)),
                })
            })
            .collect();
        let settled: Vec<&Inventory> = self.inv.values().filter(|i| i.settled.is_some()).collect();
        let pnl: f64 = settled.iter().filter_map(|i| i.pnl).sum();
        let mut by_series: BTreeMap<String, (u64, f64, f64)> = BTreeMap::new();
        for i in self.inv.values() {
            let e = by_series.entry(i.series.clone()).or_default();
            e.0 += i.fills;
            e.1 += i.shares_filled;
            e.2 += i.pnl.unwrap_or(0.0);
        }
        let s = |x: &Sample| json!({"n": x.n(), "mean": round_opt(x.mean(), 2), "se": round_opt(x.se(), 2), "t": round_opt(x.t(), 2)});
        let realised: f64 = self.realised_by_day.values().sum();
        json!({
            "enabled": true,
            "params": {"shares": self.cfg.shares, "half_spread": self.cfg.half_spread, "skew_k": self.cfg.skew_k,
                       "max_pos": self.cfg.max_pos, "max_poly_spread": self.cfg.max_poly_spread,
                       "pause_jump": self.cfg.pause_jump, "pause_ms": self.cfg.pause_ms,
                       "requote_ticks": self.cfg.requote_ticks, "series": self.cfg.series.len()},
            "markets": markets,
            "ledger": {"start_cash": round_dp(self.start_cash, 2), "cash": round_dp(self.start_cash + realised - self.open_cost(), 2),
                       "open_cost": round_dp(self.open_cost(), 2), "realised_today": round_dp(self.realised_today(t_ns), 2),
                       "settled": settled.len(), "pnl": round_dp(pnl, 2),
                       "rebate_est": round_dp(self.inv.values().map(|i| i.rebate_est).sum::<f64>(), 3)},
            "gates": {"fills": self.totals.fills, "shares": round_dp(self.totals.shares, 2),
                      "capture_cents": s(&self.totals.capture), "markout5_cents": s(&self.totals.markout5),
                      "markout30_cents": s(&self.totals.markout30), "net_cents_per_share": s(&self.totals.net_per_share)},
            "by_series": by_series.into_iter().map(|(k, (f, sh, p))| (k, json!({"fills": f, "shares": round_dp(sh, 2), "pnl": round_dp(p, 2)}))).collect::<Map<String, Value>>(),
            "counters": self.counters,
            "rtt_ms": self.rtt_ms,
            "recent": self.recent,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> MakerCfg {
        MakerCfg {
            series: vec!["btc-up-or-down-15-min".into()],
            shares: 10.0,
            half_spread: 0.02,
            skew_k: 0.01,
            max_pos: 30.0,
            max_poly_spread: 0.04,
            pause_jump: 0.02,
            pause_ms: 1500,
            requote_ticks: 5.0,
            min_requote_ms: 250,
            stop_s_15m: 60,
            stop_s_1h: 300,
            stop_s_1d: 1800,
            max_open_usd: 100.0,
            daily_loss_usd: 50.0,
            paper_cash: 100.0,
            trades_poll_15m_ms: 2000,
            trades_poll_other_ms: 10000,
        }
    }

    fn market(open_ms: i64) -> MakerMarket {
        MakerMarket {
            slug: "btc-up-or-down-15-min-1".into(),
            series: "btc-up-or-down-15-min".into(),
            asset: "BTC".into(),
            kind: Kind::M15,
            open_ms,
            close_ms: open_ms + 900_000,
            yes_token: "Y".into(),
            no_token: "N".into(),
            external_slug: "btc-updown-15m-1".into(),
            poly_up: "PU".into(),
            poly_down: "PD".into(),
            taker_delay_ms: 500.0,
            min_size: 50.0,
            max_spread: Some(0.035),
            rebate_mult: Some(0.3),
        }
    }

    fn maker() -> Maker {
        let dir = std::env::temp_dir().join(format!("pk-limm-{}-{}", std::process::id(), std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().subsec_nanos()));
        Maker::new(cfg(), &dir, Map::new())
    }

    fn print(t_ms: i64, yes_px: f64, taker_buys_yes: bool, shares: f64) -> Print {
        Print { t_ms, yes_px, taker_buys_yes, shares, key: format!("{t_ms}-{yes_px}-{taker_buys_yes}-{shares}") }
    }

    #[test]
    fn quote_prices() {
        // Around 0.50 with no inventory: 0.48 / 0.52.
        assert_eq!(desired(0.50, 0.02, 0.01, 0.0, 30.0, Some(0.40), Some(0.60)), (Some(0.48), Some(0.52)));
        // Long 30 YES: no more bids; the ask leans down by the skew.
        let (b, a) = desired(0.50, 0.02, 0.01, 30.0, 30.0, Some(0.40), Some(0.60));
        assert_eq!(b, None);
        assert!((a.unwrap() - 0.51).abs() < 1e-9);
        // Post-only: the bid never reaches Limitless's ask.
        assert_eq!(desired(0.50, 0.02, 0.0, 0.0, 30.0, Some(0.40), Some(0.47)).0, Some(0.469));
        // Outside 0.01–0.99: that side is not quoted.
        assert_eq!(desired(0.005, 0.02, 0.0, 0.0, 30.0, None, None), (None, Some(0.025)));
    }

    #[test]
    fn fills_follow_the_print_and_the_queue() {
        let mut s = Span { px: 0.48, remaining: 10.0, live_ms: 1000, dead_ms: Some(5000), queue: 4.0 };
        // Before the post arrived, or after the cancel: nothing.
        assert_eq!(fill_from_print(Side::Bid, &mut s, &print(900, 0.47, false, 5.0)), 0.0);
        assert_eq!(fill_from_print(Side::Bid, &mut s, &print(5000, 0.47, false, 5.0)), 0.0);
        // A taker buying YES never fills our YES bid.
        assert_eq!(fill_from_print(Side::Bid, &mut s, &print(2000, 0.40, true, 5.0)), 0.0);
        // At our price: the 4 shares ahead trade first.
        assert_eq!(fill_from_print(Side::Bid, &mut s, &print(2000, 0.48, false, 5.0)), 1.0);
        // Through our price: fills, up to what remains.
        assert_eq!(fill_from_print(Side::Bid, &mut s, &print(3000, 0.45, false, 20.0)), 9.0);
        assert_eq!(fill_from_print(Side::Bid, &mut s, &print(3500, 0.45, false, 20.0)), 0.0);
        let mut a = Span { px: 0.52, remaining: 10.0, live_ms: 0, dead_ms: None, queue: 0.0 };
        assert_eq!(fill_from_print(Side::Ask, &mut a, &print(10, 0.55, true, 3.0)), 3.0);
        assert_eq!(fill_from_print(Side::Ask, &mut a, &print(10, 0.51, true, 3.0)), 0.0);
    }

    fn poly_book(id: &str, bid: f64, ask: f64) -> String {
        format!(r#"{{"event_type":"book","asset_id":"{id}","bids":[{{"price":"{bid}","size":"100"}}],"asks":[{{"price":"{ask}","size":"100"}}]}}"#)
    }

    fn lim_book(bid: f64, ask: f64, v: i64) -> String {
        format!(r#"{{"ev":"orderbookUpdate","d":{{"marketSlug":"btc-up-or-down-15-min-1","version":{v},"orderbook":{{"bids":[{{"price":{bid},"size":5000000}}],"asks":[{{"price":{ask},"size":5000000}}]}}}}}}"#)
    }

    #[test]
    fn quotes_fill_and_settle() {
        let mut mk = maker();
        let open_ms = 1_000_000;
        let t = |ms: i64| ms * 1_000_000;
        mk.add(market(open_ms), t(open_ms));
        mk.set_rtt(40.0);
        mk.on_limitless(&lim_book(0.40, 0.60, 1), t(open_ms + 100_000));
        mk.on_poly(&poly_book("PU", 0.49, 0.51), t(open_ms + 100_000));
        let st = mk.status(t(open_ms + 100_000));
        assert_eq!((st["markets"][0]["bid"].as_f64(), st["markets"][0]["ask"].as_f64()), (Some(0.48), Some(0.52)));
        // A taker sells YES at 0.45 after our bid went live (20 ms later): we buy 10 YES at 0.48.
        let p = print(open_ms + 100_100, 0.45, false, 30.0);
        let fresh = mk.on_prints("btc-up-or-down-15-min-1", vec![p.clone()], t(open_ms + 102_000));
        assert_eq!(fresh.len(), 1);
        assert!(mk.on_prints("btc-up-or-down-15-min-1", vec![p], t(open_ms + 103_000)).is_empty(), "seen once");
        let inv = mk.inv.get("btc-up-or-down-15-min-1").unwrap();
        assert!((inv.yes - 10.0).abs() < 1e-9 && (inv.yes_cost - 4.8).abs() < 1e-9);
        // A taker buys YES at 0.56: our ask (NO bid at 0.48) fills 10.
        mk.tick(t(open_ms + 104_000));
        let ask_px = mk.status(t(open_ms + 104_000))["markets"][0]["ask"].as_f64().unwrap();
        mk.on_prints("btc-up-or-down-15-min-1", vec![print(open_ms + 104_100, ask_px + 0.04, true, 10.0)], t(open_ms + 105_000));
        let inv = mk.inv.get("btc-up-or-down-15-min-1").unwrap().clone();
        assert!((inv.no - 10.0).abs() < 1e-9);
        // 10 pairs locked: $10 payout against their cost, whatever the outcome.
        let locked = inv.locked_pnl();
        assert!((inv.settle_pnl("yes") - locked).abs() < 1e-9 && (inv.settle_pnl("no") - locked).abs() < 1e-9);
        assert!(locked > 0.0);
        mk.settle("btc-up-or-down-15-min-1", "no", t(open_ms + 950_000));
        assert!(mk.unsettled(t(open_ms + 2_000_000)).is_empty());
        assert_eq!(mk.status(t(open_ms + 950_000))["ledger"]["settled"], 1);
    }

    #[test]
    fn no_quotes_near_the_close_or_in_a_pause() {
        let mut mk = maker();
        let open_ms = 1_000_000;
        let t = |ms: i64| ms * 1_000_000;
        mk.add(market(open_ms), t(open_ms));
        mk.on_limitless(&lim_book(0.40, 0.60, 1), t(open_ms + 100_000));
        mk.on_poly(&poly_book("PU", 0.49, 0.51), t(open_ms + 100_000));
        // Polymarket jumps 3¢ in a second: paused, quotes pulled.
        mk.on_poly(&poly_book("PU", 0.52, 0.54), t(open_ms + 101_200));
        let st = mk.status(t(open_ms + 101_200));
        assert_eq!((st["markets"][0]["paused"].as_bool(), st["markets"][0]["bid"].as_f64()), (Some(true), None));
        // In the last 60 s: nothing quoted.
        mk.on_poly(&poly_book("PU", 0.52, 0.54), t(open_ms + 850_000));
        mk.tick(t(open_ms + 850_000));
        assert_eq!(mk.status(t(open_ms + 850_000))["markets"][0]["bid"], Value::Null);
    }
}
