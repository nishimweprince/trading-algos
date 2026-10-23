//! SOL hourly on Limitless, priced from Helius on-chain SOL. Paper only.
//!
//! Limitless's "Solana Up or Down Hourly" (a Polymarket mirror) resolves Up
//! when Binance SOL/USDT's 1-hour candle closes at or above its open
//! (`metadata.openPrice`). There is no Kalshi 15-minute market to follow, so
//! the signal is a fair value from the on-chain price:
//!
//! ```text
//! S  = the freshest Helius source (Pyth, Orca or Raydium) × its basis
//!      basis = EWMA(Binance SOL/USDT mid / that source), ~5 min
//! σ  = EWMA of squared 1 s log returns of S (per √s)
//! P(Up) = Φ( ln(S / open) / (σ · √τ) )   τ = seconds to the close
//! ```
//!
//! Helius's Pyth push account updates only every ~10 s; the Orca and Raydium
//! pools change about once a second, so the signal is whichever on-chain
//! price arrived last. Each source's basis keeps the USD/USDT and venue gap
//! out of the signal: on-chain decides when the price moves, Binance (the
//! settlement source) where it sits. The
//! fair value from Binance alone is journaled beside it, so whether Helius
//! adds anything over the settlement source is measured, not assumed.
//!
//! - **Entry:** the cheap side's ask at least `min_edge` under its fair value,
//!   and at most `max_px` once both taker fees leave `min_net_edge`
//!   (`pk_core::net_edge_max_px`); the ask within [`min_px`, `max_px`], the
//!   Limitless spread within `max_spread`, not in the first or last
//!   `early_s`/`late_s` of the hour, and σ warmed up. One entry per hour.
//! - **Fills** are delayed exactly as the 15-minute engine's: the book at
//!   decision + round trip + `takerDelayMs`, with the instant fill beside it.
//! - **Exit:** sell once the held side's bid reaches its fair value (less 1¢)
//!   and the exit floor; otherwise hold to settlement (Limitless's outcome).

use std::collections::{BTreeMap, VecDeque};

use pk_core::book::{Book, Quote};
use pk_core::decision::net_edge_max_px;
use pk_core::exit::exit_floor;
use pk_core::fees::taker_fee;
use pk_core::journal::{JsonlDaily, Sink};
use pk_core::ledger::{Ledger, Position};
use pk_core::NS;
use pk_core::num::{round_dp, round_opt};
use pk_stats::{Rolling, Sample};
use serde::Serialize;
use serde_json::{Map, Value, json};

use crate::engine::take;

pub const ASSET: &str = "SOLH";
pub const HOUR_S: i64 = 3600;
const EXIT_MARGIN: f64 = 0.01;
const MARKOUT_NS: i64 = 30 * NS;
/// One-second samples before σ is trusted.
const VOL_WARMUP: u32 = 120;
const RECENT: usize = 40;

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct HourWindow {
    pub slug: String,
    pub open_unix: i64,
    pub open_price: f64,
    pub yes_token: String,
    pub no_token: String,
    pub condition_id: String,
    pub exchange: String,
    pub taker_delay_ms: f64,
    pub fee: bool,
}

/// The window from `GET /markets/sol-hourly-price`, or why it is not this hour's.
pub fn parse_hour(m: &Value, hour_open: i64) -> Result<HourWindow, String> {
    let s = |p: &str| m.pointer(p).and_then(Value::as_str).map(str::to_owned);
    let start = s("/startAt").and_then(|t| chrono::DateTime::parse_from_rfc3339(&t).ok()).map(|t| t.timestamp());
    if start != Some(hour_open) {
        return Err(format!("startAt {:?} is not {hour_open}", m.get("startAt")));
    }
    if m.get("expirationTimestamp").and_then(Value::as_i64) != Some((hour_open + HOUR_S) * 1000) {
        return Err("not a one-hour market".into());
    }
    let chart = m.pointer("/metadata/chart");
    let binance = chart.and_then(|c| c.get("source")).and_then(Value::as_str) == Some("binance")
        && chart.and_then(|c| c.get("symbol")).and_then(Value::as_str) == Some("SOLUSDT");
    if !binance {
        return Err("not settled on Binance SOL/USDT".into());
    }
    let open_price = m
        .pointer("/metadata/openPrice")
        .and_then(|v| v.as_str().and_then(|x| x.parse().ok()).or_else(|| v.as_f64()))
        .ok_or("no openPrice yet")?;
    let (Some(yes), Some(no), Some(exchange)) = (s("/tokens/yes"), s("/tokens/no"), s("/venue/exchange")) else {
        return Err("tokens or venue missing".into());
    };
    Ok(HourWindow {
        slug: s("/slug").ok_or("no slug")?,
        open_unix: hour_open,
        open_price,
        yes_token: yes,
        no_token: no,
        condition_id: s("/conditionId").unwrap_or_default(),
        exchange,
        taker_delay_ms: m.pointer("/settings/takerDelayMs").and_then(Value::as_f64).unwrap_or(0.0),
        fee: m.pointer("/metadata/fee").and_then(Value::as_bool).unwrap_or(true),
    })
}

