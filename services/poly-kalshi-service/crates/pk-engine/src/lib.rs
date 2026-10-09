//! The engine: every tape line in, decisions, paper fills and statistics out.
//!
//! Live and replay drive the same [`Engine::handle_line`], so a replay of the
//! tape makes the decisions the live process made. Venues: `k` (Kalshi), `p`
//! (Polymarket), `c` (control lines written by this service); anything else
//! (e.g. `b`, PolyBolt reference prices) is counted and ignored.
//!
//! **Window identity and the ghost gap (kpc trap 3).** Books are keyed by the
//! window's own ticker and token ids, never by asset; at every roll the old
//! window's books are dropped; a leg counts only if its book updated since the
//! window opened.
//!
//! **Evaluation (kpc trap 5).** The rule runs on every book update of the
//! asset's current window, not on a one-second sample.
//!
//! **Roles** keep statistics off the trading path:
//! - `Trade`: books, the live decision, the paper venue, the ledger, and the
//!   `decision`, `fill` and `settle` records. Nothing else.
//! - `Observe`: everything `Trade` skips, on the same lines, in another
//!   process: shadow variants, markouts, the per-second series, the uncensored
//!   gap, BBO checks, feed latency, `window` and `resolution` records. It
//!   decides the live rule too, so a window is consumed as the trader consumed
//!   it, but it never orders. Markouts start from the trader's fills
//!   ([`Engine::note_fill`]).
//! - `Full`: both in one object (replay, tests), the reference the split is
//!   checked against.

use std::collections::{HashMap, VecDeque};

use pk_core::decision::{Action, Decision, StrategyParams, WINDOW_S, decide};
use pk_core::fees::taker_fee;
use pk_core::journal::Sink;
use pk_core::ledger::{Ledger, Position};
use pk_core::metrics::Metrics;
use pk_core::num::{lag_ms, px_key, round_dp, round_opt};
use pk_core::risk::RiskState;
use pk_core::shadow::{Shadow, quote_json};
use pk_core::window::{Window, secs_f64, window_open};
use pk_core::{Book, BookSide, BookState, NS, Quote};
use serde_json::{Map, Value, json};

pub mod paper;
pub mod parse;
pub mod reverse;

pub use paper::{FillResult, PaperVenue};

pub const SERIES_POINTS: usize = 2 * WINDOW_S as usize;
/// Identities kept for late resolutions.
pub const KEEP_WINDOWS_S: i64 = 6 * 3600;
pub const FULL_THRESHOLDS: [f64; 5] = [0.08, 0.10, 0.12, 0.15, 0.20];
const FULL_KEYS: [&str; 5] = ["0.08", "0.10", "0.12", "0.15", "0.20"];
/// The best bid/ask carried on `price_change` is checked against our book on
/// 1 in this many; the counters are both sampled, so their ratio is the rate.
pub const BBO_CHECK_EVERY: u64 = 50;
pub const MARKOUT_S: [i64; 3] = [1, 5, 30];
/// Exit telemetry: the bid a sell would hit, and the gap's closure.
pub const EXIT_S: [i64; 4] = [5, 30, 60, 120];
pub const EXIT_GAP: f64 = 0.03;
const HORIZONS: [i64; 5] = [1, 5, 30, 60, 120];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Role {
    Full,
    Trade,
    Observe,
}

impl std::str::FromStr for Role {
    type Err = anyhow::Error;
    fn from_str(s: &str) -> anyhow::Result<Self> {
        match s {
            "full" => Ok(Role::Full),
            "trade" => Ok(Role::Trade),
            "observe" => Ok(Role::Observe),
            other => anyhow::bail!("role must be full, trade or observe, not {other:?}"),
        }
    }
}

