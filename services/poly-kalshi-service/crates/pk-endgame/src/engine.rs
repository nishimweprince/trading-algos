//! The endgame rule, its paper venue and its book-keeping. No I/O: the runtime
//! feeds it ticks, book frames and clock ticks, and carries out its actions.
//!
//! Each window and asset, from `entry_from_s` to `entry_until_s` before the
//! close, buy the side whose model probability beats its ask by `min_edge`
//! after the taker fee. One side per window (by default one entry), fixed
//! shares, a FAK limit at the ask plus `limit_offset`, never past the price
//! that leaves `min_net_edge`.
//!
//! **Paper** fills land `paper_latency_ms` after the decision, against the
//! book as it is then (the instant fill is journaled beside it, so the cost of
//! the delay is measured). **Live** hands the order to the runtime.
//!
//! Every position settles on Polymarket's own resolution (Gamma); our TWAP's
//! outcome is journaled beside it, so their agreement is a measured number.
//!
//! | Journal record | |
//! |---|---|
//! | `eg_window` | a window identified on Gamma |
//! | `eg_sec` | one second of the closing stretch: model and both books |
//! | `eg_decision` | an entry fired, with everything it saw |
//! | `eg_fill`, `eg_no_fill` | what the (paper or live) order got |
//! | `eg_close` | a window's averages, our outcome and the last gate |
//! | `eg_resolution` | Polymarket's outcome against ours |
//! | `eg_settle` | a position's P&L |
//! | `eg_halt`, `eg_ack`, `eg_kill`, `eg_redeem`, `eg_status` | risk and housekeeping (`eg_halt.record_only` in paper) |
//! | `eg_feed` | a feed dropped, went silent or reconnected |
//! | `eg_shadow_decision`, `eg_shadow_fill`, `eg_shadow_no_fill`, `eg_shadow_settle` | a shadow rule ([`SHADOWS`]): its own entry, paper fill and P&L, never traded |

use std::collections::{BTreeMap, BTreeSet, HashMap};

use chrono::{DateTime, Utc};
use pk_core::book::{Book, BookSide};
use pk_core::fees::taker_fee;
use pk_core::num::px_key;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::config::{Config, Mode};
use crate::model::{Series, fair, fair_from};

pub const NS: i64 = 1_000_000_000;
pub const WINDOW_S: i64 = 900;
/// Windows are asked about for this long after their close.
const RESOLVE_FOR_S: i64 = 2 * 3_600;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct WindowInfo {
    pub asset: String,
    pub open: i64,
    pub up_token: String,
    pub down_token: String,
    pub condition_id: String,
    pub slug: String,
    pub fee_rate: f64,
    pub fee_exponent: f64,
    /// The Kalshi market on the same window (`KX<ASSET>15M`), once found.
    #[serde(default)]
    pub kalshi_ticker: Option<String>,
}

impl WindowInfo {
    pub fn close(&self) -> i64 {
        self.open + WINDOW_S
    }
    fn token(&self, up: bool) -> &str {
        if up { &self.up_token } else { &self.down_token }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Position {
    pub id: u64,
    pub asset: String,
    pub open: i64,
    pub up: bool,
    pub token: String,
    pub condition_id: String,
    pub shares: f64,
    /// pUSD paid, before the fee.
    pub usd: f64,
    pub fee: f64,
    pub entry_ns: i64,
    pub live: bool,
    pub outcome: Option<String>,
    pub pnl: Option<f64>,
    #[serde(default)]
    pub redeemed: bool,
    #[serde(default)]
    pub redeem_tries: u32,
    /// The rule set it was entered under ("1" before epochs were recorded).
    #[serde(default = "epoch_one")]
    pub epoch: String,
}

fn epoch_one() -> String {
    "1".into()
}

impl Position {
    fn won(&self) -> Option<bool> {
        self.outcome.as_deref().map(|o| (o == "yes") == self.up)
    }
}

/// A shadow rule: the traded rule with one setting changed (PREREG, epoch 3).
/// Each fires at most once per coin-window, on the same evaluations, gates
/// and Kalshi filter as the traded rule, and fills on paper against the book
/// `paper_latency_ms` later. Shadows never count toward open cost, P&L or halts.
#[derive(Clone, Copy, Debug)]
pub struct ShadowRule {
    pub name: &'static str,
    /// `None` keeps the traded rule's value.
    pub min_edge: Option<f64>,
    pub min_px: Option<f64>,
    pub sd_mult_early: Option<f64>,
    /// Bid–ask spread of the side bought; no bid counts as wider than any cap.
    pub max_spread: Option<f64>,
}

pub const SHADOWS: [ShadowRule; 4] = [
    ShadowRule { name: "edge9", min_edge: Some(0.09), min_px: None, sd_mult_early: None, max_spread: None },
    ShadowRule { name: "fav40", min_edge: None, min_px: Some(0.40), sd_mult_early: None, max_spread: None },
    ShadowRule { name: "sd11", min_edge: None, min_px: None, sd_mult_early: Some(1.1), max_spread: None },
    ShadowRule { name: "spread10", min_edge: None, min_px: None, sd_mult_early: None, max_spread: Some(0.10) },
];

/// A shadow rule's filled paper position, until Polymarket resolves it.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ShadowPos {
    pub rule: String,
    pub sid: u64,
    pub asset: String,
    pub open: i64,
    pub up: bool,
    pub shares: f64,
    pub usd: f64,
    pub fee: f64,
    pub edge: f64,
    pub ask: f64,
}

/// An order between the decision and its fill.
#[derive(Clone, Debug)]
struct Order {
    id: u64,
    asset: usize,
    open: i64,
    up: bool,
    token: String,
    limit: f64,
    shares: f64,
    decision_ns: i64,
    fill_at_ns: i64,
    instant: (f64, f64, f64),
    /// A shadow rule's order (paper only, never sent): (rule, edge, ask).
    shadow: Option<(&'static str, f64, f64)>,
}

/// What the runtime is asked to do.
#[derive(Clone, Debug, PartialEq)]
pub enum Action {
    /// A live FAK buy.
    Enter { id: u64, asset: String, token: String, limit: f64, shares: f64 },
    /// Redeem a settled condition the wallet holds a winner of.
    Redeem { condition_id: String },
}

/// What a live order got back.
#[derive(Clone, Debug, Default)]
pub struct LiveResult {
    pub shares: f64,
    pub usd: f64,
    pub order_id: Option<String>,
    pub error: Option<String>,
    pub ack_us: Option<u64>,
}

/// One Kalshi market's book: YES bids and NO bids (a NO bid at p is a YES ask at 1 − p).
#[derive(Default)]
struct KBook {
    yes: BTreeMap<i32, f64>,
    no: BTreeMap<i32, f64>,
    snapshot: bool,
    last_ns: i64,
}

/// Kalshi's quote on one window, YES side.
#[derive(Clone, Copy, Debug)]
pub struct KQuote {
    pub bid: f64,
    pub ask: f64,
    pub mid: f64,
    /// Since this market's book last changed.
    pub age_ms: i64,
}

impl KBook {
    fn quote(&self, t_ns: i64) -> Option<KQuote> {
        if !self.snapshot {
            return None;
        }
        let bid = self.yes.keys().next_back().map(|&k| pk_core::num::key_px(k))?;
        // In integer ticks, so 1 − 0.55 is exactly 0.45.
        let ask = pk_core::num::key_px(pk_core::num::px_key(1.0) - *self.no.keys().next_back()?);
        (ask > bid).then(|| KQuote { bid, ask, mid: (bid + ask) / 2.0, age_ms: (t_ns - self.last_ns) / 1_000_000 })
    }
}

/// Spot mids from one venue for one asset: the newest and ~15 s of history.
#[derive(Default)]
struct Spot {
    mid: f64,
    last_ns: i64,
    hist: std::collections::VecDeque<(i64, f64)>,
}

impl Spot {
    fn push(&mut self, mid: f64, t_ns: i64) {
        if !(mid.is_finite() && mid > 0.0) {
            return;
        }
        self.mid = mid;
        self.last_ns = t_ns;
        self.hist.push_back((t_ns, mid));
        while self.hist.front().is_some_and(|h| h.0 < t_ns - 15 * NS) {
            self.hist.pop_front();
        }
    }