/// The standard normal CDF (Abramowitz–Stegun 7.1.26, error < 1.5e-7).
pub fn norm_cdf(x: f64) -> f64 {
    let z = x.abs() / std::f64::consts::SQRT_2;
    let t = 1.0 / (1.0 + 0.327_591_1 * z);
    let poly = t * (0.254_829_592 + t * (-0.284_496_736 + t * (1.421_413_741 + t * (-1.453_152_027 + t * 1.061_405_429))));
    let erf = 1.0 - poly * (-z * z).exp();
    if x >= 0.0 { 0.5 * (1.0 + erf) } else { 0.5 * (1.0 - erf) }
}

/// P(close ≥ open) for a driftless walk with σ per √s and `tau_s` left.
pub fn fair_up(spot: f64, open: f64, sigma: f64, tau_s: f64) -> Option<f64> {
    if spot <= 0.0 || open <= 0.0 || sigma <= 0.0 {
        return None;
    }
    if tau_s <= 0.0 {
        return Some(if spot >= open { 1.0 } else { 0.0 });
    }
    Some(norm_cdf((spot / open).ln() / (sigma * tau_s.sqrt())))
}

#[derive(Clone, Debug)]
pub struct HourlyCfg {
    pub min_edge: f64,
    pub min_net_edge: f64,
    pub min_px: f64,
    pub max_px: f64,
    pub max_spread: f64,
    pub early_s: i64,
    pub late_s: i64,
    pub shares: f64,
    pub limit_offset: f64,
    pub exit_slip: f64,
    pub fee_rate: f64,
    pub vol_halflife_s: f64,
    pub basis_halflife_s: f64,
    pub paper_extra_ms: f64,
    pub max_open_usd: f64,
}

/// One on-chain (or Binance) source: latest price, and its feed lag.
#[derive(Default)]
struct Src {
    px: Option<f64>,
    at: Option<i64>,
    updates: u64,
    lag_ms: Option<Rolling>,
}

impl Src {
    fn push(&mut self, px: f64, t: i64, venue_ms: Option<f64>) {
        self.px = Some(px);
        self.at = Some(t);
        self.updates += 1;
        if let Some(v) = venue_ms {
            self.lag_ms.get_or_insert_with(|| Rolling::new(1000)).push(t as f64 / 1e6 - v);
        }
    }

    fn json(&self, t: i64) -> Value {
        let q = |p: f64| round_opt(self.lag_ms.as_ref().and_then(|r| r.quantile(p)), 0);
        json!({"px": self.px.map(|p| round_dp(p, 4)), "age_ms": self.at.map(|a| (t - a) / 1_000_000),
               "updates": self.updates, "lag_ms_p50": q(0.5), "lag_ms_p90": q(0.9)})
    }
}

#[derive(Clone, Debug)]
struct Pending {
    exit: bool,
    decided_ns: i64,
    due_ns: i64,
    yes: bool,
    limit: f64,
    shares: f64,
    instant: (f64, f64),
    fair: f64,
}