#[derive(Debug, Default)]
pub struct AssetState {
    pub window: Option<Window>,
    pub open_unix: Option<i64>,
    pub traded: bool,
    pub decision: Option<Map<String, Value>>,
    pub max_abs_gap: f64,
    pub evals: u64,
    pub reasons: Vec<(&'static str, u64)>,
    pub last_gap: Option<f64>,
    pub max_abs_gap_full: f64,
    pub first_cross: [Option<(f64, &'static str)>; 5],
    pub last_hint_ns: i64,
}

impl AssetState {
    fn count_reason(&mut self, reason: &'static str) {
        match self.reasons.iter_mut().find(|(r, _)| *r == reason) {
            Some((_, n)) => *n += 1,
            None => self.reasons.push((reason, 1)),
        }
    }
}

#[derive(Debug, Clone)]
pub(crate) struct Pending {
    pub(crate) due_ns: i64,
    pub(crate) window: Window,
    pub(crate) token: String,
    pub(crate) side: &'static str,
    pub(crate) decision: Decision,
    pub(crate) decision_ns: i64,
}

#[derive(Debug)]
struct Markout {
    fill_ns: i64,
    token: String,
    ref_px: f64,
    record: Map<String, Value>,
    window: Option<Window>,
    next: usize,
    gap_closed: bool,
}

/// An entry the trader may hand to a real order path (the probe today).
#[derive(Clone, Debug, PartialEq)]
pub struct OrderIntent {
    pub asset: String,
    pub open_unix: i64,
    pub token: String,
    pub side: &'static str,
    pub entry_px: f64,
    pub limit_px: f64,
    pub contracts: i64,
    pub decision_ns: i64,
    /// The leg's touch at the decision, for pricing a probe against.
    pub best_bid: Option<f64>,
    pub best_ask: Option<f64>,
}

/// An asset's gap is nearing the threshold: sign an order for this leg ahead.
#[derive(Clone, Debug, PartialEq)]
pub struct PresignHint {
    pub asset: String,
    pub token: String,
    pub best_bid: Option<f64>,
    pub best_ask: Option<f64>,
}

/// Hints start at this fraction of the threshold, at most one per asset per second.
pub const PRESIGN_FRACTION: f64 = 0.75;

pub type Alert = Box<dyn FnMut(&str, &str, &[String]) + Send>;

pub struct EngineConfig {
    pub params: StrategyParams,
    pub assets: Vec<String>,
    pub role: Role,
    pub venue: Option<PaperVenue>,
    pub shadow: Shadow,
    /// The order's limit above the touch, never above the cap.
    pub limit_offset: f64,
    /// Order at most the shares offered at or below the limit at the decision.
    pub size_to_depth: bool,
    /// epoch / build / config, added to every journal record.
    pub stamp: Map<String, Value>,
    /// Write an `age_outlier` record when the cheap leg's book is older than
    /// this at a window-consuming decision (the 1.2 s Polymarket age case).
    pub age_outlier_ms: Option<f64>,
    /// Queue an [`OrderIntent`] for every entry the paper venue takes.
    pub emit_intents: bool,
    /// Which venue is the signal and which the execution (research).
    pub direction: Direction,
}

/// `Forward` is the trade: Kalshi signals, Polymarket executes. `Reverse`
/// (research only) flips them: Polymarket signals, Kalshi executes and settles.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Direction {
    #[default]
    Forward,
    Reverse,
}

impl EngineConfig {
    pub fn new(params: StrategyParams, assets: Vec<String>) -> Self {
        Self {
            params,
            assets,
            role: Role::Full,
            venue: None,
            shadow: Shadow::default(),
            limit_offset: 0.0,
            size_to_depth: false,
            stamp: Map::new(),
            age_outlier_ms: None,
            emit_intents: false,
            direction: Direction::Forward,
        }
    }
}

pub struct Engine {
    pub params: StrategyParams,
    pub assets: Vec<String>,
    pub role: Role,
    pub(crate) trade: bool,
    pub(crate) observe: bool,
    pub(crate) size_to_depth: bool,
    pub stamp: Map<String, Value>,
    pub ledger: Ledger,
    pub risk: RiskState,
    pub venue: Option<PaperVenue>,
    journal: Box<dyn Sink + Send>,
    alert: Option<Alert>,
    windows: Vec<std::collections::BTreeMap<i64, Window>>,
    by_condition: HashMap<String, Window>,
    pub books: HashMap<String, Book>,
    /// book id -> (asset index, open_unix)
    ids: HashMap<String, (usize, i64)>,
    pub state: Vec<AssetState>,
    pub(crate) pending: Vec<Pending>,
    markouts: Vec<Markout>,
    bbo_seen: u64,
    pub bbo_check_every: u64,
    pub shadow: Shadow,
    pub limit_offset: f64,
    pub(crate) resolutions: HashMap<(String, i64), Vec<(String, String)>>,
    pub recent: VecDeque<Value>,
    pub counters: HashMap<String, u64>,
    pub last_ns: i64,
    pub last_by_venue: HashMap<char, i64>,
    pub metrics: Metrics,
    pub series: Vec<VecDeque<Value>>,
    sample_sec: Option<i64>,
    grid_open: Option<i64>,
    age_outlier_ms: Option<f64>,
    emit_intents: bool,
    pub(crate) reverse: bool,
    pub intents: Vec<OrderIntent>,
    pub hints: Vec<PresignHint>,
    prev_p_ns: Option<i64>,
    last_reconnect_p_ns: Option<i64>,
}

fn side_str(buy_yes: Option<bool>) -> &'static str {
    if buy_yes == Some(true) { "yes" } else { "no" }
}

fn invalid_key(leg: usize, state: &str) -> String {
    format!("invalid_{}_{state}", ["kalshi", "poly_yes", "poly_no"][leg])
}

impl Engine {
    pub fn new(cfg: EngineConfig, ledger: Ledger, risk: RiskState, journal: Box<dyn Sink + Send>) -> Self {
        let n = cfg.assets.len();
        Self {
            trade: matches!(cfg.role, Role::Full | Role::Trade),
            observe: matches!(cfg.role, Role::Full | Role::Observe),
            role: cfg.role,
            size_to_depth: cfg.size_to_depth,
            stamp: cfg.stamp,
            params: cfg.params,
            ledger,
            risk,
            venue: cfg.venue,
            journal,
            alert: None,
            windows: vec![Default::default(); n],
            by_condition: HashMap::new(),
            books: HashMap::new(),
            ids: HashMap::new(),
            state: (0..n).map(|_| AssetState::default()).collect(),
            pending: Vec::new(),
            markouts: Vec::new(),
            bbo_seen: 0,
            bbo_check_every: BBO_CHECK_EVERY,
            shadow: cfg.shadow,
            limit_offset: cfg.limit_offset,
            resolutions: HashMap::new(),
            recent: VecDeque::with_capacity(200),
            counters: HashMap::new(),
            last_ns: 0,
            last_by_venue: HashMap::new(),
            metrics: Metrics::default(),
            series: (0..n).map(|_| VecDeque::with_capacity(SERIES_POINTS)).collect(),
            sample_sec: None,
            grid_open: None,
            age_outlier_ms: cfg.age_outlier_ms,
            emit_intents: cfg.emit_intents,
            reverse: cfg.direction == Direction::Reverse,
            intents: Vec::new(),
            hints: Vec::new(),
            prev_p_ns: None,
            last_reconnect_p_ns: None,
            assets: cfg.assets,
        }
    }

    pub fn set_alert(&mut self, alert: Alert) {
        self.alert = Some(alert);
    }

    pub fn set_journal(&mut self, journal: Box<dyn Sink + Send>) {
        self.journal = journal;
    }

    pub(crate) fn count(&mut self, key: &str) {
        match self.counters.get_mut(key) {
            Some(n) => *n += 1,
            None => {
                self.counters.insert(key.to_owned(), 1);
            }
        }
    }

    fn asset_index(&self, asset: &str) -> Option<usize> {
        self.assets.iter().position(|a| a == asset)
    }

    // --- input ------------------------------------------------------------------

    pub fn handle_line(&mut self, recv_ns: i64, venue: char, text: &str) {
        self.last_ns = recv_ns;
        // The last Polymarket frame before this line, for `age_outlier`.
        self.prev_p_ns = self.last_by_venue.get(&'p').copied();
        self.last_by_venue.insert(venue, recv_ns);
        if self.observe {
            let sec = recv_ns.div_euclid(NS);
            if self.sample_sec != Some(sec) {
                if let Some(prev) = self.sample_sec {
                    self.sample(prev);
                }
                self.sample_sec = Some(sec);
            }
        }
        let grid = window_open(recv_ns.div_euclid(NS));
        if self.grid_open != Some(grid) {
            self.grid_open = Some(grid);
            self.roll(recv_ns);
        }
        if !self.pending.is_empty() {
            self.resolve_pending(recv_ns);
        }
        if !self.markouts.is_empty() {
            self.resolve_markouts(recv_ns);
        }
        let touched = match venue {
            'p' => self.poly(text, recv_ns),
            'k' => self.kalshi(text, recv_ns),
            'c' => self.control(text, recv_ns),
            _ => {
                self.count("unknown_venue");
                return;
            }
        };
        let Some(touched) = touched else {
            self.count("bad_json");
            return;
        };
        for idx in touched {
            self.evaluate(idx, recv_ns);
        }
    }

    fn sample(&mut self, sec: i64) {
        for idx in 0..self.assets.len() {
            let Some(w) = &self.state[idx].window else { continue };
            if !(w.open_unix <= sec && sec < w.close_unix()) {
                continue;
            }
            let open = w.open_unix;
            let Some((k, y, n)) = self.quotes(idx) else { continue };
            let s = &mut self.series[idx];
            if s.len() == SERIES_POINTS {
                s.pop_front();
            }
            s.push_back(json!([
                sec,
                open,
                round_opt(k.mid(), 4),
                round_opt(y.mid(), 4),
                y.bid,
                y.ask,
                n.bid,
                n.ask
            ]));
        }
    }

