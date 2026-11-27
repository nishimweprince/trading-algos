//! The Kalshi-priced rule on Polymarket US, its paper venue and book-keeping.
//! No I/O: the runtime feeds Kalshi frames, Polymarket US quotes and books,
//! and clock ticks, and carries out the actions.
//!
//! Both venues list the same 15-minute Up/Down contract on the same CF
//! Benchmarks index ("Yes" when the 60 s average at the close is at least the
//! one at the open), so Kalshi's mid is a fair value for Polymarket US. From
//! `entry_after_open_s` after the open to `stop_before_close_s` before the
//! close, buy the Polymarket US side that is cheaper than Kalshi's by
//! `min_edge`..=`max_edge` after the fee. Buying No takes Yes bids: its price
//! is 1 − the Yes bid. Paper fills land `paper_latency_ms` after the decision
//! against the book fetched then. Positions settle on Polymarket US's own
//! settlement; Kalshi's result is journaled beside it.
//!
//! | Journal record | |
//! |---|---|
//! | `pu_window` | a window found on both venues |
//! | `pu_sec` | each second: both quotes and the gaps |
//! | `pu_decision`, `pu_fill`, `pu_no_fill` | an entry and what its order got |
//! | `pu_close`, `pu_resolution`, `pu_settle` | the window's last gate, both outcomes, P&L |
//! | `pu_shadow`, `pu_shadow_settle` | the first gap ≥ `shadow_min_edge` per coin-window (the min-edge-3¢ rule), top-of-book cost, never filled |
//! | `pu_fill_probe`, `pu_probe_settle` | delay study: the order walked against the book 25–150 ms after the decision (never a position) |
//! | `pu_book_err` | a window's first book error past its open |
//! | `pu_halt`, `pu_ack`, `pu_kill`, `pu_status` | risk and housekeeping |
//!
//! Sizing is measured, not traded: each fill also journals what the sizing
//! rules (`RULES`) would have bought against the same book, capped by depth
//! at the limit, a per-trade and a per-window cost cap; `pu_settle` carries
//! each rule's P&L. P&L is booked to the day the position was entered.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use chrono::{DateTime, Utc};
use pk_core::num::{key_px, px_key};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::config::Config;

pub const NS: i64 = 1_000_000_000;
pub const WINDOW_S: i64 = 900;
const RESOLVE_FOR_S: i64 = 2 * 3_600;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct WindowInfo {
    pub asset: String,
    pub open: i64,
    pub slug: String,
    pub kalshi_ticker: Option<String>,
    pub tick: f64,
    pub min_qty: f64,
    pub index: String,
    /// The market's `feeCoefficient`: the taker fee Θ for this window
    /// (`fee_theta` from the config only when the market does not say).
    #[serde(default)]
    pub fee_coef: Option<f64>,
}

impl WindowInfo {
    pub fn close(&self) -> i64 {
        self.open + WINDOW_S
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Position {
    pub id: u64,
    pub asset: String,
    pub open: i64,
    /// Bought Yes (Up) or No (Down).
    pub up: bool,
    pub slug: String,
    pub shares: f64,
    pub usd: f64,
    pub fee: f64,
    pub entry_ns: i64,
    pub outcome: Option<String>,
    pub pnl: Option<f64>,
    pub epoch: String,
    /// What each sizing rule would have bought against the same book.
    #[serde(default)]
    pub alt: BTreeMap<String, Alt>,
}

/// A sizing rule's counterfactual fill.
#[derive(Clone, Copy, Debug, Default, Serialize, Deserialize)]
pub struct Alt {
    pub target: f64,
    pub shares: f64,
    pub usd: f64,
    pub fee: f64,
}

/// Counterfactual sizing rules, journaled beside the flat paper size.
pub const RULES: [&str; 9] =
    ["flat", "tiers", "kelly_q", "kelly_t", "kelly_s", "kelly_c", "kelly_cal", "usd_flat", "usd_tiers"];

/// Dollar tiers on the return per dollar (edge / cost): under 10% → 1× the
/// stake, 10–20% → 2×, 20% and up → 3×.
pub fn usd_tier_mult(rpd: f64) -> f64 {
    if rpd < 0.10 {
        1.0
    } else if rpd < 0.20 {
        2.0
    } else {
        3.0
    }
}

/// A typical gap (6¢): the calibration prior's pseudo-shadows sit here.
const CAL_PRIOR_X: f64 = 0.06;

/// The fit of q_cal = c + β·(q − c) on settled shadows, through the origin:
/// x = q − c (the gap to Kalshi's mid after fee), r = won − c (the realized gap).
#[derive(Clone, Copy, Debug, Default, Serialize)]
pub struct Cal {
    pub n: u64,
    pub sxx: f64,
    pub sxr: f64,
}

impl Cal {
    pub fn add(&mut self, x: f64, r: f64) {
        if x.is_finite() && r.is_finite() {
            self.n += 1;
            self.sxx += x * x;
            self.sxr += x * r;
        }
    }

    /// β shrunk toward `prior` by `prior_n` pseudo-shadows at a 6¢ gap, in [0, 1].
    pub fn beta(&self, prior: f64, prior_n: f64) -> f64 {
        let lam = prior_n * CAL_PRIOR_X * CAL_PRIOR_X;
        let d = self.sxx + lam;
        if d <= 0.0 { prior } else { ((self.sxr + lam * prior) / d).clamp(0.0, 1.0) }
    }
}

/// Kalshi's near side for a buy at all-in cost `c`: Up is worth at least the
/// Yes bid, Down at least 1 − the Yes ask. The gap to it halves every
/// `half_ms` of book age.
pub fn cons_q(up: bool, k: &Quote, c: f64, half_ms: f64) -> f64 {
    let near = if up { k.bid } else { 1.0 - k.ask };
    c + (near - c) * 0.5f64.powf(k.age_ms.max(0) as f64 / half_ms)
}

/// Edge tiers (after fee): under 8¢ → 10 shares, 8–11¢ → 20, 11¢ and up → 30.
pub fn tier_shares(edge: f64) -> f64 {
    if edge < 0.08 {
        10.0
    } else if edge < 0.11 {
        20.0
    } else {
        30.0
    }
}

/// Kelly fraction for a $1 binary bought at all-in cost `c` with win probability `q`.
pub fn kelly_f(q: f64, c: f64) -> f64 {
    if !(0.0..1.0).contains(&c) { 0.0 } else { ((q - c) / (1.0 - c)).max(0.0) }
}

/// Walks sorted (price, qty) levels for `want` shares: (shares, usd, fee).
fn walk(levels: &[(f64, f64)], want: f64, fee_of: impl Fn(f64) -> f64) -> (f64, f64, f64) {
    let (mut got, mut usd, mut fee) = (0.0, 0.0, 0.0);
    for &(p, q) in levels {
        let take = q.min(want - got);
        if take <= 1e-9 {
            break;
        }
        got += take;
        usd += take * p;
        fee += fee_of(p) * take;
    }
    (got, usd, fee)
}

impl Position {
    fn won(&self) -> Option<bool> {
        self.outcome.as_deref().map(|o| (o == "yes") == self.up)
    }
}

#[derive(Default, Serialize, Deserialize)]
pub struct Persisted {
    pub positions: Vec<Position>,
    pub realized: BTreeMap<String, f64>,
    pub halted_day: Option<String>,
    pub acked_day: Option<String>,
    pub next_id: u64,
    /// Fills each probe delay got (counterfactual, settled beside the positions).
    #[serde(default)]
    pub probes: Vec<Probe>,
    /// Quick-exit probes of open positions.
    #[serde(default)]
    pub exits: Vec<ExitProbe>,
}

/// The quick-exit study: what selling the position into the bid `delay_ms`
/// after the fill would have got (never sold; settled beside holding).
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ExitProbe {
    pub id: u64,
    pub asset: String,
    pub open: i64,
    pub up: bool,
    pub delay_ms: i64,
    /// The position's shares and its all-in entry cost.
    pub shares: f64,
    pub entry_cost: f64,
    pub sold: f64,
    pub proceeds: f64,
    pub fee: f64,
    pub epoch: String,
    #[serde(default)]
    pub settled: bool,
}

/// What a paper order would have filled had it arrived `delay_ms` after the decision.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Probe {
    pub id: u64,
    pub asset: String,
    pub open: i64,
    pub up: bool,
    pub delay_ms: i64,
    pub shares: f64,
    pub usd: f64,
    pub fee: f64,
    pub epoch: String,
    #[serde(default)]
    pub settled: bool,
}

/// Ask the runtime to fetch a Polymarket US book at `at_ns` for a paper fill
/// (and at `decision_ns` + each probe delay for the delay study).
#[derive(Clone, Debug, PartialEq)]
pub struct FetchBook {
    pub id: u64,
    pub slug: String,
    pub at_ns: i64,
    pub decision_ns: i64,
}

/// The Polymarket US quote at a decision: bid, ask, receipt age, venue age.
#[derive(Clone, Copy, Debug)]
pub struct PmView {
    pub bid: Option<f64>,
    pub ask: Option<f64>,
    pub age_ms: i64,
    /// Now − the book's `transactTime` (its last change, so a quiet book ages).
    pub venue_age_ms: Option<i64>,
    pub venue_ns: Option<i64>,
}

#[derive(Default)]
struct KBook {
    yes: BTreeMap<i32, f64>,
    no: BTreeMap<i32, f64>,
    snapshot: bool,
    last_ns: i64,
}

#[derive(Clone, Copy, Debug)]
pub struct Quote {
    pub bid: f64,
    pub ask: f64,
    pub age_ms: i64,
}

impl Quote {
    pub fn mid(&self) -> f64 {
        (self.bid + self.ask) / 2.0
    }
}

impl KBook {
    fn quote(&self, t_ns: i64) -> Option<Quote> {
        if !self.snapshot {
            return None;
        }
        let bid = key_px(*self.yes.keys().next_back()?);
        let ask = key_px(px_key(1.0) - *self.no.keys().next_back()?);
        (ask > bid).then(|| Quote { bid, ask, age_ms: (t_ns - self.last_ns) / 1_000_000 })
    }
}

/// The Polymarket US Yes quote, as polled.
#[derive(Clone, Copy, Default)]
struct PmQuote {
    bid: Option<f64>,
    ask: Option<f64>,
    t_ns: i64,
    venue_ns: Option<i64>,
}

#[derive(Clone, Debug)]
struct Order {
    asset: usize,
    open: i64,
    up: bool,
    limit: f64,
    shares: f64,
    decision_ns: i64,
    /// At the decision: Kalshi's side mid, the Polymarket US ask, its fee, the edge.
    fair: f64,
    px: f64,
    fee: f64,
    edge: f64,
    /// The fee coefficient the decision used; fills are charged at it.
    theta: f64,
    /// Kalshi's near side, age-discounted (`cons_q`).
    q_cons: f64,
}

/// A side's gap: (up, (Kalshi side mid, Polymarket US ask, fee, edge)).
type Side = (bool, (f64, f64, f64, f64));

#[derive(Clone, Copy, Debug)]
struct Shadow {
    up: bool,
    px: f64,
    fee: f64,
    edge: f64,
    /// The gap to Kalshi's near side (`cons_q` − c).
    x_cons: f64,
}

#[derive(Default)]
struct WinState {
    entries: usize,
    gate: Option<&'static str>,
    best_edge: Option<f64>,
    closed: bool,
    resolved: bool,
    shadow: Option<Shadow>,
    /// The per-dollar gate's shadow entry (first gap with edge / cost ≥ `rpd_min`).
    rpd: Option<Shadow>,
}

#[derive(Default, Serialize)]
struct Counters {
    kalshi_frames: u64,
    kalshi_resets: u64,
    pm_polls: u64,
    decisions: u64,
    fills: u64,
    no_fills: u64,
    settled: u64,
    agree: u64,
    disagree: u64,
}

pub struct Engine {
    pub cfg: Config,
    windows: BTreeMap<(usize, i64), WindowInfo>,
    wstate: HashMap<(usize, i64), WinState>,
    kbooks: HashMap<String, KBook>,
    kowner: HashMap<String, (usize, i64)>,
    k_last_frame_ns: i64,
    pm: HashMap<(usize, i64), PmQuote>,
    pending: HashMap<u64, Order>,
    /// Orders kept for the probe fetches, until their window is pruned.
    probe_orders: HashMap<u64, Order>,
    pub p: Persisted,
    killed: bool,
    last_grid: Vec<i64>,
    counters: Counters,
    records: Vec<Value>,
    pub dirty: bool,
    /// Cost each sizing rule has used per window open, across coins.
    window_alt: HashMap<i64, BTreeMap<&'static str, f64>>,
    /// The day a record-only loss breach was last journaled.
    breach_noted: Option<String>,
    /// Settled shadows of `cal_epochs` (seeded from the journal at start).
    pub cal: Cal,
}

fn iso(t_ns: i64) -> String {
    DateTime::<Utc>::from_timestamp_nanos(t_ns).format("%Y-%m-%dT%H:%M:%S%.3fZ").to_string()
}

pub fn day_of(t_s: i64) -> String {
    DateTime::<Utc>::from_timestamp(t_s, 0).map(|d| d.format("%Y-%m-%d").to_string()).unwrap_or_default()
}

fn r4(x: f64) -> Value {
    if x.is_finite() { ((x * 1e4).round() / 1e4).into() } else { Value::Null }
}

fn r6(x: f64) -> Value {
    if x.is_finite() { ((x * 1e6).round() / 1e6).into() } else { Value::Null }
}

/// The levels a sale of one side walks, best (highest) first: Yes sells into
/// Yes bids; No sells at 1 − the Yes offer.
fn exit_levels(up: bool, bids: &[(f64, f64)], offers: &[(f64, f64)]) -> Vec<(f64, f64)> {
    let mut v: Vec<(f64, f64)> =
        if up { bids.to_vec() } else { offers.iter().map(|(p, q)| (1.0 - p, *q)).collect() };
    v.retain(|(p, q)| *q > 0.0 && *p > 0.0);
    v.sort_by(|a, b| b.0.total_cmp(&a.0));
    v
}

/// The levels a buy of one side walks, cheapest first, at or under `limit`,
/// and that side's best price. A Yes buy lifts offers; a No buy sells into
/// Yes bids, at 1 − bid.
fn side_levels(up: bool, limit: f64, bids: &[(f64, f64)], offers: &[(f64, f64)]) -> (Vec<(f64, f64)>, f64) {
    let mut v: Vec<(f64, f64)> =
        if up { offers.to_vec() } else { bids.iter().map(|(p, q)| (1.0 - p, *q)).collect() };
    v.sort_by(|a, b| a.0.total_cmp(&b.0));
    let best = v.iter().filter(|l| l.1 > 0.0).map(|l| l.0).fold(f64::INFINITY, f64::min);
    v.retain(|(p, q)| *p <= limit + 1e-9 && *q > 0.0);
    (v, best)
}

fn floor_to(x: f64, step: f64) -> f64 {
    let v = ((x / step) + 1e-9).floor() * step;
    (v * 1e4).round() / 1e4
}

impl Engine {
    pub fn new(cfg: Config, persisted: Persisted) -> Self {
        let n = cfg.assets.len();
        Self {
            cfg,
            windows: BTreeMap::new(),
            wstate: HashMap::new(),
            kbooks: HashMap::new(),
            kowner: HashMap::new(),
            k_last_frame_ns: 0,
            pm: HashMap::new(),
            pending: HashMap::new(),
            probe_orders: HashMap::new(),
            p: persisted,
            killed: false,
            last_grid: vec![0; n],
            counters: Counters::default(),
            records: Vec::new(),
            dirty: false,
            window_alt: HashMap::new(),
            breach_noted: None,
            cal: Cal::default(),
        }
    }