    /// The mid in force at `t_ns` (the newest at or before it).
    fn at(&self, t_ns: i64) -> Option<f64> {
        let i = self.hist.partition_point(|h| h.0 <= t_ns);
        (i > 0).then(|| self.hist[i - 1].1)
    }
}

/// Spot venues, in order of preference for the adjusted model.
pub const SPOT_VENUES: [&str; 2] = ["cb", "bn"];

#[derive(Default)]
struct WinState {
    /// The opening average and its seconds.
    k: Option<(f64, usize)>,
    entries: usize,
    sides: [bool; 2],
    /// Why the newest evaluation did not fire.
    gate: Option<&'static str>,
    best_edge: Option<f64>,
    closed: bool,
    ours: Option<&'static str>,
    resolved: bool,
    /// Shadow rules that already fired in this window (bit per [`SHADOWS`] index).
    shadow_fired: u8,
}

#[derive(Default, Serialize)]
struct Counters {
    ticks: u64,
    poly_frames: u64,
    decisions: u64,
    fills: u64,
    no_fills: u64,
    settled: u64,
    agree: u64,
    disagree: u64,
    book_resets: u64,
    feed_events: u64,
    kalshi_frames: u64,
    kalshi_resets: u64,
    spot_frames: u64,
}

/// The persisted part (`state.json`).
#[derive(Default, Serialize, Deserialize)]
pub struct Persisted {
    pub positions: Vec<Position>,
    pub realized: BTreeMap<String, f64>,
    pub halted_day: Option<String>,
    pub acked_day: Option<String>,
    pub next_id: u64,
    /// Shadow rules' open paper positions.
    #[serde(default)]
    pub shadows: Vec<ShadowPos>,
    #[serde(default)]
    pub next_shadow_id: u64,
}

pub struct Engine {
    pub cfg: Config,
    series: Vec<Series>,
    windows: BTreeMap<(usize, i64), WindowInfo>,
    wstate: HashMap<(usize, i64), WinState>,
    books: HashMap<String, Book>,
    owner: HashMap<String, (usize, i64, bool)>,
    kbooks: HashMap<String, KBook>,
    kowner: HashMap<String, (usize, i64)>,
    /// Arrival of the newest Polymarket market-channel frame of any kind.
    poly_last_frame_ns: i64,
    /// The day a record-only (paper) loss breach was last journaled.
    breach_noted: Option<String>,
    /// The newest frame on the Kalshi socket (any market): the feed is alive.
    k_last_frame_ns: i64,
    /// `spot[asset][venue]`, venues as [`SPOT_VENUES`].
    spot: Vec<[Spot; 2]>,
    pending: Vec<Order>,
    inflight: HashMap<u64, Order>,
    pub p: Persisted,
    killed: bool,
    last_grid: Vec<i64>,
    counters: Counters,
    records: Vec<Value>,
    pub dirty: bool,
}

fn iso(t_ns: i64) -> String {
    DateTime::<Utc>::from_timestamp_nanos(t_ns).format("%Y-%m-%dT%H:%M:%S%.3fZ").to_string()
}

fn day_of(t_s: i64) -> String {
    DateTime::<Utc>::from_timestamp(t_s, 0).map(|d| d.format("%Y-%m-%d").to_string()).unwrap_or_default()
}

fn r4(x: f64) -> Value {
    if x.is_finite() { ((x * 1e4).round() / 1e4).into() } else { Value::Null }
}

fn r6(x: f64) -> Value {
    if x.is_finite() { ((x * 1e6).round() / 1e6).into() } else { Value::Null }
}

/// Polymarket's tick: 0.001 beyond 0.96 / under 0.04, else 0.01.
pub fn tick_for(px: f64) -> f64 {
    if !(0.04..=0.96).contains(&px) { 0.001 } else { 0.01 }
}

fn floor_to(x: f64, step: f64) -> f64 {
    let v = ((x / step) + 1e-9).floor() * step;
    (v * 1e4).round() / 1e4
}

/// A buy of `shares` at up to `limit` against `book`: (shares, pUSD, fee).
fn walk(book: &Book, limit: f64, shares: f64, rate: f64, exponent: f64) -> (f64, f64, f64) {
    if book.snapshot_ns.is_none() {
        return (0.0, 0.0, 0.0);
    }
    let (mut got, mut usd, mut fee) = (0.0, 0.0, 0.0);
    for (px, size) in book.asks_upto(limit) {
        let q = size.min(shares - got);
        if q <= 1e-9 {
            break;
        }
        got += q;
        usd += q * px;
        fee += taker_fee(px, q, rate, exponent);
    }
    (got, usd, fee)
}

impl Engine {
    pub fn new(cfg: Config, persisted: Persisted) -> Self {
        let n = cfg.assets.len();
        Self {
            cfg,
            series: vec![Series::default(); n],
            windows: BTreeMap::new(),
            wstate: HashMap::new(),
            books: HashMap::new(),
            owner: HashMap::new(),
            kbooks: HashMap::new(),
            kowner: HashMap::new(),
            poly_last_frame_ns: 0,
            breach_noted: None,
            k_last_frame_ns: 0,
            spot: (0..n).map(|_| [Spot::default(), Spot::default()]).collect(),
            pending: Vec::new(),
            inflight: HashMap::new(),
            p: persisted,
            killed: false,
            last_grid: vec![0; n],
            counters: Counters::default(),
            records: Vec::new(),
            dirty: false,
        }
    }

    fn record(&mut self, t_ns: i64, kind: &str, mut body: Value) {
        if let Some(m) = body.as_object_mut() {
            m.insert("type".into(), kind.into());
            m.insert("t".into(), iso(t_ns).into());
            m.insert("t_ns".into(), t_ns.into());
        }
        self.records.push(body);
    }

    /// The journal records written since the last call.
    pub fn take_records(&mut self) -> Vec<Value> {
        std::mem::take(&mut self.records)
    }

    pub fn asset_index(&self, asset: &str) -> Option<usize> {
        self.cfg.assets.iter().position(|a| a.eq_ignore_ascii_case(asset))
    }

    pub fn has_window(&self, asset: usize, open: i64) -> bool {
        self.windows.contains_key(&(asset, open))
    }

    pub fn set_window(&mut self, w: WindowInfo, t_ns: i64) {
        let Some(i) = self.asset_index(&w.asset) else { return };
        if self.windows.contains_key(&(i, w.open)) {
            return;
        }
        self.owner.insert(w.up_token.clone(), (i, w.open, true));
        self.owner.insert(w.down_token.clone(), (i, w.open, false));
        if let Some(t) = &w.kalshi_ticker {
            self.kowner.insert(t.clone(), (i, w.open));
        }
        self.record(
            t_ns,
            "eg_window",
            json!({"asset": w.asset, "open": w.open, "slug": w.slug, "up": w.up_token, "down": w.down_token,
                   "condition_id": w.condition_id, "fee_rate": w.fee_rate, "fee_exponent": w.fee_exponent,
                   "kalshi": w.kalshi_ticker, "epoch": self.cfg.epoch}),
        );
        self.windows.insert((i, w.open), w);
    }

    /// Tokens whose books are needed: windows not yet closed, opening within 15 min.
    pub fn wanted_tokens(&self, now_s: i64) -> BTreeSet<String> {
        self.windows
            .values()
            .filter(|w| w.close() > now_s - 5 && w.open <= now_s + WINDOW_S)
            .flat_map(|w| [w.up_token.clone(), w.down_token.clone()])
            .collect()
    }

    /// Kalshi markets whose books are needed (the same windows as [`Self::wanted_tokens`]).
    pub fn wanted_kalshi(&self, now_s: i64) -> BTreeSet<String> {
        self.windows
            .values()
            .filter(|w| w.close() > now_s - 5 && w.open <= now_s + WINDOW_S)
            .filter_map(|w| w.kalshi_ticker.clone())
            .collect()
    }

    /// Windows still waiting for their Kalshi market: (asset, open).
    pub fn missing_kalshi(&self, now_s: i64) -> Vec<(String, i64)> {
        self.windows
            .values()
            .filter(|w| w.kalshi_ticker.is_none() && w.close() > now_s && w.open <= now_s + WINDOW_S)
            .map(|w| (w.asset.clone(), w.open))
            .collect()
    }

    pub fn set_kalshi_ticker(&mut self, asset: &str, open: i64, ticker: &str, t_ns: i64) {
        let Some(i) = self.asset_index(asset) else { return };
        let Some(w) = self.windows.get_mut(&(i, open)) else { return };
        if w.kalshi_ticker.is_some() {
            return;
        }
        w.kalshi_ticker = Some(ticker.to_owned());
        self.kowner.insert(ticker.to_owned(), (i, open));
        self.record(t_ns, "eg_kalshi_window", json!({"asset": asset, "open": open, "ticker": ticker}));
    }

