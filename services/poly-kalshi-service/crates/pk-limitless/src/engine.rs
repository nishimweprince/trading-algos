//! The Limitless engine: Kalshi's book and Limitless's book per asset, the kpc
//! rule on them, and what an order would get.
//!
//! - The rule is `pk_core::decide()` unchanged, with Limitless's YES book in
//!   place of Polymarket's (NO is its mirror: bid = 1 − YES ask) and Limitless's
//!   fee, `0.05 · p(1 − p)` per share, which is Polymarket's formula at rate 0.05.
//! - **Paper fills are delayed.** A taker order on Limitless is held for the
//!   market's `takerDelayMs` before it matches, and makers may cancel in that
//!   time. So a paper entry fills against the book as it stands at decision +
//!   measured round trip + taker delay, never at the decision's touch. The fill
//!   at the decision's book is journaled next to it (`instant_*`), so the cost
//!   of the delay is measured, not assumed.
//! - The active exit sells once Limitless's bid reaches Kalshi's price of the
//!   side (`pk_core::exit`), with the same delay; otherwise the position is
//!   held to settlement.
//! - **Kalshi → Limitless response:** each Kalshi YES-mid move of ≥ 2¢ within
//!   1 s opens a probe; the time until Limitless's mid moves 1¢ the same way is
//!   its lag. A probe where Limitless had already moved in the second before
//!   counts as "before"; none within 10 s counts as no answer.
//!
//! Pure apart from the journal and ledger files: callers pass receive times.

use std::collections::{BTreeMap, BTreeSet, VecDeque};

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

use crate::discovery::LimWindow;

const SERIES_POINTS: usize = 1800;
const RECENT: usize = 50;
const RESP_JUMP: f64 = 0.02 - 1e-9;
const RESP_STEP: f64 = 0.01 - 1e-9;
const RESP_TIMEOUT_NS: i64 = 10 * NS;
const RESP_COOLDOWN_NS: i64 = 5 * NS;
const MARKOUT_NS: i64 = 5 * NS;
const TRAIL_NS: i64 = 1_500_000_000;

#[derive(Clone, Debug)]
pub struct EngineCfg {
    pub params: StrategyParams,
    pub shares: f64,
    pub limit_offset: f64,
    pub exit_slip: f64,
    pub active_exit: bool,
    pub fee_rate: f64,
    pub paper_extra_ms: f64,
    pub live: bool,
    pub max_open_usd: f64,
    pub daily_loss_usd: f64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
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

/// An order the live path must send (live mode only).
#[derive(Clone, Debug)]
pub struct Intent {
    pub kind: Kind,
    pub asset: String,
    pub open_unix: i64,
    pub window: LimWindow,
    pub yes: bool,
    pub limit: f64,
    pub shares: f64,
}

#[derive(Clone, Debug)]
struct Pending {
    kind: Kind,
    decided_ns: i64,
    /// Paper: when the held order matches. Live: None (the venue answers).
    due_ns: Option<i64>,
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
    l0: f64,
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

struct Asset {
    name: String,
    window: Option<LimWindow>,
    kbook: Book,
    lbook: Book,
    l_version: Option<i64>,
    consumed: bool,
    seen_reasons: BTreeSet<&'static str>,
    last_gap: Option<f64>,
    max_abs_gap: f64,
    last_decision: Option<Value>,
    pending: Option<Pending>,
    k_trail: VecDeque<(i64, f64)>,
    l_trail: VecDeque<(i64, f64)>,
    resp: Option<Resp>,
    last_resp_ns: i64,
    stat: RespStat,
    series: VecDeque<Value>,
    last_sec: i64,
    l_lag: Rolling,
    l_gap: Rolling,
    l_last_ns: Option<i64>,
    k_lag: Rolling,
}

impl Asset {
    fn new(name: &str) -> Self {
        Self {
            name: name.to_owned(),
            window: None,
            kbook: Book::default(),
            lbook: Book::default(),
            l_version: None,
            consumed: false,
            seen_reasons: BTreeSet::new(),
            last_gap: None,
            max_abs_gap: 0.0,
            last_decision: None,
            pending: None,
            k_trail: VecDeque::new(),
            l_trail: VecDeque::new(),
            resp: None,
            last_resp_ns: 0,
            stat: RespStat::default(),
            series: VecDeque::new(),
            last_sec: 0,
            l_lag: Rolling::new(2000),
            l_gap: Rolling::new(2000),
            l_last_ns: None,
            k_lag: Rolling::new(2000),
        }
    }