    /// β for calibrated Kelly: the settled shadows so far, shrunk toward `kelly_shrink`.
    pub fn beta(&self) -> f64 {
        self.cal.beta(self.cfg.kelly_shrink, self.cfg.cal_prior_n)
    }

    /// Polymarket US taker fee per contract at price `p`.
    pub fn fee(theta: f64, p: f64) -> f64 {
        theta * p * (1.0 - p)
    }

    /// The taker fee coefficient for a window: the market's own, else the config's.
    fn theta(&self, i: usize, open: i64) -> f64 {
        self.windows
            .get(&(i, open))
            .and_then(|w| w.fee_coef)
            .filter(|c| c.is_finite() && *c >= 0.0)
            .unwrap_or(self.cfg.fee_theta)
    }

    fn record(&mut self, t_ns: i64, kind: &str, mut body: Value) {
        if let Some(m) = body.as_object_mut() {
            m.insert("type".into(), kind.into());
            m.insert("t".into(), iso(t_ns).into());
            m.insert("t_ns".into(), t_ns.into());
        }
        self.records.push(body);
    }

    /// Journals a runtime record (book errors, failed probes).
    pub fn journal(&mut self, kind: &str, body: Value, t_ns: i64) {
        self.record(t_ns, kind, body);
    }

    pub fn take_records(&mut self) -> Vec<Value> {
        std::mem::take(&mut self.records)
    }

    pub fn asset_index(&self, asset: &str) -> Option<usize> {
        self.cfg.assets.iter().position(|a| a.eq_ignore_ascii_case(asset))
    }

    pub fn window(&self, asset: usize, open: i64) -> Option<&WindowInfo> {
        self.windows.get(&(asset, open))
    }

    pub fn set_window(&mut self, w: WindowInfo, t_ns: i64) {
        let Some(i) = self.asset_index(&w.asset) else { return };
        if self.windows.contains_key(&(i, w.open)) {
            return;
        }
        if let Some(t) = &w.kalshi_ticker {
            self.kowner.insert(t.clone(), (i, w.open));
        }
        self.record(
            t_ns,
            "pu_window",
            json!({"asset": w.asset, "open": w.open, "slug": w.slug, "kalshi": w.kalshi_ticker, "tick": w.tick,
                   "min_qty": w.min_qty, "index": w.index, "fee_coef": w.fee_coef, "epoch": self.cfg.epoch}),
        );
        self.windows.insert((i, w.open), w);
    }

    /// Kalshi markets to subscribe to: windows not yet closed, opening within 15 min.
    pub fn wanted_kalshi(&self, now_s: i64) -> BTreeSet<String> {
        self.windows
            .values()
            .filter(|w| w.close() > now_s - 5 && w.open <= now_s + WINDOW_S)
            .filter_map(|w| w.kalshi_ticker.clone())
            .collect()
    }

    /// Windows whose Polymarket US quote should be polled now: (asset index, open, slug).
    pub fn to_poll(&self, now_s: i64) -> Vec<(usize, i64, String)> {
        self.windows
            .iter()
            .filter(|(_, w)| w.open <= now_s && now_s < w.close())
            .map(|((i, o), w)| (*i, *o, w.slug.clone()))
            .collect()
    }

    /// A fresh Polymarket US book's top. `t_ns` is when it was received (the
    /// runtime backdates a CDN-cached response by its `Age`); `venue_ns` is
    /// the book's `transactTime`.
    pub fn on_pm_quote(
        &mut self,
        asset: usize,
        open: i64,
        bid: Option<f64>,
        ask: Option<f64>,
        venue_ns: Option<i64>,
        t_ns: i64,
    ) {
        self.pm.insert((asset, open), PmQuote { bid, ask, t_ns, venue_ns });
        self.counters.pm_polls += 1;
    }