    pub fn series_since(&self, since: i64) -> Map<String, Value> {
        self.assets
            .iter()
            .zip(&self.series)
            .map(|(a, pts)| {
                let v: Vec<Value> = pts
                    .iter()
                    .filter(|p| p.get(0).and_then(Value::as_i64).is_some_and(|s| s > since))
                    .cloned()
                    .collect();
                (a.clone(), Value::Array(v))
            })
            .collect()
    }

    // --- windows ----------------------------------------------------------------

    fn register(&mut self, window: Window, recv_ns: i64) {
        let Some(idx) = self.asset_index(&window.asset) else { return };
        if let Some(known) = self.windows[idx].get(&window.open_unix) {
            if *known == window {
                return;
            }
            tracing::warn!(asset = %window.asset, "window_changed");
            for old in known.ids() {
                self.books.remove(old);
                self.ids.remove(old);
            }
        }
        self.by_condition.insert(window.condition_id.clone(), window.clone());
        if window.close_unix() as f64 > secs_f64(recv_ns) {
            for id in window.ids() {
                self.books.entry(id.to_owned()).or_default();
                self.ids.insert(id.to_owned(), (idx, window.open_unix));
            }
        }
        self.windows[idx].insert(window.open_unix, window);
        self.roll(recv_ns);
    }

    fn roll(&mut self, recv_ns: i64) {
        let now_open = window_open(recv_ns.div_euclid(NS));
        for idx in 0..self.assets.len() {
            let window = self.windows[idx].get(&now_open).cloned();
            let st = &self.state[idx];
            if st.open_unix == Some(now_open) && st.window == window {
                continue;
            }
            if st.open_unix != Some(now_open) {
                self.close_window(idx, recv_ns);
                self.state[idx] = AssetState { open_unix: Some(now_open), ..Default::default() };
            }
            self.state[idx].window = window;
            // Drop every book of an earlier window: nothing may read them again.
            let stale: Vec<String> = self
                .ids
                .iter()
                .filter(|(_, (o, open))| *o == idx && *open < now_open)
                .map(|(k, _)| k.clone())
                .collect();
            for id in stale {
                self.ids.remove(&id);
                self.books.remove(&id);
            }
            let cutoff = now_open - KEEP_WINDOWS_S;
            self.shadow.forget_before(cutoff);
            let old: Vec<i64> = self.windows[idx].range(..cutoff).map(|(o, _)| *o).collect();
            for open in old {
                if let Some(w) = self.windows[idx].remove(&open) {
                    self.by_condition.remove(&w.condition_id);
                }
                self.resolutions.remove(&(self.assets[idx].clone(), open));
            }
        }
    }

    fn close_window(&mut self, idx: usize, recv_ns: i64) {
        let st = &self.state[idx];
        let Some(open) = st.open_unix else { return };
        if !self.observe {
            return;
        }
        let identified = st.window.is_some();
        let action = st
            .decision
            .as_ref()
            .and_then(|d| d.get("action"))
            .cloned()
            .unwrap_or_else(|| Value::from("none"));
        let mut first_cross = Map::new();
        let mut cross_side = Map::new();
        for (i, key) in FULL_KEYS.iter().enumerate() {
            first_cross.insert((*key).into(), st.first_cross[i].map(|c| c.0).into());
            cross_side.insert((*key).into(), st.first_cross[i].map(|c| c.1).into());
        }
        let reasons: Map<String, Value> =
            st.reasons.iter().map(|(r, n)| ((*r).to_owned(), (*n).into())).collect();
        let record = json!({
            "type": "window",
            "asset": self.assets[idx],
            "open_unix": open,
            "identified": identified,
            "action": action,
            "max_abs_gap": round_dp(st.max_abs_gap, 4),
            "max_abs_gap_full": round_dp(st.max_abs_gap_full, 4),
            "first_cross_s": first_cross,
            "gap_at_cross_side": cross_side,
            "evals": st.evals,
            "reasons": reasons,
        });
        if !identified {
            self.count("windows_unidentified");
        }
        self.write(recv_ns, record, false);
        if identified {
            let key = (self.assets[idx].clone(), open);
            for record in self.shadow.close(&key) {
                self.write(recv_ns, record, false);
            }
        }
    }

    // --- venues -----------------------------------------------------------------

    fn book_owner(&self, id: &str) -> Option<usize> {
        self.ids.get(id).map(|(i, _)| *i)
    }

    fn poly(&mut self, text: &str, recv_ns: i64) -> Option<Vec<usize>> {
        let msgs = parse::poly_msgs(text).ok()?;
        let mut touched: Vec<usize> = Vec::new();
        let observe = self.observe;
        let want_lag = observe || self.params.max_poly_lag_ms.is_some();
        let mut lag: Option<f64> = None;
        for msg in msgs.iter().flatten() {
            if want_lag {
                lag = lag_ms(msg.timestamp.0, recv_ns);
                if observe && let Some(l) = lag {
                    self.metrics.add("feed_ms_p", l);
                }
            }
            match msg.kind() {
                Some("book") => {
                    let Some(id) = msg.asset_id.as_ref().map(|i| i.as_str()) else { continue };
                    let Some(owner) = self.book_owner(id) else { continue };
                    let Some(book) = self.books.get_mut(id) else { continue };
                    let lv = |v: &Option<Vec<parse::Level>>| {
                        v.as_deref()
                            .unwrap_or_default()
                            .iter()
                            .map(|l| (px_key(l.price.0), l.size.0))
                            .collect::<Vec<_>>()
                    };
                    book.snapshot_keys(lv(&msg.bids).into_iter(), lv(&msg.asks).into_iter(), recv_ns);
                    if want_lag {
                        book.lag_ms = lag;
                    }
                    if !touched.contains(&owner) {
                        touched.push(owner);
                    }
                }
                Some("price_change") => {
                    for change in msg.price_changes.as_deref().unwrap_or_default() {
                        let Some(id) = change.asset_id.as_ref().map(|i| i.as_str()) else { continue };
                        let Some(owner) = self.book_owner(id) else { continue };
                        let Some(book) = self.books.get_mut(id) else { continue };
                        if book.snapshot_ns.is_none() {
                            continue;
                        }
                        let side =
                            if change.side.as_deref() == Some("BUY") { BookSide::Bid } else { BookSide::Ask };
                        book.set_level(side, px_key(change.price.0), change.size.0, recv_ns);
                        if want_lag {
                            book.lag_ms = lag;
                        }
                        if observe {
                            self.bbo_seen += 1;
                            let every = self.bbo_check_every;
                            if self.bbo_seen % every == 1 % every {
                                let (bid, ask) = (change.best_bid, change.best_ask);
                                self.check_bbo(id, bid.map(|n| n.0), ask.map(|n| n.0), "pc");
                            }
                        }
                        if !touched.contains(&owner) {
                            touched.push(owner);
                        }
                    }
                }
                Some("best_bid_ask") if observe => {
                    let Some(id) = msg.asset_id.as_ref().map(|i| i.as_str()) else { continue };
                    if self.book_owner(id).is_some()
                        && self.books.get(id).is_some_and(|b| b.snapshot_ns.is_some())
                    {
                        self.check_bbo(id, msg.best_bid.map(|n| n.0), msg.best_ask.map(|n| n.0), "event");
                    }
                }
                Some("market_resolved") => {
                    let market = msg.market.as_ref().map(|m| m.as_str().to_owned()).unwrap_or_default();
                    let winner =
                        msg.winning_asset_id.as_ref().map(|w| w.as_str().to_owned()).unwrap_or_default();
                    self.poly_resolved(&market, &winner, recv_ns);
                }
                _ => {}
            }
        }
        Some(touched)
    }