    /// A Kalshi `orderbook_snapshot` / `orderbook_delta` frame.
    pub fn on_kalshi(&mut self, text: &str, t_ns: i64) {
        let Ok(f) = serde_json::from_str::<pk_engine::parse::KalshiFrame<'_>>(text) else { return };
        self.k_last_frame_ns = t_ns;
        self.counters.kalshi_frames += 1;
        let Some(m) = f.msg.as_ref() else { return };
        let Some(ticker) = m.market_ticker.as_ref().map(|t| t.as_str()) else { return };
        if !self.kowner.contains_key(ticker) {
            return;
        }
        let b = self.kbooks.entry(ticker.to_owned()).or_default();
        match f.kind.as_deref() {
            Some("orderbook_snapshot") => {
                b.yes = m.levels(true).into_iter().filter(|l| l.1 > 0.0).map(|(p, q)| (px_key(p), q)).collect();
                b.no = m.levels(false).into_iter().filter(|l| l.1 > 0.0).map(|(p, q)| (px_key(p), q)).collect();
                b.snapshot = true;
                b.last_ns = t_ns;
            }
            Some("orderbook_delta") if b.snapshot => {
                let Some((price, delta)) = m.delta() else { return };
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
            _ => {}
        }
    }

    /// The Kalshi socket reconnected or lost a sequence: books wait for new snapshots.
    pub fn invalidate_kalshi(&mut self) {
        self.kbooks.clear();
        self.counters.kalshi_resets += 1;
    }

    /// Kalshi's quote on a window, if its feed is alive.
    fn kalshi_quote(&self, i: usize, open: i64, t_ns: i64) -> Option<KQuote> {
        let w = self.windows.get(&(i, open))?;
        if t_ns - self.k_last_frame_ns > self.cfg.kalshi_max_age_ms * 1_000_000 {
            return None;
        }
        self.kbooks.get(w.kalshi_ticker.as_ref()?)?.quote(t_ns)
    }

    /// A Coinbase `ticker` (`venue` "cb") or Binance `bookTicker` (`venue` "bn")
    /// frame. Recorded only: no rule reads spot.
    pub fn on_spot_frame(&mut self, venue: &str, text: &str, t_ns: i64) {
        let Ok(v) = serde_json::from_str::<Value>(text) else { return };
        let num = |k: &str| v.get(k).and_then(|x| x.as_str().and_then(|s| s.parse::<f64>().ok()).or_else(|| x.as_f64()));
        let (asset, bid, ask) = match venue {
            "cb" if v.get("type").and_then(Value::as_str) == Some("ticker") => {
                let Some(p) = v.get("product_id").and_then(Value::as_str) else { return };
                (p.split('-').next().unwrap_or_default().to_owned(), num("best_bid"), num("best_ask"))
            }
            "bn" => {
                let Some(sym) = v.get("s").and_then(Value::as_str) else { return };
                (sym.trim_end_matches("USDT").to_owned(), num("b"), num("a"))
            }
            _ => return,
        };
        let (Some(b), Some(a)) = (bid, ask) else { return };
        let Some(i) = self.asset_index(&asset) else { return };
        let Some(j) = SPOT_VENUES.iter().position(|x| *x == venue) else { return };
        self.spot[i][j].push((a + b) / 2.0, t_ns);
        self.counters.spot_frames += 1;
    }

    /// The spot-adjusted price for the seconds still to come: the newest
    /// Chainlink tick moved by spot's change since that tick's second.
    /// (adjusted price, venue, spot move in bp). Fresh spot only (≤ 3 s).
    fn spot_adjusted(&self, i: usize, t_ns: i64) -> Option<(f64, &'static str, f64)> {
        let (sec, px) = self.series[i].last()?;
        for (j, venue) in SPOT_VENUES.iter().enumerate() {
            let sp = &self.spot[i][j];
            if sp.last_ns == 0 || t_ns - sp.last_ns > 3 * NS {
                continue;
            }
            // Chainlink stamps the second it observed; take spot at its middle.
            let Some(then) = sp.at(sec * NS + NS / 2) else { continue };
            let ratio = sp.mid / then;
            return Some((px * ratio, venue, (ratio - 1.0) * 1e4));
        }
        None
    }

    /// The same model as the rule's, from the spot-adjusted price. Data only.
    fn model_spot(&self, i: usize, open: i64, t_ns: i64) -> Option<(f64, &'static str, f64)> {
        let (_, k, sigma, mult) = self.model(i, open, t_ns).ok()?;
        let (s_adj, venue, move_bp) = self.spot_adjusted(i, t_ns)?;
        let f = fair_from(&self.series[i], k, open + WINDOW_S, sigma * mult, self.cfg.sd_floor, Some(s_adj))?;
        Some((f.p_up, venue, move_bp))
    }

    fn spot_json(&self, i: usize, t_ns: i64) -> Value {
        let mut m = serde_json::Map::new();
        for (j, venue) in SPOT_VENUES.iter().enumerate() {
            let sp = &self.spot[i][j];
            if sp.last_ns > 0 {
                m.insert((*venue).into(), json!({"mid": sp.mid, "age_ms": (t_ns - sp.last_ns) / 1_000_000}));
            }
        }
        if let Some((p, venue, mv)) = self.model_spot_cached(i, t_ns) {
            m.insert("p_up".into(), r6(p));
            m.insert("src".into(), venue.into());
            m.insert("move_bp".into(), r4(mv));
        }
        Value::Object(m)
    }

    fn model_spot_cached(&self, i: usize, t_ns: i64) -> Option<(f64, &'static str, f64)> {
        let now_s = t_ns / NS;
        let open = now_s - now_s.rem_euclid(WINDOW_S);
        self.model_spot(i, open, t_ns)
    }

    pub fn on_tick(&mut self, asset: usize, sec: i64, px: f64, t_ns: i64) {
        if let Some(s) = self.series.get_mut(asset) {
            s.push(sec, px, t_ns);
            self.counters.ticks += 1;
        }
    }

    /// A Polymarket market-channel frame. Returns the windows whose books it changed.
    pub fn on_poly(&mut self, text: &str, t_ns: i64) -> BTreeSet<(usize, i64)> {
        let mut touched = BTreeSet::new();
        let Ok(msgs) = pk_engine::parse::poly_msgs(text) else { return touched };
        self.counters.poly_frames += 1;
        self.poly_last_frame_ns = t_ns;
        for msg in msgs.iter().flatten() {
            match msg.kind() {
                Some("book") => {
                    let Some(id) = msg.asset_id.as_ref().map(|i| i.as_str()) else { continue };
                    let Some(&(i, open, _)) = self.owner.get(id) else { continue };
                    touched.insert((i, open));
                    let lv = |v: &Option<Vec<pk_engine::parse::Level>>| {
                        v.as_deref().unwrap_or_default().iter().map(|l| (px_key(l.price.0), l.size.0)).collect::<Vec<_>>()
                    };
                    let b = self.books.entry(id.to_owned()).or_default();
                    b.snapshot_keys(lv(&msg.bids).into_iter(), lv(&msg.asks).into_iter(), t_ns);
                }
                Some("price_change") => {
                    for ch in msg.price_changes.as_deref().unwrap_or_default() {
                        let Some(id) = ch.asset_id.as_ref().map(|i| i.as_str()) else { continue };
                        let Some(b) = self.books.get_mut(id) else { continue };
                        if b.snapshot_ns.is_none() {
                            continue;
                        }
                        if let Some(&(i, open, _)) = self.owner.get(id) {
                            touched.insert((i, open));
                        }
                        let side = if ch.side.as_deref() == Some("BUY") { BookSide::Bid } else { BookSide::Ask };
                        b.set_level(side, px_key(ch.price.0), ch.size.0, t_ns);
                    }
                }
                _ => {}
            }
        }
        touched
    }

    /// A feed dropped, went silent or reconnected: journaled so the windows it
    /// touched can be left out of scoring.
    pub fn on_feed_event(&mut self, venue: &str, event: &str, detail: &str, t_ns: i64) {
        self.counters.feed_events += 1;
        let detail: String = detail.chars().take(200).collect();
        self.record(t_ns, "eg_feed", json!({"venue": venue, "event": event, "detail": detail}));
    }

    /// The Polymarket socket reconnected: every book waits for a new snapshot.
    pub fn invalidate_books(&mut self) {
        for b in self.books.values_mut() {
            b.invalidate();
        }
        self.counters.book_resets += 1;
    }

    pub fn set_kill(&mut self, on: bool, t_ns: i64) {
        if on != self.killed {
            self.killed = on;
            self.record(t_ns, "eg_kill", json!({"on": on}));
        }
    }

    /// Entries stopped by today's loss halt. Never in record-only (paper)
    /// mode, even with a halt persisted from an enforcing run.
    pub fn halted(&self, t_ns: i64) -> bool {
        self.cfg.enforce_halt && self.p.halted_day.as_deref() == Some(day_of(t_ns / NS).as_str())
    }

    /// The ACK file: clear today's halt; it will not latch again today.
    pub fn ack(&mut self, t_ns: i64) {
        if self.halted(t_ns) {
            let day = day_of(t_ns / NS);
            self.p.halted_day = None;
            self.p.acked_day = Some(day.clone());
            self.dirty = true;
            self.record(t_ns, "eg_ack", json!({"day": day}));
        }
    }

    fn open_cost(&self) -> f64 {
        let held: f64 = self.p.positions.iter().filter(|p| p.outcome.is_none()).map(|p| p.usd + p.fee).sum();
        let waiting: f64 =
            self.pending.iter().chain(self.inflight.values()).filter(|o| o.shadow.is_none()).map(|o| o.limit * o.shares).sum();
        held + waiting
    }

    /// One clock step: closes, the closing-stretch grid, entries, paper fills.
    pub fn tick(&mut self, t_ns: i64) -> Vec<Action> {
        let mut actions = Vec::new();
        let now_s = t_ns / NS;
        let now_f = t_ns as f64 / NS as f64;
        let open_now = now_s - now_s.rem_euclid(WINDOW_S);
        for i in 0..self.cfg.assets.len() {
            // The window that just closed: its averages and our outcome.
            let prev = open_now - WINDOW_S;
            if now_s >= open_now + 3 && self.windows.contains_key(&(i, prev)) {
                self.close_window(i, prev, t_ns);
            }
            if !self.windows.contains_key(&(i, open_now)) {
                continue;
            }
            if now_s >= open_now + 3 {
                let ws = self.wstate.entry((i, open_now)).or_default();
                if ws.k.is_none() {
                    ws.k = self.series[i].boundary_avg(open_now);
                }
            }
            let left = (open_now + WINDOW_S) as f64 - now_f;
            if left <= self.cfg.grid_from_s as f64 && self.last_grid[i] != now_s {
                self.last_grid[i] = now_s;
                self.grid(i, open_now, left, t_ns);
            }
            if let Some(a) = self.evaluate_now(i, t_ns, "tick") {
                actions.push(a);
            }
        }
        let due: Vec<Order> = {
            let (due, keep): (Vec<Order>, Vec<Order>) = self.pending.drain(..).partition(|o| o.fill_at_ns <= t_ns);
            self.pending = keep;
            due
        };
        for o in due {
            if o.shadow.is_some() {
                self.shadow_fill(o, t_ns);
            } else {
                self.paper_fill(o, t_ns);
            }
        }
        actions
    }

    /// Evaluates asset `i`'s live window now, if it is inside the entry period.
    /// Called on the clock and whenever its book or its Chainlink price changes,
    /// so an entry does not wait for the next clock step. Paper orders fill on
    /// the clock (`tick`).
    pub fn evaluate_now(&mut self, i: usize, t_ns: i64, trigger: &'static str) -> Option<Action> {
        let now_s = t_ns / NS;
        let open = now_s - now_s.rem_euclid(WINDOW_S);
        if !self.windows.contains_key(&(i, open)) {
            return None;
        }
        let left = (open + WINDOW_S) as f64 - t_ns as f64 / NS as f64;
        if left > self.cfg.entry_from_s as f64 || left < self.cfg.entry_until_s as f64 {
            return None;
        }
        self.evaluate(i, open, left, t_ns, trigger)
    }

    /// The fair value now: (fair, K, σ, the sd multiplier in force).
    fn model(&self, i: usize, open: i64, t_ns: i64) -> Result<(crate::model::Fair, f64, f64, f64), &'static str> {
        self.model_with(i, open, t_ns, self.cfg.sd_mult_early)
    }

    /// [`Self::model`] with another early sd multiplier (a shadow rule's).
    fn model_with(
        &self,
        i: usize,
        open: i64,
        t_ns: i64,
        sd_mult_early: f64,
    ) -> Result<(crate::model::Fair, f64, f64, f64), &'static str> {
        let close = open + WINDOW_S;
        let ws = self.wstate.get(&(i, open)).ok_or("no_open_avg")?;
        let (k, kn) = ws.k.ok_or("no_open_avg")?;
        if kn < self.cfg.min_open_ticks {
            return Err("open_coverage");
        }
        let s = &self.series[i];
        if t_ns - s.last_arrival_ns > self.cfg.max_tick_age_ms * 1_000_000 {
            return Err("stale_chainlink");
        }
        let (last_sec, _) = s.last().ok_or("no_chainlink")?;
        let sigma = s.sigma(last_sec, self.cfg.sigma_window_s, self.cfg.sigma_min_n).ok_or("no_sigma")?;
        let left_s = close as f64 - t_ns as f64 / NS as f64;
        let mult = if left_s > self.cfg.sd_mult_from_s as f64 { sd_mult_early } else { 1.0 };
        let f = fair(s, k, close, sigma * mult, self.cfg.sd_floor).ok_or("no_fair")?;
        Ok((f, k, sigma, mult))
    }