    /// A Kalshi `orderbook_snapshot` / `orderbook_delta` frame. Returns the
    /// window whose book it changed.
    pub fn on_kalshi(&mut self, text: &str, t_ns: i64) -> Option<(usize, i64)> {
        let Ok(f) = serde_json::from_str::<pk_engine::parse::KalshiFrame<'_>>(text) else { return None };
        self.k_last_frame_ns = t_ns;
        self.counters.kalshi_frames += 1;
        let m = f.msg.as_ref()?;
        let ticker = m.market_ticker.as_ref().map(|t| t.as_str())?;
        let owner = *self.kowner.get(ticker)?;
        let b = self.kbooks.entry(ticker.to_owned()).or_default();
        match f.kind.as_deref() {
            Some("orderbook_snapshot") => {
                b.yes =
                    m.levels(true).into_iter().filter(|l| l.1 > 0.0).map(|(p, q)| (px_key(p), q)).collect();
                b.no =
                    m.levels(false).into_iter().filter(|l| l.1 > 0.0).map(|(p, q)| (px_key(p), q)).collect();
                b.snapshot = true;
                b.last_ns = t_ns;
            }
            Some("orderbook_delta") if b.snapshot => {
                let (price, delta) = m.delta()?;
                let side = if m.side.as_deref() == Some("no") { &mut b.no } else { &mut b.yes };
                let k = px_key(price);
                let q = side.get(&k).copied().unwrap_or(0.0) + delta;
                if q > 1e-9 {
                    side.insert(k, q);
                } else {
                    side.remove(&k);
                }
                b.last_ns = t_ns;
            }
            _ => return None,
        }
        Some(owner)
    }

    pub fn invalidate_kalshi(&mut self) {
        self.kbooks.clear();
        self.counters.kalshi_resets += 1;
    }

    fn kalshi_quote(&self, i: usize, open: i64, t_ns: i64) -> Option<Quote> {
        let w = self.windows.get(&(i, open))?;
        if t_ns - self.k_last_frame_ns > self.cfg.kalshi_max_age_ms * 1_000_000 {
            return None;
        }
        self.kbooks.get(w.kalshi_ticker.as_ref()?)?.quote(t_ns)
    }

    fn pm_quote(&self, i: usize, open: i64, t_ns: i64) -> Option<PmView> {
        let q = self.pm.get(&(i, open))?;
        Some(PmView {
            bid: q.bid,
            ask: q.ask,
            age_ms: (t_ns - q.t_ns) / 1_000_000,
            venue_age_ms: q.venue_ns.map(|v| (t_ns - v) / 1_000_000),
            venue_ns: q.venue_ns,
        })
    }

    pub fn set_kill(&mut self, on: bool, t_ns: i64) {
        if on != self.killed {
            self.killed = on;
            self.record(t_ns, "pu_kill", json!({"on": on}));
        }
    }

    pub fn halted(&self, t_ns: i64) -> bool {
        self.p.halted_day.as_deref() == Some(day_of(t_ns / NS).as_str())
    }

    pub fn ack(&mut self, t_ns: i64) {
        if self.halted(t_ns) {
            let day = day_of(t_ns / NS);
            self.p.halted_day = None;
            self.p.acked_day = Some(day.clone());
            self.dirty = true;
            self.record(t_ns, "pu_ack", json!({"day": day}));
        }
    }

    fn open_cost(&self) -> f64 {
        let held: f64 = self.p.positions.iter().filter(|p| p.outcome.is_none()).map(|p| p.usd + p.fee).sum();
        held + self.pending.values().map(|o| o.limit * o.shares).sum::<f64>()
    }

    /// One clock step: closes, the per-second grid, entries.
    pub fn tick(&mut self, t_ns: i64) -> Vec<FetchBook> {
        let mut out = Vec::new();
        let now_s = t_ns / NS;
        let open_now = now_s - now_s.rem_euclid(WINDOW_S);
        for i in 0..self.cfg.assets.len() {
            let prev = open_now - WINDOW_S;
            if now_s >= open_now + 3 && self.windows.contains_key(&(i, prev)) {
                self.close_window(i, prev, t_ns);
            }
            if !self.windows.contains_key(&(i, open_now)) {
                continue;
            }
            if self.last_grid[i] != now_s {
                self.last_grid[i] = now_s;
                self.grid(i, open_now, t_ns);
            }
            if let Some(a) = self.evaluate_now(i, open_now, t_ns, "tick") {
                out.push(a);
            }
        }
        out
    }

    /// Evaluates one window now, if it is the live one and inside its entry
    /// period. Called on the clock and whenever either venue's quote changes,
    /// so an entry does not wait for the next clock step.
    pub fn evaluate_now(
        &mut self,
        i: usize,
        open: i64,
        t_ns: i64,
        trigger: &'static str,
    ) -> Option<FetchBook> {
        let now_s = t_ns / NS;
        if open != now_s - now_s.rem_euclid(WINDOW_S) || !self.windows.contains_key(&(i, open)) {
            return None;
        }
        let since_open = now_s - open;
        let left = open + WINDOW_S - now_s;
        if since_open < self.cfg.entry_after_open_s || left <= self.cfg.stop_before_close_s {
            return None;
        }
        self.evaluate(i, open, t_ns, trigger)
    }

    /// The gap on each side: (Kalshi side mid, Polymarket US ask, fee, edge).
    fn gaps(theta: f64, k: &Quote, bid: Option<f64>, ask: Option<f64>) -> [Option<(f64, f64, f64, f64)>; 2] {
        let mid = k.mid();
        let fee = |p| Self::fee(theta, p);
        let yes = ask.map(|a| (mid, a, fee(a), mid - a - fee(a)));
        let no = bid.map(|b| {
            let a = 1.0 - b;
            (1.0 - mid, a, fee(a), (1.0 - mid) - a - fee(a))
        });
        [yes, no]
    }

    fn grid(&mut self, i: usize, open: i64, t_ns: i64) {
        let Some(w) = self.windows.get(&(i, open)) else { return };
        let asset = w.asset.clone();
        let kq = self.kalshi_quote(i, open, t_ns);
        let pq = self.pm_quote(i, open, t_ns);
        let gaps = match (kq, pq) {
            (Some(k), Some(p)) => Self::gaps(self.theta(i, open), &k, p.bid, p.ask),
            _ => [None, None],
        };
        let left = open + WINDOW_S - t_ns / NS;
        self.record(
            t_ns,
            "pu_sec",
            json!({
                "asset": asset, "open": open, "left": left,
                "kalshi": kq.map(|q| json!({"bid": r4(q.bid), "ask": r4(q.ask), "age_ms": q.age_ms})),
                "pm": pq.map(|p| json!({"bid": p.bid, "ask": p.ask, "age_ms": p.age_ms, "venue_age_ms": p.venue_age_ms})),
                "edge_yes": gaps[0].map(|g| r6(g.3)), "edge_no": gaps[1].map(|g| r6(g.3)),
                "epoch": self.cfg.epoch,
            }),
        );
    }

    fn evaluate(&mut self, i: usize, open: i64, t_ns: i64, trigger: &'static str) -> Option<FetchBook> {
        let gate = |e: &mut Self, why: &'static str| {
            e.wstate.entry((i, open)).or_default().gate = Some(why);
            None
        };
        if self.killed {
            return gate(self, "kill");
        }
        if self.halted(t_ns) {
            return gate(self, "halted");
        }
        if self.wstate.get(&(i, open)).is_some_and(|s| s.entries >= self.cfg.max_entries_per_window) {
            // The per-dollar gate keeps looking after the 5¢ rule's entry, so the two are compared on whole windows.
            self.rpd_late(i, open, t_ns);
            return gate(self, "entries_done");
        }
        let Some(k) = self.kalshi_quote(i, open, t_ns) else { return gate(self, "no_kalshi") };
        if k.ask - k.bid > self.cfg.kalshi_max_spread + 1e-9 {
            return gate(self, "kalshi_spread");
        }
        let Some(pm) = self.pm_quote(i, open, t_ns) else { return gate(self, "no_pm_quote") };
        let (bid, ask, age_ms) = (pm.bid, pm.ask, pm.age_ms);
        if age_ms > self.cfg.pm_max_age_ms {
            return gate(self, "stale_pm_quote");
        }
        let theta = self.theta(i, open);
        let sides: Vec<Side> = Self::gaps(theta, &k, bid, ask)
            .into_iter()
            .zip([true, false])
            .filter_map(|(g, up)| g.map(|g| (up, g)))
            .filter(|(_, g)| (self.cfg.min_px..=self.cfg.max_px).contains(&g.1))
            .collect();
        if sides.is_empty() {
            return gate(self, "price_range");
        }
        {
            let top = sides.iter().map(|x| x.1.3).fold(f64::NEG_INFINITY, f64::max);
            let ws = self.wstate.entry((i, open)).or_default();
            ws.best_edge = Some(ws.best_edge.map_or(top, |e: f64| e.max(top)));
        }
        self.shadow(i, open, &sides, &k, t_ns);
        self.rpd_shadow(i, open, &sides, &k, t_ns);
        let pick = sides
            .iter()
            .filter(|x| x.1.3 >= self.cfg.min_edge - 1e-12 && x.1.3 <= self.cfg.max_edge + 1e-12)
            .copied()
            .max_by(|a, b| a.1.3.total_cmp(&b.1.3));
        let Some((up, (fair, px, fee, edge))) = pick else {
            let over = sides.iter().any(|x| x.1.3 > self.cfg.max_edge);
            return gate(self, if over { "edge_too_big" } else { "edge" });
        };
        let w = self.windows.get(&(i, open))?.clone();
        let tick = if w.tick > 0.0 { w.tick } else { 0.01 };
        let cap = fair - fee - self.cfg.min_net_edge;
        let limit = floor_to((px + self.cfg.limit_offset).min(cap), tick).max(px);
        let shares = self.cfg.shares.max(w.min_qty);
        let q_cons_v = cons_q(up, &k, px + fee, self.cfg.cons_age_half_ms);
        if self.open_cost() + limit * shares > self.cfg.max_open_cost_usd + 1e-9 {
            return gate(self, "max_open_cost");
        }
        self.p.next_id += 1;
        let id = self.p.next_id;
        {
            let ws = self.wstate.entry((i, open)).or_default();
            ws.entries += 1;
            ws.gate = Some("fired");
        }
        self.counters.decisions += 1;
        self.dirty = true;
        let left = (open + WINDOW_S) as f64 - t_ns as f64 / NS as f64;
        self.record(
            t_ns,
            "pu_decision",
            json!({
                "id": id, "asset": w.asset, "open": open, "slug": w.slug, "side": if up { "up" } else { "down" },
                "left": r4(left), "p_side": r4(fair), "kalshi_side": r4(fair),
                "kalshi_bid": r4(k.bid), "kalshi_ask": r4(k.ask), "kalshi_age_ms": k.age_ms,
                "pm_bid": bid, "pm_ask": ask, "pm_age_ms": age_ms, "pm_venue_age_ms": pm.venue_age_ms,
                "pm_venue_ts": pm.venue_ns.map(iso), "trigger": trigger,
                "fee_coef": w.fee_coef, "fee_theta": theta,
                "ask": r4(px), "fee": r6(fee), "edge": r6(edge), "limit": r4(limit), "shares": shares,
                "q_cons": r4(q_cons_v), "kalshi_book_age_ms": k.age_ms,
                "cost": r4(px + fee), "rpd": r4(edge / (px + fee)),
                "epoch": self.cfg.epoch, "mode": "paper",
            }),
        );
        let at_ns = t_ns + self.cfg.paper_latency_ms * 1_000_000;
        let order = Order {
            asset: i,
            open,
            up,
            limit,
            shares,
            decision_ns: t_ns,
            fair,
            px,
            fee,
            edge,
            theta,
            q_cons: q_cons_v,
        };
        if !self.cfg.paper_delays_ms.is_empty() {
            self.probe_orders.insert(id, order.clone());
        }
        self.pending.insert(id, order);
        Some(FetchBook { id, slug: w.slug, at_ns, decision_ns: t_ns })
    }