    /// The venue's own top of book against ours: a desync shows up here first.
    /// `pc` is the best bid/ask carried on each `price_change` (matched ours
    /// 99.86% on the first tape); `event` is the separate `best_bid_ask` event,
    /// one update stale, counted only to notice if that changes.
    fn check_bbo(&mut self, id: &str, bid: Option<f64>, ask: Option<f64>, source: &str) {
        let (Some(bid), Some(ask)) = (bid, ask) else { return };
        self.count(if source == "pc" { "bbo_checks_pc" } else { "bbo_checks_event" });
        let Some(book) = self.books.get(id) else { return };
        let (ours_bid, ours_ask) = (book.best_bid(), book.best_ask());
        let r4 = |x: f64| pk_core::num::key_px(px_key(x));
        if ours_bid.is_some_and(|b| r4(bid) != b) || ours_ask.is_some_and(|a| r4(ask) != a) {
            self.count(if source == "pc" { "bbo_mismatch_pc" } else { "bbo_mismatch_event" });
        }
    }

    fn poly_resolved(&mut self, market: &str, winner: &str, recv_ns: i64) {
        let Some(w) = self.by_condition.get(market) else { return };
        let outcome = if winner == w.yes_token {
            "yes"
        } else if winner == w.no_token {
            "no"
        } else {
            return;
        };
        let (asset, open) = (w.asset.clone(), w.open_unix);
        self.resolution(&asset, open, "poly", outcome, recv_ns);
    }

    fn kalshi(&mut self, text: &str, recv_ns: i64) -> Option<Vec<usize>> {
        let frame: parse::KalshiFrame = match serde_json::from_str(text) {
            Ok(f) => f,
            Err(e) if e.is_data() => {
                serde_json::from_str::<serde::de::IgnoredAny>(text).ok()?;
                return Some(Vec::new());
            }
            Err(_) => return None,
        };
        let kind = frame.kind.as_deref();
        if !matches!(kind, Some("orderbook_snapshot" | "orderbook_delta")) {
            return Some(Vec::new());
        }
        let msg = frame.msg.unwrap_or_default();
        if self.observe {
            let venue_ms = msg.ts_ms.0.or(frame.sending_ts_ms.0);
            if let Some(l) = lag_ms(venue_ms, recv_ns) {
                self.metrics.add("feed_ms_k", l);
            }
        }
        let ticker = msg.market_ticker.as_ref().map(|t| t.as_str()).unwrap_or("None");
        let Some(owner) = self.book_owner(ticker) else { return Some(Vec::new()) };
        let Some(book) = self.books.get_mut(ticker) else { return Some(Vec::new()) };
        if kind == Some("orderbook_snapshot") {
            let yes = msg.levels(true).into_iter().map(|(p, s)| (px_key(p), s));
            let no = msg.levels(false).into_iter().map(|(p, s)| (px_key(1.0 - p), s));
            book.snapshot_keys(yes, no, recv_ns);
        } else {
            if book.snapshot_ns.is_none() {
                return Some(Vec::new());
            }
            let Some((price, delta)) = msg.delta() else { return Some(Vec::new()) };
            if msg.side.as_deref() == Some("yes") {
                book.add_level(BookSide::Bid, px_key(price), delta, recv_ns);
            } else {
                book.add_level(BookSide::Ask, px_key(1.0 - price), delta, recv_ns);
            }
        }
        Some(vec![owner])
    }

    fn control(&mut self, text: &str, recv_ns: i64) -> Option<Vec<usize>> {
        let ctl: parse::Control = match serde_json::from_str(text) {
            Ok(c) => c,
            Err(e) if e.is_data() => {
                serde_json::from_str::<serde::de::IgnoredAny>(text).ok()?;
                return Some(Vec::new());
            }
            Err(_) => return None,
        };
        match ctl.kind.as_deref() {
            Some("window") => {
                let Ok(window) = serde_json::from_str::<Window>(text) else { return Some(Vec::new()) };
                let idx = self.asset_index(&window.asset);
                self.register(window, recv_ns);
                Some(idx.into_iter().collect())
            }
            Some(kind @ ("reconnect" | "resync")) if matches!(ctl.venue.as_deref(), Some("k" | "p")) => {
                let prefix = ctl.venue.as_deref().unwrap_or_default().to_owned();
                if prefix == "p" {
                    self.last_reconnect_p_ns = Some(recv_ns);
                }
                match &ctl.tokens {
                    // A sharded socket: only that shard's books go stale.
                    Some(tokens) => {
                        for t in tokens {
                            let id = match t {
                                Value::String(s) => s.clone(),
                                other => other.to_string(),
                            };
                            if let Some(b) = self.books.get_mut(&id) {
                                b.invalidate();
                            }
                        }
                    }
                    None => {
                        for (id, b) in self.books.iter_mut() {
                            if (prefix == "k") == id.starts_with("KX") {
                                b.invalidate();
                            }
                        }
                    }
                }
                self.count(&format!("{kind}_{prefix}"));
                Some(Vec::new())
            }
            Some("resolution") => {
                if let (Some(asset), Some(open), Some(venue), Some(outcome)) =
                    (ctl.asset.as_deref(), ctl.open_unix, ctl.venue.as_deref(), ctl.outcome.as_deref())
                {
                    let (asset, venue, outcome) = (asset.to_owned(), venue.to_owned(), outcome.to_owned());
                    self.resolution(&asset, open.0 as i64, &venue, &outcome, recv_ns);
                }
                Some(Vec::new())
            }
            _ => Some(Vec::new()),
        }
    }

    // --- decisions --------------------------------------------------------------