    fn grid(&mut self, i: usize, open: i64, left: f64, t_ns: i64) {
        let Some(w) = self.windows.get(&(i, open)) else { return };
        let book = |b: Option<&Book>| match b {
            Some(b) if b.snapshot_ns.is_some() => json!({
                "bid": b.best_bid(), "ask": b.best_ask(),
                "ask_size": b.best_ask().and_then(|a| b.asks.get(&px_key(a)).copied()),
            }),
            _ => Value::Null,
        };
        let up = book(self.books.get(&w.up_token));
        let down = book(self.books.get(&w.down_token));
        let asset = w.asset.clone();
        let s_last = self.series[i].last();
        let m = self.model(i, open, t_ns);
        let model = match &m {
            Ok((f, k, sigma, mult)) => json!({"p_up": r6(f.p_up), "mean": f.mean, "k": k, "sd": r6(f.sd),
                                        "sigma": r6(*sigma), "mult": mult, "seen": f.seen, "to_come": f.to_come}),
            Err(why) => json!({"gate": why}),
        };
        let spot = self.spot_json(i, t_ns);
        let kalshi = match self.kalshi_quote(i, open, t_ns) {
            Some(q) => json!({"bid": r4(q.bid), "ask": r4(q.ask), "mid": r4(q.mid), "age_ms": q.age_ms}),
            None => Value::Null,
        };
        self.record(
            t_ns,
            "eg_sec",
            json!({"asset": asset, "open": open, "left": r4(left), "s": s_last.map(|x| x.1),
                   "s_sec": s_last.map(|x| x.0), "model": model, "up": up, "down": down, "kalshi": kalshi,
                   "spot": spot, "epoch": self.cfg.epoch}),
        );
    }

    fn evaluate(&mut self, i: usize, open: i64, left: f64, t_ns: i64, trigger: &'static str) -> Option<Action> {
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
        let (f, k, sigma, mult) = match self.model(i, open, t_ns) {
            Ok(x) => x,
            Err(why) => return gate(self, why),
        };
        // A silent socket leaves the books frozen at their last state (its own
        // silence check takes 30 s to reconnect): no decisions on them.
        let poly_silence_ms = (t_ns - self.poly_last_frame_ns) / 1_000_000;
        if poly_silence_ms > self.cfg.max_poly_silence_ms {
            return gate(self, "poly_silent");
        }
        self.evaluate_shadows(i, open, left, t_ns);
        let w = self.windows.get(&(i, open))?.clone();
        let ws = self.wstate.entry((i, open)).or_default();
        if ws.entries >= self.cfg.max_entries_per_window {
            ws.gate = Some("entries_done");
            return None;
        }
        let sides_done = ws.sides;
        // Each side with an ask in the price range; a side needs only an ask to buy.
        let mut sides: Vec<(bool, f64, f64, f64, f64)> = Vec::new(); // (up, p, ask, fee, edge)
        let mut any_book = false;
        let mut stale = false;
        for up in [true, false] {
            let Some(b) = self.books.get(w.token(up)) else { continue };
            if b.snapshot_ns.is_none() {
                continue;
            }
            any_book = true;
            if b.last_update_ns.is_none_or(|u| t_ns - u > self.cfg.max_book_age_ms * 1_000_000) {
                stale = true;
                continue;
            }
            let Some(ask) = b.best_ask() else { continue };
            if sides_done[usize::from(!up)] || !(self.cfg.min_px..=self.cfg.max_px).contains(&ask) {
                continue;
            }
            let p = if up { f.p_up } else { 1.0 - f.p_up };
            let fee = taker_fee(ask, 1.0, w.fee_rate, w.fee_exponent);
            sides.push((up, p, ask, fee, p - ask - fee));
        }
        if !any_book {
            return gate(self, "no_book");
        }
        if sides.is_empty() {
            return gate(self, if stale { "stale_book" } else { "price_range" });
        }
        {
            let top = sides.iter().map(|x| x.4).fold(f64::NEG_INFINITY, f64::max);
            let ws = self.wstate.entry((i, open)).or_default();
            ws.best_edge = Some(ws.best_edge.map_or(top, |e: f64| e.max(top)));
        }
        // The edge band: under min_edge is no trade; over max_edge is taken for model error.
        let in_band = sides
            .iter()
            .filter(|x| x.4 >= self.cfg.min_edge - 1e-12 && x.4 <= self.cfg.max_edge + 1e-12)
            .copied()
            .max_by(|a, b| a.4.total_cmp(&b.4));
        let Some((up, p, ask, fee, edge)) = in_band else {
            let over = sides.iter().any(|x| x.4 > self.cfg.max_edge);
            return gate(self, if over { "edge_too_big" } else { "edge" });
        };
        // Kalshi must agree: its mid for the same side clears the ask and the fee.
        let kq = self.kalshi_quote(i, open, t_ns);
        let k_side = kq.map(|q| if up { q.mid } else { 1.0 - q.mid });
        let k_margin = k_side.map(|m| m - ask - fee);
        if self.cfg.kalshi_filter {
            match kq {
                Some(q) if q.ask - q.bid <= self.cfg.kalshi_max_spread + 1e-9 => {}
                _ => return gate(self, "no_kalshi"),
            }
            if k_margin.is_none_or(|m| m < self.cfg.kalshi_min_margin) {
                return gate(self, "kalshi_disagrees");
            }
        }
        let tick = tick_for(ask);
        let cap = p - self.cfg.min_net_edge - fee;
        let limit = floor_to((ask + self.cfg.limit_offset).min(cap), tick).max(ask);
        let shares = self.cfg.shares;
        if self.open_cost() + limit * shares > self.cfg.max_open_cost_usd + 1e-9 {
            return gate(self, "max_open_cost");
        }
        let book = self.books.get(w.token(up))?;
        let instant = walk(book, limit, shares, w.fee_rate, w.fee_exponent);
        let ask_size = book.asks.get(&px_key(ask)).copied();
        let bid = book.best_bid();
        let other_ask = self.books.get(w.token(!up)).and_then(Book::best_ask);
        let spot_p = self.model_spot(i, open, t_ns);
        let (last_sec, s) = self.series[i].last()?;
        let tick_age_ms = (t_ns - self.series[i].last_arrival_ns) / 1_000_000;
        let book_age_ms = book.last_update_ns.map(|u| (t_ns - u) / 1_000_000);
        self.p.next_id += 1;
        let id = self.p.next_id;
        {
            let ws = self.wstate.entry((i, open)).or_default();
            ws.entries += 1;
            ws.sides[usize::from(!up)] = true;
            ws.gate = Some("fired");
        }
        self.counters.decisions += 1;
        self.dirty = true;
        let live = self.cfg.mode == Mode::Live;
        self.record(
            t_ns,
            "eg_decision",
            json!({
                "id": id, "asset": w.asset, "open": open, "side": if up { "up" } else { "down" },
                "left": r4(left), "p_up": r6(f.p_up), "p_side": r6(p), "mean": f.mean, "k": k,
                "sd": r6(f.sd), "sigma": r6(sigma), "mult": mult, "seen": f.seen, "to_come": f.to_come,
                "kalshi_mid": kq.map(|q| r4(q.mid)), "kalshi_side": k_side.map(r4), "kalshi_margin": k_margin.map(r6),
                "kalshi_spread": kq.map(|q| r4(q.ask - q.bid)), "kalshi_age_ms": kq.map(|q| q.age_ms),
                "epoch": self.cfg.epoch,
                "spot_p_up": spot_p.map(|x| r6(x.0)), "spot_src": spot_p.map(|x| x.1), "spot_move_bp": spot_p.map(|x| r4(x.2)),
                "s": s, "s_sec": last_sec, "tick_age_ms": tick_age_ms,
                "book_age_ms": book_age_ms, "poly_silence_ms": poly_silence_ms, "trigger": trigger,
                "ask": ask, "ask_size": ask_size, "bid": bid, "other_ask": other_ask,
                "fee": r6(fee), "edge": r6(edge), "limit": r4(limit), "shares": shares,
                "instant_shares": r4(instant.0), "instant_usd": r4(instant.1), "instant_fee": r6(instant.2),
                "mode": if live { "live" } else { "paper" },
            }),
        );
        let order = Order {
            id,
            asset: i,
            open,
            up,
            token: w.token(up).to_owned(),
            limit,
            shares,
            decision_ns: t_ns,
            fill_at_ns: t_ns + self.cfg.paper_latency_ms * 1_000_000,
            instant,
            shadow: None,
        };
        if live {
            self.inflight.insert(id, order);
            let token = w.token(up).to_owned();
            Some(Action::Enter { id, asset: w.asset, token, limit, shares })
        } else {
            self.pending.push(order);
            None
        }
    }

    /// Every shadow rule not yet fired in this window: the traded rule's
    /// selection with the rule's one change, then a paper order.
    fn evaluate_shadows(&mut self, i: usize, open: i64, left: f64, t_ns: i64) {
        let Some(w) = self.windows.get(&(i, open)).cloned() else { return };
        let fired = self.wstate.get(&(i, open)).map_or(0, |s| s.shadow_fired);
        for (n, r) in SHADOWS.iter().enumerate() {
            if fired & (1 << n) != 0 {
                continue;
            }
            let mult = r.sd_mult_early.unwrap_or(self.cfg.sd_mult_early);
            let Ok((f, _, _, used_mult)) = self.model_with(i, open, t_ns, mult) else { continue };
            let min_px = r.min_px.unwrap_or(self.cfg.min_px);
            let min_edge = r.min_edge.unwrap_or(self.cfg.min_edge);
            let mut sides: Vec<(bool, f64, f64, f64, f64, Option<f64>)> = Vec::new(); // (up, p, ask, fee, edge, bid)
            for up in [true, false] {
                let Some(b) = self.books.get(w.token(up)) else { continue };
                if b.snapshot_ns.is_none() || b.last_update_ns.is_none_or(|u| t_ns - u > self.cfg.max_book_age_ms * 1_000_000) {
                    continue;
                }
                let Some(ask) = b.best_ask() else { continue };
                if !(min_px..=self.cfg.max_px).contains(&ask) {
                    continue;
                }
                let bid = b.best_bid();
                if let Some(cap) = r.max_spread
                    && bid.is_none_or(|bid| ask - bid > cap + 1e-9)
                {
                    continue;
                }
                let p = if up { f.p_up } else { 1.0 - f.p_up };
                let fee = taker_fee(ask, 1.0, w.fee_rate, w.fee_exponent);
                sides.push((up, p, ask, fee, p - ask - fee, bid));
            }
            let Some((up, p, ask, fee, edge, bid)) = sides
                .into_iter()
                .filter(|x| x.4 >= min_edge - 1e-12 && x.4 <= self.cfg.max_edge + 1e-12)
                .max_by(|a, b| a.4.total_cmp(&b.4))
            else {
                continue;
            };
            let kq = self.kalshi_quote(i, open, t_ns);
            let k_side = kq.map(|q| if up { q.mid } else { 1.0 - q.mid });
            if self.cfg.kalshi_filter {
                let fresh = kq.is_some_and(|q| q.ask - q.bid <= self.cfg.kalshi_max_spread + 1e-9);
                if !fresh || k_side.is_none_or(|m| m - ask - fee < self.cfg.kalshi_min_margin) {
                    continue;
                }
            }
            let tick = tick_for(ask);
            let cap = p - self.cfg.min_net_edge - fee;
            let limit = floor_to((ask + self.cfg.limit_offset).min(cap), tick).max(ask);
            let shares = self.cfg.shares;
            let instant = self.books.get(w.token(up)).map_or((0.0, 0.0, 0.0), |b| walk(b, limit, shares, w.fee_rate, w.fee_exponent));
            self.wstate.entry((i, open)).or_default().shadow_fired |= 1 << n;
            self.p.next_shadow_id += 1;
            let sid = self.p.next_shadow_id;
            self.dirty = true;
            self.record(
                t_ns,
                "eg_shadow_decision",
                json!({"rule": r.name, "sid": sid, "asset": w.asset, "open": open, "side": if up { "up" } else { "down" },
                       "left": r4(left), "p_side": r6(p), "mult": used_mult, "kalshi_side": k_side.map(r4),
                       "ask": ask, "bid": bid, "fee": r6(fee), "edge": r6(edge), "limit": r4(limit), "shares": shares,
                       "instant_shares": r4(instant.0), "epoch": self.cfg.epoch}),
            );
            self.pending.push(Order {
                id: sid,
                asset: i,
                open,
                up,
                token: w.token(up).to_owned(),
                limit,
                shares,
                decision_ns: t_ns,
                fill_at_ns: t_ns + self.cfg.paper_latency_ms * 1_000_000,
                instant,
                shadow: Some((r.name, edge, ask)),
            });
        }
    }

