//! The OKX engine: Kalshi's book and OKX's event-contract book per asset, the
//! kpc rule on them, and what an order would get.
//!
//! - The rule is `pk_core::decide()` unchanged, with OKX's UP book in place of
//!   Polymarket's YES book. OKX publishes the UP side only; DOWN is its mirror
//!   (bid = 1 − UP ask), as Limitless's NO is.
//! - Kalshi YES and OKX UP mean the same thing (close ≥ open), but on different
//!   indexes: Kalshi's BRTI 60 s average, OKX's own `<ASSET>-USDT` index
//!   averaged over the last 60 s against the open's (`stk`). Each window's two
//!   outcomes, and the outcome OKX's recorded index implies, are journaled
//!   (`okx_outcome`), so how often the venues disagree is measured.
//! - **Paper fills are delayed** by the measured round trip (no venue hold on
//!   OKX): a paper entry fills against the book at decision + round trip, never
//!   at the decision's touch. The instant fill is journaled beside it.
//! - **Fees** come from the account (`pk-okx check`, `account/trade-fee?instType=EVENTS`):
//!   on 2026-10-25, Lv1, taker 4.5% of notional (`rate · p` per share, the
//!   `notional` model) and no settlement fee. The entry cap and the exit floor
//!   are computed for that model: buying at `px` and selling at Kalshi's price
//!   must net `min_net_edge` after both fees, and an exit must cover the entry,
//!   both fees and a cent. The older `rate · (p(1 − p))^exponent` model stays
//!   for replays. Every fill journals its fee apart from its gross.
//! - **Thin books:** an entry the touch cannot fill in full is counted
//!   (`entries_partial`); the fill rate is measured, not assumed.
//! - **Kalshi → OKX response:** each Kalshi YES-mid move of ≥ 2¢ within 1 s
//!   opens a probe; the time until OKX's mid moves 1¢ the same way is its lag.
//!
//! Pure apart from the journal and ledger files: callers pass receive times.

use std::collections::{BTreeMap, BTreeSet, HashMap, VecDeque};

use pk_core::book::Book;
use pk_core::decision::{Action, StrategyParams, decide};
use pk_core::exit::{exit_floor, kalshi_fair, should_exit};
use pk_core::fees::taker_fee;
use pk_core::journal::{JsonlDaily, Sink};
use pk_core::ledger::{Ledger, Position};
use pk_core::num::{key_px, lag_ms, px_key, round_dp, round_opt};
use pk_core::{BookSide, NS, Quote, WINDOW_S};
use pk_stats::{Hist, Rolling, Sample};
use serde_json::{Map, Value, json};

use crate::discovery::OkxWindow;

const SERIES_POINTS: usize = 1800;
const RECENT: usize = 50;
const RESP_JUMP: f64 = 0.02 - 1e-9;
const RESP_STEP: f64 = 0.01 - 1e-9;
const RESP_TIMEOUT_NS: i64 = 10 * NS;
const RESP_COOLDOWN_NS: i64 = 5 * NS;
const MARKOUT_NS: i64 = 5 * NS;
const TRAIL_NS: i64 = 1_500_000_000;
/// OKX's settlement window for 15-minute events (since 2026-08-17).
const SETTLE_AVG_MS: i64 = 60_000;
/// How much index history to keep per underlying: outcomes are read up to
/// minutes after the close, and their settlement average needs the last 60 s.
const INDEX_KEEP_MS: i64 = 20 * 60_000;

#[derive(Clone, Debug)]
pub struct EngineCfg {
    pub params: StrategyParams,
    pub shares: f64,
    pub limit_offset: f64,
    pub exit_slip: f64,
    pub active_exit: bool,
    pub fee_rate: f64,
    pub fee_exponent: f64,
    /// Taker fee = `fee_rate · p` per share (OKX's), else `fee_rate · (p(1 − p))^fee_exponent`.
    pub fee_notional: bool,
    pub settle_fee: f64,
    pub paper_latency_ms: f64,
    pub paper_extra_ms: f64,
    pub max_open_usd: f64,
    pub daily_loss_usd: f64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Kind {
    Entry,
    Exit,
}

impl Kind {
    fn as_str(self) -> &'static str {
        match self {
            Kind::Entry => "entry",
            Kind::Exit => "exit",
        }
    }
}

#[derive(Clone, Debug)]
struct Pending {
    kind: Kind,
    decided_ns: i64,
    due_ns: i64,
    yes: bool,
    limit: f64,
    shares: f64,
    touch: f64,
    instant: (f64, f64),
}

#[derive(Debug)]
struct Resp {
    t0: i64,
    dir: f64,
    o0: f64,
}

#[derive(Debug)]
struct RespStat {
    n: u64,
    responded: u64,
    before: u64,
    lags: Rolling,
}

impl Default for RespStat {
    fn default() -> Self {
        Self { n: 0, responded: 0, before: 0, lags: Rolling::new(500) }
    }
}

impl RespStat {
    fn json(&self) -> Value {
        json!({
            "n": self.n, "responded": self.responded, "before": self.before,
            "lag_ms_p10": round_opt(self.lags.quantile(0.1), 0),
            "lag_ms_p50": round_opt(self.lags.quantile(0.5), 0),
            "lag_ms_p90": round_opt(self.lags.quantile(0.9), 0),
        })
    }
}

/// Venue outcomes compared, over every window seen on both.
#[derive(Debug, Default)]
struct Agreement {
    n: u64,
    okx_vs_kalshi: u64,
    okx_vs_index: u64,
    compared_index: u64,
}

struct Asset {
    name: String,
    window: Option<OkxWindow>,
    kbook: Book,
    obook: Book,
    o_seq: Option<i64>,
    consumed: bool,
    seen_reasons: BTreeSet<&'static str>,
    last_gap: Option<f64>,
    max_abs_gap: f64,
    last_decision: Option<Value>,
    pending: Option<Pending>,
    k_trail: VecDeque<(i64, f64)>,
    o_trail: VecDeque<(i64, f64)>,
    resp: Option<Resp>,
    last_resp_ns: i64,
    stat: RespStat,
    series: VecDeque<Value>,
    last_sec: i64,
    o_lag: Rolling,
    o_gap: Rolling,
    o_last_ns: Option<i64>,
    k_lag: Rolling,
}