    pub fn quotes(&self, idx: usize) -> Option<(Quote, Quote, Quote)> {
        let w = self.state[idx].window.as_ref()?;
        let since = Some(w.open_unix * NS);
        let [k, y, n] = w.ids().map(|id| self.books.get(id));
        Some((k?.quote(since), y?.quote(since), n?.quote(since)))
    }

    /// Shares offered at or below `limit` on the leg a buy would take.
    fn depth(&self, w: &Window, buy_yes: bool, limit: f64) -> f64 {
        let id = if buy_yes { &w.yes_token } else { &w.no_token };
        self.books.get(id).map(|b| b.depth_upto(limit)).unwrap_or(0.0)
    }

    /// Frame lag (ms) of the YES and NO books' latest updates.
    fn lags(&self, w: &Window) -> (Option<f64>, Option<f64>) {
        let lag = |id: &str| self.books.get(id).and_then(|b| b.lag_ms);
        (lag(&w.yes_token), lag(&w.no_token))
    }

    /// The uncensored gap: every update inside the entry window, consumed or not.
    pub(crate) fn track_gap(&mut self, idx: usize, k: &Quote, y: &Quote, sec_into: f64) {
        let p = &self.params;
        if !((p.entry_early_s as f64) <= sec_into && sec_into <= (WINDOW_S - p.entry_late_s) as f64) {
            return;
        }
        let (Some(km), Some(ym)) = (k.mid(), y.mid()) else { return };
        let gap = km - ym;
        let st = &mut self.state[idx];
        st.max_abs_gap_full = st.max_abs_gap_full.max(gap.abs());
        if st.first_cross.iter().all(Option::is_some) {
            return;
        }
        for (i, t) in FULL_THRESHOLDS.iter().enumerate() {
            if st.first_cross[i].is_none() && gap.abs() >= *t {
                st.first_cross[i] = Some((round_dp(sec_into, 3), if gap > 0.0 { "yes" } else { "no" }));
            }
        }
    }

    fn evaluate(&mut self, idx: usize, recv_ns: i64) {
        if self.reverse {
            return self.evaluate_reverse(idx, recv_ns);
        }
        let Some(window) = self.state[idx].window.clone() else { return };
        let Some((k, y, n)) = self.quotes(idx) else { return };
        let sec_into = secs_f64(recv_ns) - window.open_unix as f64;
        if self.observe {
            self.track_gap(idx, &k, &y, sec_into);
            if self.shadow.is_active() {
                let key = (window.asset.clone(), window.open_unix);
                let lags = self.lags(&window);
                let mut shadow = std::mem::take(&mut self.shadow);
                shadow.evaluate(
                    &key,
                    (&k, &y, &n),
                    sec_into,
                    recv_ns,
                    |buy_yes, limit| self.depth(&window, buy_yes, limit),
                    (window.fee_rate, window.fee_exponent),
                    lags,
                );
                self.shadow = shadow;
            }
        }
        if self.state[idx].traded {
            return;
        }
        let lags = self.params.max_poly_lag_ms.map(|_| self.lags(&window));
        let mut decision = decide(&k, &y, &n, &self.params, sec_into, false, None, lags);
        self.state[idx].evals += 1;
        if self.observe {
            self.state[idx].count_reason(decision.label());
            if decision.reason == "invalid_leg" {
                self.count_invalid_legs(&window);
            }
        }
        if let Some(gap) = decision.gap {
            let st = &mut self.state[idx];
            st.last_gap = Some(gap);
            st.max_abs_gap = st.max_abs_gap.max(gap.abs());
        }
        if !decision.consumes_window() {
            if self.emit_intents && self.trade {
                self.maybe_hint(idx, &window, &decision, recv_ns);
            }
            return;
        }
        self.state[idx].traded = true;
        if !self.trade {
            // The observer consumes the window as the trader did; the trader
            // writes the decision record and places the order.
            let mut d = Map::new();
            d.insert("action".into(), decision.action.as_str().into());
            d.insert("side".into(), side_str(decision.buy_yes).into());
            d.insert("entry_px".into(), decision.entry_px.into());
            d.insert("sec_into".into(), round_dp(sec_into, 3).into());
            d.insert("gap".into(), round_dp(decision.gap.unwrap_or(0.0), 4).into());
            self.state[idx].decision = Some(d);
            return;
        }
        if decision.action == Action::Enter && self.size_to_depth {
            let entry = decision.entry_px.expect("an entry has a price");
            let offered = self.depth(&window, decision.buy_yes == Some(true), self.limit_px(entry));
            let capped = decision.contracts.min(offered.floor() as i64).max(1);
            if capped < decision.contracts {
                decision.contracts = capped;
            }
        }
        let age = |id: &str| {
            self.books.get(id).and_then(|b| b.last_update_ns).map(|t| round_dp((recv_ns - t) as f64 / 1e6, 1))
        };
        let ages = [age(&window.kalshi_ticker), age(&window.yes_token), age(&window.no_token)];
        if let Some(a) = ages[0] {
            self.metrics.add("age_ms_kalshi", a);
        }
        let poly_age = if decision.buy_yes == Some(true) { ages[1] } else { ages[2] };
        if let Some(a) = poly_age {
            self.metrics.add("age_ms_poly", a);
        }
        let mut record = Map::new();
        record.insert("type".into(), "decision".into());
        record.insert("asset".into(), window.asset.clone().into());
        record.insert("open_unix".into(), window.open_unix.into());
        record.insert("t_ns".into(), recv_ns.into());
        record.insert("sec_into".into(), round_dp(sec_into, 3).into());
        record.insert("action".into(), decision.action.as_str().into());
        record.insert("reason".into(), decision.reason.into());
        record.insert("gap".into(), round_dp(decision.gap.unwrap_or(0.0), 4).into());
        record.insert("side".into(), side_str(decision.buy_yes).into());
        record.insert("entry_px".into(), decision.entry_px.into());
        record.insert("contracts".into(), decision.contracts.into());
        record.insert("kalshi".into(), quote_json(&k));
        record.insert("poly_yes".into(), quote_json(&y));
        record.insert("poly_no".into(), quote_json(&n));
        record.insert("age_ms".into(), json!({"kalshi": ages[0], "poly_yes": ages[1], "poly_no": ages[2]}));
        record.insert(
            "depth_at_limit".into(),
            decision
                .entry_px
                .map(|px| round_dp(self.depth(&window, decision.buy_yes == Some(true), px), 2))
                .into(),
        );
        record.insert(
            "params".into(),
            json!({
                "gap_threshold": self.params.gap_threshold,
                "max_entry_px": self.params.max_entry_px,
                "notional_usd": self.params.notional_usd,
            }),
        );
        if let (Some(limit), Some(age)) = (self.age_outlier_ms, poly_age)
            && age > limit
        {
            self.age_outlier(&window, &decision, age, recv_ns);
        }
        if decision.action == Action::Enter {
            let blocked = self.submit(&window, &decision, recv_ns);
            record.insert("blocked".into(), blocked.into());
        }
        self.state[idx].decision = Some(record.clone());
        self.write(recv_ns, Value::Object(record), true);
    }