pub struct Hourly {
    pub cfg: HourlyCfg,
    window: Option<HourWindow>,
    book: Book,
    l_version: Option<i64>,
    srcs: BTreeMap<&'static str, Src>,
    /// σ²: EWMA of squared one-second log returns of the on-chain price.
    var: Option<f64>,
    vol_samples: u32,
    sec: i64,
    sec_px: Option<f64>,
    /// Per on-chain source: (Binance / source EWMA, last update).
    basis: BTreeMap<&'static str, (f64, i64)>,
    /// The source the signal last came from.
    last_src: Option<&'static str>,
    consumed: bool,
    pending: Option<Pending>,
    last_eval: Option<Value>,
    pub ledger: Ledger,
    journal: JsonlDaily,
    stamp: Map<String, Value>,
    counters: BTreeMap<String, u64>,
    recent: VecDeque<Value>,
    rtt_ms: Option<f64>,
    net_per_share: Sample,
    delay_cost: Sample,
    markout30: Sample,
    /// Fair value at entry minus outcome (Brier-style check of the model).
    brier: Sample,
    markouts: Vec<(i64, bool, f64)>,
    bought: std::collections::HashMap<String, (f64, f64)>,
}

impl Hourly {
    pub fn new(cfg: HourlyCfg, ledger: Ledger, journal: JsonlDaily, stamp: Map<String, Value>) -> Self {
        let srcs = ["pyth", "orca", "raydium", "binance"].into_iter().map(|s| (s, Src::default())).collect();
        Self {
            cfg,
            window: None,
            book: Book::default(),
            l_version: None,
            srcs,
            var: None,
            vol_samples: 0,
            sec: 0,
            sec_px: None,
            basis: BTreeMap::new(),
            last_src: None,
            consumed: false,
            pending: None,
            last_eval: None,
            ledger,
            journal,
            stamp,
            counters: BTreeMap::new(),
            recent: VecDeque::new(),
            rtt_ms: None,
            net_per_share: Sample::default(),
            delay_cost: Sample::default(),
            markout30: Sample::default(),
            brier: Sample::default(),
            markouts: Vec::new(),
            bought: std::collections::HashMap::new(),
        }
    }

    fn count(&mut self, k: &str) {
        *self.counters.entry(k.to_owned()).or_insert(0) += 1;
    }

    fn record(&mut self, t: i64, kind: &str, mut r: Value) {
        if let Some(o) = r.as_object_mut() {
            o.insert("type".into(), kind.into());
            o.insert("t_ns".into(), t.into());
            o.insert("asset".into(), ASSET.into());
            for (k, v) in &self.stamp {
                o.insert(k.clone(), v.clone());
            }
        }
        self.journal.write(t, &r);
        if kind != "limh_eval" {
            if self.recent.len() == RECENT {
                self.recent.pop_back();
            }
            self.recent.push_front(r);
        }
    }

    pub fn set_rtt(&mut self, ms: f64) {
        self.rtt_ms = Some(ms);
    }

    pub fn window(&self) -> Option<&HourWindow> {
        self.window.as_ref()
    }

    pub fn slug(&self) -> Option<String> {
        self.window.as_ref().map(|w| w.slug.clone())
    }

    pub fn set_window(&mut self, w: HourWindow, t: i64) {
        if self.window.as_ref() == Some(&w) {
            return;
        }
        self.book = Book::default();
        self.l_version = None;
        self.consumed = false;
        self.last_eval = None;
        if self.pending.as_ref().is_some_and(|p| p.exit) {
            self.pending = None;
        }
        self.window = Some(w.clone());
        self.count("windows");
        self.record(t, "limh_window", serde_json::to_value(&w).unwrap_or(Value::Null));
    }

    /// One Helius line (`h`): `{"s","p","slot","mt"}`, `mt` in µs.
    pub fn on_helius(&mut self, text: &str, t: i64) {
        let Ok(v) = serde_json::from_str::<Value>(text) else { return };
        let (Some(name), Some(px)) = (v.get("s").and_then(Value::as_str), v.get("p").and_then(Value::as_f64)) else {
            return;
        };
        let venue_ms = v.get("mt").and_then(Value::as_f64).map(|us| us / 1e3);
        let key = match name {
            "pyth" => "pyth",
            "orca" => "orca",
            "raydium" => "raydium",
            _ => return,
        };
        if let Some(s) = self.srcs.get_mut(key) {
            s.push(px, t, venue_ms);
        }
        self.update_basis(key, t);
        self.last_src = Some(key);
        if let Some(sx) = self.onchain() {
            self.vol_tick(sx, t);
        }
        self.evaluate(t);
    }

