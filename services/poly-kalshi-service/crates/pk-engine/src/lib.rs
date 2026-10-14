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
//!   process: markouts, the per-second series, the uncensored gap, BBO checks, feed latency, `window` and `resolution` records. It
//!   decides the live rule too, so a window is consumed as the trader consumed
//!   it, but it never orders. Markouts start from the trader's fills
//!   ([`Engine::note_fill`]).
//! - `Full`: both in one object (replay, tests), the reference the split is
//!   checked against.
//!
//! **Live.** With `EngineConfig::live`, an entry is an [`OrderIntent`] for the
//! real order path and its cost is reserved in the ledger. What the order path
//! learns comes back as control lines, so the tape replays the same
//! ledger: `live_fill` and `live_exit` (positions), `balance` (the wallet, and
//! the sizing check when flat), `latency` (the paper venue's measured latency)
//! and `sizing` (the size's seed).
//!
//! **Active exit** (`EngineConfig::active_exit`, [`pk_core::exit`]). A held
//! leg is sold once Polymarket's bid reaches Kalshi's price: live, as an
//! [`ExitIntent`] for the order path (its result comes back as `live_exit`);
//! paper, at the bid at once. A position closed this way writes a `settle`
//! record with `outcome: "exit"`, so results count it like any other.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::Arc;

use pk_core::decision::{Action, Decision, StrategyParams, WINDOW_S, decide, net_edge_max_px};
use pk_core::exit::{EXIT_MIN_SHARES, exit_floor, kalshi_fair, should_exit};
use pk_core::fees::taker_fee;
use pk_core::journal::Sink;
use pk_core::ledger::{Ledger, Position};
use pk_core::metrics::Metrics;
use pk_core::num::{lag_ms, px_key, round_dp, round_opt};
use pk_core::risk::RiskState;
use pk_core::sizing::Sizer;
use pk_core::window::{Window, secs_f64, window_open};
use pk_core::{Book, BookSide, BookState, NS, Quote, quote_json};
use serde_json::{Map, Value, json};

pub mod fast;
pub mod paper;
pub mod parse;
pub mod reverse;

pub use fast::{Arm, ArmSide, ArmUpdate, Claim, FastFire, FastGate};
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
/// A live exit signal is not repeated for this long (the order is in flight).
pub const EXIT_RETRY_NS: i64 = 5 * NS;
/// After an exit's FAK sold nothing (`exit_miss`) the next signal may fire at
/// once; after a balance reject (shares not credited yet) a second later. Past
/// this many misses a position waits [`EXIT_RETRY_NS`] between tries.
pub const EXIT_MAX_ATTEMPTS: u32 = 8;
pub const EXIT_BALANCE_RETRY_NS: i64 = NS;

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
    pub condition_id: String,
    pub fee_rate: f64,
    pub fee_exponent: f64,
    /// The cost reserved for it (shares × entry + the taker fee).
    pub reserved: f64,
    /// Live: a real order, not a probe.
    pub live: bool,
}

/// A held leg to sell now (the active exit): a FAK at the bid on the order path.
#[derive(Clone, Debug, PartialEq)]
pub struct ExitIntent {
    pub asset: String,
    pub open_unix: i64,
    pub token: String,
    pub side: String,
    pub shares: f64,
    pub bid: f64,
    pub fair: f64,
    /// The lowest price the sell may take: entry, fee and a cent ([`exit_floor`]).
    pub min_px: f64,
    pub decision_ns: i64,
    pub fee_rate: f64,
    pub fee_exponent: f64,
}

/// An asset's gap is nearing the threshold: sign an order for this leg ahead.
#[derive(Clone, Debug, PartialEq)]
pub struct PresignHint {
    pub asset: String,
    pub token: String,
    pub best_bid: Option<f64>,
    pub best_ask: Option<f64>,
    /// The entry a decision at this ask would send: its limit and size.
    pub limit_px: Option<f64>,
    pub contracts: Option<i64>,
}

/// Hints start at this fraction of the threshold, at most one per asset per second.
pub const PRESIGN_FRACTION: f64 = 0.75;

pub type Alert = Box<dyn FnMut(&str, &str, &[String]) + Send>;