    /// Why the cheap leg's book was old at a decision: venue silence (no
    /// Polymarket frame at all for a while: a stall or a slow-consumer drop), a
    /// recent reconnect, a late frame (transport lag), or simply a quiet book.
    fn age_outlier(&mut self, w: &Window, d: &Decision, age_ms: f64, recv_ns: i64) {
        let token = if d.buy_yes == Some(true) { &w.yes_token } else { &w.no_token };
        let book = self.books.get(token);
        let ms = |t: Option<i64>| t.map(|t| round_dp((recv_ns - t) as f64 / 1e6, 1));
        let record = json!({
            "type": "age_outlier",
            "asset": w.asset,
            "open_unix": w.open_unix,
            "t_ns": recv_ns,
            "leg": if d.buy_yes == Some(true) { "poly_yes" } else { "poly_no" },
            "age_ms": age_ms,
            "frame_lag_ms": book.and_then(|b| b.lag_ms).map(|l| round_dp(l, 1)),
            "snapshot_age_ms": ms(book.and_then(|b| b.snapshot_ns)),
            "poly_feed_silence_ms": ms(self.prev_p_ns),
            "since_reconnect_p_ms": ms(self.last_reconnect_p_ns),
        });
        self.count("age_outliers");
        self.write(recv_ns, record, false);
    }

    fn maybe_hint(&mut self, idx: usize, w: &Window, d: &Decision, recv_ns: i64) {
        let Some(gap) = d.gap else { return };
        if d.reason != "below_threshold" || gap.abs() < self.params.gap_threshold * PRESIGN_FRACTION {
            return;
        }
        if recv_ns - self.state[idx].last_hint_ns < NS {
            return;
        }
        self.state[idx].last_hint_ns = recv_ns;
        let token = if gap > 0.0 { &w.yes_token } else { &w.no_token };
        let book = self.books.get(token);
        self.hints.push(PresignHint {
            asset: w.asset.clone(),
            token: token.clone(),
            best_bid: book.and_then(Book::best_bid),
            best_ask: book.and_then(Book::best_ask),
        });
    }

    /// Whether `asset`'s window opening at `open_unix` is identified.
    pub fn has_window(&self, asset: &str, open_unix: i64) -> bool {
        self.asset_index(asset).is_some_and(|i| self.windows[i].contains_key(&open_unix))
    }

    pub fn window(&self, asset: &str, open_unix: i64) -> Option<&Window> {
        self.asset_index(asset).and_then(|i| self.windows[i].get(&open_unix))
    }

    /// Every identified window, all assets.
    pub fn all_windows(&self) -> impl Iterator<Item = &Window> {
        self.windows.iter().flat_map(|w| w.values())
    }

    pub fn resolution_known(&self, asset: &str, open_unix: i64, venue: &str) -> bool {
        self.resolutions
            .get(&(asset.to_owned(), open_unix))
            .is_some_and(|s| s.iter().any(|(v, _)| v == venue))
    }

    pub fn markouts_open(&self) -> usize {
        self.markouts.len()
    }

    pub(crate) fn count_invalid_legs(&mut self, w: &Window) {
        let since = Some(w.open_unix * NS);
        let keys: Vec<String> = w
            .ids()
            .iter()
            .enumerate()
            .filter_map(|(leg, id)| {
                let state = match self.books.get(*id) {
                    None => "no_book",
                    Some(b) => match b.state(since) {
                        BookState::Ok => return None,
                        s => s.as_str(),
                    },
                };
                Some(invalid_key(leg, state))
            })
            .collect();
        for k in keys {
            self.count(&k);
        }
    }

    fn submit(&mut self, w: &Window, d: &Decision, recv_ns: i64) -> Option<String> {
        let Some(venue) = self.venue else { return Some("mode_off".into()) };
        let entry = d.entry_px.expect("an entry has a price");
        let est =
            d.contracts as f64 * entry + taker_fee(entry, d.contracts as f64, w.fee_rate, w.fee_exponent);
        let blocked = self.risk.check(&self.ledger, est, recv_ns);
        if blocked == Some("insufficient_cash") {
            self.ledger.reject("insufficient_cash", &w.asset, round_dp(est, 2));
            let lines = vec![
                format!("cash={:.2} needed={est:.2}", self.ledger.cash),
                "P&L will flat-line from here".to_owned(),
            ];
            if let Some(alert) = self.alert.as_mut() {
                alert("insufficient_cash", "poly-kalshi: paper entry rejected, insufficient cash", &lines);
            }
        }
        if let Some(b) = blocked {
            self.count(&format!("blocked_{b}"));
            return Some(b.to_owned());
        }
        let side = side_str(d.buy_yes);
        let token = if d.buy_yes == Some(true) { w.yes_token.clone() } else { w.no_token.clone() };
        if self.emit_intents {
            let book = self.books.get(&token);
            self.intents.push(OrderIntent {
                best_bid: book.and_then(Book::best_bid),
                best_ask: book.and_then(Book::best_ask),
                asset: w.asset.clone(),
                open_unix: w.open_unix,
                token: token.clone(),
                side,
                entry_px: entry,
                limit_px: self.limit_px(entry),
                contracts: d.contracts,
                decision_ns: recv_ns,
            });
        }
        self.pending.push(Pending {
            due_ns: recv_ns + venue.latency_ns,
            window: w.clone(),
            token,
            side,
            decision: *d,
            decision_ns: recv_ns,
        });
        None
    }

    pub fn limit_px(&self, entry_px: f64) -> f64 {
        round_dp((entry_px + self.limit_offset).min(self.params.max_entry_px), 4)
    }

    fn resolve_pending(&mut self, recv_ns: i64) {
        if !self.pending.iter().any(|p| p.due_ns <= recv_ns) {
            return;
        }
        let (due, keep): (Vec<Pending>, Vec<Pending>) =
            std::mem::take(&mut self.pending).into_iter().partition(|p| p.due_ns <= recv_ns);
        self.pending = keep;
        for order in due {
            self.fill(order, recv_ns);
        }
    }