impl Asset {
    fn new(name: &str) -> Self {
        Self {
            name: name.to_owned(),
            window: None,
            kbook: Book::default(),
            obook: Book::default(),
            o_seq: None,
            consumed: false,
            seen_reasons: BTreeSet::new(),
            last_gap: None,
            max_abs_gap: 0.0,
            last_decision: None,
            pending: None,
            k_trail: VecDeque::new(),
            o_trail: VecDeque::new(),
            resp: None,
            last_resp_ns: 0,
            stat: RespStat::default(),
            series: VecDeque::new(),
            last_sec: 0,
            o_lag: Rolling::new(2000),
            o_gap: Rolling::new(2000),
            o_last_ns: None,
            k_lag: Rolling::new(2000),
        }
    }

    fn oquotes(&self) -> (Quote, Quote) {
        let y = self.obook.quote(None);
        let n = match (y.valid, y.bid, y.ask) {
            (true, Some(b), Some(a)) => Quote { valid: true, bid: Some(1.0 - a), ask: Some(1.0 - b) },
            _ => Quote::INVALID,
        };
        (y, n)
    }
}

fn push_trail(t: &mut VecDeque<(i64, f64)>, at: i64, v: f64) {
    t.push_back((at, v));
    while t.len() > 1 && t[1].0 <= at - TRAIL_NS {
        t.pop_front();
    }
}

fn trail_at(t: &VecDeque<(i64, f64)>, at: i64) -> Option<f64> {
    t.iter().take_while(|(ti, _)| *ti <= at).last().map(|(_, v)| *v)
}

/// Shares and USDT a taker order gets from `book` within `limit`.
/// `yes`: UP (else DOWN, the mirror of the UP book); `buy` or sell.
pub fn take(book: &Book, yes: bool, buy: bool, limit: f64, shares: f64) -> (f64, f64) {
    let levels: Vec<(f64, f64)> = match (yes, buy) {
        (true, true) => book.asks.iter().map(|(&k, &s)| (key_px(k), s)).collect(),
        (false, true) => book.bids.iter().rev().map(|(&k, &s)| (1.0 - key_px(k), s)).collect(),
        (true, false) => book.bids.iter().rev().map(|(&k, &s)| (key_px(k), s)).collect(),
        (false, false) => book.asks.iter().map(|(&k, &s)| (1.0 - key_px(k), s)).collect(),
    };
    let (mut got, mut usd) = (0.0, 0.0);
    for (p, s) in levels {
        let inside = if buy { p <= limit + 1e-9 } else { p >= limit - 1e-9 };
        if !inside || got >= shares - 1e-9 {
            break;
        }
        let q = s.min(shares - got);
        got += q;
        usd += q * p;
    }
    (got, usd)
}

/// `[[price, size, "0", orders], ..]` as (price, size).
fn levels(v: Option<&Value>) -> Vec<(f64, f64)> {
    v.and_then(Value::as_array)
        .map(|l| {
            l.iter()
                .filter_map(|x| {
                    let p: f64 = x.get(0)?.as_str()?.parse().ok()?;
                    let s: f64 = x.get(1)?.as_str()?.parse().ok()?;
                    Some((p, s))
                })
                .collect()
        })
        .unwrap_or_default()
}

/// The mean of the index samples in `(end − 60 s, end]`, and how many there were.
pub fn settle_average(samples: &VecDeque<(i64, f64)>, end_ms: i64) -> Option<(f64, usize)> {
    let inside: Vec<f64> = samples.iter().filter(|(t, _)| *t > end_ms - SETTLE_AVG_MS && *t <= end_ms).map(|(_, v)| *v).collect();
    (!inside.is_empty()).then(|| (inside.iter().sum::<f64>() / inside.len() as f64, inside.len()))
}

pub struct Engine {
    pub cfg: EngineCfg,
    assets: Vec<Asset>,
    pub ledger: Ledger,
    journal: JsonlDaily,
    stamp: Map<String, Value>,
    counters: BTreeMap<String, u64>,
    recent: VecDeque<Value>,
    rtt_ms: Option<f64>,
    decide_ns: Hist,
    markouts: Vec<(i64, usize, bool, f64)>,
    markout5: Sample,
    net_per_share: Sample,
    delay_cost: Sample,
    totals: RespStat,
    bought: HashMap<String, f64>,
    /// Index samples per underlying: (venue ms, price).
    index: HashMap<String, VecDeque<(i64, f64)>>,
    agree: Agreement,
}