    /// The signal price: the latest on-chain quote, basis-corrected to Binance.
    pub fn onchain(&self) -> Option<f64> {
        let k = self.last_src?;
        Some(self.srcs[k].px? * self.basis.get(k)?.0)
    }

    /// One Binance spot line (`n`): `{"stream":..,"data":{"b","a"}}`, SOLUSDT only.
    pub fn on_binance(&mut self, text: &str, t: i64) {
        let Ok(v) = serde_json::from_str::<Value>(text) else { return };
        let d = &v["data"];
        if d.get("s").and_then(Value::as_str) != Some("SOLUSDT") {
            return;
        }
        let num = |k: &str| d.get(k).and_then(Value::as_str).and_then(|x| x.parse::<f64>().ok());
        let (Some(b), Some(a)) = (num("b"), num("a")) else { return };
        if let Some(s) = self.srcs.get_mut("binance") {
            s.push((a + b) / 2.0, t, None);
        }
        for k in ["pyth", "orca", "raydium"] {
            self.update_basis(k, t);
        }
    }

    fn vol_tick(&mut self, px: f64, t: i64) {
        // σ on the clock: each second's return, quiet seconds included as zero.
        let sec = t.div_euclid(NS);
        match (self.sec_px, sec > self.sec) {
            (Some(prev), true) if self.sec > 0 => {
                let gap = (sec - self.sec).clamp(1, 600) as f64;
                let r2 = (px / prev).ln().powi(2) / gap; // spread over the seconds it covers
                let a = 1.0 - 0.5f64.powf(gap / self.cfg.vol_halflife_s);
                self.var = Some(match self.var {
                    Some(v) => v + a * (r2 - v),
                    None => r2,
                });
                self.vol_samples = self.vol_samples.saturating_add(gap as u32);
                self.sec = sec;
                self.sec_px = Some(px);
            }
            (None, _) => {
                self.sec = sec;
                self.sec_px = Some(px);
            }
            _ => {}
        }
    }

    fn update_basis(&mut self, k: &'static str, t: i64) {
        let (Some(p), Some(b)) = (self.srcs[k].px, self.srcs["binance"].px) else { return };
        let ratio = b / p;
        let next = match self.basis.get(k) {
            Some(&(x, at)) => {
                let dt = ((t - at) as f64 / 1e9).max(0.0);
                x + (1.0 - 0.5f64.powf(dt / self.cfg.basis_halflife_s)) * (ratio - x)
            }
            None => ratio,
        };
        self.basis.insert(k, (next, t));
    }

    pub fn sigma(&self) -> Option<f64> {
        (self.vol_samples >= VOL_WARMUP).then(|| self.var.map(f64::sqrt)).flatten().filter(|s| *s > 0.0)
    }

    fn quotes(&self) -> (Quote, Quote) {
        let y = self.book.quote(None);
        let n = match (y.valid, y.bid, y.ask) {
            (true, Some(b), Some(a)) => Quote { valid: true, bid: Some(1.0 - a), ask: Some(1.0 - b) },
            _ => Quote::INVALID,
        };
        (y, n)
    }

    /// Fair P(Up) from Helius Pyth (basis-corrected) and from Binance alone.
    fn fairs(&self, t: i64) -> (Option<f64>, Option<f64>) {
        let (Some(w), Some(sigma)) = (&self.window, self.sigma()) else { return (None, None) };
        let tau = (w.open_unix + HOUR_S) as f64 - t as f64 / 1e9;
        let helius = self.onchain().and_then(|sx| fair_up(sx, w.open_price, sigma, tau));
        let binance = self.srcs["binance"].px.and_then(|b| fair_up(b, w.open_price, sigma, tau));
        (helius, binance)
    }

    pub fn on_limitless(&mut self, text: &str, t: i64) {
        let Ok(v) = serde_json::from_str::<Value>(text) else { return };
        if v.get("type").and_then(Value::as_str) == Some("pk_reconnect") {
            self.book.invalidate();
            self.l_version = None;
            return;
        }
        if v.get("ev").and_then(Value::as_str) != Some("orderbookUpdate") {
            return;
        }
        let d = &v["d"];
        if self.window.as_ref().map(|w| w.slug.as_str()) != d.get("marketSlug").and_then(Value::as_str) {
            return;
        }
        let version = d.get("version").and_then(Value::as_i64).unwrap_or(0);
        if self.l_version.is_some_and(|last| version < last && version != 0 || version == 0 && last > 0) {
            return;
        }
        self.l_version = Some(version);
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
        self.book.snapshot(&side("bids"), &side("asks"), t);
        self.evaluate(t);
    }