    fn fill(&mut self, order: Pending, recv_ns: i64) {
        if self.reverse {
            return self.fill_reverse(order, recv_ns);
        }
        let venue = self.venue.expect("orders exist only with a venue");
        let (d, w) = (&order.decision, &order.window);
        let entry = d.entry_px.expect("an entry has a price");
        let empty = Book::default();
        let book = self.books.get(&order.token).unwrap_or(&empty);
        let limit = self.limit_px(entry);
        let depth = book.depth_upto(limit);
        let result = venue.fill(book, limit, d.contracts, w.fee_rate, w.fee_exponent);
        let touch_fee = taker_fee(entry, d.contracts as f64, w.fee_rate, w.fee_exponent);
        let record = json!({
            "type": "fill",
            "asset": w.asset,
            "open_unix": w.open_unix,
            "t_ns": recv_ns,
            "latency_ms": round_dp((recv_ns - order.decision_ns) as f64 / 1e6, 1),
            "side": order.side,
            "limit_px": limit,
            "ordered": d.contracts,
            "shares": result.shares,
            "vwap": round_opt(result.vwap(), 4),
            "cost": round_dp(result.cost, 4),
            "fee": round_dp(result.fee, 4),
            "levels": result.levels,
            "best_ask_at_fill": result.best_ask_at_fill,
            "touch_px": entry,
            "touch_shares": d.contracts,
            "touch_fee": round_dp(touch_fee, 4),
            "depth_at_limit": round_dp(depth, 2),
        });
        self.count(if result.shares > 0.0 { "fills" } else { "no_fills" });
        if self.observe {
            self.add_markout(recv_ns, w, order.side, result.shares > 0.0, result.vwap().unwrap_or(limit));
        }
        if result.shares > 0.0 {
            let position = Position {
                asset: w.asset.clone(),
                open_unix: w.open_unix,
                side: order.side.into(),
                shares: result.shares,
                cost: result.cost,
                fee: result.fee,
                entry_ns: recv_ns,
                status: "open".into(),
                outcome: None,
                payout: None,
                pnl: None,
                settled_ns: None,
            };
            if let Err(e) = self.ledger.buy(position) {
                tracing::error!(error = %e, "ledger buy");
                self.count("engine_errors");
            }
            let known = self
                .resolutions
                .get(&(w.asset.clone(), w.open_unix))
                .and_then(|seen| seen.iter().find(|(v, _)| v == "poly").map(|(_, o)| o.clone()));
            if let Some(outcome) = known {
                self.settle(&w.asset.clone(), w.open_unix, &outcome, recv_ns);
            }
        }
        self.write(recv_ns, record, true);
    }

    fn add_markout(&mut self, fill_ns: i64, w: &Window, side: &str, filled: bool, ref_px: f64) {
        if self.reverse {
            return; // markouts would read the Polymarket leg
        }
        let token = if side == "yes" { w.yes_token.clone() } else { w.no_token.clone() };
        let mut record = Map::new();
        record.insert("type".into(), "markout".into());
        record.insert("asset".into(), w.asset.clone().into());
        record.insert("open_unix".into(), w.open_unix.into());
        record.insert("side".into(), side.into());
        record.insert("filled".into(), filled.into());
        record.insert("ref_px".into(), round_dp(ref_px, 4).into());
        record.insert("exit_gap_s".into(), Value::Null);
        record.insert("exit_gap_bid".into(), Value::Null);
        record.insert("exit_gap_bid_size".into(), Value::Null);
        self.markouts.push(Markout {
            fill_ns,
            token,
            ref_px,
            record,
            window: Some(w.clone()),
            next: 0,
            gap_closed: false,
        });
    }

    /// The observer's side of a trader `fill` record: start its markout. Its
    /// first gap-closure check is one line later than in the `full` engine.
    pub fn note_fill(&mut self, record: &Value) {
        if !self.observe || record.get("type").and_then(Value::as_str) != Some("fill") {
            return;
        }
        let asset = record.get("asset").and_then(Value::as_str).unwrap_or_default();
        let Some(open) = record.get("open_unix").and_then(Value::as_i64) else { return };
        let Some(idx) = self.asset_index(asset) else { return };
        let Some(w) = self.windows[idx].get(&open).cloned() else { return };
        let r = record
            .get("vwap")
            .and_then(Value::as_f64)
            .or_else(|| record.get("limit_px").and_then(Value::as_f64));
        let (Some(ref_px), Some(t_ns)) = (r, record.get("t_ns").and_then(Value::as_i64)) else { return };
        let side = if record.get("side").and_then(Value::as_str) == Some("yes") { "yes" } else { "no" };
        let filled = record.get("shares").and_then(Value::as_f64).is_some_and(|s| s > 0.0);
        self.add_markout(t_ns, &w, side, filled, ref_px);
    }

    /// At the first line past each horizon, read the cheap leg's book as it
    /// stood. Markout (¢/share) = 100 × (mid − price paid). At each exit horizon
    /// the held leg's best bid and size; until it happens, whether the gap has
    /// closed to `EXIT_GAP`. Both are read before the line is applied.
    fn resolve_markouts(&mut self, recv_ns: i64) {
        let mut markouts = std::mem::take(&mut self.markouts);
        let mut done = Vec::new();
        markouts.retain_mut(|m| {
            if !m.gap_closed && m.window.is_some() {
                self.check_gap_close(m, recv_ns);
            }
            while m.next < HORIZONS.len() && recv_ns >= m.fill_ns + HORIZONS[m.next] * NS {
                let h = HORIZONS[m.next];
                m.next += 1;
                let book = self.books.get(&m.token);
                let q = book.map(|b| b.quote(None));
                if MARKOUT_S.contains(&h) {
                    let ask = q.filter(|q| q.valid).and_then(|q| q.ask);
                    let mid = q.and_then(|q| q.mid());
                    m.record.insert(format!("ask_{h}s"), ask.into());
                    m.record.insert(format!("mid_{h}s"), round_opt(mid, 4).into());
                    m.record.insert(
                        format!("markout_{h}s"),
                        mid.map(|mid| round_dp(100.0 * (mid - m.ref_px), 2)).into(),
                    );
                }
                if EXIT_S.contains(&h) {
                    let bid = q.filter(|q| q.valid).and_then(|q| q.bid);
                    m.record.insert(format!("bid_{h}s"), bid.into());
                    m.record.insert(
                        format!("bid_size_{h}s"),
                        bid.and_then(|b| book.and_then(|bk| bk.bid_size_at(b))).into(),
                    );
                }
            }
            if m.next < HORIZONS.len() {
                true
            } else {
                done.push(std::mem::take(&mut m.record));
                false
            }
        });
        self.markouts = markouts;
        for record in done {
            self.write(recv_ns, Value::Object(record), false);
        }
    }

    fn check_gap_close(&self, m: &mut Markout, recv_ns: i64) {
        let Some(w) = &m.window else { return };
        let since = Some(w.open_unix * NS);
        let (Some(kb), Some(yb)) = (self.books.get(&w.kalshi_ticker), self.books.get(&w.yes_token)) else {
            return;
        };
        let (Some(km), Some(ym)) = (kb.quote(since).mid(), yb.quote(since).mid()) else { return };
        if (km - ym).abs() > EXIT_GAP + 1e-9 {
            return;
        }
        m.gap_closed = true;
        let book = self.books.get(&m.token);
        let bid = book.map(|b| b.quote(None)).filter(|q| q.valid).and_then(|q| q.bid);
        m.record.insert("exit_gap_s".into(), round_dp((recv_ns - m.fill_ns) as f64 / 1e9, 3).into());
        m.record.insert("exit_gap_bid".into(), bid.into());
        m.record.insert(
            "exit_gap_bid_size".into(),
            bid.and_then(|b| book.and_then(|bk| bk.bid_size_at(b))).into(),
        );
    }