    /// After the window's entry: the per-dollar gate's shadow on the current quotes.
    fn rpd_late(&mut self, i: usize, open: i64, t_ns: i64) {
        if self.wstate.get(&(i, open)).is_some_and(|s| s.rpd.is_some()) {
            return;
        }
        let Some(k) = self.kalshi_quote(i, open, t_ns) else { return };
        if k.ask - k.bid > self.cfg.kalshi_max_spread + 1e-9 {
            return;
        }
        let Some(pm) = self.pm_quote(i, open, t_ns) else { return };
        if pm.age_ms > self.cfg.pm_max_age_ms {
            return;
        }
        let sides: Vec<Side> = Self::gaps(self.theta(i, open), &k, pm.bid, pm.ask)
            .into_iter()
            .zip([true, false])
            .filter_map(|(g, up)| g.map(|g| (up, g)))
            .filter(|(_, g)| (self.cfg.min_px..=self.cfg.max_px).contains(&g.1))
            .collect();
        self.rpd_shadow(i, open, &sides, &k, t_ns);
    }

    /// The per-dollar gate (shadow only): the first gap in a coin-window whose
    /// return per dollar (edge / cost) is at least `rpd_min`, with an edge of at
    /// least `min_net_edge` and at most `max_edge`, costed at the ask plus fee.
    fn rpd_shadow(&mut self, i: usize, open: i64, sides: &[Side], k: &Quote, t_ns: i64) {
        let ws = self.wstate.entry((i, open)).or_default();
        if ws.rpd.is_some() {
            return;
        }
        let rpd = |g: &(f64, f64, f64, f64)| g.3 / (g.1 + g.2);
        let Some(&(up, (fair, px, fee, edge))) = sides
            .iter()
            .filter(|x| {
                x.1.3 >= self.cfg.min_net_edge - 1e-12
                    && x.1.3 <= self.cfg.max_edge + 1e-12
                    && rpd(&x.1) >= self.cfg.rpd_min - 1e-12
            })
            .max_by(|a, b| rpd(&a.1).total_cmp(&rpd(&b.1)))
        else {
            return;
        };
        let x_cons = cons_q(up, k, px + fee, self.cfg.cons_age_half_ms) - (px + fee);
        ws.rpd = Some(Shadow { up, px, fee, edge, x_cons });
        let asset = self.cfg.assets[i].clone();
        let left = (open + WINDOW_S) as f64 - t_ns as f64 / NS as f64;
        self.record(
            t_ns,
            "pu_rpd",
            json!({"asset": asset, "open": open, "side": if up { "up" } else { "down" }, "left": r4(left),
                   "p_side": r4(fair), "ask": r4(px), "fee": r6(fee), "cost": r4(px + fee), "edge": r6(edge),
                   "rpd": r4(edge / (px + fee)), "passes_5c": edge >= self.cfg.min_edge - 1e-12,
                   "epoch": self.cfg.epoch}),
        );
    }

    /// The min-edge-3¢ rule's entry: the first gap of `shadow_min_edge`..=`max_edge`
    /// in a coin-window before any real entry, costed at the ask plus fee.
    fn shadow(&mut self, i: usize, open: i64, sides: &[Side], k: &Quote, t_ns: i64) {
        let ws = self.wstate.entry((i, open)).or_default();
        if ws.shadow.is_some() || ws.entries > 0 {
            return;
        }
        let Some(&(up, (fair, px, fee, edge))) = sides
            .iter()
            .filter(|x| x.1.3 >= self.cfg.shadow_min_edge - 1e-12 && x.1.3 <= self.cfg.max_edge + 1e-12)
            .max_by(|a, b| a.1.3.total_cmp(&b.1.3))
        else {
            return;
        };
        let x_cons = cons_q(up, k, px + fee, self.cfg.cons_age_half_ms) - (px + fee);
        ws.shadow = Some(Shadow { up, px, fee, edge, x_cons });
        let asset = self.cfg.assets[i].clone();
        let left = (open + WINDOW_S) as f64 - t_ns as f64 / NS as f64;
        self.record(
            t_ns,
            "pu_shadow",
            json!({"asset": asset, "open": open, "side": if up { "up" } else { "down" }, "left": r4(left),
                   "p_side": r4(fair), "kalshi_spread": r4(k.ask - k.bid), "kalshi_book_age_ms": k.age_ms,
                   "ask": r4(px), "fee": r6(fee), "edge": r6(edge), "x_cons": r6(x_cons),
                   "real": edge >= self.cfg.min_edge - 1e-12, "epoch": self.cfg.epoch}),
        );
    }

    /// Each sizing rule's fill against the levels at or under the limit (sorted by price).
    fn size_rules(&mut self, o: &Order, levels: &[(f64, f64)], got: f64) -> BTreeMap<String, Alt> {
        let c = o.px + o.fee;
        let b = self.cfg.bankroll_usd;
        let depth: f64 = levels.iter().map(|l| l.1).sum();
        let q_adj = c + self.cfg.kelly_shrink * (o.fair - c);
        let q_cal = c + self.beta() * (o.fair - c);
        let used = self.window_alt.entry(o.open).or_default();
        let mut out = BTreeMap::new();
        for rule in RULES {
            let target = match rule {
                "flat" => o.shares,
                "tiers" => tier_shares(o.edge),
                "kelly_q" => 0.25 * kelly_f(o.fair, c) * b / c,
                "kelly_t" => 0.10 * kelly_f(o.fair, c) * b / c,
                "kelly_s" => 0.25 * kelly_f(q_adj, c) * b / c,
                "kelly_c" => 0.25 * kelly_f(o.q_cons, c) * b / c,
                "usd_flat" => self.cfg.stake_usd / c,
                "usd_tiers" => self.cfg.stake_usd * usd_tier_mult(o.edge / c) / c,
                _ => 0.25 * kelly_f(q_cal, c) * b / c,
            };
            let want = if rule == "flat" {
                got
            } else {
                let room = (self.cfg.window_risk_frac * b - used.get(rule).copied().unwrap_or(0.0)).max(0.0);
                target.min(self.cfg.trade_risk_frac * b / c).min(room / c).min(depth).max(0.0)
            };
            let (shares, usd, fee) = walk(levels, want, |p| Self::fee(o.theta, p));
            *used.entry(rule).or_default() += usd + fee;
            out.insert(rule.to_owned(), Alt { target, shares, usd, fee });
        }
        out
    }

    /// The book fetched for a paper fill: Yes bids and offers as (price, qty).
    pub fn on_book(&mut self, id: u64, bids: &[(f64, f64)], offers: &[(f64, f64)], t_ns: i64) {
        let Some(o) = self.pending.remove(&id) else { return };
        let Some(w) = self.windows.get(&(o.asset, o.open)).cloned() else { return };
        let (levels, best) = side_levels(o.up, o.limit, bids, offers);
        let (got, usd, fee) = walk(&levels, o.shares, |p| Self::fee(o.theta, p));
        let depth: f64 = levels.iter().map(|l| l.1).sum();
        let alt = if got > 1e-9 { self.size_rules(&o, &levels, got) } else { BTreeMap::new() };
        let body = json!({
            "id": id, "asset": w.asset, "open": o.open, "side": if o.up { "up" } else { "down" }, "mode": "paper",
            "limit": r4(o.limit), "ask_now": if best.is_finite() { r4(best) } else { Value::Null },
            "held_ms": (t_ns - o.decision_ns) / 1_000_000,
            "shares": r4(got), "usd": r4(usd), "fee": r6(fee),
            "avg": if got > 0.0 { r4(usd / got) } else { Value::Null },
            "depth_at_limit": r4(depth),
            "levels": levels.iter().take(12).map(|(p, q)| json!([r4(*p), r4(*q)])).collect::<Vec<_>>(),
            "cal": {"beta": r4(self.beta()), "n": self.cal.n, "q_side": r4(o.fair), "q_cons": r4(o.q_cons),
                    "q_cal": r4(o.px + o.fee + self.beta() * (o.fair - o.px - o.fee))},
            "sizes": alt.iter().map(|(k, a)| (k.clone(), json!({"target": r4(a.target), "shares": r4(a.shares), "usd": r4(a.usd), "fee": r6(a.fee)})))
                .collect::<serde_json::Map<String, Value>>(),
        });
        if got > 1e-9 {
            self.counters.fills += 1;
            self.record(t_ns, "pu_fill", body);
            self.p.positions.push(Position {
                id,
                asset: w.asset.clone(),
                open: o.open,
                up: o.up,
                slug: w.slug.clone(),
                shares: got,
                usd,
                fee,
                entry_ns: t_ns,
                outcome: None,
                pnl: None,
                epoch: self.cfg.epoch.clone(),
                alt,
            });
            self.dirty = true;
        } else {
            self.counters.no_fills += 1;
            self.record(t_ns, "pu_no_fill", body);
        }
    }