    fn shadow_fill(&mut self, o: Order, t_ns: i64) {
        let Some((rule, edge, ask)) = o.shadow else { return };
        let Some(w) = self.windows.get(&(o.asset, o.open)).cloned() else { return };
        let book = self.books.get(&o.token);
        let ask_now = book.and_then(Book::best_ask);
        let (shares, usd, fee) =
            book.map(|b| walk(b, o.limit, o.shares, w.fee_rate, w.fee_exponent)).unwrap_or((0.0, 0.0, 0.0));
        let body = json!({"rule": rule, "sid": o.id, "asset": w.asset, "open": o.open, "side": if o.up { "up" } else { "down" },
                          "limit": r4(o.limit), "ask_now": ask_now, "shares": r4(shares), "usd": r4(usd), "fee": r6(fee),
                          "avg": if shares > 0.0 { r4(usd / shares) } else { Value::Null }, "epoch": self.cfg.epoch});
        if shares > 1e-9 {
            self.record(t_ns, "eg_shadow_fill", body);
            self.p.shadows.push(ShadowPos {
                rule: rule.to_owned(),
                sid: o.id,
                asset: w.asset.clone(),
                open: o.open,
                up: o.up,
                shares,
                usd,
                fee,
                edge,
                ask,
            });
            self.dirty = true;
        } else {
            self.record(t_ns, "eg_shadow_no_fill", body);
        }
    }

    fn book_position(&mut self, o: &Order, shares: f64, usd: f64, fee: f64, live: bool, t_ns: i64) {
        let Some(w) = self.windows.get(&(o.asset, o.open)) else { return };
        let pos = Position {
            id: o.id,
            asset: w.asset.clone(),
            open: o.open,
            up: o.up,
            token: o.token.clone(),
            condition_id: w.condition_id.clone(),
            shares,
            usd,
            fee,
            entry_ns: t_ns,
            live,
            outcome: None,
            pnl: None,
            redeemed: false,
            redeem_tries: 0,
            epoch: self.cfg.epoch.clone(),
        };
        self.p.positions.push(pos);
        self.dirty = true;
    }

    fn paper_fill(&mut self, o: Order, t_ns: i64) {
        let Some(w) = self.windows.get(&(o.asset, o.open)).cloned() else { return };
        let book = self.books.get(&o.token);
        let ask_now = book.and_then(Book::best_ask);
        let (shares, usd, fee) =
            book.map(|b| walk(b, o.limit, o.shares, w.fee_rate, w.fee_exponent)).unwrap_or((0.0, 0.0, 0.0));
        let held_ms = (t_ns - o.decision_ns) / 1_000_000;
        let body = json!({
            "id": o.id, "asset": w.asset, "open": o.open, "side": if o.up { "up" } else { "down" },
            "mode": "paper", "limit": r4(o.limit), "ask_now": ask_now, "held_ms": held_ms,
            "shares": r4(shares), "usd": r4(usd), "fee": r6(fee),
            "avg": if shares > 0.0 { r4(usd / shares) } else { Value::Null },
            "instant_shares": r4(o.instant.0), "instant_usd": r4(o.instant.1),
        });
        if shares > 1e-9 {
            self.counters.fills += 1;
            self.record(t_ns, "eg_fill", body);
            self.book_position(&o, shares, usd, fee, false, t_ns);
        } else {
            self.counters.no_fills += 1;
            self.record(t_ns, "eg_no_fill", body);
        }
    }

    /// A live order's result.
    pub fn on_live(&mut self, id: u64, r: LiveResult, t_ns: i64) {
        let Some(o) = self.inflight.remove(&id) else { return };
        let Some(w) = self.windows.get(&(o.asset, o.open)).cloned() else { return };
        let fee = if r.shares > 0.0 { taker_fee(r.usd / r.shares, r.shares, w.fee_rate, w.fee_exponent) } else { 0.0 };
        let body = json!({
            "id": id, "asset": w.asset, "open": o.open, "side": if o.up { "up" } else { "down" },
            "mode": "live", "limit": r4(o.limit), "shares": r4(r.shares), "usd": r4(r.usd), "fee": r6(fee),
            "avg": if r.shares > 0.0 { r4(r.usd / r.shares) } else { Value::Null },
            "order_id": r.order_id, "error": r.error, "ack_us": r.ack_us,
            "held_ms": (t_ns - o.decision_ns) / 1_000_000,
            "instant_shares": r4(o.instant.0), "instant_usd": r4(o.instant.1),
        });
        if r.shares > 1e-9 {
            self.counters.fills += 1;
            self.record(t_ns, "eg_fill", body);
            self.book_position(&o, r.shares, r.usd, fee, true, t_ns);
        } else {
            self.counters.no_fills += 1;
            self.record(t_ns, "eg_no_fill", body);
        }
    }

    fn close_window(&mut self, i: usize, open: i64, t_ns: i64) {
        let close = open + WINDOW_S;
        let Some(asset) = self.windows.get(&(i, open)).map(|w| w.asset.clone()) else { return };
        let ws = self.wstate.entry((i, open)).or_default();
        if ws.closed {
            return;
        }
        ws.closed = true;
        let k = ws.k.or_else(|| self.series[i].boundary_avg(open));
        let c = self.series[i].boundary_avg(close);
        let ours = match (k, c) {
            (Some((k, kn)), Some((c, cn))) if kn >= self.cfg.min_open_ticks && cn >= self.cfg.min_open_ticks => {
                Some(if c >= k { "yes" } else { "no" })
            }
            _ => None,
        };
        let ws = self.wstate.entry((i, open)).or_default();
        ws.ours = ours;
        let (gate, best_edge, entries) = (ws.gate, ws.best_edge, ws.entries);
        self.record(
            t_ns,
            "eg_close",
            json!({"asset": asset, "open": open, "k": k.map(|x| x.0), "k_n": k.map(|x| x.1),
                   "twap": c.map(|x| x.0), "twap_n": c.map(|x| x.1), "ours": ours,
                   "last_gate": gate, "best_edge": best_edge.map(r6), "entries": entries, "epoch": self.cfg.epoch}),
        );
    }

    /// Windows to ask Gamma about: closed at least 20 s ago, not resolved, recent.
    pub fn to_resolve(&self, now_s: i64) -> Vec<WindowInfo> {
        self.windows
            .iter()
            .filter(|(key, w)| {
                let done = self.wstate.get(key).is_some_and(|s| s.resolved);
                !done && now_s >= w.close() + 20 && now_s < w.close() + RESOLVE_FOR_S
            })
            .map(|(_, w)| w.clone())
            .collect()
    }