    pub fn tick(&mut self, t: i64) {
        if self.pending.as_ref().is_some_and(|p| t >= p.due_ns) {
            self.fill(t);
        }
        let due: Vec<_> = self.markouts.iter().filter(|m| t >= m.0).cloned().collect();
        self.markouts.retain(|m| t < m.0);
        for (_, yes, vwap) in due {
            if let Some(m) = self.book.quote(None).mid() {
                self.markout30.push(((if yes { m } else { 1.0 - m }) - vwap) * 100.0);
            }
        }
        self.evaluate(t);
    }

    fn evaluate(&mut self, t: i64) {
        let Some(w) = self.window.clone() else { return };
        if self.pending.is_some() {
            return;
        }
        let (fair, fair_binance) = self.fairs(t);
        let (y, n) = self.quotes();
        let key = format!("{ASSET}:{}", w.open_unix);
        let held = self.ledger.positions.get(&key).filter(|p| p.is_open()).cloned();
        let sec_into = t as f64 / 1e9 - w.open_unix as f64;
        self.last_eval = Some(json!({
            "sec_into": round_dp(sec_into, 1), "fair_up": fair.map(|f| round_dp(f, 4)),
            "fair_up_binance": fair_binance.map(|f| round_dp(f, 4)),
            "sigma_bps_per_min": self.sigma().map(|s| round_dp(s * 60f64.sqrt() * 1e4, 2)),
            "basis_bps": self.last_src.and_then(|k| self.basis.get(k)).map(|b| round_dp((b.0 - 1.0) * 1e4, 2)),
            "signal_src": self.last_src, "signal_px": self.onchain().map(|x| round_dp(x, 4)),
            "lim": {"bid": y.bid, "ask": y.ask},
        }));
        let Some(fair) = fair else { return };
        if let Some(p) = held {
            self.maybe_exit(&w, &p, fair, t);
            return;
        }
        if self.consumed || sec_into < self.cfg.early_s as f64 || sec_into > (HOUR_S - self.cfg.late_s) as f64 {
            return;
        }
        if !y.valid {
            return;
        }
        let spread = y.ask.unwrap_or(1.0) - y.bid.unwrap_or(0.0);
        // The cheaper side relative to its fair value.
        let up_edge = y.ask.map(|a| fair - a);
        let dn_edge = n.ask.map(|a| (1.0 - fair) - a);
        let (yes, edge, ask, side_fair) = match (up_edge, dn_edge) {
            (Some(u), Some(d)) if u >= d => (true, u, y.ask.unwrap_or(1.0), fair),
            (_, Some(d)) => (false, d, n.ask.unwrap_or(1.0), 1.0 - fair),
            (Some(u), None) => (true, u, y.ask.unwrap_or(1.0), fair),
            _ => return,
        };
        if edge < self.cfg.min_edge - 1e-9 {
            return;
        }
        let reason = if spread > self.cfg.max_spread + 1e-9 {
            Some("spread")
        } else if ask < self.cfg.min_px - 1e-9 {
            Some("below_floor")
        } else if ask > self.cfg.max_px + 1e-9 {
            Some("above_cap")
        } else {
            None
        };
        let cap = net_edge_max_px(side_fair, self.cfg.min_net_edge, self.cfg.fee_rate, 1.0);
        let reason = reason.or((ask > cap + 1e-9).then_some("net_edge"));
        if let Some(r) = reason {
            // Not consumed: the next quote may qualify. Counted once per hour.
            let k = format!("blocked_{r}");
            if !self.counters.contains_key(&format!("{k}:{}", w.open_unix)) {
                self.counters.insert(format!("{k}:{}", w.open_unix), 1);
                self.count(&k);
            }
            return;
        }
        self.consumed = true;
        let mut limit = (ask + self.cfg.limit_offset).min(cap).min(self.cfg.max_px).min(0.999);
        limit = (limit * 1e3 + 1e-6).floor() / 1e3;
        let cost = limit * self.cfg.shares;
        let mut rec = json!({
            "open_unix": w.open_unix, "sec_into": round_dp(sec_into, 1), "side": if yes { "yes" } else { "no" },
            "ask": ask, "limit": limit, "fair": round_dp(side_fair, 4), "edge": round_dp(edge, 4),
            "fair_up": round_dp(fair, 4), "fair_up_binance": fair_binance.map(|f| round_dp(f, 4)),
            "sigma_bps_per_min": self.sigma().map(|s| round_dp(s * 60f64.sqrt() * 1e4, 2)),
            "signal_src": self.last_src, "signal_px": self.onchain(), "binance": self.srcs["binance"].px, "open_price": w.open_price,
            "lim": {"bid": y.bid, "ask": y.ask},
        });
        if self.ledger.open_notional() + cost > self.cfg.max_open_usd + 1e-9 || !self.ledger.can_afford(cost * (1.0 + self.cfg.fee_rate)) {
            rec["blocked"] = "cash".into();
            self.count("blocked_cash");
        } else {
            self.count("entries_decided");
            let instant = take(&self.book, yes, true, limit, self.cfg.shares);
            let due_ns = t + self.hold_ns(&w);
            self.pending =
                Some(Pending { exit: false, decided_ns: t, due_ns, yes, limit, shares: self.cfg.shares, instant, fair: side_fair });
        }
        self.record(t, "limh_decision", rec);
    }