    /// The book fetched `delay_ms` after decision `id`: what an order arriving
    /// then would have filled. Never a position; settled as `pu_probe_settle`.
    #[allow(clippy::too_many_arguments)]
    pub fn on_probe(
        &mut self,
        id: u64,
        delay_ms: i64,
        bids: &[(f64, f64)],
        offers: &[(f64, f64)],
        venue_ns: Option<i64>,
        cache_age_s: Option<i64>,
        t_ns: i64,
    ) {
        let Some(o) = self.probe_orders.get(&id).cloned() else { return };
        let asset = self.cfg.assets[o.asset].clone();
        let (levels, best) = side_levels(o.up, o.limit, bids, offers);
        let (got, usd, fee) = walk(&levels, o.shares, |p| Self::fee(o.theta, p));
        let avg = if got > 1e-9 { usd / got } else { f64::NAN };
        self.record(
            t_ns,
            "pu_fill_probe",
            json!({"id": id, "asset": asset, "open": o.open, "side": if o.up { "up" } else { "down" },
                   "delay_ms": delay_ms, "held_ms": (t_ns - o.decision_ns) / 1_000_000,
                   "venue_ts": venue_ns.map(iso), "venue_age_ms": venue_ns.map(|v| (t_ns - v) / 1_000_000),
                   "cache_age_s": cache_age_s,
                   "limit": r4(o.limit), "decision_ask": r4(o.px), "ask_now": if best.is_finite() { r4(best) } else { Value::Null },
                   "want": r4(o.shares), "shares": r4(got), "usd": r4(usd), "fee": r6(fee), "avg": r4(avg),
                   "slip": r4(avg - o.px), "depth_at_limit": r4(levels.iter().map(|l| l.1).sum()),
                   "epoch": self.cfg.epoch}),
        );
        self.p.probes.push(Probe {
            id,
            asset,
            open: o.open,
            up: o.up,
            delay_ms,
            shares: got,
            usd,
            fee,
            epoch: self.cfg.epoch.clone(),
            settled: false,
        });
        self.dirty = true;
    }

    /// Did paper order `id` fill (an open position)?
    pub fn filled(&self, id: u64) -> bool {
        self.p.positions.iter().any(|p| p.id == id && p.outcome.is_none())
    }

    /// The quick-exit study: sell position `id` into the book `delay_ms` after
    /// its fill (counterfactual; the position is held to settlement).
    pub fn on_exit(&mut self, id: u64, delay_ms: i64, bids: &[(f64, f64)], offers: &[(f64, f64)], t_ns: i64) {
        let Some(p) = self.p.positions.iter().find(|p| p.id == id && p.outcome.is_none()).cloned() else { return };
        let theta = self.windows.get(&(self.asset_index(&p.asset).unwrap_or(0), p.open)).map_or(self.cfg.fee_theta, |w| {
            w.fee_coef.unwrap_or(self.cfg.fee_theta)
        });
        let levels = exit_levels(p.up, bids, offers);
        let (sold, proceeds, fee) = walk(&levels, p.shares, |x| Self::fee(theta, x));
        let entry_cost = p.usd + p.fee;
        let best = levels.first().map_or(f64::NAN, |l| l.0);
        let pnl_sold = proceeds - fee - entry_cost * sold / p.shares.max(1e-9);
        self.record(
            t_ns,
            "pu_exit_probe",
            json!({"id": id, "asset": p.asset, "open": p.open, "side": if p.up { "up" } else { "down" },
                   "delay_ms": delay_ms, "held_ms": (t_ns - p.entry_ns) / 1_000_000,
                   "shares": r4(p.shares), "entry_avg": r4(p.usd / p.shares.max(1e-9)), "entry_cost": r4(entry_cost),
                   "best_bid": r4(best), "depth": r4(levels.iter().map(|l| l.1).sum()),
                   "sold": r4(sold), "proceeds": r4(proceeds), "fee": r6(fee),
                   "avg": if sold > 1e-9 { r4(proceeds / sold) } else { Value::Null },
                   "pnl_sold": r4(pnl_sold), "per_share_sold": if sold > 1e-9 { r4(pnl_sold / sold) } else { Value::Null },
                   "epoch": self.cfg.epoch}),
        );
        self.p.exits.push(ExitProbe {
            id,
            asset: p.asset.clone(),
            open: p.open,
            up: p.up,
            delay_ms,
            shares: p.shares,
            entry_cost,
            sold,
            proceeds,
            fee,
            epoch: self.cfg.epoch.clone(),
            settled: false,
        });
        self.dirty = true;
    }

    /// The book could not be fetched: the paper order did not fill.
    pub fn on_book_error(&mut self, id: u64, error: &str, t_ns: i64) {
        if let Some(o) = self.pending.remove(&id) {
            self.counters.no_fills += 1;
            let asset = self.cfg.assets[o.asset].clone();
            self.record(
                t_ns,
                "pu_no_fill",
                json!({"id": id, "asset": asset, "open": o.open, "error": error}),
            );
        }
    }

    fn close_window(&mut self, i: usize, open: i64, t_ns: i64) {
        let Some(asset) = self.windows.get(&(i, open)).map(|w| w.asset.clone()) else { return };
        let ws = self.wstate.entry((i, open)).or_default();
        if ws.closed {
            return;
        }
        ws.closed = true;
        let (gate, best, entries) = (ws.gate, ws.best_edge, ws.entries);
        self.record(
            t_ns,
            "pu_close",
            json!({"asset": asset, "open": open, "last_gate": gate, "best_edge": best.map(r6), "entries": entries,
                   "epoch": self.cfg.epoch}),
        );
    }

    /// Windows to ask Polymarket US about: closed at least 20 s ago, recent, unresolved.
    pub fn to_resolve(&self, now_s: i64) -> Vec<WindowInfo> {
        self.windows
            .iter()
            .filter(|(key, w)| {
                !self.wstate.get(key).is_some_and(|s| s.resolved)
                    && now_s >= w.close() + 20
                    && now_s < w.close() + RESOLVE_FOR_S
            })
            .map(|(_, w)| w.clone())
            .collect()
    }

    /// Polymarket US settled a window (`yes` = Up); `kalshi` is Kalshi's result, if known.
    pub fn on_resolved(&mut self, asset: &str, open: i64, outcome: &str, kalshi: Option<&str>, t_ns: i64) {
        let Some(i) = self.asset_index(asset) else { return };
        let ws = self.wstate.entry((i, open)).or_default();
        if ws.resolved {
            return;
        }
        ws.resolved = true;
        let shadow = ws.shadow;
        let rpd = ws.rpd;
        match kalshi {
            Some(k) if k == outcome => self.counters.agree += 1,
            Some(_) => self.counters.disagree += 1,
            None => {}
        }
        self.record(
            t_ns,
            "pu_resolution",
            json!({"asset": asset, "open": open, "poly": outcome, "kalshi": kalshi, "agree": kalshi.map(|k| k == outcome)}),
        );
        let mut settled = Vec::new();
        for p in
            self.p.positions.iter_mut().filter(|p| p.asset == asset && p.open == open && p.outcome.is_none())
        {
            p.outcome = Some(outcome.to_owned());
            let payout = if p.won() == Some(true) { p.shares } else { 0.0 };
            p.pnl = Some(payout - p.usd - p.fee);
            settled.push(p.clone());
        }
        if let Some(sh) = shadow {
            let won = (outcome == "yes") == sh.up;
            let per_share = f64::from(u8::from(won)) - sh.px - sh.fee;
            if self.cfg.cal_epochs.contains(&self.cfg.epoch) {
                self.cal.add(sh.edge, per_share);
            }
            self.record(
                t_ns,
                "pu_shadow_settle",
                json!({"asset": asset, "open": open, "side": if sh.up { "up" } else { "down" }, "edge": r6(sh.edge),
                       "x_cons": r6(sh.x_cons), "cost": r4(sh.px + sh.fee), "real": sh.edge >= self.cfg.min_edge - 1e-12, "won": won, "per_share": r6(per_share),
                       "epoch": self.cfg.epoch}),
            );
        }
        if let Some(sh) = rpd {
            let won = (outcome == "yes") == sh.up;
            let cost = sh.px + sh.fee;
            let per_share = f64::from(u8::from(won)) - cost;
            self.record(
                t_ns,
                "pu_rpd_settle",
                json!({"asset": asset, "open": open, "side": if sh.up { "up" } else { "down" }, "edge": r6(sh.edge),
                       "cost": r4(cost), "rpd": r4(sh.edge / cost), "passes_5c": sh.edge >= self.cfg.min_edge - 1e-12,
                       "won": won, "per_share": r6(per_share), "per_usd": r6(per_share / cost),
                       "epoch": self.cfg.epoch}),
            );
        }
        let mut probe_recs = Vec::new();
        for pr in self.p.probes.iter_mut().filter(|p| p.asset == asset && p.open == open && !p.settled) {
            pr.settled = true;
            let won = (outcome == "yes") == pr.up;
            let pnl = if won { pr.shares } else { 0.0 } - pr.usd - pr.fee;
            probe_recs.push(("pu_probe_settle", json!({"id": pr.id, "asset": asset, "open": open, "delay_ms": pr.delay_ms, "won": won,
                                   "shares": r4(pr.shares), "cost": r4(pr.usd + pr.fee), "pnl": r4(pnl), "epoch": pr.epoch})));
        }
        for ex in self.p.exits.iter_mut().filter(|x| x.asset == asset && x.open == open && !x.settled) {
            ex.settled = true;
            let won = (outcome == "yes") == ex.up;
            let payout = if won { 1.0 } else { 0.0 };
            // Sold shares exit at the bid; any remainder is held to settlement.
            let pnl = ex.proceeds - ex.fee + (ex.shares - ex.sold) * payout - ex.entry_cost;
            let hold = ex.shares * payout - ex.entry_cost;
            probe_recs.push((
                "pu_exit_settle",
                json!({"id": ex.id, "asset": asset, "open": open, "delay_ms": ex.delay_ms, "won": won,
                       "shares": r4(ex.shares), "sold": r4(ex.sold), "full": ex.sold >= ex.shares - 1e-9,
                       "pnl": r4(pnl), "hold_pnl": r4(hold), "diff": r4(pnl - hold), "epoch": ex.epoch}),
            ));
        }
        if !probe_recs.is_empty() {
            self.dirty = true;
        }
        for (kind, r) in probe_recs {
            self.record(t_ns, kind, r);
        }
        for p in settled {
            let pnl = p.pnl.unwrap_or(0.0);
            // Booked to the day the position was entered, not the day its window closed.
            let day = day_of(p.entry_ns / NS);
            *self.p.realized.entry(day.clone()).or_default() += pnl;
            let payout = |a: &Alt| if p.won() == Some(true) { a.shares } else { 0.0 };
            let alt: serde_json::Map<String, Value> = p
                .alt
                .iter()
                .map(|(k, a)| (k.clone(), json!({"shares": r4(a.shares), "cost": r4(a.usd + a.fee), "pnl": r4(payout(a) - a.usd - a.fee)})))
                .collect();
            self.counters.settled += 1;
            self.dirty = true;
            self.record(
                t_ns,
                "pu_settle",
                json!({"id": p.id, "asset": p.asset, "open": p.open, "side": if p.up { "up" } else { "down" },
                       "outcome": outcome, "won": p.won(), "shares": r4(p.shares), "usd": r4(p.usd), "fee": r6(p.fee),
                       "pnl": r4(pnl), "per_share": r4(pnl / p.shares.max(1e-9)), "live": false, "epoch": p.epoch,
                       "entry_day": day, "day_pnl": r4(self.p.realized.get(&day).copied().unwrap_or(0.0)), "alt": alt}),
            );
        }
        let today = day_of(t_ns / NS);
        let pnl_today = self.p.realized.get(&today).copied().unwrap_or(0.0);
        if pnl_today <= -self.cfg.daily_loss_usd
            && self.p.halted_day.as_deref() != Some(&today)
            && self.p.acked_day.as_deref() != Some(&today)
        {
            if self.cfg.enforce_halt {
                self.p.halted_day = Some(today.clone());
                self.dirty = true;
                self.record(
                    t_ns,
                    "pu_halt",
                    json!({"day": today, "pnl": r4(pnl_today), "limit": self.cfg.daily_loss_usd}),
                );
            } else if self.breach_noted.as_deref() != Some(&today) {
                // Paper: journal the breach, keep collecting the sample.
                self.breach_noted = Some(today.clone());
                self.record(
                    t_ns,
                    "pu_halt",
                    json!({"day": today, "pnl": r4(pnl_today), "limit": self.cfg.daily_loss_usd, "record_only": true}),
                );
            }
        }
    }