    /// Polymarket resolved a window (`"yes"` = Up).
    pub fn on_resolved(&mut self, asset: &str, open: i64, outcome: &str, t_ns: i64) -> Vec<Action> {
        let Some(i) = self.asset_index(asset) else { return Vec::new() };
        let ws = self.wstate.entry((i, open)).or_default();
        if ws.resolved {
            return Vec::new();
        }
        ws.resolved = true;
        let ours = ws.ours;
        match ours {
            Some(o) if o == outcome => self.counters.agree += 1,
            Some(_) => self.counters.disagree += 1,
            None => {}
        }
        self.record(t_ns, "eg_resolution", json!({"asset": asset, "open": open, "poly": outcome, "ours": ours,
                                                   "agree": ours.map(|o| o == outcome)}));
        let day = day_of(open + WINDOW_S);
        let mut settled = Vec::new();
        for p in self.p.positions.iter_mut().filter(|p| p.asset == asset && p.open == open && p.outcome.is_none()) {
            p.outcome = Some(outcome.to_owned());
            let payout = if p.won() == Some(true) { p.shares } else { 0.0 };
            let pnl = payout - p.usd - p.fee;
            p.pnl = Some(pnl);
            settled.push(p.clone());
        }
        let (done, keep): (Vec<ShadowPos>, Vec<ShadowPos>) =
            std::mem::take(&mut self.p.shadows).into_iter().partition(|p| p.asset == asset && p.open == open);
        self.p.shadows = keep;
        for p in done {
            let won = (outcome == "yes") == p.up;
            let pnl = if won { p.shares } else { 0.0 } - p.usd - p.fee;
            self.dirty = true;
            self.record(
                t_ns,
                "eg_shadow_settle",
                json!({"rule": p.rule, "sid": p.sid, "asset": p.asset, "open": p.open, "side": if p.up { "up" } else { "down" },
                       "outcome": outcome, "won": won, "shares": r4(p.shares), "usd": r4(p.usd), "fee": r6(p.fee),
                       "pnl": r4(pnl), "per_share": r4(pnl / p.shares.max(1e-9)), "edge": r6(p.edge), "ask": p.ask,
                       "epoch": self.cfg.epoch}),
            );
        }
        let mut actions = Vec::new();
        for p in settled {
            *self.p.realized.entry(day.clone()).or_default() += p.pnl.unwrap_or(0.0);
            self.counters.settled += 1;
            self.dirty = true;
            let pnl = p.pnl.unwrap_or(0.0);
            self.record(
                t_ns,
                "eg_settle",
                json!({"id": p.id, "asset": p.asset, "open": p.open, "side": if p.up { "up" } else { "down" },
                       "outcome": outcome, "won": p.won(), "shares": r4(p.shares), "usd": r4(p.usd),
                       "fee": r6(p.fee), "pnl": r4(pnl), "per_share": r4(pnl / p.shares.max(1e-9)),
                       "live": p.live, "epoch": p.epoch, "day_pnl": r4(self.p.realized.get(&day).copied().unwrap_or(0.0))}),
            );
            if p.live && p.won() == Some(true) {
                actions.push(Action::Redeem { condition_id: p.condition_id.clone() });
            }
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
                self.record(t_ns, "eg_halt", json!({"day": today, "pnl": r4(pnl_today), "limit": self.cfg.daily_loss_usd}));
            } else if self.breach_noted.as_deref() != Some(&today) {
                // Paper: a halt costs the sample, not money. Journal the breach, keep going.
                self.breach_noted = Some(today.clone());
                self.record(
                    t_ns,
                    "eg_halt",
                    json!({"day": today, "pnl": r4(pnl_today), "limit": self.cfg.daily_loss_usd, "record_only": true}),
                );
            }
        }
        actions.sort_by(|a, b| format!("{a:?}").cmp(&format!("{b:?}")));
        actions.dedup();
        actions
    }

    /// Live winners still to redeem (retried until done, at most 20 times).
    pub fn redeem_due(&self) -> Vec<String> {
        let mut c: Vec<String> = self
            .p
            .positions
            .iter()
            .filter(|p| p.live && p.won() == Some(true) && !p.redeemed && p.redeem_tries < 20)
            .map(|p| p.condition_id.clone())
            .collect();
        c.sort();
        c.dedup();
        c
    }

    pub fn on_redeemed(&mut self, condition_id: &str, result: Result<String, String>, t_ns: i64) {
        let ok = result.is_ok();
        for p in self.p.positions.iter_mut().filter(|p| p.condition_id == condition_id && p.live) {
            p.redeem_tries += 1;
            if ok {
                p.redeemed = true;
            }
        }
        self.dirty = true;
        let body = match result {
            Ok(tx) => json!({"condition_id": condition_id, "tx": tx}),
            Err(e) => json!({"condition_id": condition_id, "error": e}),
        };
        self.record(t_ns, "eg_redeem", body);
    }

    /// Forget old windows, their books and settled positions.
    pub fn prune(&mut self, now_s: i64) {
        let old: Vec<(usize, i64)> = self
            .windows
            .iter()
            .filter(|(_, w)| w.close() + RESOLVE_FOR_S + 600 < now_s)
            .map(|(k, _)| *k)
            .collect();
        for key in old {
            if let Some(w) = self.windows.remove(&key) {
                for t in [&w.up_token, &w.down_token] {
                    self.owner.remove(t);
                    self.books.remove(t);
                }
                if let Some(t) = &w.kalshi_ticker {
                    self.kowner.remove(t);
                    self.kbooks.remove(t);
                }
            }
            self.wstate.remove(&key);
        }
        let before = self.p.positions.len();
        self.p.positions.retain(|p| p.outcome.is_none() || (p.live && p.won() == Some(true) && !p.redeemed && p.redeem_tries < 20));
        if self.p.positions.len() != before {
            self.dirty = true;
        }
        let n = self.p.shadows.len();
        self.p.shadows.retain(|p| p.open + WINDOW_S + RESOLVE_FOR_S + 600 >= now_s);
        if self.p.shadows.len() != n {
            self.dirty = true;
        }
        let keep_from = day_of(now_s - 40 * 86_400);
        self.p.realized.retain(|d, _| d.as_str() >= keep_from.as_str());
    }

    pub fn status(&mut self, t_ns: i64) {
        let body = self.status_value(t_ns);
        self.record(t_ns, "eg_status", body);
    }

