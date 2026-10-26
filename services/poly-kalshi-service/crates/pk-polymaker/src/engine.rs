//! The maker: two resting post-only buys per coin on Polymarket's 15-minute
//! Up/Down market, priced off Kalshi's YES mid for the same window.
//!
//! Why a maker: Polymarket holds every taker order about 210 ms, and in that
//! hold its makers pull the trades worth having (the live taker filled 24% of
//! its entries, the misses worth +20.8¢/share against +4.2¢ for the fills).
//! Resting orders and cancels are not held. The 4.5-day tape replay of this
//! rule (h = 0.02, Kalshi 100 ms late) made +1.00¢ ± 0.41 per share filled.
//!
//! - **Fair value:** q = Kalshi's YES mid, when its spread is at most
//!   `max_kalshi_spread` and its book is fresh. No fair value, no quotes.
//! - **Quotes:** buy Up at `floor(q − h − skew)` and buy Down at
//!   `floor(1 − (q + h − skew))` in 1¢ ticks. `skew = skew_k · net / max_net`,
//!   net = Up − Down shares this window. Each price is kept strictly below
//!   its token's best ask (post-only), within `min_px..=max_px`.
//! - **Not quoted:** a side whose fill would take |net| past `max_net`, or
//!   open cost past `max_open_cost_usd`; the window's first `entry_early_s`
//!   and last `stop_s`; a pause (Kalshi mid moved `pause_jump` within 1 s:
//!   `pause_ms`); stale or reconnected books; halt, kill or stale feeds.
//! - **Requotes:** a target must hold before the bid moves (Kalshi's mid
//!   flickers across a cent): a bid above its target is cancelled once that
//!   has lasted `adverse_ms`, one below it is raised once a gap of
//!   `requote_cents` has lasted `min_requote_ms`. Jumps of `pause_jump` pull
//!   at once (the pause), as do the gates above.
//! - **Fills** come from the venue's own order state (`size_matched`), polled
//!   by the executor; makers pay no fee. Up + Down pairs pay $1 whatever the
//!   outcome; the net settles on Polymarket's resolution.
//! - **Risk:** realised P&L today at or below −`daily_loss_usd` latches a
//!   halt (persisted) until the next UTC day or an ack.

use std::collections::{BTreeMap, HashMap, VecDeque};

use pk_core::book::Book;
use pk_core::journal::{JsonlDaily, Sink};
use pk_core::ledger::utc_day;
use pk_core::num::{px_key, round_dp, round_opt};
use pk_core::{BookSide, NS, WINDOW_S, Window};
use pk_stats::Sample;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

const TICK: f64 = 0.01;
const TRAIL_MS: i64 = 5_000;
const RECENT: usize = 80;
const MARKOUTS_MS: [i64; 2] = [5_000, 30_000];

#[derive(Clone, Debug)]
pub struct PmCfg {
    pub half_spread: f64,
    pub shares: f64,
    pub min_shares: f64,
    pub max_net: f64,
    pub skew_k: f64,
    pub pause_jump: f64,
    pub pause_ms: i64,
    pub max_kalshi_spread: f64,
    pub kalshi_max_age_ms: i64,
    pub min_px: f64,
    pub max_px: f64,
    pub entry_early_s: i64,
    pub stop_s: i64,
    pub requote_cents: f64,
    pub min_requote_ms: i64,
    pub adverse_ms: i64,
    pub reject_cooldown_ms: i64,
    pub max_orders_per_s: f64,
    pub daily_loss_usd: f64,
    pub max_open_cost_usd: f64,
}

/// What the executor must do.
#[derive(Clone, Debug, PartialEq)]
pub enum Action {
    Post { asset: String, up: bool, token: String, px: f64, size: f64 },
    Cancel { asset: String, up: bool, order_id: String },
    CancelAll { reason: String },
}

fn floor_tick(x: f64) -> f64 {
    round_dp((x / TICK + 1e-6).floor() * TICK, 2)
}

/// The two bid prices (Up, Down) before the size and cost checks.
pub fn desired_px(cfg: &PmCfg, q: f64, net: f64, up_ask: Option<f64>, down_ask: Option<f64>) -> (Option<f64>, Option<f64>) {
    let skew = if cfg.max_net > 0.0 { cfg.skew_k * net / cfg.max_net } else { 0.0 };
    let clamp = |px: f64, ask: Option<f64>| -> Option<f64> {
        let ask = ask?;
        let px = if px >= ask - 1e-9 { round_dp(ask - TICK, 2) } else { px };
        (cfg.min_px - 1e-9..=cfg.max_px + 1e-9).contains(&px).then_some(px)
    };
    let up = clamp(floor_tick(q - cfg.half_spread - skew), up_ask);
    let down = clamp(floor_tick(1.0 - (q + cfg.half_spread - skew)), down_ask);
    (up, down)
}