    pub fn prune(&mut self, now_s: i64) {
        let old: Vec<(usize, i64)> = self
            .windows
            .iter()
            .filter(|(_, w)| w.close() + RESOLVE_FOR_S + 600 < now_s)
            .map(|(k, _)| *k)
            .collect();
        for key in old {
            if let Some(w) = self.windows.remove(&key)
                && let Some(t) = &w.kalshi_ticker
            {
                self.kowner.remove(t);
                self.kbooks.remove(t);
            }
            self.wstate.remove(&key);
            self.pm.remove(&key);
            self.window_alt.remove(&key.1);
        }
        let live: BTreeSet<i64> = self.windows.keys().map(|k| k.1).collect();
        self.probe_orders.retain(|_, o| live.contains(&o.open));
        let n = self.p.probes.len();
        self.p.probes.retain(|p| !p.settled && p.open + WINDOW_S + RESOLVE_FOR_S + 600 >= now_s);
        self.p.exits.retain(|x| !x.settled && x.open + WINDOW_S + RESOLVE_FOR_S + 600 >= now_s);
        if self.p.probes.len() != n {
            self.dirty = true;
        }
        let before = self.p.positions.len();
        self.p.positions.retain(|p| p.outcome.is_none());
        if self.p.positions.len() != before {
            self.dirty = true;
        }
        let keep_from = day_of(now_s - 40 * 86_400);
        self.p.realized.retain(|d, _| d.as_str() >= keep_from.as_str());
    }

    pub fn status_value(&self, t_ns: i64) -> Value {
        let today = day_of(t_ns / NS);
        json!({
            "mode": "paper",
            "venue": "polymarket-us",
            "counters": serde_json::to_value(&self.counters).unwrap_or(Value::Null),
            "day_pnl": r4(self.p.realized.get(&today).copied().unwrap_or(0.0)),
            "open_positions": self.p.positions.iter().filter(|p| p.outcome.is_none()).count(),
            "open_cost": r4(self.open_cost()),
            "halted": self.halted(t_ns), "killed": self.killed,
            "windows": self.windows.len(),
            "cal": {"beta": r4(self.beta()), "n": self.cal.n, "prior": self.cfg.kelly_shrink,
                    "prior_n": self.cfg.cal_prior_n, "epochs": self.cfg.cal_epochs},
            "epoch": self.cfg.epoch,
            "kalshi_age_ms": if self.k_last_frame_ns > 0 { Value::from((t_ns - self.k_last_frame_ns) / 1_000_000) } else { Value::Null },
            "kalshi_books": self.kbooks.values().filter(|b| b.snapshot).count(),
            "pm_age_ms": self.pm.values().map(|q| (t_ns - q.t_ns) / 1_000_000).min(),
        })
    }