pub struct EngineConfig {
    pub params: StrategyParams,
    pub assets: Vec<String>,
    pub role: Role,
    pub venue: Option<PaperVenue>,
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
    /// Real orders: entries become intents; the paper venue simulates nothing.
    pub live: bool,
    /// Order size as a percent of the balance (0 = `params.notional_usd`).
    pub order_size_pct: f64,
    /// Apply `latency` control lines to the paper venue (the trader, and a
    /// replay that should reproduce it). Off: the configured latency holds.
    pub follow_tape_latency: bool,
    /// Sell a held leg once Polymarket's bid reaches Kalshi's price.
    pub active_exit: bool,
    /// Size an entry at its limit price (what a FAK can spend), not the touch.
    pub size_at_limit: bool,
    /// Every entry orders this many shares (`PK_ORDER_SHARES`); `None` sizes
    /// from the notional.
    pub order_shares: Option<i64>,
    /// How far under the bid the active exit sells (cents).
    pub active_exit_ticks: u32,
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
            limit_offset: 0.0,
            size_to_depth: false,
            stamp: Map::new(),
            age_outlier_ms: None,
            emit_intents: false,
            direction: Direction::Forward,
            live: false,
            order_size_pct: 0.0,
            follow_tape_latency: false,
            active_exit: false,
            size_at_limit: false,
            order_shares: None,
            active_exit_ticks: 0,
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
    pub live: bool,
    pub sizer: Sizer,
    follow_tape_latency: bool,
    pub intents: Vec<OrderIntent>,
    pub hints: Vec<PresignHint>,
    pub exits: Vec<ExitIntent>,
    active_exit: bool,
    size_at_limit: bool,
    exit_until: HashMap<String, i64>,
    exit_misses: HashMap<String, u32>,
    order_shares: Option<i64>,
    /// The venue of the frame being applied ('k', 'p' or 'c').
    frame_venue: char,
    /// Exit FAKs sent and not yet answered (`live_exit` / `exit_miss`), by window key.
    exits_in_flight: HashSet<String>,
    /// A wallet balance that disagreed with the ledger once; applied when the next read agrees.
    unconfirmed_balance: Option<f64>,
    /// A wallet read has been applied (the first one always is).
    balance_seen: bool,
    /// The armed trigger (live trade engine only), and what it was last given per asset.
    fast_gate: Option<Arc<dyn FastGate>>,
    pub arms: Vec<ArmUpdate>,
    armed: Vec<Option<Arm>>,
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
        let notional = cfg.params.notional_usd;
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
            emit_intents: cfg.emit_intents || cfg.live,
            reverse: cfg.direction == Direction::Reverse,
            live: cfg.live,
            follow_tape_latency: cfg.follow_tape_latency,
            sizer: Sizer::new(cfg.order_size_pct, notional),
            intents: Vec::new(),
            hints: Vec::new(),
            exits: Vec::new(),
            active_exit: cfg.active_exit,
            size_at_limit: cfg.size_at_limit,
            exit_until: HashMap::new(),
            exit_misses: HashMap::new(),
            order_shares: cfg.order_shares,
            frame_venue: 'c',
            exits_in_flight: HashSet::new(),
            unconfirmed_balance: None,
            balance_seen: false,
            fast_gate: None,
            arms: Vec::new(),
            armed: vec![None; n],
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