impl Engine {
    pub fn new(cfg: EngineCfg, assets: &[String], ledger: Ledger, journal: JsonlDaily, stamp: Map<String, Value>) -> Self {
        Self {
            cfg,
            assets: assets.iter().map(|a| Asset::new(a)).collect(),
            ledger,
            journal,
            stamp,
            counters: BTreeMap::new(),
            recent: VecDeque::new(),
            rtt_ms: None,
            decide_ns: Hist::new(),
            markouts: Vec::new(),
            markout5: Sample::default(),
            net_per_share: Sample::default(),
            delay_cost: Sample::default(),
            totals: RespStat::default(),
            bought: HashMap::new(),
            index: HashMap::new(),
            agree: Agreement::default(),
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
        if kind != "okx_resp" {
            if self.recent.len() == RECENT {
                self.recent.pop_back();
            }
            self.recent.push_front(r);
        }
    }

    /// The measured round trip to OKX (warm HTTP/2), for paper fills.
    pub fn set_rtt(&mut self, ms: f64) {
        self.rtt_ms = Some(ms);
    }

    pub fn window(&self, asset: &str) -> Option<&OkxWindow> {
        self.assets.iter().find(|a| a.name == asset)?.window.as_ref()
    }

    pub fn tickers(&self) -> Vec<String> {
        self.assets.iter().filter_map(|a| a.window.as_ref().map(|w| w.kalshi_ticker.clone())).collect()
    }

    pub fn instruments(&self) -> BTreeSet<String> {
        self.assets.iter().filter_map(|a| a.window.as_ref().map(|w| w.inst_id.clone())).collect()
    }

    /// A new window for an asset: its books start empty.
    pub fn set_window(&mut self, w: OkxWindow, t_ns: i64) {
        let Some(i) = self.assets.iter().position(|a| a.name == w.asset) else { return };
        if self.assets[i].window.as_ref() == Some(&w) {
            return;
        }
        let a = &mut self.assets[i];
        a.kbook = Book::default();
        a.obook = Book::default();
        a.o_seq = None;
        a.consumed = false;
        a.seen_reasons.clear();
        a.last_gap = None;
        a.max_abs_gap = 0.0;
        a.last_decision = None;
        a.k_trail.clear();
        a.o_trail.clear();
        a.resp = None;
        a.pending = None;
        a.window = Some(w.clone());
        self.count("windows");
        self.record(t_ns, "okx_window", serde_json::to_value(&w).unwrap_or(Value::Null));
    }

    /// One Kalshi frame (`k` line).
    pub fn on_kalshi(&mut self, text: &str, t: i64) {
        let Ok(frame) = serde_json::from_str::<pk_engine::parse::KalshiFrame>(text) else { return };
        let kind = frame.kind.as_deref();
        if !matches!(kind, Some("orderbook_snapshot" | "orderbook_delta")) {
            return;
        }
        let msg = frame.msg.unwrap_or_default();
        let ticker = msg.market_ticker.as_ref().map(|t| t.as_str()).unwrap_or_default();
        let Some(i) =
            self.assets.iter().position(|a| a.window.as_ref().is_some_and(|w| w.kalshi_ticker == ticker))
        else {
            return;
        };
        let a = &mut self.assets[i];
        if let Some(l) = lag_ms(msg.ts_ms.0.or(frame.sending_ts_ms.0), t) {
            a.k_lag.push(l);
        }
        if kind == Some("orderbook_snapshot") {
            let yes = msg.levels(true).into_iter().map(|(p, s)| (px_key(p), s));
            let no = msg.levels(false).into_iter().map(|(p, s)| (px_key(1.0 - p), s));
            a.kbook.snapshot_keys(yes, no, t);
        } else {
            if a.kbook.snapshot_ns.is_none() {
                return;
            }
            let Some((price, delta)) = msg.delta() else { return };
            if msg.side.as_deref() == Some("yes") {
                a.kbook.add_level(BookSide::Bid, px_key(price), delta, t);
            } else {
                a.kbook.add_level(BookSide::Ask, px_key(1.0 - price), delta, t);
            }
        }
        if let Some(m) = a.kbook.quote(None).mid() {
            push_trail(&mut a.k_trail, t, m);
            self.kalshi_moved(i, m, t);
        }
        self.evaluate(i, t);
    }

    fn kalshi_moved(&mut self, i: usize, kmid: f64, t: i64) {
        let a = &mut self.assets[i];
        if a.resp.is_some() || t - a.last_resp_ns < RESP_COOLDOWN_NS || !(0.15..=0.85).contains(&kmid) {
            return;
        }
        let Some(k1) = trail_at(&a.k_trail, t - NS) else { return };
        if (kmid - k1).abs() < RESP_JUMP {
            return;
        }
        let Some(o0) = a.obook.quote(None).mid() else { return };
        let dir = (kmid - k1).signum();
        a.last_resp_ns = t;
        let before = trail_at(&a.o_trail, t - NS).is_some_and(|o1| dir * (o0 - o1) >= RESP_STEP);
        if before {
            a.stat.n += 1;
            a.stat.before += 1;
            a.stat.responded += 1;
            self.totals.n += 1;
            self.totals.before += 1;
            self.totals.responded += 1;
            let r = json!({"asset": a.name, "jump": round_dp(kmid - k1, 3), "lag_ms": null, "before": true});
            self.record(t, "okx_resp", r);
        } else {
            a.resp = Some(Resp { t0: t, dir, o0 });
        }
    }

    fn resp_answered(&mut self, i: usize, omid: f64, t: i64) {
        let a = &mut self.assets[i];
        let Some(r) = &a.resp else { return };
        if r.dir * (omid - r.o0) < RESP_STEP {
            return;
        }
        let lag = (t - r.t0) as f64 / 1e6;
        a.resp = None;
        a.stat.n += 1;
        a.stat.responded += 1;
        a.stat.lags.push(lag);
        self.totals.n += 1;
        self.totals.responded += 1;
        self.totals.lags.push(lag);
        let r = json!({"asset": a.name, "lag_ms": round_dp(lag, 1), "before": false});
        self.record(t, "okx_resp", r);
    }

    /// A Kalshi reconnect or resync: its books are invalid until fresh snapshots.
    pub fn invalidate_kalshi(&mut self) {
        for a in &mut self.assets {
            a.kbook.invalidate();
        }
        self.count("kalshi_resets");
    }

    /// One OKX line (`o`): a raw v5 push, or our reconnect / resync marker.
    pub fn on_okx(&mut self, text: &str, t: i64) {
        let Ok(v) = serde_json::from_str::<Value>(text) else { return };
        match v.get("type").and_then(Value::as_str) {
            Some("pk_reconnect") => {
                for a in &mut self.assets {
                    a.obook.invalidate();
                    a.o_seq = None;
                }
                self.count("okx_resets");
                return;
            }
            Some("pk_resync") => {
                let inst = v.get("instId").and_then(Value::as_str).unwrap_or_default();
                if let Some(a) = self.assets.iter_mut().find(|a| a.window.as_ref().is_some_and(|w| w.inst_id == inst)) {
                    a.obook.invalidate();
                    a.o_seq = None;
                }
                self.count("okx_resyncs");
                return;
            }
            _ => {}
        }
        match v.pointer("/arg/channel").and_then(Value::as_str) {
            Some("books") => self.on_book(&v, t),
            Some("index-tickers") => {
                let Some(d) = v.pointer("/data/0") else { return };
                let id = d.get("instId").and_then(Value::as_str).unwrap_or_default().to_owned();
                let px = d.get("idxPx").and_then(Value::as_str).and_then(|s| s.parse::<f64>().ok());
                let ts = d.get("ts").and_then(Value::as_str).and_then(|s| s.parse::<i64>().ok());
                if let (Some(px), Some(ts)) = (px, ts) {
                    let s = self.index.entry(id).or_default();
                    s.push_back((ts, px));
                    while s.front().is_some_and(|(t0, _)| *t0 < ts - INDEX_KEEP_MS) {
                        s.pop_front();
                    }
                }
            }
            _ => {}
        }
    }

    fn on_book(&mut self, v: &Value, t: i64) {
        let inst = v.pointer("/arg/instId").and_then(Value::as_str).unwrap_or_default();
        let Some(i) = self.assets.iter().position(|a| a.window.as_ref().is_some_and(|w| w.inst_id == inst)) else {
            return;
        };
        let Some(d) = v.pointer("/data/0") else { return };
        let seq = d.get("seqId").and_then(Value::as_i64);
        let prev = d.get("prevSeqId").and_then(Value::as_i64);
        let snapshot = v.get("action").and_then(Value::as_str) == Some("snapshot");
        let a = &mut self.assets[i];
        if snapshot {
            a.obook.snapshot(&levels(d.get("bids")), &levels(d.get("asks")), t);
        } else {
            // An update off the sequence: the feed resubscribes; drop the book until then.
            if a.obook.snapshot_ns.is_none() || a.o_seq.is_none() || prev != a.o_seq {
                if a.obook.snapshot_ns.is_some() {
                    a.obook.invalidate();
                }
                a.o_seq = None;
                self.count("okx_seq_breaks");
                return;
            }
            for (p, s) in levels(d.get("bids")) {
                a.obook.set_level(BookSide::Bid, px_key(p), s, t);
            }
            for (p, s) in levels(d.get("asks")) {
                a.obook.set_level(BookSide::Ask, px_key(p), s, t);
            }
        }
        a.o_seq = seq;
        if let Some(ts) = d.get("ts").and_then(Value::as_str).and_then(|s| s.parse::<f64>().ok()) {
            a.o_lag.push(t as f64 / 1e6 - ts);
        }
        if let Some(prev) = a.o_last_ns {
            a.o_gap.push((t - prev) as f64 / 1e6);
        }
        a.o_last_ns = Some(t);
        if let Some(m) = a.obook.quote(None).mid() {
            push_trail(&mut a.o_trail, t, m);
            self.resp_answered(i, m, t);
        }
        self.evaluate(i, t);
    }

    /// Timers: held paper orders, probe timeouts, markouts, the per-second series.
    pub fn tick(&mut self, t: i64) {
        for i in 0..self.assets.len() {
            if self.assets[i].pending.as_ref().is_some_and(|p| t >= p.due_ns) {
                self.fill_paper(i, t);
            }
            let a = &mut self.assets[i];
            if a.resp.as_ref().is_some_and(|r| t - r.t0 > RESP_TIMEOUT_NS) {
                a.resp = None;
                a.stat.n += 1;
                self.totals.n += 1;
            }
            let sec = t.div_euclid(NS);
            if sec != a.last_sec {
                a.last_sec = sec;
                if let Some(w) = &a.window {
                    let k = a.kbook.quote(None);
                    let o = a.obook.quote(None);
                    if a.series.len() == SERIES_POINTS {
                        a.series.pop_front();
                    }
                    a.series.push_back(json!([sec, w.open_unix, k.mid(), o.mid(), o.bid, o.ask]));
                }
            }
            self.evaluate(i, t);
        }
        let due: Vec<_> = self.markouts.iter().filter(|m| t >= m.0).cloned().collect();
        self.markouts.retain(|m| t < m.0);
        for (_, i, yes, vwap) in due {
            if let Some(m) = self.assets[i].oquotes().0.mid() {
                let mid = if yes { m } else { 1.0 - m };
                self.markout5.push((mid - vwap) * 100.0);
            }
        }
    }

    fn evaluate(&mut self, i: usize, t: i64) {
        let Some(w) = self.assets[i].window.clone() else { return };
        if self.assets[i].pending.is_some() || t >= w.exp_ms * 1_000_000 {
            return;
        }
        let key = format!("{}:{}", w.asset, w.open_unix);
        let held = self.ledger.positions.get(&key).filter(|p| p.is_open()).cloned();
        match held {
            Some(p) => self.maybe_exit(i, &w, &p, t),
            None => self.maybe_enter(i, &w, t),
        }
    }

    fn fee(&self, price: f64, shares: f64) -> f64 {
        if self.cfg.fee_notional {
            if shares <= 0.0 || !(0.0 < price && price < 1.0) { 0.0 } else { self.cfg.fee_rate * price * shares }
        } else {
            taker_fee(price, shares, self.cfg.fee_rate, self.cfg.fee_exponent)
        }
    }

    /// The highest entry price that still nets `min_net_edge` after both notional fees,
    /// selling at `fair`: `fair(1 − r) − px(1 + r) ≥ m`.
    fn notional_cap(&self, fair: f64) -> f64 {
        let r = self.cfg.fee_rate;
        let m = self.cfg.params.min_net_edge.unwrap_or(0.0);
        (fair * (1.0 - r) - m) / (1.0 + r)
    }

    /// The lowest exit price whose proceeds after the fee cover the entry, its fee and a cent.
    fn exit_floor(&self, vwap: f64, tick: f64) -> f64 {
        if self.cfg.fee_notional {
            let r = self.cfg.fee_rate;
            round_dp((((vwap * (1.0 + r) + 0.01) / (1.0 - r)) / tick - 1e-6).ceil() * tick, 4)
        } else {
            exit_floor(vwap, self.cfg.fee_rate, self.cfg.fee_exponent)
        }
    }

    fn hold_ns(&self) -> i64 {
        ((self.rtt_ms.unwrap_or(self.cfg.paper_latency_ms) + self.cfg.paper_extra_ms) * 1e6) as i64
    }

    fn maybe_enter(&mut self, i: usize, w: &OkxWindow, t: i64) {
        let a = &self.assets[i];
        if a.consumed {
            return;
        }
        let k = a.kbook.quote(None);
        let (oy, on) = a.oquotes();
        let sec_into = t as f64 / 1e9 - w.open_unix as f64;
        let hour = chrono::DateTime::from_timestamp_nanos(t).format("%H").to_string().parse().ok();
        let t0 = std::time::Instant::now();
        // The notional fee is checked below, with its own cap; decide() sees none.
        let fee = if self.cfg.fee_notional { (0.0, 1.0) } else { (self.cfg.fee_rate, self.cfg.fee_exponent) };
        let d = decide(&k, &oy, &on, &self.cfg.params, sec_into, false, hour, None, fee);
        self.decide_ns.record(t0.elapsed().as_nanos() as u64);
        let a = &mut self.assets[i];
        if let Some(g) = d.gap {
            a.last_gap = Some(g);
            a.max_abs_gap = a.max_abs_gap.max(g.abs());
        }
        let reason = d.label();
        if !matches!(reason, "too_early" | "too_late" | "invalid_leg" | "below_threshold") && a.seen_reasons.insert(reason) {
            self.count(&format!("reason_{reason}"));
        }
        if d.action == Action::None {
            return;
        }
        let yes = d.buy_yes.unwrap_or(true);
        let touch = d.entry_px.unwrap_or(0.0);
        let cap = (self.cfg.fee_notional && d.action == Action::Enter)
            .then(|| kalshi_fair(&k, yes).map(|f| self.notional_cap(f)))
            .flatten();
        let a = &mut self.assets[i];
        a.consumed = true;
        let mut limit = (touch + self.cfg.limit_offset).min(d.max_px.unwrap_or(0.999)).min(0.999);
        if let Some(c) = cap {
            limit = limit.min(c);
        }
        limit = (limit / w.tick + 1e-6).floor() * w.tick;
        limit = round_dp(limit, 4);
        let net_edge_blocked = cap.is_some_and(|c| touch > c + 1e-9);
        let instant = take(&a.obook, yes, true, limit, self.cfg.shares);
        let mut rec = json!({
            "asset": w.asset, "open_unix": w.open_unix, "inst_id": w.inst_id, "sec_into": round_dp(sec_into, 3),
            "action": d.action.as_str(), "reason": d.reason, "gap": d.gap.map(|g| round_dp(g, 4)),
            "side": if yes { "yes" } else { "no" }, "entry_px": d.entry_px, "limit": limit,
            "shares": self.cfg.shares, "kalshi": {"bid": k.bid, "ask": k.ask},
            "okx": {"bid": oy.bid, "ask": oy.ask}, "instant_shares": round_dp(instant.0, 3),
        });
        if d.action == Action::Enter {
            self.count("entries_decided");
            let cost = limit * self.cfg.shares;
            let blocked = if net_edge_blocked {
                Some("net_edge")
            } else if self.ledger.open_notional() + cost > self.cfg.max_open_usd + 1e-9 {
                Some("max_open")
            } else if -self.ledger.realised_today(t) >= self.cfg.daily_loss_usd {
                Some("daily_loss")
            } else if !self.ledger.can_afford(cost + self.fee(limit, self.cfg.shares)) {
                Some("cash")
            } else {
                None
            };
            if let Some(b) = blocked {
                rec["blocked"] = b.into();
                self.count(&format!("blocked_{b}"));
            } else {
                self.assets[i].pending = Some(Pending {
                    kind: Kind::Entry,
                    decided_ns: t,
                    due_ns: t + self.hold_ns(),
                    yes,
                    limit,
                    shares: self.cfg.shares,
                    touch,
                    instant,
                });
            }
        } else {
            self.count("skips");
        }
        self.assets[i].last_decision = Some(rec.clone());
        self.record(t, "okx_decision", rec);
    }

    fn maybe_exit(&mut self, i: usize, w: &OkxWindow, p: &Position, t: i64) {
        if !self.cfg.active_exit || p.shares < w.min_size {
            return;
        }
        let a = &self.assets[i];
        let yes = p.side == "yes";
        let k = a.kbook.quote(None);
        let (oy, on) = a.oquotes();
        let bid = if yes { oy.bid } else { on.bid };
        let (Some(bid), Some(fair)) = (bid, kalshi_fair(&k, yes)) else { return };
        let vwap = p.vwap();
        let floor = self.exit_floor(vwap, w.tick);
        if !should_exit(bid, fair, vwap) || bid < floor - 1e-9 {
            return;
        }
        let limit = round_dp((((bid - self.cfg.exit_slip).max(floor)) / w.tick - 1e-6).ceil() * w.tick, 4);
        let instant = take(&a.obook, yes, false, limit, p.shares);
        self.assets[i].pending = Some(Pending {
            kind: Kind::Exit,
            decided_ns: t,
            due_ns: t + self.hold_ns(),
            yes,
            limit,
            shares: p.shares,
            touch: bid,
            instant,
        });
        self.count("exits_decided");
        let r = json!({"asset": w.asset, "open_unix": w.open_unix, "side": p.side, "bid": bid,
                       "fair": round_dp(fair, 4), "floor": floor, "limit": limit, "shares": p.shares});
        self.record(t, "okx_exit_decision", r);
    }

    fn fill_paper(&mut self, i: usize, t: i64) {
        let Some(p) = self.assets[i].pending.take() else { return };
        let Some(w) = self.assets[i].window.clone() else { return };
        let got = take(&self.assets[i].obook, p.yes, p.kind == Kind::Entry, p.limit, p.shares);
        self.book_fill(i, &w, &p, got.0, got.1, t);
    }

    fn book_fill(&mut self, i: usize, w: &OkxWindow, p: &Pending, shares: f64, usd: f64, t: i64) {
        let side = if p.yes { "yes" } else { "no" };
        let vwap = if shares > 0.0 { usd / shares } else { 0.0 };
        let fee = self.fee(vwap, shares);
        let (ishares, iusd) = p.instant;
        let ivwap = if ishares > 0.0 { iusd / ishares } else { 0.0 };
        let mut rec = json!({
            "asset": w.asset, "open_unix": w.open_unix, "kind": p.kind.as_str(), "side": side,
            "limit": p.limit, "touch_px": p.touch, "shares": round_dp(shares, 3), "wanted": p.shares,
            "vwap": round_dp(vwap, 4), "gross": round_dp(usd, 4), "fee": round_dp(fee, 5),
            "instant_shares": round_dp(ishares, 3), "instant_vwap": round_dp(ivwap, 4),
            "held_ms": round_dp((t - p.decided_ns) as f64 / 1e6, 1), "venue": "paper",
        });
        if shares < w.min_size - 1e-9 {
            self.count(&format!("{}_missed", p.kind.as_str()));
            self.record(t, "okx_no_fill", rec);
            return;
        }
        match p.kind {
            Kind::Entry => {
                self.count("entries_filled");
                if shares < p.shares - 1e-9 {
                    self.count("entries_partial");
                }
                if ishares > 0.0 {
                    // ¢/share the delay cost against the decision's book (buy: paid more).
                    self.delay_cost.push((vwap - ivwap) * 100.0);
                }
                let pos = Position {
                    asset: w.asset.clone(),
                    open_unix: w.open_unix,
                    side: side.into(),
                    shares,
                    cost: usd,
                    fee,
                    entry_ns: t,
                    status: "open".into(),
                    outcome: None,
                    payout: None,
                    pnl: None,
                    settled_ns: None,
                };
                self.bought.insert(pos.key(), shares);
                if let Err(e) = self.ledger.buy(pos) {
                    rec["error"] = e.to_string().into();
                }
                self.markouts.push((t + MARKOUT_NS, i, p.yes, vwap));
                self.record(t, "okx_fill", rec);
            }
            Kind::Exit => {
                self.count("exits_filled");
                if ishares > 0.0 {
                    self.delay_cost.push((ivwap - vwap) * 100.0);
                }
                if let Some(closed) = self.ledger.close(&w.asset, w.open_unix, shares, usd, fee, t) {
                    if !closed.is_open() {
                        self.settled(&closed);
                    }
                    rec["pnl"] = closed.pnl.map(|x| round_dp(x, 4)).into();
                }
                self.record(t, "okx_exit", rec);
            }
        }
    }

    /// Net ¢ per share bought, once a position is done (sold out or settled).
    fn settled(&mut self, p: &Position) {
        let bought = self.bought.remove(&p.key()).unwrap_or(p.shares);
        if let (Some(pnl), true) = (p.pnl, bought > 0.0) {
            self.net_per_share.push(pnl * 100.0 / bought);
        }
    }

    /// The index's settlement average for a window, from the recorded `index-tickers`.
    pub fn index_close(&self, w: &OkxWindow) -> Option<(f64, usize)> {
        settle_average(self.index.get(&w.underlying)?, w.exp_ms)
    }

    /// Both venues' outcomes for a window (and the one OKX's index implies),
    /// then the paper position's settlement, with OKX's fee on a winning share.
    pub fn outcome(&mut self, w: &OkxWindow, okx: &str, kalshi: Option<&str>, index: Option<(f64, usize)>, t: i64) {
        let implied = index.map(|(avg, _)| if avg >= w.strike { "yes" } else { "no" });
        self.agree.n += 1;
        if kalshi.is_some_and(|k| k != okx) {
            self.agree.okx_vs_kalshi += 1;
            self.count("disagree_kalshi");
        }
        if let Some(i) = implied {
            self.agree.compared_index += 1;
            if i != okx {
                self.agree.okx_vs_index += 1;
                self.count("disagree_index");
            }
        }
        let r = json!({"asset": w.asset, "open_unix": w.open_unix, "inst_id": w.inst_id, "okx": okx,
                       "kalshi": kalshi, "strike": w.strike,
                       "index_avg60": index.map(|(a, _)| round_dp(a, 4)), "index_n": index.map(|(_, n)| n),
                       "index_implied": implied});
        self.record(t, "okx_outcome", r);
        let key = format!("{}:{}", w.asset, w.open_unix);
        if let Some(p) = self.ledger.positions.get_mut(&key)
            && p.is_open()
            && p.side == okx
        {
            p.fee += self.cfg.settle_fee * p.shares;
        }
        if let Some(p) = self.ledger.settle(&w.asset, w.open_unix, okx, t) {
            self.count("settled");
            self.settled(&p);
            let r = json!({"asset": w.asset, "open_unix": w.open_unix, "side": p.side, "outcome": okx,
                           "shares": p.shares, "payout": p.payout, "fee": round_dp(p.fee, 5),
                           "pnl": p.pnl.map(|x| round_dp(x, 4))});
            self.record(t, "okx_settle", r);
        }
    }

    /// Open positions whose window has closed (`asset`, `open_unix`).
    pub fn unsettled(&self, t: i64) -> Vec<(String, i64)> {
        self.ledger
            .open_positions()
            .filter(|p| t / NS > p.open_unix + WINDOW_S + 20)
            .map(|p| (p.asset.clone(), p.open_unix))
            .collect()
    }

    pub fn note(&mut self, t: i64, kind: &str, r: Value) {
        self.record(t, kind, r);
    }

    pub fn series_since(&self, since: i64) -> Value {
        let points: Map<String, Value> = self
            .assets
            .iter()
            .map(|a| {
                let pts: Vec<Value> = a
                    .series
                    .iter()
                    .filter(|p| p.get(0).and_then(Value::as_i64).is_some_and(|s| s > since))
                    .cloned()
                    .collect();
                (a.name.clone(), Value::Array(pts))
            })
            .collect();
        json!({"fields": ["sec", "open_unix", "kalshi_mid", "okx_mid", "okx_bid", "okx_ask"], "points": points})
    }

    pub fn status(&self, t: i64) -> Value {
        let q = |r: &Rolling, p: f64| round_opt(r.quantile(p), 1);
        let assets: Map<String, Value> = self
            .assets
            .iter()
            .map(|a| {
                let k = a.kbook.quote(None);
                let (oy, _) = a.oquotes();
                let w = a.window.as_ref();
                let depth = |bids: bool| -> f64 {
                    let m = if bids { &a.obook.bids } else { &a.obook.asks };
                    let best = if bids { m.keys().next_back() } else { m.keys().next() };
                    best.and_then(|k| m.get(k)).copied().unwrap_or(0.0)
                };
                let held = w.and_then(|w| self.ledger.positions.get(&format!("{}:{}", w.asset, w.open_unix)));
                let index = w.and_then(|w| self.index.get(&w.underlying)).and_then(|s| s.back()).map(|(_, p)| *p);
                (
                    a.name.clone(),
                    json!({
                        "open_unix": w.map(|w| w.open_unix),
                        "inst_id": w.map(|w| w.inst_id.clone()),
                        "identified": w.is_some(),
                        "sec_into": w.map(|w| round_dp(t as f64 / 1e9 - w.open_unix as f64, 1)),
                        "strike": w.map(|w| w.strike),
                        "index": index,
                        "consumed": a.consumed,
                        "kalshi": {"bid": k.bid, "ask": k.ask, "mid": k.mid(),
                                   "age_ms": a.kbook.last_update_ns.map(|u| (t - u) / 1_000_000)},
                        "okx": {"bid": oy.bid, "ask": oy.ask, "mid": oy.mid(),
                                "spread": match (oy.bid, oy.ask) { (Some(b), Some(x)) => Some(round_dp(x - b, 3)), _ => None },
                                "bid_size": round_dp(depth(true), 1), "ask_size": round_dp(depth(false), 1),
                                "seq": a.o_seq,
                                "age_ms": a.obook.last_update_ns.map(|u| (t - u) / 1_000_000)},
                        "gap": a.last_gap.map(|g| round_dp(g, 4)),
                        "max_abs_gap": round_dp(a.max_abs_gap, 4),
                        "decision": a.last_decision,
                        "pending": a.pending.as_ref().map(|p| json!({"kind": p.kind.as_str(), "limit": p.limit,
                            "age_ms": (t - p.decided_ns) / 1_000_000})),
                        "position": held,
                        "response": a.stat.json(),
                        "feed": {
                            "okx_lag_ms_p50": q(&a.o_lag, 0.5), "okx_lag_ms_p90": q(&a.o_lag, 0.9),
                            "okx_lag_ms_p99": q(&a.o_lag, 0.99),
                            "okx_interval_ms_p50": q(&a.o_gap, 0.5), "okx_interval_ms_p90": q(&a.o_gap, 0.9),
                            "kalshi_lag_ms_p50": q(&a.k_lag, 0.5), "kalshi_lag_ms_p90": q(&a.k_lag, 0.9),
                        },
                    }),
                )
            })
            .collect();
        let l = &self.ledger;
        let settled: Vec<&Position> = l.positions.values().filter(|p| !p.is_open()).collect();
        let pnl: f64 = settled.iter().filter_map(|p| p.pnl).sum();
        let wins = settled.iter().filter(|p| p.pnl.is_some_and(|x| x > 0.0)).count();
        let s = |x: &Sample| json!({"n": x.n(), "mean": round_opt(x.mean(), 2), "se": round_opt(x.se(), 2), "t": round_opt(x.t(), 2)});
        let decided = self.counters.get("entries_decided").copied().unwrap_or(0);
        let filled = self.counters.get("entries_filled").copied().unwrap_or(0);
        json!({
            "mode": "paper",
            "stamp": self.stamp,
            "params": self.cfg.params,
            "shares": self.cfg.shares,
            "limit_offset": self.cfg.limit_offset,
            "fee": {"rate": self.cfg.fee_rate, "exponent": self.cfg.fee_exponent, "settle": self.cfg.settle_fee,
                    "model": if self.cfg.fee_notional { "notional" } else { "p(1-p)" },
                    "paper_extra_ms": self.cfg.paper_extra_ms},
            "assets": assets,
            "ledger": {
                "start_cash": round_dp(l.start_cash, 2), "cash": round_dp(l.cash, 2),
                "open_notional": round_dp(l.open_notional(), 2), "realised_today": round_dp(l.realised_today(t), 2),
                "open": l.open_positions().count(), "settled": settled.len(), "wins": wins,
                "pnl": round_dp(pnl, 2), "rejects": l.rejects,
            },
            "gates": {
                "net_cents_per_share": s(&self.net_per_share),
                "markout5_cents": s(&self.markout5),
                "delay_cost_cents": s(&self.delay_cost),
                "fill_rate": (decided > 0).then(|| round_dp(filled as f64 / decided as f64, 3)),
            },
            "settlement": {
                "windows": self.agree.n, "okx_vs_kalshi": self.agree.okx_vs_kalshi,
                "okx_vs_index": self.agree.okx_vs_index, "index_compared": self.agree.compared_index,
            },
            "counters": self.counters,
            "response": self.totals.json(),
            "decide_ns": self.decide_ns.summary(),
            "rtt_ms": self.rtt_ms,
            "recent": self.recent,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn engine() -> Engine {
        let cfg = EngineCfg {
            params: StrategyParams {
                gap_threshold: 0.20,
                max_entry_px: 0.60,
                notional_usd: 1e9,
                max_kalshi_spread: Some(0.02),
                max_poly_spread: Some(0.05),
                min_edge: Some(0.10),
                ..Default::default()
            },
            shares: 10.0,
            limit_offset: 0.06,
            exit_slip: 0.01,
            active_exit: true,
            fee_rate: 0.07,
            fee_exponent: 1.0,
            fee_notional: false,
            settle_fee: 0.01,
            paper_latency_ms: 30.0,
            paper_extra_ms: 0.0,
            max_open_usd: 100.0,
            daily_loss_usd: 100.0,
        };
        let dir = std::env::temp_dir().join(format!("pk-okx-test-{}-{}", std::process::id(), rand_suffix()));
        Engine::new(cfg, &["BTC".into()], Ledger::new(100.0), JsonlDaily::new(dir), Map::new())
    }

    fn rand_suffix() -> u64 {
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.subsec_nanos() as u64).unwrap_or(0)
    }

    fn window(open: i64) -> OkxWindow {
        OkxWindow {
            asset: "BTC".into(),
            open_unix: open,
            kalshi_ticker: "KX".into(),
            inst_id: "BTC-E".into(),
            strike: 100.0,
            exp_ms: (open + 900) * 1000,
            underlying: "BTC-USDT".into(),
            tick: 0.001,
            min_size: 0.01,
        }
    }

    fn snap(asks: &str, bids: &str, seq: i64) -> String {
        format!(r#"{{"arg":{{"channel":"books","instId":"BTC-E"}},"action":"snapshot","data":[{{"asks":{asks},"bids":{bids},"ts":"1","seqId":{seq},"prevSeqId":-1}}]}}"#)
    }

    fn upd(asks: &str, bids: &str, seq: i64, prev: i64) -> String {
        format!(r#"{{"arg":{{"channel":"books","instId":"BTC-E"}},"action":"update","data":[{{"asks":{asks},"bids":{bids},"ts":"2","seqId":{seq},"prevSeqId":{prev}}}]}}"#)
    }

    const KALSHI_70: &str = r#"{"type":"orderbook_snapshot","msg":{"market_ticker":"KX","yes_dollars_fp":[["0.70","100"]],"no_dollars_fp":[["0.29","100"]]}}"#;

    #[test]
    fn book_updates_follow_the_sequence() {
        let mut e = engine();
        let open = 1_792_765_800;
        let t = (open + 100) * NS;
        e.set_window(window(open), t);
        e.on_okx(&snap(r#"[["0.52","10","0","1"]]"#, r#"[["0.48","10","0","1"]]"#, 5), t);
        e.on_okx(&upd(r#"[["0.52","0","0","0"],["0.53","4","0","1"]]"#, "[]", 6, 5), t + 1);
        let st = e.status(t + 1);
        assert_eq!(st["assets"]["BTC"]["okx"]["ask"].as_f64(), Some(0.53));
        // A gap drops the book until a new snapshot.
        e.on_okx(&upd("[]", r#"[["0.49","1","0","1"]]"#, 9, 8), t + 2);
        assert_eq!(e.status(t + 2)["assets"]["BTC"]["okx"]["mid"], Value::Null);
        assert_eq!(e.counters.get("okx_seq_breaks"), Some(&1));
        e.on_okx(&snap(r#"[["0.55","1","0","1"]]"#, r#"[["0.45","1","0","1"]]"#, 20), t + 3);
        assert_eq!(e.status(t + 3)["assets"]["BTC"]["okx"]["mid"].as_f64(), Some(0.5));
    }

    #[test]
    fn paper_entry_fills_after_the_round_trip_against_the_later_book() {
        let mut e = engine();
        let open = 1_792_765_800;
        let t = (open + 100) * NS;
        e.set_window(window(open), t);
        e.on_okx(&snap(r#"[["0.46","50","0","1"]]"#, r#"[["0.44","50","0","1"]]"#, 1), t);
        e.on_kalshi(KALSHI_70, t + 1);
        assert_eq!(e.status(t + 1)["assets"]["BTC"]["pending"]["kind"], "entry");
        // The maker pulls 0.46 inside the round trip; 0.49 is still within the limit.
        e.on_okx(&upd(r#"[["0.46","0","0","0"],["0.49","50","0","1"]]"#, "[]", 2, 1), t + 10_000_000);
        e.tick(t + 20_000_000);
        assert!(e.ledger.positions.is_empty(), "still in flight");
        e.tick(t + 40_000_000);
        let p = e.ledger.positions.values().next().expect("filled");
        assert!((p.vwap() - 0.49).abs() < 1e-9 && (p.shares - 10.0).abs() < 1e-9);
        assert!((p.fee - taker_fee(0.49, 10.0, 0.07, 1.0)).abs() < 1e-12);
        assert_eq!(e.status(t)["gates"]["delay_cost_cents"]["n"], 1);
        // UP wins: the settlement fee comes off each winning share.
        let w = window(open);
        e.outcome(&w, "yes", Some("yes"), Some((101.0, 30)), (open + 950) * NS);
        let p = e.ledger.positions.values().next().unwrap();
        let expect = 10.0 - 4.9 - taker_fee(0.49, 10.0, 0.07, 1.0) - 0.1;
        assert!((p.pnl.unwrap() - expect).abs() < 1e-9, "{:?}", p.pnl);
        assert_eq!(e.status(t)["settlement"]["okx_vs_kalshi"], 0);
    }

    #[test]
    fn notional_fee_caps_the_entry_and_sets_the_exit_floor() {
        let mut e = engine();
        e.cfg.fee_notional = true;
        e.cfg.fee_rate = 0.045;
        e.cfg.params.min_net_edge = Some(0.03);
        // Kalshi 0.70 → cap (0.70·0.955 − 0.03)/1.045 = 0.6110; the touch 0.46 is well under it.
        assert!((e.notional_cap(0.70) - 0.61100478).abs() < 1e-6);
        assert!((e.fee(0.46, 10.0) - 0.207).abs() < 1e-12);
        // Exit floor for a 0.46 entry: (0.46·1.045 + 0.01)/0.955 = 0.5138 → 0.514.
        assert!((e.exit_floor(0.46, 0.001) - 0.514).abs() < 1e-9);
        let open = 1_792_765_800;
        let t = (open + 100) * NS;
        e.set_window(window(open), t);
        // A wide OKX book (0.40 / 0.62) is allowed here; the fee-aware cap is what bounds the entry.
        e.cfg.params.max_entry_px = 0.80;
        e.cfg.params.max_poly_spread = None;
        e.on_okx(&snap(r#"[["0.62","50","0","1"]]"#, r#"[["0.40","50","0","1"]]"#, 1), t);
        let k = r#"{"type":"orderbook_snapshot","msg":{"market_ticker":"KX","yes_dollars_fp":[["0.84","100"]],"no_dollars_fp":[["0.15","100"]]}}"#;
        e.on_kalshi(k, t + 1);
        e.tick(t + 100_000_000);
        // Kalshi fair 0.845 → cap 0.744: the 0.62 touch passes and fills.
        assert_eq!(e.counters.get("entries_filled"), Some(&1));
        let p = e.ledger.positions.values().next().unwrap();
        assert!((p.fee - 0.045 * 0.62 * 10.0).abs() < 1e-9);
    }

    #[test]
    fn thin_touch_is_a_partial_fill() {
        let mut e = engine();
        let open = 1_792_765_800;
        let t = (open + 100) * NS;
        e.set_window(window(open), t);
        e.on_okx(&snap(r#"[["0.46","3","0","1"]]"#, r#"[["0.44","50","0","1"]]"#, 1), t);
        e.on_kalshi(KALSHI_70, t + 1);
        e.tick(t + 50_000_000);
        let p = e.ledger.positions.values().next().expect("filled");
        assert!((p.shares - 3.0).abs() < 1e-9);
        assert_eq!(e.counters.get("entries_partial"), Some(&1));
    }

    #[test]
    fn outcomes_are_compared() {
        let mut e = engine();
        let w = window(1_792_765_800);
        e.outcome(&w, "yes", Some("no"), Some((99.0, 40)), 1);
        let s = &e.status(1)["settlement"];
        assert_eq!((s["windows"].as_u64(), s["okx_vs_kalshi"].as_u64(), s["okx_vs_index"].as_u64()), (Some(1), Some(1), Some(1)));
        let mut idx = VecDeque::new();
        for (ms, p) in [(0, 1.0), (40_000, 2.0), (70_000, 4.0), (100_000, 6.0), (130_000, 9.0)] {
            idx.push_back((ms, p));
        }
        assert_eq!(settle_average(&idx, 100_000), Some((5.0, 2)));
    }

    #[test]
    fn response_lag_is_timed() {
        let mut e = engine();
        let open = 1_792_765_800;
        let t = (open + 100) * NS;
        e.set_window(window(open), t);
        e.on_okx(&snap(r#"[["0.51","5","0","1"]]"#, r#"[["0.49","5","0","1"]]"#, 1), t);
        let k = |y: &str, n: &str| format!(r#"{{"type":"orderbook_snapshot","msg":{{"market_ticker":"KX","yes_dollars_fp":[["{y}","100"]],"no_dollars_fp":[["{n}","100"]]}}}}"#);
        e.on_kalshi(&k("0.49", "0.49"), t);
        e.on_kalshi(&k("0.52", "0.46"), t + 1_200_000_000);
        e.on_okx(&upd(r#"[["0.51","0","0","0"],["0.53","5","0","1"]]"#, r#"[["0.49","0","0","0"],["0.51","5","0","1"]]"#, 2, 1), t + 1_500_000_000);
        let r = &e.status(t)["response"];
        assert_eq!((r["n"].as_u64(), r["responded"].as_u64()), (Some(1), Some(1)));
        assert_eq!(r["lag_ms_p50"].as_f64(), Some(300.0));
    }
}