    pub fn status(&mut self, t_ns: i64) {
        let body = self.status_value(t_ns);
        self.record(t_ns, "pu_status", body);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::tests::vars;

    const OPEN: i64 = 1_800_000_000 - 1_800_000_000 % WINDOW_S;

    fn engine() -> Engine {
        engine_with(&[("PK_PU_MIN_EDGE", "0.03"), ("PK_PU_SHADOW_MIN_EDGE", "0.02")])
    }

    fn engine_with(kv: &[(&str, &str)]) -> Engine {
        let mut v = vec![("PK_PU_ASSETS", "BTC"), ("PK_PU_FEE_THETA", "0.0695")];
        v.extend_from_slice(kv);
        let mut e = Engine::new(Config::from_vars(vars(&v)).unwrap(), Persisted::default());
        e.set_window(
            WindowInfo {
                asset: "BTC".into(),
                open: OPEN,
                slug: "cpc-btc-updown-15m-x".into(),
                kalshi_ticker: Some("KX".into()),
                tick: 0.01,
                min_qty: 0.01,
                index: "BRTI".into(),
                fee_coef: None,
            },
            OPEN * NS,
        );
        e
    }

    fn kalshi(e: &mut Engine, bid: f64, ask: f64, t: i64) {
        let f = json!({"type": "orderbook_snapshot", "sid": 1, "seq": 1,
                       "msg": {"market_ticker": "KX", "yes_dollars_fp": [[format!("{bid:.4}"), "100.00"]],
                               "no_dollars_fp": [[format!("{:.4}", 1.0 - ask), "100.00"]]}});
        e.on_kalshi(&f.to_string(), t);
    }

    fn decisions(e: &mut Engine) -> Vec<Value> {
        e.take_records().into_iter().filter(|r| r["type"] == "pu_decision").collect()
    }

    #[test]
    fn buys_yes_when_polymarket_us_is_cheaper_than_kalshi() {
        let mut e = engine();
        let t = (OPEN + 300) * NS;
        kalshi(&mut e, 0.59, 0.61, t);
        e.on_pm_quote(0, OPEN, Some(0.54), Some(0.55), None, t);
        let acts = e.tick(t);
        assert_eq!(acts.len(), 1);
        let d = &decisions(&mut e)[0];
        assert_eq!(d["side"], "up");
        // 0.60 − 0.55 − 0.0695 · 0.55 · 0.45 ≈ 0.0328
        assert!((d["edge"].as_f64().unwrap() - 0.0328).abs() < 1e-3, "{d}");
        assert_eq!(d["limit"].as_f64().unwrap(), 0.56);
        // Paper fill against the fetched book: 10 at 0.55, nothing at 0.57.
        e.on_book(acts[0].id, &[(0.54, 50.0)], &[(0.55, 6.0), (0.56, 2.0), (0.57, 100.0)], t + 150_000_000);
        let f = e.take_records().into_iter().find(|r| r["type"] == "pu_fill").unwrap();
        assert_eq!(f["shares"].as_f64().unwrap(), 8.0);
        assert!((f["avg"].as_f64().unwrap() - 0.5525).abs() < 1e-4);
    }

    #[test]
    fn buys_no_by_taking_yes_bids() {
        let mut e = engine();
        let t = (OPEN + 300) * NS;
        kalshi(&mut e, 0.39, 0.41, t); // No worth 0.60
        e.on_pm_quote(0, OPEN, Some(0.45), Some(0.47), None, t); // No costs 0.55
        let acts = e.tick(t);
        let d = &decisions(&mut e)[0];
        assert_eq!(d["side"], "down");
        e.on_book(acts[0].id, &[(0.45, 20.0)], &[(0.47, 20.0)], t + 150_000_000);
        let f = e.take_records().into_iter().find(|r| r["type"] == "pu_fill").unwrap();
        assert_eq!(f["shares"].as_f64().unwrap(), 10.0);
        assert!((f["avg"].as_f64().unwrap() - 0.55).abs() < 1e-9);
        // Settles on Polymarket US: No won.
        e.on_resolved("BTC", OPEN, "no", Some("no"), (OPEN + 930) * NS);
        let s = e.take_records().into_iter().find(|r| r["type"] == "pu_settle").unwrap();
        let want = 10.0 - 5.5 - 10.0 * 0.0695 * 0.55 * 0.45;
        assert!((s["pnl"].as_f64().unwrap() - want).abs() < 1e-3);
    }

    #[test]
    fn gates() {
        let t = (OPEN + 300) * NS;
        // No gap.
        let mut e = engine();
        kalshi(&mut e, 0.54, 0.56, t);
        e.on_pm_quote(0, OPEN, Some(0.54), Some(0.56), None, t);
        assert!(e.tick(t).is_empty());
        // Too early in the window.
        let mut e = engine();
        kalshi(&mut e, 0.59, 0.61, OPEN * NS + 30 * NS);
        e.on_pm_quote(0, OPEN, Some(0.54), Some(0.55), None, OPEN * NS + 30 * NS);
        assert!(e.tick(OPEN * NS + 30 * NS).is_empty());
        // A gap over the cap is taken for a stale book.
        let mut e = engine();
        kalshi(&mut e, 0.79, 0.81, t);
        e.on_pm_quote(0, OPEN, Some(0.54), Some(0.55), None, t);
        assert!(e.tick(t).is_empty());
        // A stale Polymarket US quote.
        let mut e = engine();
        kalshi(&mut e, 0.59, 0.61, t);
        e.on_pm_quote(0, OPEN, Some(0.54), Some(0.55), None, t - 2 * NS);
        assert!(e.tick(t).is_empty());
        // A wide Kalshi book.
        let mut e = engine();
        kalshi(&mut e, 0.55, 0.65, t);
        e.on_pm_quote(0, OPEN, Some(0.50), Some(0.51), None, t);
        assert!(e.tick(t).is_empty());
    }

    #[test]
    fn one_entry_per_window_and_agreement() {
        let mut e = engine();
        let t = (OPEN + 300) * NS;
        kalshi(&mut e, 0.59, 0.61, t);
        e.on_pm_quote(0, OPEN, Some(0.54), Some(0.55), None, t);
        assert_eq!(e.tick(t).len(), 1);
        e.on_pm_quote(0, OPEN, Some(0.54), Some(0.55), None, t + NS);
        assert!(e.tick(t + NS).is_empty());
        e.on_resolved("BTC", OPEN, "yes", Some("no"), (OPEN + 930) * NS);
        let r = e.take_records().into_iter().find(|r| r["type"] == "pu_resolution").unwrap();
        assert_eq!(r["agree"], false);
    }

    #[test]
    fn sizing_rules_are_capped_by_depth_risk_and_window() {
        let mut e = engine();
        let t = (OPEN + 300) * NS;
        kalshi(&mut e, 0.62, 0.64, t); // fair 0.63
        e.on_pm_quote(0, OPEN, Some(0.50), Some(0.52), None, t); // edge ≈ 0.63 − 0.52 − 0.0173 ≈ 0.093
        let acts = e.tick(t);
        let d = &decisions(&mut e)[0];
        assert!((d["edge"].as_f64().unwrap() - 0.0927).abs() < 1e-3, "{d}");
        // 25 at the ask, 15 one tick up (the limit), more above the limit.
        e.on_book(acts[0].id, &[(0.50, 10.0)], &[(0.52, 25.0), (0.53, 15.0), (0.60, 500.0)], t + 150_000_000);
        let f = e.take_records().into_iter().find(|r| r["type"] == "pu_fill").unwrap();
        assert_eq!(f["depth_at_limit"].as_f64().unwrap(), 40.0);
        let sz = &f["sizes"];
        assert_eq!(sz["flat"]["shares"].as_f64().unwrap(), 10.0);
        assert_eq!(sz["tiers"]["shares"].as_f64().unwrap(), 20.0); // 8–11¢ tier
        // Quarter Kelly wants ~ 0.25 · 0.193 · 1000 / 0.537 ≈ 90 shares; 2% risk caps it at 20 / 0.537 ≈ 37.2.
        assert!(sz["kelly_q"]["target"].as_f64().unwrap() > 80.0);
        assert!(
            (sz["kelly_q"]["shares"].as_f64().unwrap() - 20.0 / (0.52 + 0.0695 * 0.52 * 0.48)).abs() < 0.1
        );
        // Shrunk Kelly asks for less than quarter Kelly.
        assert!(sz["kelly_s"]["target"].as_f64().unwrap() < sz["kelly_q"]["target"].as_f64().unwrap());
        // No settled shadows yet: β is its prior (0.5), so calibrated Kelly is shrunk Kelly.
        assert!((sz["kelly_cal"]["target"].as_f64().unwrap() - sz["kelly_s"]["target"].as_f64().unwrap()).abs() < 1e-9);
        assert_eq!(f["cal"]["beta"].as_f64().unwrap(), 0.5);
        // Conservative Kelly prices the Up side at Kalshi's Yes bid (0.62, fresh book), not the mid.
        let c = f["cal"]["q_cons"].as_f64().unwrap();
        assert!((c - 0.62).abs() < 1e-4, "{c}");
        assert!(sz["kelly_c"]["target"].as_f64().unwrap() < sz["kelly_q"]["target"].as_f64().unwrap());
        // Dollar rules: $5 / c, and 2× at 17% per dollar (10–20% tier).
        let c = 0.52 + 0.0695 * 0.52 * 0.48;
        assert!((sz["usd_flat"]["target"].as_f64().unwrap() - 5.0 / c).abs() < 1e-3);
        assert!((sz["usd_tiers"]["target"].as_f64().unwrap() - 10.0 / c).abs() < 1e-3);
        // Settle: each rule's P&L is journaled; booked to the entry day.
        e.on_resolved("BTC", OPEN, "yes", Some("yes"), (OPEN + 930) * NS);
        let s = e.take_records().into_iter().find(|r| r["type"] == "pu_settle").unwrap();
        assert!(s["alt"]["tiers"]["pnl"].as_f64().unwrap() > 9.0);
        assert_eq!(s["entry_day"], day_of(OPEN + 300));
    }

    #[test]
    fn shadow_logs_the_first_three_cent_gap_and_settles_it() {
        let mut e = engine_with(&[]); // defaults: min 5¢, shadow from 3¢
        let t = (OPEN + 300) * NS;
        kalshi(&mut e, 0.59, 0.61, t);
        e.on_pm_quote(0, OPEN, Some(0.54), Some(0.55), None, t); // edge ≈ 3.3¢: shadow only
        assert!(e.tick(t).is_empty());
        let sh = e.take_records().into_iter().find(|r| r["type"] == "pu_shadow").unwrap();
        assert_eq!(sh["real"], false);
        e.on_resolved("BTC", OPEN, "no", None, (OPEN + 930) * NS);
        let ss = e.take_records().into_iter().find(|r| r["type"] == "pu_shadow_settle").unwrap();
        assert_eq!(ss["won"], false);
        assert!(ss["per_share"].as_f64().unwrap() < -0.55);
        // Epoch 5 is a calibration epoch: the settled shadow feeds β.
        assert_eq!(e.cal.n, 1);
        assert!(e.beta() < 0.5);
    }

    #[test]
    fn the_per_dollar_gate_takes_cheap_small_gaps_as_a_shadow() {
        let mut e = engine_with(&[]); // 5¢ to trade, 10% per dollar to shadow
        let t = (OPEN + 300) * NS;
        kalshi(&mut e, 0.12, 0.14, t); // Up fair 0.13
        e.on_pm_quote(0, OPEN, Some(0.09), Some(0.10), None, t); // c ≈ 0.1063, edge ≈ 2.4¢ → 22% per dollar
        assert!(e.tick(t).is_empty());
        let recs = e.take_records();
        let r = recs.iter().find(|r| r["type"] == "pu_rpd").unwrap();
        assert_eq!((r["side"].as_str(), r["passes_5c"].as_bool()), (Some("up"), Some(false)));
        assert!((r["rpd"].as_f64().unwrap() - 0.223).abs() < 1e-3, "{r}");
        assert!(!recs.iter().any(|r| r["type"] == "pu_shadow" || r["type"] == "pu_decision"));
        e.on_resolved("BTC", OPEN, "yes", None, (OPEN + 930) * NS);
        let s = e.take_records().into_iter().find(|r| r["type"] == "pu_rpd_settle").unwrap();
        assert_eq!(s["won"], true);
        assert!((s["per_usd"].as_f64().unwrap() - (1.0 - 0.106255) / 0.106255).abs() < 1e-3);
    }

    #[test]
    fn the_per_dollar_gate_keeps_looking_after_the_entry() {
        let mut e = engine_with(&[]);
        let t = (OPEN + 300) * NS;
        kalshi(&mut e, 0.59, 0.61, t);
        e.on_pm_quote(0, OPEN, Some(0.52), Some(0.53), None, t); // edge ≈ 5.3¢ but 9.6% per dollar
        assert_eq!(e.tick(t).len(), 1);
        let recs = e.take_records();
        let d = recs.iter().find(|r| r["type"] == "pu_decision").unwrap();
        assert!((d["rpd"].as_f64().unwrap() - 0.0963).abs() < 1e-3, "{d}");
        assert!(!recs.iter().any(|r| r["type"] == "pu_rpd"));
        // Kalshi moves up: the window is done for the 5¢ rule; the per-dollar gate still fires.
        kalshi(&mut e, 0.62, 0.64, t + NS);
        e.on_pm_quote(0, OPEN, Some(0.52), Some(0.53), None, t + NS);
        assert!(e.tick(t + NS).is_empty());
        let r = e.take_records().into_iter().find(|r| r["type"] == "pu_rpd").unwrap();
        assert_eq!(r["passes_5c"], true);
        assert!(r["rpd"].as_f64().unwrap() > 0.15);
    }

    #[test]
    fn exit_probes_walk_the_bid_and_settle_beside_holding() {
        let mut e = engine();
        let t = (OPEN + 300) * NS;
        kalshi(&mut e, 0.59, 0.61, t);
        e.on_pm_quote(0, OPEN, Some(0.54), Some(0.55), None, t);
        let acts = e.tick(t);
        e.on_book(acts[0].id, &[(0.54, 50.0)], &[(0.55, 50.0)], t + 150_000_000);
        assert!(e.filled(acts[0].id));
        e.take_records();
        let fee = |p: f64| 0.0695 * p * (1.0 - p);
        let cost = 10.0 * (0.55 + fee(0.55));
        // +2 s: the Yes bid has caught up to 0.58, 6 there and 10 at 0.57.
        e.on_exit(acts[0].id, 2000, &[(0.58, 6.0), (0.57, 10.0)], &[(0.60, 50.0)], t + 2_150_000_000);
        // +10 s: no bid at all.
        e.on_exit(acts[0].id, 10_000, &[], &[(0.60, 50.0)], t + 10_150_000_000);
        let ex: Vec<Value> = e.take_records().into_iter().filter(|r| r["type"] == "pu_exit_probe").collect();
        assert_eq!(ex[0]["sold"].as_f64().unwrap(), 10.0);
        assert_eq!(ex[0]["best_bid"].as_f64().unwrap(), 0.58);
        let proceeds = 6.0 * 0.58 + 4.0 * 0.57;
        let exit_fee = 6.0 * fee(0.58) + 4.0 * fee(0.57);
        assert!((ex[0]["pnl_sold"].as_f64().unwrap() - (proceeds - exit_fee - cost)).abs() < 1e-3, "{}", ex[0]);
        assert_eq!(ex[1]["sold"].as_f64().unwrap(), 0.0);
        // It lost: the 2 s exit kept its sale, the 10 s one held to a loss.
        e.on_resolved("BTC", OPEN, "no", None, (OPEN + 930) * NS);
        let st: Vec<Value> = e.take_records().into_iter().filter(|r| r["type"] == "pu_exit_settle").collect();
        assert_eq!(st.len(), 2);
        let two = st.iter().find(|r| r["delay_ms"] == 2000).unwrap();
        assert!((two["pnl"].as_f64().unwrap() - (proceeds - exit_fee - cost)).abs() < 1e-3);
        assert!((two["hold_pnl"].as_f64().unwrap() + cost).abs() < 1e-3);
        assert_eq!(two["full"], true);
        let ten = st.iter().find(|r| r["delay_ms"] == 10_000).unwrap();
        assert_eq!(ten["diff"].as_f64().unwrap(), 0.0);
        // A down position sells No at 1 − the Yes offer.
        assert_eq!(exit_levels(false, &[(0.4, 5.0)], &[(0.45, 3.0), (0.47, 2.0)]), vec![(0.55, 3.0), (0.53, 2.0)]);
    }

    #[test]
    fn calibration_shrinks_to_its_prior_and_follows_the_data() {
        let mut c = Cal::default();
        assert_eq!(c.beta(0.5, 20.0), 0.5);
        for _ in 0..200 {
            c.add(0.06, 0.0); // gaps that never paid
        }
        assert!(c.beta(0.5, 20.0) < 0.05);
        let mut c = Cal::default();
        for _ in 0..200 {
            c.add(0.06, 0.06); // gaps that paid in full
        }
        assert!(c.beta(0.5, 20.0) > 0.95);
        // Losing gaps clamp at 0; overpaying ones at 1.
        let mut c = Cal::default();
        c.add(0.06, -0.5);
        assert_eq!(c.beta(0.5, 0.0), 0.0);
        // Near side: Up at the Yes bid, Down at 1 − the Yes ask; the gap halves per half-life of book age.
        let k = Quote { bid: 0.60, ask: 0.62, age_ms: 10_000 };
        assert!((cons_q(true, &k, 0.50, 10_000.0) - 0.55).abs() < 1e-12);
        assert!((cons_q(false, &k, 0.30, 10_000.0) - 0.34).abs() < 1e-12);
    }

    #[test]
    fn paper_loss_breach_is_record_only_unless_enforced() {
        for (enforce, halted) in [("0", false), ("1", true)] {
            let mut e = engine_with(&[
                ("PK_PU_MIN_EDGE", "0.03"),
                ("PK_PU_SHADOW_MIN_EDGE", "0.02"),
                ("PK_PU_DAILY_LOSS_USD", "1"),
                ("PK_PU_ENFORCE_HALT", enforce),
            ]);
            let t = (OPEN + 300) * NS;
            kalshi(&mut e, 0.59, 0.61, t);
            e.on_pm_quote(0, OPEN, Some(0.54), Some(0.55), None, t);
            let acts = e.tick(t);
            e.on_book(acts[0].id, &[(0.54, 50.0)], &[(0.55, 50.0)], t + 150_000_000);
            let after = (OPEN + 930) * NS;
            e.on_resolved("BTC", OPEN, "no", None, after);
            let h = e.take_records().into_iter().find(|r| r["type"] == "pu_halt").unwrap();
            assert_eq!(h["record_only"].as_bool().unwrap_or(false), !halted);
            assert_eq!(e.halted(after), halted);
        }
    }

    #[test]
    fn a_fresh_quote_evaluates_without_waiting_for_the_clock() {
        let mut e = engine();
        let t = (OPEN + 300) * NS;
        kalshi(&mut e, 0.59, 0.61, t);
        e.on_pm_quote(0, OPEN, Some(0.54), Some(0.55), Some(t - 40_000_000), t);
        let a = e.evaluate_now(0, OPEN, t, "pm_quote").expect("fires on the quote");
        assert_eq!(a.decision_ns, t);
        let d = &decisions(&mut e)[0];
        assert_eq!(d["trigger"], "pm_quote");
        assert_eq!(d["pm_venue_age_ms"], 40);
        assert_eq!(d["fee_theta"], 0.0695);
        // Before the entry period, and for a window that is not live, nothing.
        let mut e = engine();
        kalshi(&mut e, 0.59, 0.61, OPEN * NS + 10 * NS);
        e.on_pm_quote(0, OPEN, Some(0.54), Some(0.55), None, OPEN * NS + 10 * NS);
        assert!(e.evaluate_now(0, OPEN, OPEN * NS + 10 * NS, "pm_quote").is_none());
        assert!(e.evaluate_now(0, OPEN - WINDOW_S, t, "pm_quote").is_none());
    }

    #[test]
    fn a_kalshi_delta_names_its_window() {
        let mut e = engine();
        let t = (OPEN + 300) * NS;
        let f = json!({"type": "orderbook_snapshot", "sid": 1, "seq": 1,
                       "msg": {"market_ticker": "KX", "yes_dollars_fp": [["0.5900", "100.00"]], "no_dollars_fp": [["0.3900", "100.00"]]}});
        assert_eq!(e.on_kalshi(&f.to_string(), t), Some((0, OPEN)));
        let other = json!({"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": {"market_ticker": "KY"}});
        assert_eq!(e.on_kalshi(&other.to_string(), t), None);
    }

    #[test]
    fn probes_are_journaled_and_settled_apart_from_the_position() {
        let mut e = engine();
        let t = (OPEN + 300) * NS;
        kalshi(&mut e, 0.59, 0.61, t);
        e.on_pm_quote(0, OPEN, Some(0.54), Some(0.55), None, t);
        let a = e.tick(t).remove(0);
        e.take_records();
        // 25 ms: the ask still there; 150 ms: it moved past the limit.
        e.on_probe(a.id, 25, &[(0.54, 50.0)], &[(0.55, 50.0)], Some(t), None, t + 25_000_000);
        e.on_probe(a.id, 150, &[(0.54, 50.0)], &[(0.58, 50.0)], Some(t), None, t + 150_000_000);
        e.on_book(a.id, &[(0.54, 50.0)], &[(0.58, 50.0)], t + 150_000_000);
        let recs = e.take_records();
        let probes: Vec<&Value> = recs.iter().filter(|r| r["type"] == "pu_fill_probe").collect();
        assert_eq!(probes.len(), 2);
        assert_eq!(probes[0]["shares"].as_f64().unwrap(), 10.0);
        assert_eq!(probes[0]["slip"].as_f64().unwrap(), 0.0);
        assert_eq!(probes[1]["shares"].as_f64().unwrap(), 0.0);
        assert!(recs.iter().any(|r| r["type"] == "pu_no_fill"));
        assert!(e.p.positions.is_empty());
        e.on_resolved("BTC", OPEN, "yes", None, (OPEN + 930) * NS);
        let ps: Vec<Value> =
            e.take_records().into_iter().filter(|r| r["type"] == "pu_probe_settle").collect();
        assert_eq!(ps.len(), 2);
        let fast = ps.iter().find(|r| r["delay_ms"] == 25).unwrap();
        let want = 10.0 - 5.5 - 10.0 * 0.0695 * 0.55 * 0.45;
        assert!((fast["pnl"].as_f64().unwrap() - want).abs() < 1e-3);
        // Settled once.
        e.on_resolved("BTC", OPEN, "yes", None, (OPEN + 931) * NS);
        assert!(e.take_records().iter().all(|r| r["type"] != "pu_probe_settle"));
    }

    #[test]
    fn the_markets_fee_coefficient_is_the_fee() {
        let mut e = engine();
        e.windows.get_mut(&(0, OPEN)).unwrap().fee_coef = Some(0.11);
        let t = (OPEN + 300) * NS;
        kalshi(&mut e, 0.61, 0.63, t); // fair 0.62
        e.on_pm_quote(0, OPEN, Some(0.54), Some(0.55), None, t);
        let a = e.tick(t).remove(0);
        let d = &decisions(&mut e)[0];
        assert_eq!(d["fee_theta"], 0.11);
        // 0.62 − 0.55 − 0.11 · 0.55 · 0.45
        assert!((d["edge"].as_f64().unwrap() - 0.042775).abs() < 1e-6, "{d}");
        e.on_book(a.id, &[(0.54, 50.0)], &[(0.55, 50.0)], t + 150_000_000);
        let f = e.take_records().into_iter().find(|r| r["type"] == "pu_fill").unwrap();
        assert!((f["fee"].as_f64().unwrap() - 10.0 * 0.11 * 0.55 * 0.45).abs() < 1e-6, "{f}");
        // At 0.11 a 4.3¢ gap is under a 5¢ minimum: no entry.
        let mut e = engine_with(&[]);
        e.windows.get_mut(&(0, OPEN)).unwrap().fee_coef = Some(0.11);
        kalshi(&mut e, 0.61, 0.63, t);
        e.on_pm_quote(0, OPEN, Some(0.54), Some(0.55), None, t);
        assert!(e.tick(t).is_empty());
    }
}