    fn lquotes(&self) -> (Quote, Quote) {
        let y = self.lbook.quote(None);
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

/// Shares and USDC a taker order gets from `book` within `limit`.
/// `yes`: the YES token (else NO, the mirror of the YES book); `buy` or sell.
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

pub struct Engine {
    pub cfg: EngineCfg,
    assets: Vec<Asset>,
    pub ledger: Ledger,
    journal: JsonlDaily,
    stamp: Map<String, Value>,
    counters: BTreeMap<String, u64>,
    intents: Vec<Intent>,
    recent: VecDeque<Value>,
    rtt_ms: Option<f64>,
    decide_ns: Hist,
    markouts: Vec<(i64, usize, bool, f64)>,
    markout5: Sample,
    net_per_share: Sample,
    delay_cost: Sample,
    totals: RespStat,
    bought: std::collections::HashMap<String, f64>,
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
            intents: Vec::new(),
            recent: VecDeque::new(),
            rtt_ms: None,
            decide_ns: Hist::new(),
            markouts: Vec::new(),
            markout5: Sample::default(),
            net_per_share: Sample::default(),
            delay_cost: Sample::default(),
            totals: RespStat::default(),
            bought: std::collections::HashMap::new(),
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
        if kind != "lim_resp" {
            if self.recent.len() == RECENT {
                self.recent.pop_back();
            }
            self.recent.push_front(r);
        }
    }

    pub fn take_intents(&mut self) -> Vec<Intent> {
        std::mem::take(&mut self.intents)
    }

    /// The order path's measured round trip (warm HTTP/2), for paper fills.
    pub fn set_rtt(&mut self, ms: f64) {
        self.rtt_ms = Some(ms);
    }

    pub fn window(&self, asset: &str) -> Option<&LimWindow> {
        self.assets.iter().find(|a| a.name == asset)?.window.as_ref()
    }

    pub fn tickers(&self) -> Vec<String> {
        self.assets.iter().filter_map(|a| a.window.as_ref().map(|w| w.kalshi_ticker.clone())).collect()
    }

    pub fn slugs(&self) -> Vec<String> {
        self.assets.iter().filter_map(|a| a.window.as_ref().map(|w| w.slug.clone())).collect()
    }

    /// A new window for an asset: its books start empty.
    pub fn set_window(&mut self, w: LimWindow, t_ns: i64) {
        let Some(i) = self.assets.iter().position(|a| a.name == w.asset) else { return };
        if self.assets[i].window.as_ref() == Some(&w) {
            return;
        }
        let a = &mut self.assets[i];
        a.kbook = Book::default();
        a.lbook = Book::default();
        a.l_version = None;
        a.consumed = false;
        a.seen_reasons.clear();
        a.last_gap = None;
        a.max_abs_gap = 0.0;
        a.last_decision = None;
        a.k_trail.clear();
        a.l_trail.clear();
        a.resp = None;
        // An entry still held by the venue keeps its window; anything else is gone.
        if a.pending.as_ref().is_some_and(|p| p.kind == Kind::Exit || p.due_ns.is_some()) {
            a.pending = None;
        }
        a.window = Some(w.clone());
        self.count("windows");
        self.record(t_ns, "lim_window", serde_json::to_value(&w).unwrap_or(Value::Null));
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
        let Some(l0) = a.lbook.quote(None).mid() else { return };
        let dir = (kmid - k1).signum();
        a.last_resp_ns = t;
        let before = trail_at(&a.l_trail, t - NS).is_some_and(|l1| dir * (l0 - l1) >= RESP_STEP);
        if before {
            a.stat.n += 1;
            a.stat.before += 1;
            a.stat.responded += 1;
            self.totals.n += 1;
            self.totals.before += 1;
            self.totals.responded += 1;
            let r = json!({"asset": a.name, "jump": round_dp(kmid - k1, 3), "lag_ms": null, "before": true});
            self.record(t, "lim_resp", r);
        } else {
            a.resp = Some(Resp { t0: t, dir, l0 });
        }
    }

    fn resp_answered(&mut self, i: usize, lmid: f64, t: i64) {
        let a = &mut self.assets[i];
        let Some(r) = &a.resp else { return };
        if r.dir * (lmid - r.l0) < RESP_STEP {
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
        self.record(t, "lim_resp", r);
    }

    /// A Kalshi reconnect or resync: its books are invalid until fresh snapshots.
    pub fn invalidate_kalshi(&mut self) {
        for a in &mut self.assets {
            a.kbook.invalidate();
        }
        self.count("kalshi_resets");
    }

    /// One Limitless line (`l`): `{"ev": .., "d": ..}`, or our reconnect marker.
    pub fn on_limitless(&mut self, text: &str, t: i64) {
        let Ok(v) = serde_json::from_str::<Value>(text) else { return };
        if v.get("type").and_then(Value::as_str) == Some("pk_reconnect") {
            for a in &mut self.assets {
                a.lbook.invalidate();
                a.l_version = None;
            }
            self.count("lim_resets");
            return;
        }
        if v.get("ev").and_then(Value::as_str) != Some("orderbookUpdate") {
            return;
        }
        let d = &v["d"];
        let slug = d.get("marketSlug").and_then(Value::as_str).unwrap_or_default();
        let Some(i) = self.assets.iter().position(|a| a.window.as_ref().is_some_and(|w| w.slug == slug)) else {
            return;
        };
        let version = d.get("version").and_then(Value::as_i64).unwrap_or(0);
        let a = &mut self.assets[i];
        // A database snapshot carries 0: only before any live frame.
        if a.l_version.is_some_and(|last| version < last && version != 0 || version == 0 && last > 0) {
            self.count("lim_stale_frames");
            return;
        }
        a.l_version = Some(version);
        let side = |k: &str| -> Vec<(f64, f64)> {
            d.pointer(&format!("/orderbook/{k}"))
                .and_then(Value::as_array)
                .map(|l| {
                    l.iter()
                        .filter_map(|x| Some((x.get("price")?.as_f64()?, x.get("size")?.as_f64()? / 1e6)))
                        .collect()
                })
                .unwrap_or_default()
        };
        a.lbook.snapshot(&side("bids"), &side("asks"), t);
        if let Some(ts) = d.get("timestamp").and_then(Value::as_str)
            && let Ok(stamp) = chrono::DateTime::parse_from_rfc3339(ts)
        {
            a.l_lag.push(t as f64 / 1e6 - stamp.timestamp_millis() as f64);
        }
        if let Some(prev) = a.l_last_ns {
            a.l_gap.push((t - prev) as f64 / 1e6);
        }
        a.l_last_ns = Some(t);
        if let Some(m) = a.lbook.quote(None).mid() {
            push_trail(&mut a.l_trail, t, m);
            self.resp_answered(i, m, t);
        }
        self.evaluate(i, t);
    }

    /// Timers: held paper orders, probe timeouts, markouts, the per-second series.
    pub fn tick(&mut self, t: i64) {
        for i in 0..self.assets.len() {
            if self.assets[i].pending.as_ref().and_then(|p| p.due_ns).is_some_and(|d| t >= d) {
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
                    let l = a.lbook.quote(None);
                    if a.series.len() == SERIES_POINTS {
                        a.series.pop_front();
                    }
                    a.series.push_back(json!([sec, w.open_unix, k.mid(), l.mid(), l.bid, l.ask]));
                }
            }
            self.evaluate(i, t);
        }
        let due: Vec<_> = self.markouts.iter().filter(|m| t >= m.0).cloned().collect();
        self.markouts.retain(|m| t < m.0);
        for (_, i, yes, vwap) in due {
            if let Some(m) = self.assets[i].lquotes().0.mid() {
                let mid = if yes { m } else { 1.0 - m };
                self.markout5.push((mid - vwap) * 100.0);
            }
        }
    }

    fn evaluate(&mut self, i: usize, t: i64) {
        let Some(w) = self.assets[i].window.clone() else { return };
        if self.assets[i].pending.is_some() {
            return;
        }
        let key = format!("{}:{}", w.asset, w.open_unix);
        let held = self.ledger.positions.get(&key).filter(|p| p.is_open()).cloned();
        match held {
            Some(p) => self.maybe_exit(i, &w, &p, t),
            None => self.maybe_enter(i, &w, t),
        }
    }

    fn fee(&self, w: &LimWindow, price: f64, shares: f64) -> f64 {
        if w.fee { taker_fee(price, shares, self.cfg.fee_rate, 1.0) } else { 0.0 }
    }

    fn hold_ns(&self, w: &LimWindow) -> i64 {
        ((self.rtt_ms.unwrap_or(50.0) + w.taker_delay_ms + self.cfg.paper_extra_ms) * 1e6) as i64
    }

    fn maybe_enter(&mut self, i: usize, w: &LimWindow, t: i64) {
        let a = &self.assets[i];
        if a.consumed {
            return;
        }
        let k = a.kbook.quote(None);
        let (ly, ln) = a.lquotes();
        let sec_into = t as f64 / 1e9 - w.open_unix as f64;
        let hour = chrono::DateTime::from_timestamp_nanos(t).format("%H").to_string().parse().ok();
        let t0 = std::time::Instant::now();
        let d = decide(&k, &ly, &ln, &self.cfg.params, sec_into, false, hour, None, (self.cfg.fee_rate, 1.0));
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
        let a = &mut self.assets[i];
        a.consumed = true;
        let yes = d.buy_yes.unwrap_or(true);
        let touch = d.entry_px.unwrap_or(0.0);
        let mut limit = (touch + self.cfg.limit_offset).min(d.max_px.unwrap_or(0.999)).min(0.999);
        limit = (limit * 1e3 + 1e-6).floor() / 1e3;
        let instant = take(&a.lbook, yes, true, limit, self.cfg.shares);
        let mut rec = json!({
            "asset": w.asset, "open_unix": w.open_unix, "sec_into": round_dp(sec_into, 3),
            "action": d.action.as_str(), "reason": d.reason, "gap": d.gap.map(|g| round_dp(g, 4)),
            "side": if yes { "yes" } else { "no" }, "entry_px": d.entry_px, "limit": limit,
            "shares": self.cfg.shares, "kalshi": {"bid": k.bid, "ask": k.ask},
            "lim": {"bid": ly.bid, "ask": ly.ask}, "instant_shares": round_dp(instant.0, 3),
        });
        if d.action == Action::Enter {
            self.count("entries_decided");
            let cost = limit * self.cfg.shares;
            let blocked = if self.ledger.open_notional() + cost > self.cfg.max_open_usd + 1e-9 {
                Some("max_open")
            } else if -self.ledger.realised_today(t) >= self.cfg.daily_loss_usd {
                Some("daily_loss")
            } else if !self.ledger.can_afford(cost * (1.0 + self.cfg.fee_rate)) {
                Some("cash")
            } else {
                None
            };
            if let Some(b) = blocked {
                rec["blocked"] = b.into();
                self.count(&format!("blocked_{b}"));
            } else {
                let due_ns = (!self.cfg.live).then(|| t + self.hold_ns(w));
                self.assets[i].pending = Some(Pending {
                    kind: Kind::Entry,
                    decided_ns: t,
                    due_ns,
                    yes,
                    limit,
                    shares: self.cfg.shares,
                    touch,
                    instant,
                });
                if self.cfg.live {
                    self.intents.push(Intent {
                        kind: Kind::Entry,
                        asset: w.asset.clone(),
                        open_unix: w.open_unix,
                        window: w.clone(),
                        yes,
                        limit,
                        shares: self.cfg.shares,
                    });
                }
            }
        } else {
            self.count("skips");
        }
        self.assets[i].last_decision = Some(rec.clone());
        self.record(t, "lim_decision", rec);
    }

    fn maybe_exit(&mut self, i: usize, w: &LimWindow, p: &Position, t: i64) {
        if !self.cfg.active_exit || p.shares < 1.0 {
            return;
        }
        let a = &self.assets[i];
        let yes = p.side == "yes";
        let k = a.kbook.quote(None);
        let (ly, ln) = a.lquotes();
        let bid = if yes { ly.bid } else { ln.bid };
        let (Some(bid), Some(fair)) = (bid, kalshi_fair(&k, yes)) else { return };
        let vwap = p.vwap();
        let floor = exit_floor(vwap, self.cfg.fee_rate, 1.0);
        if !should_exit(bid, fair, vwap) || bid < floor - 1e-9 {
            return;
        }
        let limit = ((bid - self.cfg.exit_slip).max(floor) * 1e3 - 1e-6).ceil() / 1e3;
        let instant = take(&a.lbook, yes, false, limit, p.shares);
        let due_ns = (!self.cfg.live).then(|| t + self.hold_ns(w));
        self.assets[i].pending =
            Some(Pending { kind: Kind::Exit, decided_ns: t, due_ns, yes, limit, shares: p.shares, touch: bid, instant });
        self.count("exits_decided");
        if self.cfg.live {
            self.intents.push(Intent {
                kind: Kind::Exit,
                asset: w.asset.clone(),
                open_unix: w.open_unix,
                window: w.clone(),
                yes,
                limit,
                shares: p.shares.floor(),
            });
        }
        let r = json!({"asset": w.asset, "open_unix": w.open_unix, "side": p.side, "bid": bid,
                       "fair": round_dp(fair, 4), "floor": floor, "limit": limit, "shares": p.shares});
        self.record(t, "lim_exit_decision", r);
    }

    fn fill_paper(&mut self, i: usize, t: i64) {
        let Some(p) = self.assets[i].pending.take() else { return };
        let Some(w) = self.assets[i].window.clone() else { return };
        let got = take(&self.assets[i].lbook, p.yes, p.kind == Kind::Entry, p.limit, p.shares);
        self.book_fill(i, &w, &p, got.0, got.1, None, t);
    }

    /// The live path's answer for an asset's pending order.
    /// `w` is the order's own window: a fill that lands after the roll still counts.
    pub fn live_result(&mut self, w: &LimWindow, shares: f64, usd: f64, fee: Option<f64>, detail: Value, t: i64) {
        let Some(i) = self.assets.iter().position(|a| a.name == w.asset) else { return };
        self.record(t, "lim_live_order", detail);
        let Some(p) = self.assets[i].pending.take() else { return };
        self.book_fill(i, w, &p, shares, usd, fee, t);
    }

    #[allow(clippy::too_many_arguments)]
    fn book_fill(&mut self, i: usize, w: &LimWindow, p: &Pending, shares: f64, usd: f64, fee: Option<f64>, t: i64) {
        let side = if p.yes { "yes" } else { "no" };
        let vwap = if shares > 0.0 { usd / shares } else { 0.0 };
        let fee = fee.unwrap_or_else(|| self.fee(w, vwap, shares));
        let (ishares, iusd) = p.instant;
        let ivwap = if ishares > 0.0 { iusd / ishares } else { 0.0 };
        let mut rec = json!({
            "asset": w.asset, "open_unix": w.open_unix, "kind": p.kind.as_str(), "side": side,
            "limit": p.limit, "touch_px": p.touch, "shares": round_dp(shares, 3), "vwap": round_dp(vwap, 4),
            "fee": round_dp(fee, 5), "instant_shares": round_dp(ishares, 3), "instant_vwap": round_dp(ivwap, 4),
            "held_ms": round_dp((t - p.decided_ns) as f64 / 1e6, 1), "venue": if self.cfg.live { "live" } else { "paper" },
        });
        if shares < 1e-9 {
            self.count(&format!("{}_missed", p.kind.as_str()));
            if p.kind == Kind::Exit {
                self.assets[i].pending = None; // re-armed by the next update
            }
            self.record(t, "lim_no_fill", rec);
            return;
        }
        match p.kind {
            Kind::Entry => {
                self.count("entries_filled");
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
                self.record(t, "lim_fill", rec);
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
                self.record(t, "lim_exit", rec);
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

    /// Open positions whose window has closed: `(asset, open_unix, slug)`.
    pub fn unsettled(&self, t: i64) -> Vec<(String, i64)> {
        self.ledger
            .open_positions()
            .filter(|p| t / NS > p.open_unix + WINDOW_S + 20)
            .map(|p| (p.asset.clone(), p.open_unix))
            .collect()
    }

    pub fn settle(&mut self, asset: &str, open_unix: i64, outcome: &str, t: i64) {
        if let Some(p) = self.ledger.settle(asset, open_unix, outcome, t) {
            self.count("settled");
            self.settled(&p);
            let r = json!({"asset": asset, "open_unix": open_unix, "side": p.side, "outcome": outcome,
                           "shares": p.shares, "payout": p.payout, "pnl": p.pnl.map(|x| round_dp(x, 4))});
            self.record(t, "lim_settle", r);
        }
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
        json!({"fields": ["sec", "open_unix", "kalshi_mid", "lim_mid", "lim_bid", "lim_ask"], "points": points})
    }

    pub fn status(&self, t: i64) -> Value {
        let q = |r: &Rolling, p: f64| round_opt(r.quantile(p), 1);
        let assets: Map<String, Value> = self
            .assets
            .iter()
            .map(|a| {
                let k = a.kbook.quote(None);
                let (ly, _) = a.lquotes();
                let w = a.window.as_ref();
                let depth = |bids: bool| -> f64 {
                    let m = if bids { &a.lbook.bids } else { &a.lbook.asks };
                    let best = if bids { m.keys().next_back() } else { m.keys().next() };
                    best.and_then(|k| m.get(k)).copied().unwrap_or(0.0)
                };
                let held = w.and_then(|w| self.ledger.positions.get(&format!("{}:{}", w.asset, w.open_unix)));
                (
                    a.name.clone(),
                    json!({
                        "open_unix": w.map(|w| w.open_unix),
                        "slug": w.map(|w| w.slug.clone()),
                        "identified": w.is_some(),
                        "sec_into": w.map(|w| round_dp(t as f64 / 1e9 - w.open_unix as f64, 1)),
                        "taker_delay_ms": w.map(|w| w.taker_delay_ms),
                        "consumed": a.consumed,
                        "kalshi": {"bid": k.bid, "ask": k.ask, "mid": k.mid(),
                                   "age_ms": a.kbook.last_update_ns.map(|u| (t - u) / 1_000_000)},
                        "lim": {"bid": ly.bid, "ask": ly.ask, "mid": ly.mid(),
                                "spread": match (ly.bid, ly.ask) { (Some(b), Some(x)) => Some(round_dp(x - b, 3)), _ => None },
                                "bid_size": round_dp(depth(true), 1), "ask_size": round_dp(depth(false), 1),
                                "version": a.l_version,
                                "age_ms": a.lbook.last_update_ns.map(|u| (t - u) / 1_000_000)},
                        "gap": a.last_gap.map(|g| round_dp(g, 4)),
                        "max_abs_gap": round_dp(a.max_abs_gap, 4),
                        "decision": a.last_decision,
                        "pending": a.pending.as_ref().map(|p| json!({"kind": p.kind.as_str(), "limit": p.limit,
                            "age_ms": (t - p.decided_ns) / 1_000_000})),
                        "position": held,
                        "response": a.stat.json(),
                        "feed": {
                            "lim_lag_ms_p50": q(&a.l_lag, 0.5), "lim_lag_ms_p90": q(&a.l_lag, 0.9),
                            "lim_lag_ms_p99": q(&a.l_lag, 0.99),
                            "lim_interval_ms_p50": q(&a.l_gap, 0.5), "lim_interval_ms_p90": q(&a.l_gap, 0.9),
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
        json!({
            "mode": if self.cfg.live { "live" } else { "paper" },
            "stamp": self.stamp,
            "params": self.cfg.params,
            "shares": self.cfg.shares,
            "limit_offset": self.cfg.limit_offset,
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

    fn book(bids: &[(f64, f64)], asks: &[(f64, f64)]) -> Book {
        let mut b = Book::default();
        b.snapshot(bids, asks, 1);
        b
    }

    #[test]
    fn taking_both_tokens() {
        let b = book(&[(0.497, 250.0), (0.478, 71.0)], &[(0.552, 33.0), (0.556, 1.0), (0.562, 302.0)]);
        let (s, u) = take(&b, true, true, 0.556, 50.0);
        assert!((s - 34.0).abs() < 1e-9 && (u - (33.0 * 0.552 + 0.556)).abs() < 1e-9);
        // NO asks are 1 − YES bids: 0.503 for 250.
        let (s, u) = take(&b, false, true, 0.51, 10.0);
        assert!((s - 10.0).abs() < 1e-9 && (u - 5.03).abs() < 1e-9);
        // Selling YES hits the bids down to the limit.
        let (s, _) = take(&b, true, false, 0.49, 300.0);
        assert!((s - 250.0).abs() < 1e-9);
        // Selling NO lifts... the YES asks, at 1 − ask: 0.448 for 33 above a 0.44 floor.
        let (s, _) = take(&b, false, false, 0.444, 100.0);
        assert!((s - 34.0).abs() < 1e-9);
    }

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
            fee_rate: 0.05,
            paper_extra_ms: 0.0,
            live: false,
            max_open_usd: 100.0,
            daily_loss_usd: 100.0,
        };
        let dir = std::env::temp_dir().join(format!("pk-lim-test-{}", std::process::id()));
        Engine::new(cfg, &["BTC".into()], Ledger::new(100.0), JsonlDaily::new(dir), Map::new())
    }

    fn window(open: i64) -> LimWindow {
        LimWindow {
            asset: "BTC".into(),
            open_unix: open,
            kalshi_ticker: "KX".into(),
            slug: "btc-s".into(),
            yes_token: "1".into(),
            no_token: "2".into(),
            condition_id: "0x1".into(),
            exchange: "0x0".into(),
            fee_module: None,
            taker_delay_ms: 500.0,
            fee: true,
        }
    }

    fn lim(asks: &str, bids: &str, v: i64) -> String {
        format!(r#"{{"ev":"orderbookUpdate","d":{{"marketSlug":"btc-s","version":{v},"orderbook":{{"bids":{bids},"asks":{asks}}},"timestamp":"2026-10-23T09:50:00.000Z"}}}}"#)
    }

    #[test]
    fn paper_entry_fills_after_the_delay_against_the_later_book() {
        let mut e = engine();
        let open = 1_792_748_700;
        let t = (open + 100) * NS;
        e.set_window(window(open), t);
        e.on_limitless(&lim(r#"[{"price":0.46,"size":50000000}]"#, r#"[{"price":0.44,"size":50000000}]"#, 1), t);
        let k = r#"{"type":"orderbook_snapshot","msg":{"market_ticker":"KX","yes_dollars_fp":[["0.70","100"]],"no_dollars_fp":[["0.29","100"]]}}"#;
        e.on_kalshi(k, t + 1);
        let st = e.status(t + 1);
        assert_eq!(st["assets"]["BTC"]["pending"]["kind"], "entry");
        // The maker pulls 0.46 during the hold; 0.49 is still inside the limit.
        e.on_limitless(&lim(r#"[{"price":0.49,"size":50000000}]"#, r#"[{"price":0.44,"size":50000000}]"#, 2), t + 100_000_000);
        e.tick(t + 300_000_000);
        assert!(e.ledger.positions.is_empty(), "still held");
        e.tick(t + 600_000_000);
        let p = e.ledger.positions.values().next().expect("filled");
        assert!((p.vwap() - 0.49).abs() < 1e-9 && (p.shares - 10.0).abs() < 1e-9);
        assert!((p.fee - taker_fee(0.49, 10.0, 0.05, 1.0)).abs() < 1e-12);
        assert_eq!(e.status(t)["gates"]["delay_cost_cents"]["n"], 1);
        // Settles: YES won.
        e.settle("BTC", open, "yes", (open + 950) * NS);
        assert!(e.ledger.positions.values().all(|p| !p.is_open()));
    }

    #[test]
    fn response_lag_is_timed() {
        let mut e = engine();
        let open = 1_792_748_700;
        let t = (open + 100) * NS;
        e.set_window(window(open), t);
        e.on_limitless(&lim(r#"[{"price":0.51,"size":5000000}]"#, r#"[{"price":0.49,"size":5000000}]"#, 1), t);
        let snap = |y: &str, n: &str| format!(r#"{{"type":"orderbook_snapshot","msg":{{"market_ticker":"KX","yes_dollars_fp":[["{y}","100"]],"no_dollars_fp":[["{n}","100"]]}}}}"#);
        e.on_kalshi(&snap("0.49", "0.49"), t); // mid 0.50
        e.on_kalshi(&snap("0.52", "0.46"), t + 1_200_000_000); // mid 0.53, 3¢ in a second
        e.on_limitless(&lim(r#"[{"price":0.53,"size":5000000}]"#, r#"[{"price":0.51,"size":5000000}]"#, 2), t + 1_500_000_000);
        let r = &e.status(t)["response"];
        assert_eq!((r["n"].as_u64(), r["responded"].as_u64()), (Some(1), Some(1)));
        assert_eq!(r["lag_ms_p50"].as_f64(), Some(300.0));
    }
}