    /// The engine's state now (the page's `live` block and the `eg_status` record).
    pub fn status_value(&self, t_ns: i64) -> Value {
        let today = day_of(t_ns / NS);
        let ages: BTreeMap<&str, Value> = self
            .cfg
            .assets
            .iter()
            .zip(&self.series)
            .map(|(a, s)| (a.as_str(), if s.last_arrival_ns > 0 { ((t_ns - s.last_arrival_ns) / 1_000_000).into() } else { Value::Null }))
            .collect();
        json!({
            "mode": if self.cfg.mode == Mode::Live { "live" } else { "paper" },
            "counters": serde_json::to_value(&self.counters).unwrap_or(Value::Null),
            "day_pnl": r4(self.p.realized.get(&today).copied().unwrap_or(0.0)),
            "open_positions": self.p.positions.iter().filter(|p| p.outcome.is_none()).count(),
            "open_cost": r4(self.open_cost()),
            "halted": self.halted(t_ns), "killed": self.killed,
            "windows": self.windows.len(),
            "books": self.books.values().filter(|b| b.snapshot_ns.is_some()).count(),
            "poly_silence_ms": if self.poly_last_frame_ns > 0 { Value::from((t_ns - self.poly_last_frame_ns) / 1_000_000) } else { Value::Null },
            "enforce_halt": self.cfg.enforce_halt,
            "chainlink_age_ms": ages,
            "epoch": self.cfg.epoch,
            "kalshi_filter": self.cfg.kalshi_filter,
            "kalshi_age_ms": if self.k_last_frame_ns > 0 { Value::from((t_ns - self.k_last_frame_ns) / 1_000_000) } else { Value::Null },
            "kalshi_books": self.kbooks.values().filter(|b| b.snapshot).count(),
            "spot_age_ms": SPOT_VENUES.iter().enumerate().map(|(j, v)| {
                let newest = self.spot.iter().map(|x| x[j].last_ns).max().unwrap_or(0);
                ((*v).to_owned(), if newest > 0 { Value::from((t_ns - newest) / 1_000_000) } else { Value::Null })
            }).collect::<serde_json::Map<String, Value>>(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    const OPEN: i64 = 1_800_000_000 - 1_800_000_000 % WINDOW_S;

    /// Epoch-1 limits (wide edge band, asks to 0.97, no Kalshi), so these
    /// tests check the mechanics; `cfg_with` sets anything else.
    fn cfg_with(extra: &[(&str, &str)]) -> Config {
        let mut v = HashMap::new();
        for (k, val) in [("PK_EG_ASSETS", "BTC"), ("PK_EG_SIGMA_MIN_N", "30"), ("PK_EG_KALSHI_FILTER", "no"),
                         ("PK_EG_MAX_PX", "0.97"), ("PK_EG_MAX_EDGE", "0.9"), ("PK_EG_SD_MULT_EARLY", "1.0")] {
            v.insert(k.to_string(), val.to_string());
        }
        for (k, val) in extra {
            v.insert(k.to_string(), val.to_string());
        }
        Config::from_vars(v).unwrap()
    }

    fn cfg() -> Config {
        cfg_with(&[])
    }

    fn window() -> WindowInfo {
        WindowInfo {
            asset: "BTC".into(),
            open: OPEN,
            up_token: "UP".into(),
            down_token: "DN".into(),
            condition_id: "0xC".into(),
            slug: "btc-updown-15m".into(),
            fee_rate: 0.05,
            fee_exponent: 1.0,
            kalshi_ticker: Some("KX".into()),
        }
    }

    fn kalshi_frame(yes_bid: f64, yes_ask: f64) -> String {
        json!({"type": "orderbook_snapshot", "sid": 1, "seq": 1,
               "msg": {"market_ticker": "KX", "yes_dollars_fp": [[format!("{yes_bid:.4}"), "100.00"]],
                       "no_dollars_fp": [[format!("{:.4}", 1.0 - yes_ask), "100.00"]]}})
        .to_string()
    }

    /// A window 20 s before the close with 40 s of a 0.3% lead printed, and these books.
    fn setup(e: &mut Engine, up: (f64, f64), down: (f64, f64)) -> i64 {
        let close = OPEN + WINDOW_S;
        let now = close - 20;
        feed(e, now, close - 60);
        e.on_poly(&book_frame("UP", up.0, up.1, 100.0), now * NS);
        e.on_poly(&book_frame("DN", down.0, down.1, 100.0), now * NS);
        e.tick((OPEN + 5) * NS);
        now
    }

    fn fired(e: &mut Engine) -> Option<Value> {
        e.take_records().into_iter().find(|r| r["type"] == "eg_decision")
    }

    fn last_gate(e: &mut Engine, now: i64) -> Value {
        e.tick((OPEN + WINDOW_S + 5) * NS);
        let _ = now;
        e.take_records().into_iter().find(|r| r["type"] == "eg_close").map(|r| r["last_gate"].clone()).unwrap()
    }

    fn book_frame(token: &str, bid: f64, ask: f64, size: f64) -> String {
        json!({"event_type": "book", "asset_id": token,
               "bids": [{"price": format!("{bid}"), "size": format!("{size}")}],
               "asks": [{"price": format!("{ask}"), "size": format!("{size}")}]})
        .to_string()
    }

    /// Ticks from 400 s before the open to `upto`, alternating ±1 bp around
    /// 100 before `lead_from` and at 100.3 after it.
    fn feed(e: &mut Engine, upto: i64, lead_from: i64) {
        for t in OPEN - 400..=upto {
            let px = if t >= lead_from { 100.3 } else if t % 2 == 0 { 100.01 } else { 99.99 };
            e.on_tick(0, t, px, t * NS);
        }
    }

    fn engine() -> Engine {
        let mut e = Engine::new(cfg(), Persisted::default());
        e.set_window(window(), OPEN * NS);
        e
    }

    #[test]
    fn fires_on_a_printed_lead_and_fills_on_paper() {
        let mut e = engine();
        let close = OPEN + WINDOW_S;
        let now = close - 20;
        feed(&mut e, now, close - 60);
        e.on_poly(&book_frame("UP", 0.80, 0.82, 100.0), now * NS);
        e.on_poly(&book_frame("DN", 0.17, 0.19, 100.0), now * NS);
        e.tick((OPEN + 5) * NS); // the opening average
        assert!(e.tick(now * NS).is_empty());
        let recs = e.take_records();
        let d = recs.iter().find(|r| r["type"] == "eg_decision").expect("a decision");
        assert_eq!(d["side"], "up");
        assert!(d["p_up"].as_f64().unwrap() > 0.99);
        assert_eq!(d["limit"].as_f64().unwrap(), 0.83);
        e.tick(now * NS + 300_000_000);
        let recs = e.take_records();
        let f = recs.iter().find(|r| r["type"] == "eg_fill").expect("a paper fill");
        assert_eq!(f["shares"].as_f64().unwrap(), 10.0);
        assert_eq!(e.p.positions.len(), 1);
        // One entry per window.
        e.tick(now * NS + 400_000_000);
        assert!(!e.take_records().iter().any(|r| r["type"] == "eg_decision"));
    }

    #[test]
    fn settles_on_polymarket_and_measures_agreement() {
        let mut e = engine();
        let close = OPEN + WINDOW_S;
        let now = close - 20;
        feed(&mut e, now, close - 60);
        e.on_poly(&book_frame("UP", 0.80, 0.82, 100.0), now * NS);
        e.on_poly(&book_frame("DN", 0.17, 0.19, 100.0), now * NS);
        e.tick((OPEN + 5) * NS);
        e.tick(now * NS);
        e.tick(now * NS + 300_000_000);
        for t in now + 1..close + 5 {
            e.on_tick(0, t, 100.3, t * NS);
        }
        e.tick((close + 5) * NS);
        let recs = e.take_records();
        let c = recs.iter().find(|r| r["type"] == "eg_close").expect("close");
        assert_eq!(c["ours"], "yes");
        assert_eq!(e.to_resolve(close + 30).len(), 1);
        let acts = e.on_resolved("BTC", OPEN, "yes", (close + 30) * NS);
        assert!(acts.is_empty()); // paper never redeems
        let recs = e.take_records();
        let s = recs.iter().find(|r| r["type"] == "eg_settle").unwrap();
        let pnl = s["pnl"].as_f64().unwrap();
        // 10 × (1 − 0.82) − fee(0.82) × 10
        let want = 10.0 * 0.18 - 10.0 * 0.05 * 0.82 * 0.18;
        assert!((pnl - want).abs() < 1e-3, "{pnl} vs {want}");
        assert_eq!(recs.iter().find(|r| r["type"] == "eg_resolution").unwrap()["agree"], true);
        assert!(e.to_resolve(close + 60).is_empty());
    }

    #[test]
    fn no_edge_no_entry() {
        let mut e = engine();
        let close = OPEN + WINDOW_S;
        let now = close - 20;
        feed(&mut e, now, close - 60);
        // The book already prices the lead.
        e.on_poly(&book_frame("UP", 0.98, 0.99, 100.0), now * NS);
        e.on_poly(&book_frame("DN", 0.01, 0.02, 100.0), now * NS);
        e.tick((OPEN + 5) * NS);
        e.tick(now * NS);
        let recs = e.take_records();
        assert!(!recs.iter().any(|r| r["type"] == "eg_decision"));
    }

    #[test]
    fn stale_chainlink_blocks() {
        let mut e = engine();
        let close = OPEN + WINDOW_S;
        let now = close - 20;
        feed(&mut e, now - 10, close - 60); // newest tick 10 s old
        e.on_poly(&book_frame("UP", 0.80, 0.82, 100.0), now * NS);
        e.on_poly(&book_frame("DN", 0.17, 0.19, 100.0), now * NS);
        e.tick((OPEN + 5) * NS);
        e.tick(now * NS);
        assert!(!e.take_records().iter().any(|r| r["type"] == "eg_decision"));
    }

    #[test]
    fn paper_fill_uses_the_book_after_the_delay() {
        let mut e = engine();
        let close = OPEN + WINDOW_S;
        let now = close - 20;
        feed(&mut e, now, close - 60);
        e.on_poly(&book_frame("UP", 0.80, 0.82, 100.0), now * NS);
        e.on_poly(&book_frame("DN", 0.17, 0.19, 100.0), now * NS);
        e.tick((OPEN + 5) * NS);
        e.tick(now * NS);
        // A maker reprices during the delay: the ask moves past the limit.
        e.on_poly(&book_frame("UP", 0.90, 0.95, 100.0), now * NS + 100_000_000);
        e.tick(now * NS + 300_000_000);
        let recs = e.take_records();
        assert!(recs.iter().any(|r| r["type"] == "eg_no_fill"));
        assert!(e.p.positions.is_empty());
    }

    #[test]
    fn halt_latches_and_ack_clears() {
        let mut e = Engine::new(cfg_with(&[("PK_EG_ENFORCE_HALT", "yes")]), Persisted::default());
        e.set_window(window(), OPEN * NS);
        let close = OPEN + WINDOW_S;
        e.p.positions.push(Position {
            id: 1, asset: "BTC".into(), open: OPEN, up: true, token: "UP".into(), condition_id: "0xC".into(),
            shares: 30.0, usd: 24.0, fee: 0.2, entry_ns: 0, live: false, outcome: None, pnl: None,
            redeemed: false, redeem_tries: 0, epoch: "2".into(),
        });
        let t = (close + 30) * NS;
        e.on_resolved("BTC", OPEN, "no", t);
        assert!(e.halted(t));
        e.ack(t);
        assert!(!e.halted(t));
        assert!(e.take_records().iter().any(|r| r["type"] == "eg_halt"));
    }

    #[test]
    fn live_mode_asks_the_runtime() {
        let mut v = HashMap::new();
        for (k, val) in [("PK_EG_ASSETS", "BTC"), ("PK_EG_SIGMA_MIN_N", "30"), ("PK_EG_MODE", "live"),
                         ("PK_EG_LIVE_CONFIRM", "yes"), ("POLY_PRIVATE_KEY", "0x1"), ("POLY_FUNDER", "0x2"),
                         ("POLY_RELAYER_API_KEY", "k"), ("PK_EG_KALSHI_FILTER", "no"), ("PK_EG_MAX_PX", "0.97"),
                         ("PK_EG_MAX_EDGE", "0.9")] {
            v.insert(k.to_string(), val.to_string());
        }
        let mut e = Engine::new(Config::from_vars(v).unwrap(), Persisted::default());
        e.set_window(window(), OPEN * NS);
        let close = OPEN + WINDOW_S;
        let now = close - 20;
        feed(&mut e, now, close - 60);
        e.on_poly(&book_frame("UP", 0.80, 0.82, 100.0), now * NS);
        e.on_poly(&book_frame("DN", 0.17, 0.19, 100.0), now * NS);
        e.tick((OPEN + 5) * NS);
        let acts = e.tick(now * NS);
        let Some(Action::Enter { id, limit, .. }) = acts.first().cloned() else { panic!("{acts:?}") };
        assert_eq!(limit, 0.83);
        e.on_live(id, LiveResult { shares: 10.0, usd: 8.2, ..Default::default() }, now * NS + 200_000_000);
        assert_eq!(e.p.positions.len(), 1);
        assert!(e.p.positions[0].live);
        for t in now + 1..close + 5 {
            e.on_tick(0, t, 100.3, t * NS);
        }
        let acts = e.on_resolved("BTC", OPEN, "yes", (close + 30) * NS);
        assert_eq!(acts, vec![Action::Redeem { condition_id: "0xC".into() }]);
        assert_eq!(e.redeem_due(), vec!["0xC".to_string()]);
        e.on_redeemed("0xC", Ok("0xtx".into()), (close + 40) * NS);
        assert!(e.redeem_due().is_empty());
    }

    // --- epoch 2 ------------------------------------------------------------

    fn epoch2(extra: &[(&str, &str)]) -> Engine {
        let mut v: Vec<(&str, &str)> = vec![("PK_EG_MAX_PX", "0.97"), ("PK_EG_MAX_EDGE", "0.12"), ("PK_EG_SD_MULT_EARLY", "1.5")];
        v.extend_from_slice(extra);
        let mut e = Engine::new(cfg_with(&v), Persisted::default());
        e.set_window(window(), OPEN * NS);
        e
    }

    #[test]
    fn an_edge_over_the_band_is_model_error() {
        let mut e = epoch2(&[]);
        let now = setup(&mut e, (0.80, 0.82), (0.17, 0.19)); // P(up) ≈ 1: edge ≈ 0.17
        e.tick(now * NS);
        assert!(fired(&mut e).is_none());
        assert_eq!(last_gate(&mut e, now), "edge_too_big");
    }

    #[test]
    fn an_edge_in_the_band_fires() {
        let mut e = epoch2(&[]);
        let now = setup(&mut e, (0.89, 0.90), (0.09, 0.10)); // edge ≈ 0.095
        e.tick(now * NS);
        let d = fired(&mut e).expect("fires in the band");
        assert_eq!(d["side"], "up");
        assert_eq!(d["epoch"], "3");
        assert_eq!(d["trigger"], "tick");
    }

    #[test]
    fn asks_over_the_cap_are_skipped() {
        let mut e = epoch2(&[("PK_EG_MAX_PX", "0.70")]);
        let now = setup(&mut e, (0.89, 0.90), (0.09, 0.10));
        e.tick(now * NS);
        // Up (0.90) is over the cap; Down (0.10) is in range but has no edge.
        assert!(fired(&mut e).is_none());
        assert_eq!(last_gate(&mut e, now), "edge");
    }

    #[test]
    fn kalshi_must_agree() {
        let opts = [("PK_EG_KALSHI_FILTER", "yes"), ("KALSHI_API_KEY_ID", "kid"), ("KALSHI_PRIVATE_KEY_PATH", "/k")];
        // No Kalshi book: no entry.
        let mut e = epoch2(&opts);
        let now = setup(&mut e, (0.89, 0.90), (0.09, 0.10));
        e.tick(now * NS);
        assert!(fired(&mut e).is_none());
        assert_eq!(last_gate(&mut e, now), "no_kalshi");
        // Kalshi says Up at 0.85: under the 0.90 ask, it disagrees.
        let mut e = epoch2(&opts);
        let now = setup(&mut e, (0.89, 0.90), (0.09, 0.10));
        e.on_kalshi(&kalshi_frame(0.84, 0.86), now * NS);
        e.tick(now * NS);
        assert!(fired(&mut e).is_none());
        assert_eq!(last_gate(&mut e, now), "kalshi_disagrees");
        // Kalshi says Up at 0.95: it agrees.
        let mut e = epoch2(&opts);
        let now = setup(&mut e, (0.89, 0.90), (0.09, 0.10));
        e.on_kalshi(&kalshi_frame(0.94, 0.96), now * NS);
        e.tick(now * NS);
        let d = fired(&mut e).expect("fires with Kalshi agreeing");
        assert_eq!(d["kalshi_mid"].as_f64().unwrap(), 0.95);
        assert!(d["kalshi_margin"].as_f64().unwrap() > 0.0);
    }

    #[test]
    fn kalshi_deltas_move_the_book() {
        let mut e = epoch2(&[]);
        e.on_kalshi(&kalshi_frame(0.40, 0.45), OPEN * NS);
        e.on_kalshi(r#"{"type":"orderbook_delta","sid":1,"seq":2,"msg":{"market_ticker":"KX","price_dollars":"0.4200","delta_fp":"50.00","side":"yes"}}"#, OPEN * NS + 1);
        let q = e.kalshi_quote(0, OPEN, OPEN * NS + 2).unwrap();
        assert_eq!((q.bid, q.ask), (0.42, 0.45));
        e.on_kalshi(r#"{"type":"orderbook_delta","sid":1,"seq":3,"msg":{"market_ticker":"KX","price_dollars":"0.4200","delta_fp":"-50.00","side":"yes"}}"#, OPEN * NS + 3);
        assert_eq!(e.kalshi_quote(0, OPEN, OPEN * NS + 4).unwrap().bid, 0.40);
        e.invalidate_kalshi();
        assert!(e.kalshi_quote(0, OPEN, OPEN * NS + 5).is_none());
    }

    #[test]
    fn the_sd_widens_outside_the_last_30_s() {
        let mut e = epoch2(&[]);
        let close = OPEN + WINDOW_S;
        feed(&mut e, close - 45, close - 60);
        e.tick((OPEN + 5) * NS);
        let (_, _, _, m45) = e.model(0, OPEN, (close - 45) * NS).unwrap();
        assert_eq!(m45, 1.5);
        for t in close - 44..=close - 20 {
            e.on_tick(0, t, 100.3, t * NS);
        }
        let (_, _, _, m20) = e.model(0, OPEN, (close - 20) * NS).unwrap();
        assert_eq!(m20, 1.0);
    }

    #[test]
    fn spot_is_recorded_and_moves_its_model_only() {
        let mut e = epoch2(&[]);
        let close = OPEN + WINDOW_S;
        let now = close - 45;
        feed(&mut e, now, close); // flat at 100 ± 1 bp: no lead
        e.tick((OPEN + 5) * NS);
        // Spot 5 bp up since the newest Chainlink second.
        e.on_spot_frame("cb", r#"{"type":"ticker","product_id":"BTC-USD","best_bid":"99.99","best_ask":"100.01"}"#, now * NS + NS / 2);
        e.on_spot_frame("cb", r#"{"type":"ticker","product_id":"BTC-USD","best_bid":"100.04","best_ask":"100.06"}"#, now * NS + 900_000_000);
        e.on_spot_frame("bn", r#"{"s":"BTCUSDT","b":"100.0","a":"100.1"}"#, now * NS + 900_000_000);
        let t = now * NS + 950_000_000;
        let (p_spot, venue, mv) = e.model_spot(0, OPEN, t).unwrap();
        let (f, ..) = e.model(0, OPEN, t).unwrap();
        assert_eq!(venue, "cb");
        assert!((mv - 5.0).abs() < 0.01, "{mv}");
        assert!(p_spot > f.p_up + 0.05, "{p_spot} vs {}", f.p_up);
        e.last_grid[0] = 0;
        e.grid(0, OPEN, 45.0, t);
        let g = e.take_records().into_iter().find(|r| r["type"] == "eg_sec").unwrap();
        assert_eq!(g["spot"]["src"], "cb");
        assert!(g["spot"]["bn"]["mid"].as_f64().is_some());
    }

    #[test]
    fn tick_size_near_the_edges() {
        assert_eq!(tick_for(0.5), 0.01);
        assert_eq!(tick_for(0.97), 0.001);
        assert_eq!(tick_for(0.03), 0.001);
    }

    #[test]
    fn paper_loss_breach_is_record_only() {
        let mut e = engine();
        assert!(!e.cfg.enforce_halt);
        let close = OPEN + WINDOW_S;
        e.p.positions.push(Position {
            id: 1, asset: "BTC".into(), open: OPEN, up: true, token: "UP".into(), condition_id: "0xC".into(),
            shares: 30.0, usd: 24.0, fee: 0.2, entry_ns: 0, live: false, outcome: None, pnl: None,
            redeemed: false, redeem_tries: 0, epoch: "3".into(),
        });
        let t = (close + 30) * NS;
        e.on_resolved("BTC", OPEN, "no", t);
        assert!(!e.halted(t));
        let h = e.take_records().into_iter().find(|r| r["type"] == "eg_halt").unwrap();
        assert_eq!(h["record_only"], true);
        // A halt persisted by an enforcing run does not stop a record-only one.
        e.p.halted_day = Some(day_of(t / NS));
        assert!(!e.halted(t));
    }

    #[test]
    fn a_silent_socket_or_a_frozen_book_blocks() {
        // The socket: nothing for 3 s.
        let mut e = epoch2(&[]);
        let now = setup(&mut e, (0.89, 0.90), (0.09, 0.10));
        e.tick(now * NS + 3_000_000_000);
        assert!(fired(&mut e).is_none());
        assert_eq!(last_gate(&mut e, now), "poly_silent");
        // The socket alive (another token), but these books unchanged for 11 s.
        let mut e = epoch2(&[]);
        let now = setup(&mut e, (0.89, 0.90), (0.09, 0.10));
        for k in 1..=11 {
            e.on_tick(0, now + k, 100.3, (now + k) * NS);
        }
        e.on_poly(&book_frame("OTHER", 0.5, 0.6, 1.0), now * NS + 11_000_000_000);
        e.tick(now * NS + 11_000_000_000);
        assert!(fired(&mut e).is_none());
        assert_eq!(last_gate(&mut e, now), "stale_book");
    }

    #[test]
    fn a_book_change_evaluates_at_once() {
        let mut e = epoch2(&[]);
        let now = setup(&mut e, (0.89, 0.90), (0.09, 0.10));
        let touched = e.on_poly(&book_frame("UP", 0.89, 0.90, 100.0), now * NS);
        assert_eq!(touched.into_iter().collect::<Vec<_>>(), vec![(0, OPEN)]);
        assert!(e.evaluate_now(0, now * NS, "book").is_none()); // paper: filled on the clock
        let d = fired(&mut e).expect("fires on the book change");
        assert_eq!(d["trigger"], "book");
        assert!(d["book_age_ms"].as_i64().unwrap() < 10);
        // Outside the entry period, nothing.
        assert!(e.evaluate_now(0, (OPEN + 100) * NS, "book").is_none());
    }

    #[test]
    fn shadow_rules_fire_fill_and_settle_apart_from_the_traded_rule() {
        let mut e = engine();
        let close = OPEN + WINDOW_S;
        let now = close - 20;
        feed(&mut e, now, close - 60);
        // Up: ask 0.93 (edge ≈ 6.6¢ at p ≈ 1), bid 0.70 (spread 23¢).
        e.on_poly(&book_frame("UP", 0.70, 0.93, 100.0), now * NS);
        e.on_poly(&book_frame("DN", 0.05, 0.30, 100.0), now * NS);
        e.tick((OPEN + 5) * NS);
        e.tick(now * NS);
        let recs = e.take_records();
        assert!(recs.iter().any(|r| r["type"] == "eg_decision"), "the traded rule fires");
        let mut rules: Vec<&str> =
            recs.iter().filter(|r| r["type"] == "eg_shadow_decision").map(|r| r["rule"].as_str().unwrap()).collect();
        rules.sort_unstable();
        // edge9: 6.6¢ < 9¢. spread10: 23¢ spread.
        assert_eq!(rules, ["fav40", "sd11"]);
        // Shadow orders do not count toward the open cost.
        assert!((e.open_cost() - 10.0 * 0.94).abs() < 1e-9, "{}", e.open_cost());
        e.tick(now * NS + 300_000_000);
        let recs = e.take_records();
        assert_eq!(recs.iter().filter(|r| r["type"] == "eg_shadow_fill").count(), 2);
        assert_eq!(e.p.positions.len(), 1);
        assert_eq!(e.p.shadows.len(), 2);
        // Once per window.
        e.tick(now * NS + 400_000_000);
        assert!(!e.take_records().iter().any(|r| r["type"] == "eg_shadow_decision"));
        e.on_resolved("BTC", OPEN, "yes", (close + 30) * NS);
        let recs = e.take_records();
        let settles: Vec<&Value> = recs.iter().filter(|r| r["type"] == "eg_shadow_settle").collect();
        assert_eq!(settles.len(), 2);
        assert!(settles.iter().all(|r| r["won"] == true && r["pnl"].as_f64().unwrap() > 0.0));
        assert!(e.p.shadows.is_empty());
        // Shadow P&L never reaches the realized book.
        let real: f64 = e.p.realized.values().sum();
        let s = recs.iter().find(|r| r["type"] == "eg_settle").unwrap();
        assert!((real - s["pnl"].as_f64().unwrap()).abs() < 1e-3); // the record is rounded to 4 dp
    }

    #[test]
    fn edge9_and_spread10_fire_on_a_wide_edge_and_a_tight_book() {
        let mut e = engine();
        let now = setup(&mut e, (0.80, 0.82), (0.17, 0.19));
        e.tick(now * NS);
        let mut rules: Vec<String> = e
            .take_records()
            .into_iter()
            .filter(|r| r["type"] == "eg_shadow_decision")
            .map(|r| r["rule"].as_str().unwrap().to_owned())
            .collect();
        rules.sort_unstable();
        assert_eq!(rules, ["edge9", "fav40", "sd11", "spread10"]);
    }
}