/// Shares a new bid on this side may rest for: None if under the venue minimum.
pub fn room(cfg: &PmCfg, net: f64, up: bool) -> Option<f64> {
    let r = if up { cfg.max_net - net } else { cfg.max_net + net };
    let size = cfg.shares.min(r).floor();
    (size >= cfg.min_shares - 1e-9).then_some(size)
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Inventory {
    pub asset: String,
    pub open_unix: i64,
    pub condition_id: String,
    pub up_token: String,
    pub down_token: String,
    pub up: f64,
    pub up_cost: f64,
    pub down: f64,
    pub down_cost: f64,
    pub fills: u64,
    pub settled: Option<String>,
    pub pnl: Option<f64>,
    pub settled_ns: Option<i64>,
    pub redeemed: Option<String>,
    pub redeem_tries: u32,
}

impl Inventory {
    pub fn net(&self) -> f64 {
        self.up - self.down
    }

    pub fn cost(&self) -> f64 {
        self.up_cost + self.down_cost
    }

    pub fn settle_pnl(&self, outcome: &str) -> f64 {
        (if outcome == "yes" { self.up } else { self.down }) - self.cost()
    }

    /// Matched pairs pay $1 whatever happens.
    pub fn locked_pnl(&self) -> f64 {
        let pairs = self.up.min(self.down);
        if pairs <= 0.0 {
            return 0.0;
        }
        pairs * (1.0 - self.up_cost / self.up - self.down_cost / self.down)
    }

    fn close_ns(&self) -> i64 {
        (self.open_unix + WINDOW_S) * NS
    }
}

/// One order we sent, until its final state is known.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct OrderRec {
    pub id: String,
    pub key: String,
    pub asset: String,
    pub up: bool,
    pub px: f64,
    pub size: f64,
    pub matched: f64,
    pub posted_ns: i64,
    /// Cancelled (or gone): no more fills after the final state.
    pub closing: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Halt {
    pub day: String,
    pub reason: String,
    pub t_ns: i64,
}

#[derive(Default, Serialize, Deserialize)]
struct Persist {
    inventories: BTreeMap<String, Inventory>,
    realised_by_day: BTreeMap<String, f64>,
    orders: BTreeMap<String, OrderRec>,
    halt: Option<Halt>,
    /// After an ack: today's limit counts from this realised P&L.
    ack: Option<(String, f64)>,
}

#[derive(Default)]
struct Slot {
    order: Option<String>,
    px: f64,
    remaining: f64,
    inflight: bool,
    last_change_ms: i64,
    cooldown_until_ms: i64,
    /// Since when the target has been above (+1) or below (−1) the resting bid.
    off: Option<(i8, i64)>,
}

struct Coin {
    name: String,
    window: Option<Window>,
    kbook: Book,
    upb: Book,
    downb: Book,
    trail: VecDeque<(i64, f64)>,
    pause_until_ms: i64,
    slots: [Slot; 2],
    quoting_ms: i64,
    live_ms: i64,
    last_tick_ms: Option<i64>,
    pauses: u64,
    gate: &'static str,
}

impl Coin {
    fn new(name: &str) -> Self {
        Self {
            name: name.to_owned(),
            window: None,
            kbook: Book::default(),
            upb: Book::default(),
            downb: Book::default(),
            trail: VecDeque::new(),
            pause_until_ms: 0,
            slots: [Slot::default(), Slot::default()],
            quoting_ms: 0,
            live_ms: 0,
            last_tick_ms: None,
            pauses: 0,
            gate: "no window",
        }
    }

    fn key(&self) -> Option<String> {
        self.window.as_ref().map(|w| wkey(&w.asset, w.open_unix))
    }

    /// Kalshi's YES mid, if tight and fresh.
    fn fair(&self, now_ms: i64, cfg: &PmCfg) -> Option<f64> {
        let q = self.kbook.quote(None);
        let (b, a) = (q.bid?, q.ask?);
        let fresh = self.kbook.last_update_ns.is_some_and(|u| now_ms - u / 1_000_000 <= cfg.kalshi_max_age_ms);
        (q.valid && a >= b && a - b <= cfg.max_kalshi_spread + 1e-9 && fresh).then(|| (a + b) / 2.0)
    }
}

pub fn wkey(asset: &str, open_unix: i64) -> String {
    format!("{asset}-{open_unix}")
}

#[derive(Default)]
struct Stat {
    fills: u64,
    shares: f64,
    capture: Sample,
    markout5: Sample,
    markout30: Sample,
    pnl: f64,
    windows: u64,
}

impl Stat {
    fn json(&self) -> Value {
        let c = |s: &Sample| json!({"n": s.n(), "mean_c": round_opt(s.mean().map(|m| m * 100.0), 2), "se_c": round_opt(s.se().map(|m| m * 100.0), 2)});
        json!({
            "fills": self.fills,
            "shares": round_dp(self.shares, 2),
            "capture": c(&self.capture),
            "markout5": c(&self.markout5),
            "markout30": c(&self.markout30),
            "pnl": round_dp(self.pnl, 4),
            "windows": self.windows,
            "pnl_per_share_c": (self.shares > 0.0).then(|| round_dp(self.pnl / self.shares * 100.0, 2)),
        })
    }
}

/// Markout waiting for its time.
struct Due {
    t_ms: i64,
    horizon: usize,
    asset: String,
    open_unix: i64,
    up: bool,
    px: f64,
    shares: f64,
}

pub struct Gates {
    pub kill: bool,
    pub feeds_ok: bool,
}

pub struct Engine {
    pub cfg: PmCfg,
    coins: Vec<Coin>,
    /// Polymarket token → (coin index, is Up).
    owner: HashMap<String, (usize, bool)>,
    inv: BTreeMap<String, Inventory>,
    orders: BTreeMap<String, OrderRec>,
    realised_by_day: BTreeMap<String, f64>,
    halt: Option<Halt>,
    ack: Option<(String, f64)>,
    state_path: std::path::PathBuf,
    journal: JsonlDaily,
    stamp: Map<String, Value>,
    actions: Vec<Action>,
    bucket: f64,
    bucket_ms: i64,
    stopped: bool,
    dues: Vec<Due>,
    totals: Stat,
    per: BTreeMap<String, Stat>,
    counters: BTreeMap<String, u64>,
    recent: VecDeque<Value>,
    series: VecDeque<Value>,
    drift: HashMap<String, (u32, f64)>,
    shadow_n: u64,
    shadow: bool,
}

impl Engine {
    pub fn new(cfg: PmCfg, assets: &[String], dir: &std::path::Path, stamp: Map<String, Value>, shadow: bool) -> Self {
        let state_path = dir.join("state.json");
        let p: Persist = std::fs::read(&state_path).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default();
        let bucket = cfg.max_orders_per_s;
        Self {
            coins: assets.iter().map(|a| Coin::new(a)).collect(),
            owner: HashMap::new(),
            inv: p.inventories,
            orders: if shadow { BTreeMap::new() } else { p.orders },
            realised_by_day: p.realised_by_day,
            halt: p.halt,
            ack: p.ack,
            state_path,
            journal: JsonlDaily::new(dir.join("journal")),
            stamp,
            actions: Vec::new(),
            bucket,
            bucket_ms: 0,
            stopped: false,
            dues: Vec::new(),
            totals: Stat::default(),
            per: BTreeMap::new(),
            counters: BTreeMap::new(),
            recent: VecDeque::new(),
            series: VecDeque::new(),
            drift: HashMap::new(),
            shadow_n: 0,
            shadow,
            cfg,
        }
    }

    pub fn save(&self) {
        let p = Persist {
            inventories: self.inv.clone(),
            realised_by_day: self.realised_by_day.clone(),
            orders: self.orders.clone(),
            halt: self.halt.clone(),
            ack: self.ack.clone(),
        };
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

    pub fn record(&mut self, t_ns: i64, kind: &str, mut r: Value) {
        if let Some(o) = r.as_object_mut() {
            o.insert("type".into(), kind.into());
            o.insert("t_ns".into(), t_ns.into());
            for (k, v) in &self.stamp {
                o.insert(k.clone(), v.clone());
            }
        }
        self.journal.write(t_ns, &r);
        if !matches!(kind, "pm_quote" | "pm_order") {
            if self.recent.len() == RECENT {
                self.recent.pop_back();
            }
            self.recent.push_front(r);
        }
    }

    // --- windows and books -----------------------------------------------------

    pub fn window(&self, asset: &str) -> Option<&Window> {
        self.coins.iter().find(|c| c.name == asset)?.window.as_ref()
    }

    pub fn tickers(&self) -> Vec<String> {
        self.coins.iter().filter_map(|c| c.window.as_ref().map(|w| w.kalshi_ticker.clone())).collect()
    }

    pub fn tokens(&self) -> std::collections::BTreeSet<String> {
        self.owner.keys().cloned().collect()
    }

    pub fn set_window(&mut self, w: Window, t_ns: i64) {
        let Some(i) = self.coins.iter().position(|c| c.name == w.asset) else { return };
        if self.coins[i].window.as_ref() == Some(&w) {
            return;
        }
        if let Some(old) = self.coins[i].window.take() {
            self.owner.remove(&old.yes_token);
            self.owner.remove(&old.no_token);
        }
        self.owner.insert(w.yes_token.clone(), (i, true));
        self.owner.insert(w.no_token.clone(), (i, false));
        let c = &mut self.coins[i];
        c.kbook = Book::default();
        c.upb = Book::default();
        c.downb = Book::default();
        c.trail.clear();
        c.pause_until_ms = 0;
        c.window = Some(w.clone());
        self.count("windows");
        self.record(t_ns, "pm_window", serde_json::to_value(&w).unwrap_or(Value::Null));
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
        let Some(i) = self.coins.iter().position(|c| c.window.as_ref().is_some_and(|w| w.kalshi_ticker == ticker)) else {
            return;
        };
        let c = &mut self.coins[i];
        if kind == Some("orderbook_snapshot") {
            let yes = msg.levels(true).into_iter().map(|(p, s)| (px_key(p), s));
            let no = msg.levels(false).into_iter().map(|(p, s)| (px_key(1.0 - p), s));
            c.kbook.snapshot_keys(yes, no, t);
        } else {
            if c.kbook.snapshot_ns.is_none() {
                return;
            }
            let Some((price, delta)) = msg.delta() else { return };
            if msg.side.as_deref() == Some("yes") {
                c.kbook.add_level(BookSide::Bid, px_key(price), delta, t);
            } else {
                c.kbook.add_level(BookSide::Ask, px_key(1.0 - price), delta, t);
            }
        }
        let Some(m) = c.kbook.quote(None).mid() else { return };
        let now_ms = t / 1_000_000;
        let base = c.trail.iter().filter(|(ts, _)| *ts >= now_ms - 1000).map(|(_, v)| *v);
        let moved = base.fold(0.0_f64, |acc, v| acc.max((m - v).abs()));
        c.trail.push_back((now_ms, m));
        while c.trail.front().is_some_and(|(ts, _)| *ts < now_ms - TRAIL_MS) {
            c.trail.pop_front();
        }
        if moved >= self.cfg.pause_jump - 1e-9 {
            let fresh = c.pause_until_ms < now_ms;
            c.pause_until_ms = now_ms + self.cfg.pause_ms;
            if fresh {
                c.pauses += 1;
            }
            // Pull at once, not at the next tick.
            self.tick_coin(i, t, &Gates { kill: false, feeds_ok: true });
        }
    }

    pub fn invalidate_kalshi(&mut self) {
        for c in &mut self.coins {
            c.kbook.invalidate();
        }
        self.count("kalshi_resets");
    }

    pub fn invalidate_poly(&mut self) {
        for c in &mut self.coins {
            c.upb.invalidate();
            c.downb.invalidate();
        }
        self.count("poly_resets");
    }

    /// A Polymarket market-channel frame (`p` line).
    pub fn on_poly(&mut self, text: &str, t_ns: i64) {
        let Ok(msgs) = pk_engine::parse::poly_msgs(text) else { return };
        for msg in msgs.iter().flatten() {
            match msg.kind() {
                Some("book") => {
                    let Some(id) = msg.asset_id.as_ref().map(|i| i.as_str()) else { continue };
                    let Some(&(i, up)) = self.owner.get(id) else { continue };
                    let lv = |v: &Option<Vec<pk_engine::parse::Level>>| {
                        v.as_deref().unwrap_or_default().iter().map(|l| (px_key(l.price.0), l.size.0)).collect::<Vec<_>>()
                    };
                    let c = &mut self.coins[i];
                    let b = if up { &mut c.upb } else { &mut c.downb };
                    b.snapshot_keys(lv(&msg.bids).into_iter(), lv(&msg.asks).into_iter(), t_ns);
                }
                Some("price_change") => {
                    for ch in msg.price_changes.as_deref().unwrap_or_default() {
                        let Some(id) = ch.asset_id.as_ref().map(|i| i.as_str()) else { continue };
                        let Some(&(i, up)) = self.owner.get(id) else { continue };
                        let c = &mut self.coins[i];
                        let b = if up { &mut c.upb } else { &mut c.downb };
                        if b.snapshot_ns.is_none() {
                            continue;
                        }
                        let side = if ch.side.as_deref() == Some("BUY") { BookSide::Bid } else { BookSide::Ask };
                        b.set_level(side, px_key(ch.price.0), ch.size.0, t_ns);
                    }
                }
                _ => {}
            }
        }
    }

    // --- risk ----------------------------------------------------------------------

    fn today_loss(&self, t_ns: i64) -> f64 {
        let day = utc_day(t_ns);
        let r = self.realised_by_day.get(&day).copied().unwrap_or(0.0);
        match &self.ack {
            Some((d, base)) if *d == day => r - base,
            _ => r,
        }
    }

    pub fn halted(&self, t_ns: i64) -> bool {
        self.halt.as_ref().is_some_and(|h| h.day == utc_day(t_ns))
    }

    pub fn halt(&mut self, reason: &str, t_ns: i64) {
        if self.halted(t_ns) {
            return;
        }
        let h = Halt { day: utc_day(t_ns), reason: reason.to_owned(), t_ns };
        tracing::warn!(reason, "pm_halt");
        self.record(t_ns, "pm_halt", serde_json::to_value(&h).unwrap_or(Value::Null));
        self.halt = Some(h);
        self.actions.push(Action::CancelAll { reason: reason.to_owned() });
        self.save();
    }

    /// Clear today's halt; the loss limit counts again from here.
    pub fn ack(&mut self, t_ns: i64) {
        let day = utc_day(t_ns);
        let r = self.realised_by_day.get(&day).copied().unwrap_or(0.0);
        self.ack = Some((day, r));
        self.halt = None;
        self.record(t_ns, "pm_ack", json!({"realised_today": round_dp(r, 4)}));
        self.save();
    }

    /// Cost of what is held unsettled and what rests.
    pub fn open_cost(&self) -> f64 {
        let held: f64 = self.inv.values().filter(|v| v.settled.is_none()).map(Inventory::cost).sum();
        let resting: f64 = self.orders.values().filter(|o| !o.closing).map(|o| o.px * (o.size - o.matched).max(0.0)).sum();
        held + resting
    }

    /// Stop quoting for good (shutdown): every slot pulls.
    pub fn stop(&mut self) {
        self.stopped = true;
    }

    // --- quoting -------------------------------------------------------------------

    fn take_token(&mut self, now_ms: i64) -> bool {
        let rate = self.cfg.max_orders_per_s;
        if self.bucket_ms > 0 {
            self.bucket = (self.bucket + (now_ms - self.bucket_ms) as f64 / 1000.0 * rate).min(rate);
        }
        self.bucket_ms = now_ms;
        if self.bucket >= 1.0 {
            self.bucket -= 1.0;
            true
        } else {
            false
        }
    }

    pub fn tick(&mut self, t_ns: i64, gates: &Gates) {
        let now_ms = t_ns / 1_000_000;
        for i in 0..self.coins.len() {
            self.tick_coin(i, t_ns, gates);
        }
        self.markouts(now_ms, t_ns);
    }

    fn net_of(&self, key: &str) -> f64 {
        self.inv.get(key).map(Inventory::net).unwrap_or(0.0)
    }

    fn tick_coin(&mut self, i: usize, t_ns: i64, gates: &Gates) {
        let now_ms = t_ns / 1_000_000;
        let cfg = self.cfg.clone();
        let halted = self.halted(t_ns);
        let open_cost = self.open_cost();
        let key = self.coins[i].key();
        let net = key.as_deref().map(|k| self.net_of(k)).unwrap_or(0.0);
        let c = &self.coins[i];
        let gate: &'static str;
        let mut want: [Option<(f64, f64)>; 2] = [None, None];
        let fair = c.fair(now_ms, &cfg);
        match &c.window {
            _ if self.stopped => gate = "stopped",
            _ if halted => gate = "halted",
            _ if gates.kill => gate = "kill",
            _ if !gates.feeds_ok => gate = "feeds stale",
            None => gate = "no window",
            Some(w) => {
                let s = t_ns / NS - w.open_unix;
                if s < cfg.entry_early_s {
                    gate = "window start";
                } else if s >= WINDOW_S - cfg.stop_s {
                    gate = "window end";
                } else if now_ms < c.pause_until_ms {
                    gate = "pause";
                } else if let Some(q) = fair {
                    let ua = if c.upb.quote(None).valid { c.upb.best_ask() } else { None };
                    let da = if c.downb.quote(None).valid { c.downb.best_ask() } else { None };
                    if ua.is_none() || da.is_none() {
                        gate = "no poly book";
                    } else {
                        gate = "quoting";
                        let (u, d) = desired_px(&cfg, q, net, ua, da);
                        for (n, px) in [u, d].into_iter().enumerate() {
                            let up = n == 0;
                            let Some(px) = px else { continue };
                            let Some(size) = room(&cfg, net, up) else { continue };
                            // The resting order's own notional is already in open cost.
                            let mine = c.slots[n].order.as_ref().map(|_| c.slots[n].px * c.slots[n].remaining).unwrap_or(0.0);
                            if open_cost - mine + px * size > cfg.max_open_cost_usd + 1e-9 {
                                continue;
                            }
                            want[n] = Some((px, size));
                        }
                    }
                } else {
                    gate = "no fair";
                }
            }
        }
        let c = &mut self.coins[i];
        if let Some(last) = c.last_tick_ms {
            let dt = (now_ms - last).clamp(0, 1000);
            if c.window.is_some() {
                c.live_ms += dt;
                if c.slots.iter().any(|s| s.order.is_some()) {
                    c.quoting_ms += dt;
                }
            }
        }
        c.last_tick_ms = Some(now_ms);
        c.gate = gate;
        for (n, w) in want.into_iter().enumerate() {
            self.step_slot(i, n, w, key.as_deref(), now_ms);
        }
    }

    fn step_slot(&mut self, i: usize, n: usize, want: Option<(f64, f64)>, key: Option<&str>, now_ms: i64) {
        let cfg = &self.cfg;
        let (asset, up) = (self.coins[i].name.clone(), n == 0);
        let order_key = self.coins[i].slots[n].order.as_ref().and_then(|id| self.orders.get(id)).map(|o| o.key.clone());
        let s = &self.coins[i].slots[n];
        if s.inflight || now_ms < s.cooldown_until_ms && s.order.is_none() {
            return;
        }
        if let Some(id) = s.order.clone() {
            let stale_window = order_key.as_deref().is_some_and(|k| Some(k) != key);
            let (adverse_ms, raise_ms, step) = (cfg.adverse_ms, cfg.min_requote_ms, cfg.requote_cents * TICK);
            let s = &mut self.coins[i].slots[n];
            let cancel = match want {
                None => true,
                _ if stale_window => true,
                Some((px, size)) => {
                    let dir: i8 = if s.px > px + 1e-9 {
                        1
                    } else if px - s.px >= step - 1e-9 {
                        -1
                    } else {
                        0
                    };
                    let since = match (dir, s.off) {
                        (0, _) => None,
                        (d, Some((od, t))) if od == d => Some(t),
                        _ => Some(now_ms),
                    };
                    s.off = since.map(|t| (dir, t));
                    let held = since.map(|t| now_ms - t).unwrap_or(0);
                    let too_high = dir == 1 && held >= adverse_ms;
                    let raise = dir == -1 && held >= raise_ms && now_ms - s.last_change_ms >= raise_ms;
                    s.remaining > size + 1e-9 || too_high || raise
                }
            };
            if cancel && now_ms >= s.cooldown_until_ms {
                s.off = None;
                s.inflight = true;
                self.actions.push(Action::Cancel { asset, up, order_id: id });
                self.count("cancels");
            }
            return;
        }
        let Some((px, size)) = want else { return };
        if !self.take_token(now_ms) {
            self.count("rate_limited");
            return;
        }
        let Some(w) = self.coins[i].window.as_ref() else { return };
        let token = if up { w.yes_token.clone() } else { w.no_token.clone() };
        let s = &mut self.coins[i].slots[n];
        s.inflight = true;
        s.px = px;
        s.remaining = size;
        s.last_change_ms = now_ms;
        self.actions.push(Action::Post { asset, up, token, px, size });
        self.count("posts");
    }

    pub fn take_actions(&mut self) -> Vec<Action> {
        std::mem::take(&mut self.actions)
    }

    fn slot_mut(&mut self, asset: &str, up: bool) -> Option<&mut Slot> {
        let c = self.coins.iter_mut().find(|c| c.name == asset)?;
        Some(&mut c.slots[if up { 0 } else { 1 }])
    }

    /// In shadow mode the executor sends nothing: every post "rests" at once.
    pub fn shadow_id(&mut self) -> String {
        self.shadow_n += 1;
        format!("shadow-{}", self.shadow_n)
    }

    /// The venue's answer to a post: an order id, or why not.
    pub fn on_posted(&mut self, asset: &str, up: bool, px: f64, size: f64, result: Result<String, String>, t_ns: i64) {
        let now_ms = t_ns / 1_000_000;
        let key = self.coins.iter().find(|c| c.name == asset).and_then(Coin::key);
        let cooldown = self.cfg.reject_cooldown_ms;
        let Some(s) = self.slot_mut(asset, up) else { return };
        s.inflight = false;
        match result {
            Ok(id) => {
                s.order = Some(id.clone());
                s.last_change_ms = now_ms;
                let rec = OrderRec {
                    id: id.clone(),
                    key: key.unwrap_or_default(),
                    asset: asset.to_owned(),
                    up,
                    px,
                    size,
                    matched: 0.0,
                    posted_ns: t_ns,
                    closing: false,
                };
                self.record(t_ns, "pm_order", json!({"event": "post", "asset": asset, "up": up, "px": px, "size": size, "id": id}));
                self.orders.insert(id, rec);
                if !self.shadow {
                    self.save();
                }
            }
            Err(why) => {
                s.order = None;
                s.cooldown_until_ms = now_ms + cooldown;
                let k = if why.contains("cross") { "post_only_rejects" } else { "post_errors" };
                self.count(k);
                self.record(t_ns, "pm_order", json!({"event": "reject", "asset": asset, "up": up, "px": px, "size": size, "error": why}));
            }
        }
    }

    /// The venue's answer to a cancel. On failure the order is kept (it may
    /// still rest) and the cancel is retried after a cooldown; one that was
    /// filled leaves through the order poll.
    pub fn on_cancelled(&mut self, asset: &str, up: bool, order_id: &str, ok: bool, error: Option<String>, t_ns: i64) {
        let now_ms = t_ns / 1_000_000;
        let cooldown = self.cfg.reject_cooldown_ms;
        let Some(s) = self.slot_mut(asset, up) else { return };
        s.inflight = false;
        if ok {
            if s.order.as_deref() == Some(order_id) {
                s.order = None;
                s.last_change_ms = now_ms;
            }
            if let Some(o) = self.orders.get_mut(order_id) {
                o.closing = true;
            }
            if self.shadow {
                self.orders.remove(order_id);
            }
            self.record(t_ns, "pm_order", json!({"event": "cancel", "asset": asset, "up": up, "id": order_id}));
        } else {
            s.cooldown_until_ms = now_ms + cooldown;
            self.count("cancel_errors");
            self.record(t_ns, "pm_order", json!({"event": "cancel_failed", "asset": asset, "up": up, "id": order_id, "error": error}));
        }
    }

    /// Orders whose final state is not known yet (the executor polls these).
    pub fn tracked(&self) -> Vec<String> {
        self.orders.keys().cloned().collect()
    }

    /// The venue says this order has matched `matched` shares in all.
    /// `done`: it no longer rests (filled, cancelled or expired).
    pub fn on_order(&mut self, id: &str, matched: f64, done: bool, t_ns: i64) {
        let Some(o) = self.orders.get_mut(id) else { return };
        let delta = matched - o.matched;
        let o = o.clone();
        if delta > 1e-6 {
            self.book_fill(&o, delta, t_ns);
            if let Some(r) = self.orders.get_mut(id) {
                r.matched = matched;
            }
        }
        if let Some(s) = self.slot_mut(&o.asset, o.up)
            && s.order.as_deref() == Some(id)
        {
            if done {
                s.order = None;
                s.inflight = false;
                s.last_change_ms = t_ns / 1_000_000;
            } else {
                s.remaining = (o.size - matched).max(0.0);
            }
        }
        if done {
            self.orders.remove(id);
        }
        if delta > 1e-6 || done {
            self.save();
        }
    }

    fn book_fill(&mut self, o: &OrderRec, shares: f64, t_ns: i64) {
        let now_ms = t_ns / 1_000_000;
        let Some(c) = self.coins.iter().find(|c| c.name == o.asset) else { return };
        let fair = c.fair(now_ms, &self.cfg).or_else(|| c.kbook.quote(None).mid());
        let w = c.window.clone();
        let open_unix: i64 = o.key.rsplit('-').next().and_then(|s| s.parse().ok()).unwrap_or(0);
        let inv = self.inv.entry(o.key.clone()).or_insert_with(|| {
            let w = w.as_ref().filter(|w| w.open_unix == open_unix);
            Inventory {
                asset: o.asset.clone(),
                open_unix,
                condition_id: w.map(|w| w.condition_id.clone()).unwrap_or_default(),
                up_token: w.map(|w| w.yes_token.clone()).unwrap_or_default(),
                down_token: w.map(|w| w.no_token.clone()).unwrap_or_default(),
                ..Default::default()
            }
        });
        if o.up {
            inv.up += shares;
            inv.up_cost += shares * o.px;
        } else {
            inv.down += shares;
            inv.down_cost += shares * o.px;
        }
        inv.fills += 1;
        let net = inv.net();
        let value = |q: f64| if o.up { q } else { 1.0 - q };
        let capture = fair.filter(|_| w.as_ref().is_some_and(|w| w.open_unix == open_unix)).map(|q| value(q) - o.px);
        for st in [&mut self.totals, self.per.entry(o.asset.clone()).or_default()] {
            st.fills += 1;
            st.shares += shares;
            if let Some(cp) = capture {
                st.capture.push(cp);
            }
        }
        for (h, dt) in MARKOUTS_MS.iter().enumerate() {
            self.dues.push(Due { t_ms: now_ms + dt, horizon: h, asset: o.asset.clone(), open_unix, up: o.up, px: o.px, shares });
        }
        self.record(
            t_ns,
            "pm_fill",
            json!({
                "asset": o.asset, "open_unix": open_unix, "up": o.up, "px": o.px, "shares": round_dp(shares, 4),
                "id": o.id, "fair": round_opt(fair, 4), "capture": round_opt(capture, 4), "net": round_dp(net, 4),
                "age_ms": (t_ns - o.posted_ns) / 1_000_000,
            }),
        );
    }

    fn markouts(&mut self, now_ms: i64, t_ns: i64) {
        if self.dues.iter().all(|d| d.t_ms > now_ms) {
            return;
        }
        let (due, keep): (Vec<Due>, Vec<Due>) = std::mem::take(&mut self.dues).into_iter().partition(|d| d.t_ms <= now_ms);
        self.dues = keep;
        for d in due {
            let Some(c) = self.coins.iter().find(|c| c.name == d.asset) else { continue };
            if c.window.as_ref().is_none_or(|w| w.open_unix != d.open_unix) {
                continue;
            }
            let Some(q) = c.kbook.quote(None).mid() else { continue };
            let m = (if d.up { q } else { 1.0 - q }) - d.px;
            for st in [&mut self.totals, self.per.entry(d.asset.clone()).or_default()] {
                if d.horizon == 0 { &mut st.markout5 } else { &mut st.markout30 }.push(m);
            }
            if d.horizon == 1 {
                self.record(t_ns, "pm_markout", json!({"asset": d.asset, "up": d.up, "px": d.px, "shares": d.shares, "q": round_dp(q, 4), "m30": round_dp(m, 4)}));
            }
        }
    }

    // --- settlement, redemption, reconciliation ------------------------------------------

    /// Windows held and closed at least 30 s ago, not yet settled.
    pub fn unsettled(&self, t_ns: i64) -> Vec<Window> {
        self.inv
            .values()
            .filter(|v| v.settled.is_none() && t_ns >= v.close_ns() + 30 * NS)
            .map(|v| Window {
                asset: v.asset.clone(),
                open_unix: v.open_unix,
                kalshi_ticker: String::new(),
                yes_token: v.up_token.clone(),
                no_token: v.down_token.clone(),
                condition_id: v.condition_id.clone(),
                fee_rate: 0.0,
                fee_exponent: 1.0,
            })
            .collect()
    }

    pub fn settle(&mut self, asset: &str, open_unix: i64, outcome: &str, t_ns: i64) {
        let key = wkey(asset, open_unix);
        // Fills still unconfirmed for this window wait for the order poll.
        if self.orders.values().any(|o| o.key == key) {
            return;
        }
        let Some(v) = self.inv.get_mut(&key) else { return };
        if v.settled.is_some() {
            return;
        }
        let pnl = v.settle_pnl(outcome);
        v.settled = Some(outcome.to_owned());
        v.pnl = Some(pnl);
        v.settled_ns = Some(t_ns);
        let v = v.clone();
        *self.realised_by_day.entry(utc_day(t_ns)).or_insert(0.0) += pnl;
        for st in [&mut self.totals, self.per.entry(asset.to_owned()).or_default()] {
            st.pnl += pnl;
            st.windows += 1;
        }
        let realised: f64 = self.realised_by_day.values().sum();
        self.series.push_back(json!({"t_ms": t_ns / 1_000_000, "realised": round_dp(realised, 4), "fills": self.totals.fills}));
        if self.series.len() > 5000 {
            self.series.pop_front();
        }
        self.record(
            t_ns,
            "pm_settle",
            json!({
                "asset": asset, "open_unix": open_unix, "outcome": outcome, "up": v.up, "up_cost": round_dp(v.up_cost, 4),
                "down": v.down, "down_cost": round_dp(v.down_cost, 4), "fills": v.fills, "locked": round_dp(v.locked_pnl(), 4),
                "pnl": round_dp(pnl, 4), "realised_today": round_dp(self.today_loss(t_ns), 4),
            }),
        );
        if self.today_loss(t_ns) <= -self.cfg.daily_loss_usd + 1e-9 {
            let msg = format!("daily loss {:.2} reached the {:.2} limit", self.today_loss(t_ns), self.cfg.daily_loss_usd);
            self.halt(&msg, t_ns);
        }
        self.save();
    }

    /// Settled windows holding a winner to redeem: (key, condition id).
    pub fn redeem_due(&self) -> Vec<(String, String)> {
        self.inv
            .iter()
            .filter(|(_, v)| v.redeemed.is_none() && v.redeem_tries < 20 && !v.condition_id.is_empty())
            .filter(|(_, v)| match v.settled.as_deref() {
                Some("yes") => v.up > 0.0,
                Some("no") => v.down > 0.0,
                _ => false,
            })
            .map(|(k, v)| (k.clone(), v.condition_id.clone()))
            .collect()
    }

    pub fn on_redeemed(&mut self, key: &str, result: Result<String, String>, t_ns: i64) {
        let Some(v) = self.inv.get_mut(key) else { return };
        v.redeem_tries += 1;
        let r = match &result {
            Ok(tx) => {
                v.redeemed = Some(tx.clone());
                json!({"key": key, "tx": tx})
            }
            Err(e) => json!({"key": key, "error": e, "tries": v.redeem_tries}),
        };
        self.record(t_ns, "pm_redeem", r);
        self.save();
    }

    /// Shares the engine believes it holds per token (unsettled windows).
    pub fn held_tokens(&self) -> BTreeMap<String, f64> {
        let mut m = BTreeMap::new();
        for v in self.inv.values().filter(|v| v.settled.is_none()) {
            if !v.up_token.is_empty() {
                *m.entry(v.up_token.clone()).or_insert(0.0) += v.up;
            }
            if !v.down_token.is_empty() {
                *m.entry(v.down_token.clone()).or_insert(0.0) += v.down;
            }
        }
        m
    }

    /// The wallet's balance of a token against ours. Three checks in a row
    /// with the engine holding more than the wallet halt (fills booked that
    /// did not happen, or shares gone).
    pub fn on_balance(&mut self, token: &str, wallet: f64, t_ns: i64) {
        let ours = self.held_tokens().get(token).copied().unwrap_or(0.0);
        let d = wallet - ours;
        let e = self.drift.entry(token.to_owned()).or_insert((0, 0.0));
        if d < -0.5 {
            e.0 += 1;
        } else {
            e.0 = 0;
        }
        e.1 = d;
        let n = e.0;
        if d.abs() > 0.5 {
            self.count("drift_checks");
            self.record(t_ns, "pm_drift", json!({"token": token, "wallet": round_dp(wallet, 4), "ours": round_dp(ours, 4), "streak": n}));
        }
        if n >= 3 {
            self.halt(&format!("inventory drift: wallet {wallet:.2} < booked {ours:.2} on {token}"), t_ns);
        }
    }

    // --- status --------------------------------------------------------------------------

    pub fn status(&self, t_ns: i64) -> Value {
        let now_ms = t_ns / 1_000_000;
        let coins: Vec<Value> = self
            .coins
            .iter()
            .map(|c| {
                let k = c.kbook.quote(None);
                let key = c.key();
                let inv = key.as_ref().and_then(|k| self.inv.get(k));
                let slot = |s: &Slot| json!({"order": s.order.is_some(), "px": s.order.as_ref().map(|_| s.px), "remaining": s.order.as_ref().map(|_| s.remaining), "inflight": s.inflight});
                let top = |b: &Book| {
                    let q = b.quote(None);
                    json!({"bid": q.bid, "ask": q.ask, "valid": q.valid})
                };
                json!({
                    "asset": c.name,
                    "window": c.window.as_ref().map(|w| w.open_unix),
                    "gate": c.gate,
                    "kalshi": {"bid": k.bid, "ask": k.ask, "mid": round_opt(k.mid(), 4), "valid": k.valid,
                               "age_ms": c.kbook.last_update_ns.map(|u| now_ms - u / 1_000_000)},
                    "fair": round_opt(c.fair(now_ms, &self.cfg), 4),
                    "poly_up": top(&c.upb),
                    "poly_down": top(&c.downb),
                    "bid_up": slot(&c.slots[0]),
                    "bid_down": slot(&c.slots[1]),
                    "net": inv.map(|v| round_dp(v.net(), 2)),
                    "cost": inv.map(|v| round_dp(v.cost(), 4)),
                    "pauses": c.pauses,
                    "uptime": (c.live_ms > 0).then(|| round_dp(c.quoting_ms as f64 / c.live_ms as f64, 3)),
                    "stats": self.per.get(&c.name).map(Stat::json),
                })
            })
            .collect();
        let held: Vec<Value> = self
            .inv
            .iter()
            .filter(|(_, v)| {
                let winner = match v.settled.as_deref() {
                    Some("yes") => v.up > 0.0,
                    Some("no") => v.down > 0.0,
                    _ => false,
                };
                v.settled.is_none() || winner && v.redeemed.is_none()
            })
            .map(|(k, v)| json!({"key": k, "up": v.up, "down": v.down, "cost": round_dp(v.cost(), 4), "settled": v.settled, "pnl": round_opt(v.pnl, 4), "redeemed": v.redeemed.is_some()}))
            .collect();
        let day = utc_day(t_ns);
        json!({
            "mode": if self.shadow { "shadow" } else { "live" },
            "halted": self.halted(t_ns),
            "halt": self.halt,
            "realised_today": round_dp(self.realised_by_day.get(&day).copied().unwrap_or(0.0), 4),
            "loss_today_vs_limit": round_dp(self.today_loss(t_ns), 4),
            "daily_loss_usd": self.cfg.daily_loss_usd,
            "realised_total": round_dp(self.realised_by_day.values().sum::<f64>(), 4),
            "realised_by_day": self.realised_by_day,
            "open_cost": round_dp(self.open_cost(), 4),
            "max_open_cost_usd": self.cfg.max_open_cost_usd,
            "orders_tracked": self.orders.len(),
            "coins": coins,
            "held": held,
            "totals": self.totals.json(),
            "counters": self.counters,
            "recent": self.recent,
            "cfg": {"half_spread": self.cfg.half_spread, "shares": self.cfg.shares, "max_net": self.cfg.max_net,
                    "pause_jump": self.cfg.pause_jump, "stop_s": self.cfg.stop_s, "entry_early_s": self.cfg.entry_early_s},
        })
    }

    pub fn series(&self, since_ms: i64) -> Value {
        Value::Array(self.series.iter().filter(|p| p["t_ms"].as_i64().unwrap_or(0) > since_ms).cloned().collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> PmCfg {
        PmCfg {
            half_spread: 0.02,
            shares: 5.0,
            min_shares: 5.0,
            max_net: 20.0,
            skew_k: 0.01,
            pause_jump: 0.02,
            pause_ms: 1500,
            max_kalshi_spread: 0.02,
            kalshi_max_age_ms: 10_000,
            min_px: 0.05,
            max_px: 0.95,
            entry_early_s: 20,
            stop_s: 60,
            requote_cents: 1.0,
            min_requote_ms: 300,
            adverse_ms: 200,
            reject_cooldown_ms: 500,
            max_orders_per_s: 8.0,
            daily_loss_usd: 15.0,
            max_open_cost_usd: 60.0,
        }
    }

    const OPEN: i64 = 1_800_000_000 - 1_800_000_000 % 900;

    fn window(asset: &str) -> Window {
        Window {
            asset: asset.into(),
            open_unix: OPEN,
            kalshi_ticker: format!("KX{asset}15M-X"),
            yes_token: format!("{asset}-up"),
            no_token: format!("{asset}-down"),
            condition_id: format!("0x{asset}"),
            fee_rate: 0.07,
            fee_exponent: 1.0,
        }
    }

    fn engine(dir: &std::path::Path) -> Engine {
        let mut e = Engine::new(cfg(), &["BTC".into()], dir, Map::new(), false);
        e.set_window(window("BTC"), OPEN * NS);
        e
    }

    fn tmp(name: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("pm-test-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        d
    }

    /// Kalshi YES book bid/ask and Polymarket Up/Down books at `t`.
    fn books(e: &mut Engine, kb: f64, ka: f64, up: (f64, f64), down: (f64, f64), t: i64) {
        let snap = json!({"type": "orderbook_snapshot", "msg": {"market_ticker": "KXBTC15M-X",
            "yes_dollars_fp": [[format!("{kb:.2}"), "100"]], "no_dollars_fp": [[format!("{:.2}", 1.0 - ka), "100"]]}});
        e.on_kalshi(&snap.to_string(), t);
        let book = |id: &str, (b, a): (f64, f64)| {
            json!({"event_type": "book", "asset_id": id, "bids": [{"price": format!("{b:.2}"), "size": "50"}],
                   "asks": [{"price": format!("{a:.2}"), "size": "50"}]})
        };
        e.on_poly(&json!([book("BTC-up", up), book("BTC-down", down)]).to_string(), t);
    }

    #[test]
    fn quotes_around_kalshi_mid_in_cents() {
        let (u, d) = desired_px(&cfg(), 0.555, 0.0, Some(0.60), Some(0.50));
        assert_eq!(u, Some(0.53));
        assert_eq!(d, Some(0.42));
    }

    #[test]
    fn post_only_clamp_and_range() {
        // Kalshi far above Polymarket: the Up bid would cross, so it sits a tick under the ask.
        let (u, d) = desired_px(&cfg(), 0.70, 0.0, Some(0.55), Some(0.47));
        assert_eq!(u, Some(0.54));
        assert_eq!(d, Some(0.28));
        // Near the edges nothing is quoted outside min_px..=max_px.
        let (u, d) = desired_px(&cfg(), 0.04, 0.0, Some(0.05), Some(0.99));
        assert_eq!(u, None);
        assert_eq!(d, Some(0.94));
    }

    #[test]
    fn skew_and_net_cap() {
        let c = cfg();
        // Long 20 Up: the Up bid is not quoted and the Down bid leans up.
        let (u, d) = desired_px(&c, 0.50, 20.0, Some(0.60), Some(0.60));
        assert_eq!(u, Some(0.47));
        assert_eq!(d, Some(0.49));
        assert_eq!(room(&c, 20.0, true), None);
        assert_eq!(room(&c, 20.0, false), Some(5.0));
        assert_eq!(room(&c, 17.0, true), None, "3 shares left is under the venue minimum");
        assert_eq!(room(&c, -18.0, false), None);
    }

    #[test]
    fn posts_both_sides_then_requotes_and_pulls() {
        let dir = tmp("quote");
        let mut e = engine(&dir);
        let t0 = (OPEN + 100) * NS;
        books(&mut e, 0.54, 0.56, (0.53, 0.57), (0.43, 0.47), t0);
        e.tick(t0, &Gates { kill: false, feeds_ok: true });
        let a = e.take_actions();
        assert_eq!(a.len(), 2, "{a:?}");
        assert!(matches!(&a[0], Action::Post { up: true, px, size, .. } if (*px - 0.53).abs() < 1e-9 && *size == 5.0));
        assert!(matches!(&a[1], Action::Post { up: false, px, .. } if (*px - 0.43).abs() < 1e-9));
        e.on_posted("BTC", true, 0.53, 5.0, Ok("o1".into()), t0);
        e.on_posted("BTC", false, 0.43, 5.0, Ok("o2".into()), t0);
        e.tick(t0 + 50_000_000, &Gates { kill: false, feeds_ok: true });
        assert!(e.take_actions().is_empty(), "nothing moved");

        // Kalshi up a cent (no pause): nothing moves on the first sight of it (the
        // mid flickers); the Down bid, now too high, goes once that has held
        // adverse_ms, and the Up bid rises once its gap has held min_requote_ms.
        let t1 = t0 + 100_000_000;
        books(&mut e, 0.55, 0.57, (0.53, 0.57), (0.43, 0.47), t1);
        e.tick(t1, &Gates { kill: false, feeds_ok: true });
        assert!(e.take_actions().is_empty());
        e.tick(t0 + 320_000_000, &Gates { kill: false, feeds_ok: true });
        let a = e.take_actions();
        assert_eq!(a, vec![Action::Cancel { asset: "BTC".into(), up: false, order_id: "o2".into() }]);
        e.tick(t0 + 410_000_000, &Gates { kill: false, feeds_ok: true });
        let a = e.take_actions();
        assert_eq!(a, vec![Action::Cancel { asset: "BTC".into(), up: true, order_id: "o1".into() }]);

        // A flicker back within adverse_ms leaves the bid alone.
        e.on_cancelled("BTC", true, "o1", true, None, t0 + 450_000_000);
        e.on_cancelled("BTC", false, "o2", true, None, t0 + 450_000_000);
        e.tick(t0 + 460_000_000, &Gates { kill: false, feeds_ok: true });
        assert_eq!(e.take_actions().len(), 2, "reposted at the new prices");
        e.on_posted("BTC", true, 0.53, 5.0, Ok("o3".into()), t0 + 470_000_000);
        e.on_posted("BTC", false, 0.42, 5.0, Ok("o4".into()), t0 + 470_000_000);
        books(&mut e, 0.54, 0.555, (0.53, 0.57), (0.43, 0.47), t0 + 500_000_000);
        e.tick(t0 + 500_000_000, &Gates { kill: false, feeds_ok: true });
        books(&mut e, 0.55, 0.57, (0.53, 0.57), (0.43, 0.47), t0 + 600_000_000);
        e.tick(t0 + 650_000_000, &Gates { kill: false, feeds_ok: true });
        assert!(e.take_actions().is_empty(), "the flicker passed");

        // Kill pulls everything at once.
        e.tick(t0 + 700_000_000, &Gates { kill: true, feeds_ok: true });
        assert_eq!(e.take_actions().len(), 2);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn kalshi_jump_pauses_and_cancels_at_once() {
        let dir = tmp("pause");
        let mut e = engine(&dir);
        let t0 = (OPEN + 100) * NS;
        books(&mut e, 0.54, 0.56, (0.53, 0.57), (0.43, 0.47), t0);
        e.tick(t0, &Gates { kill: false, feeds_ok: true });
        e.take_actions();
        e.on_posted("BTC", true, 0.53, 5.0, Ok("o1".into()), t0);
        e.on_posted("BTC", false, 0.43, 5.0, Ok("o2".into()), t0);
        // +3¢ within a second: both bids go in the same frame's handling.
        books(&mut e, 0.57, 0.59, (0.53, 0.57), (0.43, 0.47), t0 + 300_000_000);
        let a = e.take_actions();
        assert_eq!(a.len(), 2, "{a:?}");
        assert!(a.iter().all(|x| matches!(x, Action::Cancel { .. })));
        e.on_cancelled("BTC", true, "o1", true, None, t0 + 350_000_000);
        e.on_cancelled("BTC", false, "o2", true, None, t0 + 350_000_000);
        e.tick(t0 + 1_000_000_000, &Gates { kill: false, feeds_ok: true });
        assert!(e.take_actions().is_empty(), "still paused");
        books(&mut e, 0.57, 0.59, (0.56, 0.60), (0.40, 0.44), t0 + 1_900_000_000);
        e.tick(t0 + 1_900_000_000, &Gates { kill: false, feeds_ok: true });
        assert_eq!(e.take_actions().len(), 2, "pause over");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn window_edges_are_not_quoted() {
        let dir = tmp("edges");
        let mut e = engine(&dir);
        for s in [5, WINDOW_S - 59] {
            let t = (OPEN + s) * NS;
            books(&mut e, 0.54, 0.56, (0.53, 0.57), (0.43, 0.47), t);
            e.tick(t, &Gates { kill: false, feeds_ok: true });
            assert!(e.take_actions().is_empty(), "second {s}");
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn fills_settle_and_latch_the_daily_loss() {
        let dir = tmp("fills");
        let mut e = engine(&dir);
        let t0 = (OPEN + 100) * NS;
        books(&mut e, 0.54, 0.56, (0.53, 0.57), (0.43, 0.47), t0);
        e.tick(t0, &Gates { kill: false, feeds_ok: true });
        e.take_actions();
        e.on_posted("BTC", true, 0.53, 5.0, Ok("o1".into()), t0);
        e.on_posted("BTC", false, 0.43, 5.0, Ok("o2".into()), t0);
        // Partial, then full on Up; full on Down.
        e.on_order("o1", 2.0, false, t0 + NS);
        e.on_order("o1", 5.0, true, t0 + 2 * NS);
        e.on_order("o2", 5.0, true, t0 + 2 * NS);
        let key = wkey("BTC", OPEN);
        let v = &e.inv[&key];
        assert_eq!((v.up, v.down), (5.0, 5.0));
        assert!((v.locked_pnl() - 5.0 * (1.0 - 0.53 - 0.43)).abs() < 1e-9);
        assert!(e.tracked().is_empty());
        let end = (OPEN + WINDOW_S + 31) * NS;
        assert_eq!(e.unsettled(end).len(), 1);
        e.settle("BTC", OPEN, "no", end);
        assert!((e.inv[&key].pnl.unwrap() - (5.0 - 4.8)).abs() < 1e-9);
        assert_eq!(e.redeem_due(), vec![(key.clone(), "0xBTC".into())]);
        assert!(!e.halted(end));

        // A losing window past the limit latches, persists, and clears on ack.
        e.inv.insert("BTC-1".into(), Inventory { asset: "BTC".into(), open_unix: 1, up: 30.0, up_cost: 16.0, ..Default::default() });
        e.settle("BTC", 1, "no", end);
        assert!(e.halted(end));
        assert!(e.take_actions().contains(&Action::CancelAll { reason: e.halt.as_ref().unwrap().reason.clone() }));
        let e2 = Engine::new(cfg(), &["BTC".into()], &dir, Map::new(), false);
        assert!(e2.halted(end), "persisted");
        assert!(!e2.halted(end + 86_400 * NS), "next UTC day");
        let mut e2 = e2;
        e2.ack(end);
        assert!(!e2.halted(end));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn open_cost_cap_blocks_a_side() {
        let dir = tmp("cap");
        let mut e = engine(&dir);
        e.inv.insert("ETH-1".into(), Inventory { asset: "ETH".into(), open_unix: 1, up: 100.0, up_cost: 58.0, ..Default::default() });
        let t0 = (OPEN + 100) * NS;
        books(&mut e, 0.54, 0.56, (0.53, 0.57), (0.43, 0.47), t0);
        e.tick(t0, &Gates { kill: false, feeds_ok: true });
        assert!(e.take_actions().is_empty(), "58 + 2.65 > 60, 58 + 2.15 > 60");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn rate_limit_holds_posts() {
        let dir = tmp("rate");
        let mut c = cfg();
        c.max_orders_per_s = 1.0;
        let mut e = Engine::new(c, &["BTC".into()], &dir, Map::new(), false);
        e.set_window(window("BTC"), OPEN * NS);
        let t0 = (OPEN + 100) * NS;
        books(&mut e, 0.54, 0.56, (0.53, 0.57), (0.43, 0.47), t0);
        e.tick(t0, &Gates { kill: false, feeds_ok: true });
        assert_eq!(e.take_actions().len(), 1);
        e.tick(t0 + 1_100_000_000, &Gates { kill: false, feeds_ok: true });
        assert_eq!(e.take_actions().len(), 1);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn drift_halts_after_three_short_checks() {
        let dir = tmp("drift");
        let mut e = engine(&dir);
        e.inv.insert(wkey("BTC", OPEN), Inventory { asset: "BTC".into(), open_unix: OPEN, up_token: "BTC-up".into(), up: 5.0, up_cost: 2.5, ..Default::default() });
        let t = (OPEN + 100) * NS;
        e.on_balance("BTC-up", 9.0, t);
        assert!(!e.halted(t), "more in the wallet is only recorded");
        for _ in 0..3 {
            e.on_balance("BTC-up", 0.0, t);
        }
        assert!(e.halted(t));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