    /// Arm the fast path ([`fast`]): live trade engines only.
    pub fn set_fast_gate(&mut self, gate: Arc<dyn FastGate>) {
        if self.live && self.trade {
            self.fast_gate = Some(gate);
        }
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
        self.frame_venue = venue;
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
            Some(kind @ ("latency" | "sizing" | "live_fill" | "live_exit" | "exit_miss" | "balance")) => {
                if self.trade {
                    let kind = kind.to_owned();
                    match serde_json::from_str::<Value>(text) {
                        Ok(v) => self.order_path_line(&kind, &v, recv_ns),
                        Err(_) => return None,
                    }
                }
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

    /// The current window's open, Kalshi YES mid and Polymarket YES mid (the
    /// observer's lead–lag monitor).
    pub fn mids(&self, idx: usize) -> Option<(i64, Option<f64>, Option<f64>)> {
        let open = self.state.get(idx)?.window.as_ref()?.open_unix;
        let (k, y, _) = self.quotes(idx)?;
        Some((open, k.mid(), y.mid()))
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
        }
        if self.trade && self.active_exit {
            self.maybe_exit(&window, &k, &y, &n, recv_ns);
        }
        if self.state[idx].traded {
            return;
        }
        // The armed trigger may have sent this window's entry already.
        let fast_fire = match &self.fast_gate {
            Some(g) if self.trade => g.fired(&window.asset, window.open_unix),
            _ => None,
        };
        let lags = self.params.max_poly_lag_ms.map(|_| self.lags(&window));
        let mut decision = match &fast_fire {
            Some(f) => Decision {
                action: Action::Enter,
                reason: "",
                gap: Some(f.gap),
                buy_yes: Some(f.buy_yes),
                entry_px: Some(f.entry_px),
                contracts: f.contracts,
                max_px: Some(f.limit_px),
            },
            None => decide(&k, &y, &n, &self.params, sec_into, false, None, lags, (window.fee_rate, window.fee_exponent)),
        };
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
                self.maybe_hint(idx, &window, y.mid(), &decision, recv_ns);
                self.maybe_arm(idx, &window, (&y, &n), &decision, recv_ns);
            }
            return;
        }
        self.state[idx].traded = true;
        self.disarm(idx);
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
        if decision.action == Action::Enter && fast_fire.is_none() {
            decision.contracts =
                self.contracts_for(decision.entry_px.expect("an entry has a price"), decision.max_px).max(1);
        }
        if decision.action == Action::Enter && self.size_to_depth && fast_fire.is_none() {
            let entry = decision.entry_px.expect("an entry has a price");
            let offered = self.depth(&window, decision.buy_yes == Some(true), self.limit_px(entry, decision.max_px));
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
        if let Some(f) = &fast_fire {
            record.insert("fast".into(), true.into());
            record.insert("fast_t_ns".into(), f.t_ns.into());
            record.insert("limit_px".into(), f.limit_px.into());
        }
        if decision.action == Action::Enter {
            let blocked = self.submit(&window, &decision, recv_ns, fast_fire.is_some());
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

    fn maybe_hint(&mut self, idx: usize, w: &Window, y_mid: Option<f64>, d: &Decision, recv_ns: i64) {
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
        let ask = book.and_then(Book::best_ask);
        let max_px = self.cross_max_px(w, y_mid, gap > 0.0);
        self.hints.push(PresignHint {
            asset: w.asset.clone(),
            token: token.clone(),
            best_bid: book.and_then(Book::best_bid),
            best_ask: ask,
            limit_px: ask.map(|a| self.limit_px(a, max_px)),
            contracts: ask.filter(|a| *a > 0.0).map(|a| self.contracts_for(a, max_px)),
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

    /// `fast`: the armed trigger already sent this entry; reserve it,
    /// but do not send it again. The risk check applies to it as to any entry
    /// (the trigger is only armed while it passes, and is disarmed on a halt).
    fn submit(&mut self, w: &Window, d: &Decision, recv_ns: i64, fast: bool) -> Option<String> {
        let Some(venue) = self.venue else { return Some("mode_off".into()) };
        let entry = d.entry_px.expect("an entry has a price");
        // The most the order can cost: at its limit when sized there.
        let worst = if self.size_at_limit { self.limit_px(entry, d.max_px) } else { entry };
        let est =
            d.contracts as f64 * worst + taker_fee(worst, d.contracts as f64, w.fee_rate, w.fee_exponent);
        let blocked = self.risk.check(&self.ledger, est, recv_ns);
        if blocked == Some("insufficient_cash") {
            self.ledger.reject("insufficient_cash", &w.asset, round_dp(est, 2));
            let lines = vec![
                format!("cash={:.2} needed={est:.2}", self.ledger.cash),
                if self.live {
                    "the wallet's last reconciled balance; fund it or lower PK_ORDER_SIZE_PCT".to_owned()
                } else {
                    "P&L will flat-line from here".to_owned()
                },
            ];
            let subject = if self.live {
                "poly-kalshi: LIVE entry rejected, insufficient balance"
            } else {
                "poly-kalshi: paper entry rejected, insufficient cash"
            };
            if let Some(alert) = self.alert.as_mut() {
                alert("insufficient_cash", subject, &lines);
            }
        }
        if let Some(b) = blocked {
            self.count(&format!("blocked_{b}"));
            return Some(b.to_owned());
        }
        let side = side_str(d.buy_yes);
        let token = if d.buy_yes == Some(true) { w.yes_token.clone() } else { w.no_token.clone() };
        if self.live {
            self.ledger.reserve(&format!("{}:{}", w.asset, w.open_unix), est);
            self.count("live_orders");
        }
        // One order per window: claim it, unless the trigger already did.
        let send = !fast
            && match &self.fast_gate {
                Some(g) => match g.claim(&w.asset, w.open_unix) {
                    Claim::Engine => true,
                    Claim::Fast(_) => {
                        self.count("fast_raced");
                        false
                    }
                },
                None => true,
            };
        if fast {
            self.count("fast_entries");
        }
        if self.emit_intents && send {
            let book = self.books.get(&token);
            self.intents.push(OrderIntent {
                best_bid: book.and_then(Book::best_bid),
                best_ask: book.and_then(Book::best_ask),
                asset: w.asset.clone(),
                open_unix: w.open_unix,
                token: token.clone(),
                side,
                entry_px: entry,
                limit_px: self.limit_px(entry, d.max_px),
                contracts: d.contracts,
                decision_ns: recv_ns,
                condition_id: w.condition_id.clone(),
                fee_rate: w.fee_rate,
                fee_exponent: w.fee_exponent,
                reserved: round_dp(est, 4),
                live: self.live,
            });
        }
        if !self.live {
            self.pending.push(Pending {
                due_ns: recv_ns + venue.latency_ns,
                window: w.clone(),
                token,
                side,
                decision: *d,
                decision_ns: recv_ns,
            });
        }
        None
    }

    /// The active exit for the window's open position, if Polymarket caught up.
    fn maybe_exit(&mut self, w: &Window, k: &Quote, y: &Quote, n: &Quote, recv_ns: i64) {
        let key = format!("{}:{}", w.asset, w.open_unix);
        let Some(p) = self.ledger.positions.get(&key).filter(|p| p.is_open() && p.shares >= EXIT_MIN_SHARES)
        else {
            return;
        };
        let yes = p.side == "yes";
        let (Some(bid), Some(fair)) = (if yes { y.bid } else { n.bid }, kalshi_fair(k, yes)) else { return };
        let (shares, vwap, side) = (p.shares, p.vwap(), p.side.clone());
        if !should_exit(bid, fair, vwap) || self.exit_until.get(&key).is_some_and(|u| recv_ns < *u) {
            return;
        }
        self.exit_until.insert(key.clone(), recv_ns + EXIT_RETRY_NS);
        self.count("exit_signals");
        let min_px = exit_floor(vwap, w.fee_rate, w.fee_exponent);
        if self.live {
            self.exits_in_flight.insert(key);
            self.exits.push(ExitIntent {
                asset: w.asset.clone(),
                open_unix: w.open_unix,
                token: if yes { w.yes_token.clone() } else { w.no_token.clone() },
                side: side.clone(),
                shares,
                bid,
                fair: round_dp(fair, 4),
                min_px,
                decision_ns: recv_ns,
                fee_rate: w.fee_rate,
                fee_exponent: w.fee_exponent,
            });
            let record = json!({
                "type": "exit_signal", "asset": w.asset, "open_unix": w.open_unix, "t_ns": recv_ns,
                "side": side, "shares": shares, "bid": bid, "fair": round_dp(fair, 4),
                "entry_vwap": round_dp(vwap, 4), "min_px": min_px,
            });
            self.write(recv_ns, record, false);
        } else {
            let fee = taker_fee(bid, shares, w.fee_rate, w.fee_exponent);
            self.close_position(&w.asset, w.open_unix, (shares, shares * bid, fee), "fair", None, recv_ns);
        }
    }

    /// Sell `shares` of a position for `proceeds` less `fee`: an `exit` record,
    /// and a `settle` record (`outcome: "exit"`) when nothing is left.
    fn close_position(
        &mut self,
        asset: &str,
        open: i64,
        (shares, proceeds, fee): (f64, f64, f64),
        reason: &str,
        order_id: Option<Value>,
        recv_ns: i64,
    ) {
        let before = self.ledger.positions.get(&format!("{asset}:{open}")).map(|p| (p.vwap(), p.entry_ns));
        let Some(p) = self.ledger.close(asset, open, shares, proceeds, fee, recv_ns) else { return };
        let (entry_vwap, entry_ns) = before.unwrap_or((0.0, recv_ns));
        self.count("exits");
        let record = json!({
            "type": "exit", "asset": asset, "open_unix": open, "t_ns": recv_ns, "reason": reason,
            "venue": if self.live { "live" } else { "paper" },
            "side": p.side, "shares": shares, "proceeds": round_dp(proceeds, 4),
            "exit_px": (shares > 0.0).then(|| round_dp(proceeds / shares, 4)),
            "entry_vwap": round_dp(entry_vwap, 4),
            "held_s": round_dp((recv_ns - entry_ns) as f64 / 1e9, 1),
            "fee": round_dp(fee, 4), "remaining": p.shares, "pnl": round_opt(p.pnl, 4),
            "order_id": order_id, "cash": round_dp(self.ledger.cash, 2),
        });
        self.write(recv_ns, record, true);
        if p.is_open() {
            return;
        }
        let record = json!({
            "type": "settle", "asset": asset, "open_unix": open, "t_ns": recv_ns,
            "side": p.side, "outcome": "exit", "shares": 0.0, "vwap": round_dp(entry_vwap, 4),
            "payout": p.payout, "pnl": round_dp(p.pnl.unwrap_or(0.0), 4),
            "cash": round_dp(self.ledger.cash, 2),
        });
        self.write(recv_ns, record, true);
        if !self.live {
            self.resize(self.ledger.cash, recv_ns, "settle");
        }
    }

    /// Arm the trigger for an asset whose gap nears the threshold, at the
    /// Polymarket quotes now; disarm it otherwise. Armed only while the risk
    /// check passes for the larger side; sent again only on a change.
    fn maybe_arm(&mut self, idx: usize, w: &Window, (y, n): (&Quote, &Quote), d: &Decision, recv_ns: i64) {
        if self.fast_gate.is_none() {
            return;
        }
        let near = d.reason == "below_threshold"
            && d.gap.is_some_and(|g| g.abs() >= self.params.gap_threshold * PRESIGN_FRACTION);
        if !near || self.risk.halted {
            self.disarm(idx);
            return;
        }
        let side = |q: &Quote, token: &str, buy_yes: bool| -> Option<ArmSide> {
            let entry = q.ask.filter(|a| q.valid && *a > 0.0 && *a < 1.0 && *a <= self.params.max_entry_px)?;
            let max_px = self.cross_max_px(w, y.mid(), buy_yes);
            if max_px.is_some_and(|m| entry > m + 1e-9) {
                return None;
            }
            let limit = self.limit_px(entry, max_px);
            let px = if self.size_at_limit { limit } else { entry };
            let contracts = self.contracts_for(entry, max_px);
            (contracts >= 1).then(|| ArmSide {
                token: token.to_owned(),
                entry_px: entry,
                limit_px: limit,
                contracts,
                reserved: round_dp(
                    contracts as f64 * px + taker_fee(px, contracts as f64, w.fee_rate, w.fee_exponent),
                    4,
                ),
            })
        };
        let arm = Arm {
            asset: w.asset.clone(),
            open_unix: w.open_unix,
            kalshi_ticker: w.kalshi_ticker.clone(),
            condition_id: w.condition_id.clone(),
            poly_yes: *y,
            poly_no: *n,
            params: self.params.clone(),
            yes: side(y, &w.yes_token, true),
            no: side(n, &w.no_token, false),
            fee_rate: w.fee_rate,
            fee_exponent: w.fee_exponent,
        };
        let worst = arm.yes.iter().chain(&arm.no).map(|s| s.reserved).fold(0.0, f64::max);
        if worst <= 0.0 || self.risk.check(&self.ledger, worst, recv_ns).is_some() {
            self.disarm(idx);
            return;
        }
        if self.armed[idx].as_ref() == Some(&arm) {
            return;
        }
        if self.armed[idx].as_ref().is_some_and(|a| a.kalshi_ticker != arm.kalshi_ticker) {
            self.disarm(idx);
        }
        self.armed[idx] = Some(arm.clone());
        self.arms.push(ArmUpdate::Arm(arm));
    }

    fn disarm(&mut self, idx: usize) {
        if let Some(a) = self.armed.get_mut(idx).and_then(Option::take) {
            self.arms.push(ArmUpdate::Disarm { kalshi_ticker: a.kalshi_ticker });
        }
    }

    /// Disarm everything (a halt).
    pub fn disarm_all(&mut self) {
        for idx in 0..self.armed.len() {
            self.disarm(idx);
        }
    }

    /// Shares for an entry at `entry_px`: `order_shares` when set, else the
    /// notional at the limit (`size_at_limit`: what a FAK can spend) or the touch.
    pub fn contracts_for(&self, entry_px: f64, max_px: Option<f64>) -> i64 {
        if let Some(n) = self.order_shares {
            return n;
        }
        let px = if self.size_at_limit { self.limit_px(entry_px, max_px) } else { entry_px };
        (self.params.notional_usd / px).floor() as i64
    }

    /// The order's limit: the touch plus the offset, never above the cap nor
    /// the decision's net-edge price (`max_px`), never below the touch.
    pub fn limit_px(&self, entry_px: f64, max_px: Option<f64>) -> f64 {
        let limit = (entry_px + self.limit_offset).min(self.params.max_entry_px);
        round_dp(max_px.map_or(limit, |m| limit.min(m)).max(entry_px), 4)
    }

    /// The net-edge price of a crossing at the least Kalshi move that fires it
    /// (Polymarket's YES mid `y_mid` plus the threshold): what an order signed
    /// ahead of the crossing may pay. `None` without `min_net_edge`.
    fn cross_max_px(&self, w: &Window, y_mid: Option<f64>, buy_yes: bool) -> Option<f64> {
        let m = self.params.min_net_edge?;
        let y_mid = y_mid?;
        let fair = if buy_yes { y_mid } else { 1.0 - y_mid } + self.params.gap_threshold;
        Some(net_edge_max_px(fair.min(0.99), m, w.fee_rate, w.fee_exponent))
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
        let limit = self.limit_px(entry, d.max_px);
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
        }
        let settles_on = if self.reverse { "kalshi" } else { "poly" };
        if venue == settles_on && self.trade {
            self.settle(asset, open_unix, outcome, recv_ns);
        }
    }

    pub(crate) fn settle(&mut self, asset: &str, open_unix: i64, outcome: &str, recv_ns: i64) {
        self.exits_in_flight.remove(&format!("{asset}:{open_unix}"));
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
        if !self.live {
            // Paper's balance is the ledger's cash; live waits for the wallet's
            // balance after the redemption (`balance`).
            self.resize(self.ledger.cash, recv_ns, "settle");
        }
    }

    // --- the order path's results (control lines) ------------------------------------

    /// Recompute the order size if nothing is at risk.
    fn resize(&mut self, balance: f64, recv_ns: i64, source: &str) {
        let open = self.ledger.open_positions().count();
        let in_flight = self.ledger.in_flight() + self.pending.len() + self.exits_in_flight.len();
        if let Some(notional) = self.sizer.reconcile(balance, open, in_flight, recv_ns) {
            self.apply_size(notional, balance, recv_ns, source);
        }
    }

    fn apply_size(&mut self, notional: f64, balance: f64, recv_ns: i64, source: &str) {
        self.params.notional_usd = notional;
        self.count("resized");
        let record = json!({
            "type": "sizing",
            "t_ns": recv_ns,
            "source": source,
            "balance": round_dp(balance, 4),
            "pct": self.sizer.pct,
            "notional": notional,
        });
        self.write(recv_ns, record, true);
    }

    fn order_path_line(&mut self, kind: &str, v: &Value, recv_ns: i64) {
        let f = |k: &str| v.get(k).and_then(Value::as_f64);
        let s = |k: &str| v.get(k).and_then(Value::as_str).unwrap_or_default().to_owned();
        // A wallet's results apply only to a live engine: a paper replay of a
        // live tape simulates its own fills.
        if matches!(kind, "live_fill" | "live_exit" | "exit_miss" | "balance") && !self.live {
            return;
        }
        match kind {
            "latency" => {
                if !self.follow_tape_latency {
                    return;
                }
                let Some(ms) = f("ms").filter(|m| m.is_finite() && *m >= 0.0) else { return };
                if let Some(venue) = self.venue.as_mut() {
                    venue.latency_ns = (ms * 1e6).round() as i64;
                }
                let record = json!({
                    "type": "latency", "t_ns": recv_ns, "ms": round_dp(ms, 1),
                    "n": v.get("n"), "source": v.get("source"),
                });
                self.write(recv_ns, record, false);
            }
            "sizing" => {
                // The seed: set from this balance whatever is open.
                let Some(balance) = f("balance") else { return };
                if let Some(notional) = self.sizer.set(balance, recv_ns) {
                    self.apply_size(notional, balance, recv_ns, "seed");
                }
            }
            "balance" => {
                let Some(balance) = f("balance").filter(|b| b.is_finite()) else { return };
                let before = self.ledger.cash;
                // The wallet and the ledger see an order's cash at different
                // times: a read that holds an exit's proceeds or a redeemed payout
                // before its result arrives (or misses one just credited) would
                // count it twice. So drift waits while an order is in flight or a
                // closed window awaits its resolution, and until two reads agree.
                if (balance - before).abs() >= 0.005 && self.balance_seen {
                    let now_s = recv_ns.div_euclid(NS);
                    let busy = self.ledger.in_flight() > 0
                        || !self.exits_in_flight.is_empty()
                        || self.ledger.open_positions().any(|p| p.open_unix + WINDOW_S <= now_s);
                    let agreed = self.unconfirmed_balance.is_some_and(|b| (b - balance).abs() < 0.005);
                    if busy || !agreed {
                        self.unconfirmed_balance = (!busy).then_some(balance);
                        let record = json!({
                            "type": "reconcile", "t_ns": recv_ns, "balance": round_dp(balance, 4),
                            "ledger_cash": round_dp(before, 4), "drift": round_dp(balance - before, 4),
                            "skipped": if busy { "in_flight" } else { "unconfirmed" },
                            "open_positions": self.ledger.open_positions().count(),
                            "in_flight": self.ledger.in_flight(),
                        });
                        self.count("reconcile_skipped");
                        self.write(recv_ns, record, false);
                        return;
                    }
                }
                self.unconfirmed_balance = None;
                self.balance_seen = true;
                let drift = self.ledger.set_cash(balance, recv_ns);
                if drift.abs() >= 0.005 {
                    let record = json!({
                        "type": "reconcile", "t_ns": recv_ns, "balance": round_dp(balance, 4),
                        "ledger_cash": round_dp(before, 4), "drift": round_dp(drift, 4),
                        "open_positions": self.ledger.open_positions().count(),
                        "in_flight": self.ledger.in_flight(),
                    });
                    self.count("reconcile_drift");
                    self.write(recv_ns, record, false);
                }
                if self.sizer.basis_balance.is_none() {
                    let open_cost: f64 = self.ledger.open_positions().map(|p| p.cost + p.fee).sum();
                    if let Some(n) = self.sizer.set(balance + open_cost, recv_ns) {
                        self.apply_size(n, balance + open_cost, recv_ns, "seed");
                    }
                } else {
                    self.resize(balance, recv_ns, "reconcile");
                }
            }
            "live_fill" => self.live_fill(v, recv_ns),
            "exit_miss" => {
                let key = format!("{}:{}", s("asset"), v.get("open_unix").and_then(Value::as_i64).unwrap_or(0));
                let reason = s("reason");
                let misses = self.exit_misses.entry(key.clone()).or_insert(0);
                *misses += 1;
                let misses = *misses;
                self.exits_in_flight.remove(&key);
                let until = match reason.as_str() {
                    _ if misses >= EXIT_MAX_ATTEMPTS => recv_ns + EXIT_RETRY_NS,
                    "balance" => recv_ns + EXIT_BALANCE_RETRY_NS,
                    _ => recv_ns,
                };
                self.exit_until.insert(key, until);
                self.count("exit_misses");
                let record = json!({
                    "type": "exit_miss", "t_ns": recv_ns, "asset": s("asset"),
                    "open_unix": v.get("open_unix"), "reason": reason, "misses": misses,
                    "error": v.get("error"),
                });
                self.write(recv_ns, record, false);
            }
            "live_exit" => {
                let (asset, open) = (s("asset"), v.get("open_unix").and_then(Value::as_i64).unwrap_or(0));
                let (shares, proceeds, fee) =
                    (f("shares").unwrap_or(0.0), f("proceeds").unwrap_or(0.0), f("fee").unwrap_or(0.0));
                let reason = v.get("reason").and_then(Value::as_str).unwrap_or("flatten").to_owned();
                let order_id = v.get("order_id").cloned();
                self.exits_in_flight.remove(&format!("{asset}:{open}"));
                self.close_position(&asset, open, (shares, proceeds, fee), &reason, order_id, recv_ns);
            }
            _ => {}
        }
    }

    /// A real order's result: release its reservation, open the position, and
    /// write a `fill` record in the paper venue's schema (so the readout and
    /// the gates read live fills the same way).
    fn live_fill(&mut self, v: &Value, recv_ns: i64) {
        let f = |k: &str| v.get(k).and_then(Value::as_f64);
        let asset = v.get("asset").and_then(Value::as_str).unwrap_or_default().to_owned();
        let Some(open) = v.get("open_unix").and_then(Value::as_i64) else { return };
        let side: &'static str =
            if v.get("side").and_then(Value::as_str) == Some("yes") { "yes" } else { "no" };
        self.ledger.release(&format!("{asset}:{open}"));
        let shares = f("shares").unwrap_or(0.0).max(0.0);
        let cost = f("cost").unwrap_or(0.0).max(0.0);
        let (rate, exponent) = self
            .window(&asset, open)
            .map(|w| (w.fee_rate, w.fee_exponent))
            .unwrap_or((pk_core::fees::DEFAULT_RATE, pk_core::fees::DEFAULT_EXPONENT));
        let vwap = (shares > 0.0).then(|| cost / shares);
        let fee = f("fee").unwrap_or_else(|| vwap.map_or(0.0, |px| taker_fee(px, shares, rate, exponent)));
        let entry = f("entry_px");
        let ordered = f("ordered").unwrap_or(0.0);
        let decision_ns = v.get("decision_ns").and_then(Value::as_i64);
        let record = json!({
            "type": "fill",
            "venue": "live",
            "asset": asset,
            "open_unix": open,
            "t_ns": recv_ns,
            "latency_ms": decision_ns.map(|d| round_dp((recv_ns - d) as f64 / 1e6, 1)),
            "decision_to_ack_ms": f("decision_to_ack_ms"),
            "side": side,
            "limit_px": f("limit_px"),
            "ordered": ordered,
            "shares": shares,
            "vwap": round_opt(vwap, 4),
            "cost": round_dp(cost, 4),
            "fee": round_dp(fee, 4),
            "levels": Value::Null,
            "best_ask_at_fill": Value::Null,
            "touch_px": entry,
            "touch_shares": ordered,
            "touch_fee": entry.map(|e| round_dp(taker_fee(e, ordered, rate, exponent), 4)),
            "depth_at_limit": Value::Null,
            "order_id": v.get("order_id"),
            "status": v.get("status"),
            "error": v.get("error"),
        });
        self.count(if shares > 0.0 { "fills" } else { "no_fills" });
        if self.observe
            && let Some(w) = self.window(&asset, open).cloned()
        {
            self.add_markout(recv_ns, &w, side, shares > 0.0, vwap.or(f("limit_px")).unwrap_or(0.0));
        }
        if shares > 0.0 {
            let position = Position {
                asset: asset.clone(),
                open_unix: open,
                side: side.into(),
                shares,
                cost,
                fee,
                entry_ns: recv_ns,
                status: "open".into(),
                outcome: None,
                payout: None,
                pnl: None,
                settled_ns: None,
            };
            if let Err(e) = self.ledger.buy(position) {
                tracing::error!(error = %e, "ledger buy (live)");
                self.count("engine_errors");
            }
        } else {
            self.ledger.save();
        }
        self.write(recv_ns, record, true);
        if shares > 0.0 {
            let known = self
                .resolutions
                .get(&(asset.clone(), open))
                .and_then(|seen| seen.iter().find(|(v, _)| v == "poly").map(|(_, o)| o.clone()));
            if let Some(outcome) = known {
                self.settle(&asset, open, &outcome, recv_ns);
            }
        }
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
                let d = decide(&k, &y, &n, &self.params, sec, false, None, None, (w.fee_rate, w.fee_exponent));
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
            "live": self.live,
            "sizing": {
                "pct": self.sizer.pct,
                "notional": self.params.notional_usd,
                "basis_balance": self.sizer.basis_balance.map(|b| round_dp(b, 2)),
                "updated_ns": self.sizer.updated_ns,
            },
            "paper_latency_ms": self.venue.map(|v| round_dp(v.latency_ns as f64 / 1e6, 1)),
        })
    }
}