    // --- settlement -------------------------------------------------------------

    fn resolution(&mut self, asset: &str, open_unix: i64, venue: &str, outcome: &str, recv_ns: i64) {
        if outcome != "yes" && outcome != "no" {
            return;
        }
        let seen = self.resolutions.entry((asset.to_owned(), open_unix)).or_default();
        if seen.iter().any(|(v, _)| v == venue) {
            return;
        }
        seen.push((venue.to_owned(), outcome.to_owned()));
        if self.observe {
            let delay_s = round_dp(secs_f64(recv_ns) - (open_unix + WINDOW_S) as f64, 1);
            match venue {
                "poly" => self.metrics.add("resolution_s_poly", delay_s),
                "kalshi" => self.metrics.add("resolution_s_kalshi", delay_s),
                _ => self.metrics.add("resolution_s_other", delay_s),
            }
            let record = json!({
                "type": "resolution",
                "asset": asset,
                "open_unix": open_unix,
                "venue": venue,
                "outcome": outcome,
                "delay_s": delay_s,
            });
            self.write(recv_ns, record, false);
            if venue == "poly" && !self.reverse {
                for record in self.shadow.settle(&(asset.to_owned(), open_unix), outcome) {
                    self.write(recv_ns, record, false);
                }
            }
        }
        let settles_on = if self.reverse { "kalshi" } else { "poly" };
        if venue == settles_on && self.trade {
            self.settle(asset, open_unix, outcome, recv_ns);
        }
    }

    pub(crate) fn settle(&mut self, asset: &str, open_unix: i64, outcome: &str, recv_ns: i64) {
        let Some(p) = self.ledger.settle(asset, open_unix, outcome, recv_ns) else { return };
        self.count("settled");
        let record = json!({
            "type": "settle",
            "asset": asset,
            "open_unix": open_unix,
            "t_ns": recv_ns,
            "side": p.side,
            "outcome": outcome,
            "shares": p.shares,
            "vwap": round_dp(p.vwap(), 4),
            "payout": p.payout,
            "pnl": round_dp(p.pnl.unwrap_or(0.0), 4),
            "cash": round_dp(self.ledger.cash, 2),
        });
        self.write(recv_ns, record, true);
    }

    // --- output -----------------------------------------------------------------

    pub(crate) fn write(&mut self, recv_ns: i64, mut record: Value, keep: bool) {
        if !self.stamp.is_empty()
            && let Some(obj) = record.as_object_mut()
        {
            for (k, v) in &self.stamp {
                obj.insert(k.clone(), v.clone());
            }
        }
        self.journal.write(recv_ns, &record);
        if keep {
            if self.recent.len() == 200 {
                self.recent.pop_front();
            }
            self.recent.push_back(record);
        }
    }

    pub fn status(&self) -> Value {
        let now = self.last_ns;
        let mut assets = Map::new();
        for (idx, asset) in self.assets.iter().enumerate() {
            let st = &self.state[idx];
            let quotes = self.quotes(idx);
            let sec_into = st.open_unix.map(|o| secs_f64(now) - o as f64);
            let mut gap = None;
            let mut preview = Value::Null;
            let mut ages = Map::new();
            let mut legs = Map::new();
            if let (Some((k, y, n)), Some(w), Some(sec)) = (quotes, &st.window, sec_into) {
                if let (Some(km), Some(ym)) = (k.mid(), y.mid()) {
                    gap = Some(round_dp(km - ym, 4));
                }
                // What the rule would do with the books as they stand.
                let d = decide(&k, &y, &n, &self.params, sec, false, None, None);
                preview = json!({
                    "action": d.action.as_str(),
                    "reason": d.reason,
                    "side": d.buy_yes.map(|b| if b { "yes" } else { "no" }),
                    "entry_px": d.entry_px,
                });
                let since = Some(w.open_unix * NS);
                for (leg, id) in ["kalshi", "poly_yes", "poly_no"].iter().zip(w.ids()) {
                    let book = self.books.get(id);
                    ages.insert(
                        (*leg).into(),
                        book.and_then(|b| b.last_update_ns)
                            .map(|t| round_dp((now - t) as f64 / 1e6, 0))
                            .into(),
                    );
                    legs.insert(
                        (*leg).into(),
                        match book {
                            None => json!({"state": "no_book", "bid": null, "ask": null}),
                            Some(b) => json!({"state": b.state(since).as_str(), "bid": b.best_bid(), "ask": b.best_ask()}),
                        },
                    );
                }
            }
            let decided = st.decision.as_ref();
            let pick = |k: &str| decided.and_then(|d| d.get(k)).cloned().unwrap_or(Value::Null);
            assets.insert(
                asset.clone(),
                json!({
                    "open_unix": st.open_unix,
                    "identified": st.window.is_some(),
                    "sec_into": sec_into.map(|s| round_dp(s, 1)),
                    "traded": st.traded,
                    "action": pick("action"),
                    "decision": decided.map(|_| json!({
                        "action": pick("action"), "side": pick("side"), "entry_px": pick("entry_px"),
                        "sec_into": pick("sec_into"), "blocked": pick("blocked"), "gap": pick("gap"),
                    })),
                    "gap": gap,
                    "last_gap": round_opt(st.last_gap, 4),
                    "max_abs_gap": round_dp(st.max_abs_gap, 4),
                    "evals": st.evals,
                    "preview": preview,
                    "age_ms": ages,
                    "legs": legs,
                    "kalshi": quotes.map(|q| quote_json(&q.0)),
                    "poly_yes": quotes.map(|q| quote_json(&q.1)),
                    "poly_no": quotes.map(|q| quote_json(&q.2)),
                }),
            );
        }
        let feed_age: Map<String, Value> = self
            .last_by_venue
            .iter()
            .map(|(v, t)| (v.to_string(), round_dp((now - t) as f64 / 1e9, 1).into()))
            .collect();
        json!({
            "now_ns": now,
            "feed_age_s": feed_age,
            "assets": assets,
            "books": self.books.len(),
            "pending": self.pending.len(),
            "markouts": self.markouts.len(),
            "counters": self.counters,
            "latency": self.metrics.snapshot(),
            "ledger": (now != 0).then(|| self.ledger.snapshot(now)),
            "risk": self.risk.snapshot(),
        })
    }
}