    fn hold_ns(&self, w: &HourWindow) -> i64 {
        ((self.rtt_ms.unwrap_or(50.0) + w.taker_delay_ms + self.cfg.paper_extra_ms) * 1e6) as i64
    }

    fn maybe_exit(&mut self, w: &HourWindow, p: &Position, fair_up: f64, t: i64) {
        let yes = p.side == "yes";
        let (y, n) = self.quotes();
        let Some(bid) = (if yes { y.bid } else { n.bid }) else { return };
        let fair = if yes { fair_up } else { 1.0 - fair_up };
        let floor = exit_floor(p.vwap(), self.cfg.fee_rate, 1.0);
        if bid < fair - EXIT_MARGIN - 1e-9 || bid < floor - 1e-9 || bid <= p.vwap() {
            return;
        }
        let limit = ((bid - self.cfg.exit_slip).max(floor) * 1e3 - 1e-6).ceil() / 1e3;
        let instant = take(&self.book, yes, false, limit, p.shares);
        self.pending =
            Some(Pending { exit: true, decided_ns: t, due_ns: t + self.hold_ns(w), yes, limit, shares: p.shares, instant, fair });
        self.count("exits_decided");
    }

    fn fill(&mut self, t: i64) {
        let Some(p) = self.pending.take() else { return };
        let Some(w) = self.window.clone() else { return };
        let (shares, usd) = take(&self.book, p.yes, !p.exit, p.limit, p.shares);
        let vwap = if shares > 0.0 { usd / shares } else { 0.0 };
        let fee = if w.fee { taker_fee(vwap, shares, self.cfg.fee_rate, 1.0) } else { 0.0 };
        let ivwap = if p.instant.0 > 0.0 { p.instant.1 / p.instant.0 } else { 0.0 };
        let mut rec = json!({
            "open_unix": w.open_unix, "kind": if p.exit { "exit" } else { "entry" }, "side": if p.yes { "yes" } else { "no" },
            "limit": p.limit, "fair": round_dp(p.fair, 4), "shares": round_dp(shares, 3), "vwap": round_dp(vwap, 4),
            "fee": round_dp(fee, 5), "instant_shares": round_dp(p.instant.0, 3), "instant_vwap": round_dp(ivwap, 4),
            "held_ms": round_dp((t - p.decided_ns) as f64 / 1e6, 1),
        });
        if shares < 1e-9 {
            self.count(if p.exit { "exit_missed" } else { "entry_missed" });
            self.record(t, "limh_no_fill", rec);
            return;
        }
        if p.instant.0 > 0.0 {
            self.delay_cost.push(if p.exit { ivwap - vwap } else { vwap - ivwap } * 100.0);
        }
        if p.exit {
            self.count("exits_filled");
            if let Some(c) = self.ledger.close(ASSET, w.open_unix, shares, usd, fee, t) {
                rec["pnl"] = c.pnl.map(|x| round_dp(x, 4)).into();
                if !c.is_open() {
                    self.done(&c, None);
                }
            }
            self.record(t, "limh_exit", rec);
        } else {
            self.count("entries_filled");
            let pos = Position {
                asset: ASSET.into(),
                open_unix: w.open_unix,
                side: if p.yes { "yes" } else { "no" }.into(),
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
            self.bought.insert(pos.key(), (shares, p.fair));
            if let Err(e) = self.ledger.buy(pos) {
                rec["error"] = e.to_string().into();
            }
            self.markouts.push((t + MARKOUT_NS, p.yes, vwap));
            self.record(t, "limh_fill", rec);
        }
    }

    fn done(&mut self, p: &Position, outcome: Option<&str>) {
        let (bought, fair) = self.bought.remove(&p.key()).unwrap_or((p.shares, f64::NAN));
        if let (Some(pnl), true) = (p.pnl, bought > 0.0) {
            self.net_per_share.push(pnl * 100.0 / bought);
        }
        if let (Some(o), false) = (outcome, fair.is_nan()) {
            let won = if o == p.side { 1.0 } else { 0.0 };
            self.brier.push((fair - won).powi(2));
        }
    }

    /// Open hourly positions past their close: `(open_unix, slug)` for those we know.
    pub fn unsettled(&self, t: i64, slugs: &std::collections::HashMap<i64, String>) -> Vec<(i64, String)> {
        self.ledger
            .open_positions()
            .filter(|p| t / NS > p.open_unix + HOUR_S + 30)
            .filter_map(|p| slugs.get(&p.open_unix).map(|s| (p.open_unix, s.clone())))
            .collect()
    }

    pub fn settle(&mut self, open_unix: i64, outcome: &str, t: i64) {
        if let Some(p) = self.ledger.settle(ASSET, open_unix, outcome, t) {
            self.count("settled");
            self.done(&p, Some(outcome));
            let r = json!({"open_unix": open_unix, "side": p.side, "outcome": outcome, "shares": p.shares,
                           "payout": p.payout, "pnl": p.pnl.map(|x| round_dp(x, 4))});
            self.record(t, "limh_settle", r);
        }
    }

    /// The minute's model line: what the fair value said, for later calibration.
    pub fn note_eval(&mut self, t: i64) {
        if let Some(e) = self.last_eval.clone() {
            self.record(t, "limh_eval", e);
        }
    }

    pub fn status(&self, t: i64) -> Value {
        let (y, _) = self.quotes();
        let s = |x: &Sample| json!({"n": x.n(), "mean": round_opt(x.mean(), 2), "se": round_opt(x.se(), 2), "t": round_opt(x.t(), 2)});
        let l = &self.ledger;
        let settled: Vec<&Position> = l.positions.values().filter(|p| !p.is_open()).collect();
        let held = self.window.as_ref().and_then(|w| l.positions.get(&format!("{ASSET}:{}", w.open_unix)));
        json!({
            "window": self.window,
            "sec_into": self.window.as_ref().map(|w| round_dp(t as f64 / 1e9 - w.open_unix as f64, 0)),
            "lim": {"bid": y.bid, "ask": y.ask, "age_ms": self.book.last_update_ns.map(|u| (t - u) / 1_000_000)},
            "eval": self.last_eval,
            "consumed": self.consumed,
            "pending": self.pending.as_ref().map(|p| json!({"exit": p.exit, "limit": p.limit})),
            "position": held,
            "sources": self.srcs.iter().map(|(k, v)| ((*k).to_owned(), v.json(t))).collect::<Map<String, Value>>(),
            "vol_samples": self.vol_samples,
            "ledger": {"start_cash": round_dp(l.start_cash, 2), "cash": round_dp(l.cash, 2),
                       "open": l.open_positions().count(), "settled": settled.len(),
                       "pnl": round_dp(settled.iter().filter_map(|p| p.pnl).sum::<f64>(), 2)},
            "gates": {"net_cents_per_share": s(&self.net_per_share), "markout30_cents": s(&self.markout30),
                      "delay_cost_cents": s(&self.delay_cost), "brier": s(&self.brier)},
            "counters": self.counters.iter().filter(|(k, _)| !k.contains(':')).map(|(k, v)| (k.clone(), json!(v))).collect::<Map<String, Value>>(),
            "recent": self.recent,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normal_and_fair() {
        assert!((norm_cdf(0.0) - 0.5).abs() < 1e-7);
        assert!((norm_cdf(1.96) - 0.975).abs() < 1e-3);
        assert!((norm_cdf(-1.0) - 0.158_655).abs() < 1e-5);
        // 1 bp/√s, 900 s left (30 bp), spot 30 bp above open: Φ(1) ≈ 0.841.
        let f = fair_up(100.3, 100.0, 1e-4, 900.0).unwrap();
        assert!((f - 0.841).abs() < 0.01, "{f}");
        assert_eq!(fair_up(99.0, 100.0, 1e-4, 0.0), Some(0.0));
    }

    fn market() -> Value {
        json!({
            "slug": "solana-up-or-down-hourly-p-1", "startAt": "2026-10-23T11:00:00.000Z",
            "expirationTimestamp": 1792756800000_i64, "tokens": {"yes": "1", "no": "2"},
            "venue": {"exchange": "0x05"}, "conditionId": "0xc", "settings": {"takerDelayMs": 500},
            "metadata": {"fee": true, "openPrice": "104.07", "chart": {"source": "binance", "symbol": "SOLUSDT"}},
        })
    }

    #[test]
    fn hour_from_market() {
        let w = parse_hour(&market(), 1792753200).unwrap();
        assert_eq!((w.open_price, w.taker_delay_ms), (104.07, 500.0));
        assert!(parse_hour(&market(), 1792753200 + 3600).is_err());
        let mut m = market();
        m["metadata"]["chart"]["source"] = "coinbase".into();
        assert!(parse_hour(&m, 1792753200).is_err());
    }

    fn hourly() -> Hourly {
        let cfg = HourlyCfg {
            min_edge: 0.10,
            min_net_edge: 0.03,
            min_px: 0.20,
            max_px: 0.60,
            max_spread: 0.10,
            early_s: 120,
            late_s: 120,
            shares: 10.0,
            limit_offset: 0.06,
            exit_slip: 0.01,
            fee_rate: 0.05,
            vol_halflife_s: 600.0,
            basis_halflife_s: 300.0,
            paper_extra_ms: 0.0,
            max_open_usd: 100.0,
        };
        let dir = std::env::temp_dir().join(format!("pk-limh-test-{}", std::process::id()));
        Hourly::new(cfg, Ledger::new(100.0), JsonlDaily::new(dir), Map::new())
    }

    #[test]
    fn enters_when_the_ask_is_far_under_fair_and_fills_after_the_hold() {
        let mut h = hourly();
        let open = 1792753200;
        let mut t = (open + 600) * NS;
        let w = parse_hour(&market(), open).unwrap();
        h.set_window(w, t);
        // 200 s of Pyth wiggling ±0.5 bp around 104.9 (spot ~80 bp over the open), Binance at par:
        // σ ≈ 1 bp/√s, 2800 s left → fair ≈ Φ(1.5) ≈ 0.93.
        for i in 0..200 {
            let px = 104.9 * (1.0 + if i % 2 == 0 { 5e-5 } else { -5e-5 });
            h.on_binance(&format!(r#"{{"data":{{"s":"SOLUSDT","b":"{px}","a":"{px}"}}}}"#), t);
            h.on_helius(&format!(r#"{{"s":"pyth","p":{px},"slot":1,"mt":{}}}"#, t / 1000), t);
            t += NS;
        }
        assert!(h.sigma().is_some());
        let (fair, _) = h.fairs(t);
        assert!(fair.unwrap() > 0.75, "{fair:?}");
        let book = |a: f64| format!(r#"{{"ev":"orderbookUpdate","d":{{"marketSlug":"solana-up-or-down-hourly-p-1","version":1,"orderbook":{{"bids":[{{"price":{},"size":50000000}}],"asks":[{{"price":{a},"size":50000000}}]}}}}}}"#, a - 0.04);
        h.on_limitless(&book(0.55), t);
        assert!(h.pending.is_some(), "{:?}", h.last_eval);
        h.tick(t + 600_000_000);
        let p = h.ledger.positions.values().next().expect("filled");
        assert_eq!(p.side, "yes");
        assert!((p.vwap() - 0.55).abs() < 1e-9);
        h.settle(open, "yes", (open + 3700) * NS);
        assert_eq!(h.status(t)["gates"]["brier"]["n"], 1);
    }
}
