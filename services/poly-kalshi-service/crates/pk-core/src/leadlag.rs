//! Lead–lag: does a spot reference move before Kalshi and Polymarket?
//!
//! The references ([`SOURCES`]) are Binance spot and perp, Kraken, Bitstamp and Coinbase,
//! and on-chain SOL (the Orca and Raydium pools and Pyth, through Helius LaserStream),
//! each timed on its own. A **shock** is a reference mid move over the last second of at least
//! [`SHOCK_K`] × the asset's typical one-second move (an EWMA of |1 s log
//! return| per asset and source, sampled on the clock: a second without a new
//! best bid or ask is a zero move), and never less than [`MIN_SHOCK_BPS`].
//!
//! A shock is only confirmed once the move is large, so timing from the
//! confirmation would credit any venue that reacted to the start of the move
//! with "moving first". Times are measured from the move's **onset** instead:
//! the first reference update in that second that was already a third of the
//! way there. Each follower (Kalshi's YES mid, Polymarket's YES mid; price up
//! = YES up) gets a **signed** lag: the time from the onset to its first 1¢
//! move that way, against its mid one second before the onset. Negative means
//! it moved before the reference did (as this host sees both). No move within
//! [`TIMEOUT_NS`] of the confirmation is no response.
//!
//! - Only shocks while Kalshi's mid is inside [0.10, 0.90] count: near 0 or 1
//!   a 15-minute binary hardly moves.
//! - One shock at a time per asset and source, at least [`COOLDOWN_NS`] apart.
//! - A window roll ends a shock early (the followers' markets changed).
//!
//! Pure: the observer feeds it stamped mids and journals the `leadlag_shock`
//! records it returns.

use std::collections::VecDeque;

use pk_stats::Rolling;
use serde_json::{Map, Value, json};

use crate::num::{round_dp, round_opt};

/// Binance spot and perp keep their first names, so older journals read the same.
pub const SOURCES: [&str; 8] = ["spot", "perp", "kraken", "bitstamp", "coinbase", "orca", "raydium", "pyth"];
const N_SOURCES: usize = SOURCES.len();
pub const FOLLOWERS: [&str; 2] = ["kalshi", "poly"];
pub const SHOCK_K: f64 = 3.0;
pub const MIN_SHOCK_BPS: f64 = 3.0;
pub const TIMEOUT_NS: i64 = 3_000_000_000;
pub const COOLDOWN_NS: i64 = 5_000_000_000;
const W_NS: i64 = 1_000_000_000;
/// Followers keep enough history to look one second before an onset that
/// can itself be up to a second before the confirmation.
const FOLLOW_KEEP_NS: i64 = 2_500_000_000;
/// The onset: the first update a third of the way to the confirmed move.
const ONSET_FRACTION: f64 = 1.0 / 3.0;
/// One-second samples before a source may fire.
const WARMUP: u32 = 60;
/// About five minutes of one-second samples.
const EWMA_ALPHA: f64 = 1.0 / 300.0;
const LAGS_KEPT: usize = 500;
const STEP: f64 = 0.01 - 1e-9;
const SENSITIVE: (f64, f64) = (0.10, 0.90);

/// `(t_ns, value)` covering the last `keep`, plus the one just before it.
#[derive(Debug, Default)]
struct Trail(VecDeque<(i64, f64)>);

impl Trail {
    fn push(&mut self, t: i64, v: f64, keep: i64) {
        self.0.push_back((t, v));
        while self.0.len() > 1 && self.0[1].0 <= t - keep {
            self.0.pop_front();
        }
    }

    /// The value in force at `t` (the last one at or before it).
    fn at(&self, t: i64) -> Option<f64> {
        self.0.iter().take_while(|(ti, _)| *ti <= t).last().map(|(_, v)| *v)
    }

    fn after(&self, t: i64) -> impl Iterator<Item = &(i64, f64)> {
        self.0.iter().filter(move |(ti, _)| *ti > t)
    }

    fn clear(&mut self) {
        self.0.clear();
    }
}

#[derive(Debug)]
struct Stat {
    n: u64,
    responded: u64,
    before: u64,
    lags: Rolling,
}

impl Default for Stat {
    fn default() -> Self {
        Self { n: 0, responded: 0, before: 0, lags: Rolling::new(LAGS_KEPT) }
    }
}

impl Stat {
    fn add(&mut self, lag_ms: Option<f64>) {
        self.n += 1;
        if let Some(l) = lag_ms {
            self.responded += 1;
            if l < 0.0 {
                self.before += 1;
            }
            self.lags.push(l);
        }
    }

    fn json(&self) -> Value {
        json!({
            "n": self.n,
            "responded": self.responded,
            // Answers before the reference's onset (the key predates the other references).
            "before_binance": self.before,
            "lag_ms_p10": round_opt(self.lags.quantile(0.1), 0),
            "lag_ms_p50": round_opt(self.lags.quantile(0.5), 0),
            "lag_ms_p90": round_opt(self.lags.quantile(0.9), 0),
        })
    }
}

#[derive(Debug)]
struct Shock {
    t0: i64,
    onset: i64,
    dir: f64,
    ret_bps: f64,
    thr_bps: f64,
    open: i64,
    k_mid0: f64,
    /// Each follower's mid one second before the onset.
    base: [Option<f64>; 2],
    /// Signed ns from the onset to the follower's first 1¢ move that way.
    lag: [Option<i64>; 2],
    against: [bool; 2],
}

impl Shock {
    fn done(&self) -> bool {
        (0..2).all(|f| self.base[f].is_none() || self.lag[f].is_some())
    }

    /// A follower at `m` at time `t`: its response, or a move the other way.
    fn follow(&mut self, f: usize, t: i64, m: f64) {
        let Some(base) = self.base[f] else { return };
        let moved = self.dir * (m - base);
        if self.lag[f].is_none() && moved >= STEP {
            self.lag[f] = Some(t - self.onset);
        } else if moved <= -STEP {
            self.against[f] = true;
        }
    }
}

#[derive(Debug)]
struct Source {
    trail: Trail,
    /// The second of the last update, the mid at that second's start, and the latest mid.
    sec: Option<i64>,
    bound_mid: Option<f64>,
    last_mid: Option<f64>,
    ewma_bps: Option<f64>,
    samples: u32,
    last_shock: Option<i64>,
    pending: Option<Shock>,
    feed_lag_ms: Rolling,
    shocks: u64,
    insensitive: u64,
    stats: [Stat; 2],
}

impl Default for Source {
    fn default() -> Self {
        Self {
            trail: Trail::default(),
            sec: None,
            bound_mid: None,
            last_mid: None,
            ewma_bps: None,
            samples: 0,
            last_shock: None,
            pending: None,
            feed_lag_ms: Rolling::new(LAGS_KEPT),
            shocks: 0,
            insensitive: 0,
            stats: [Stat::default(), Stat::default()],
        }
    }
}

#[derive(Debug, Default)]
struct Follower {
    open: Option<i64>,
    mid: Option<f64>,
    trail: Trail,
}

#[derive(Debug, Default)]
struct Asset {
    sources: [Source; N_SOURCES],
    followers: [Follower; 2],
}

pub struct LeadLag {
    pub assets: Vec<String>,
    state: Vec<Asset>,
    totals: [[Stat; 2]; N_SOURCES],
    /// Merged into each record (epoch, build, config).
    stamp: Map<String, Value>,
}

impl LeadLag {
    pub fn new(assets: Vec<String>, stamp: Map<String, Value>) -> Self {
        let state = assets.iter().map(|_| Asset::default()).collect();
        Self { assets, state, totals: Default::default(), stamp }
    }

    pub fn asset_index(&self, asset: &str) -> Option<usize> {
        self.assets.iter().position(|a| a == asset)
    }

    /// A reference mid for asset `a`, source `src` (an index into [`SOURCES`]),
    /// received at `t`. `venue_ms` is the venue's own stamp when the stream has one.
    pub fn on_ref(&mut self, a: usize, src: usize, t: i64, mid: f64, venue_ms: Option<i64>) -> Vec<Value> {
        let mut out = self.expire(a, t);
        let Some(asset) = self.state.get_mut(a) else { return out };
        if mid.is_nan() || mid <= 0.0 {
            return out;
        }
        let s = &mut asset.sources[src];
        if let Some(v) = venue_ms {
            s.feed_lag_ms.push(t as f64 / 1e6 - v as f64);
        }
        // The typical one-second move, on the clock. The mid at each second's
        // start is the last one before it, so after a quiet stretch the first
        // second carries the move and the rest are zeros.
        let sec = t.div_euclid(W_NS);
        match (s.sec, s.bound_mid, s.last_mid) {
            (Some(prev), Some(bound), Some(last)) if prev != sec => {
                let gap = (sec - prev).clamp(1, 3600);
                let r = (last / bound).ln().abs() * 1e4;
                let e = match s.ewma_bps {
                    Some(e) => e + EWMA_ALPHA * (r - e),
                    None => r,
                };
                s.ewma_bps = Some(e * (1.0 - EWMA_ALPHA).powi(gap as i32 - 1));
                s.samples = s.samples.saturating_add(gap as u32);
                s.bound_mid = Some(last);
            }
            (None, ..) => s.bound_mid = Some(mid),
            _ => {}
        }
        s.sec = Some(sec);
        s.last_mid = Some(mid);
        s.trail.push(t, mid, W_NS);
        let (Some(ago), Some(ewma)) = (s.trail.at(t - W_NS), s.ewma_bps) else { return out };
        let ret_bps = (mid / ago).ln() * 1e4;
        let thr_bps = (SHOCK_K * ewma).max(MIN_SHOCK_BPS);
        if s.samples < WARMUP
            || ret_bps.abs() < thr_bps
            || s.pending.is_some()
            || s.last_shock.is_some_and(|l| t - l < COOLDOWN_NS)
        {
            return out;
        }
        s.last_shock = Some(t);
        let k = &asset.followers[0];
        let (Some(open), Some(k_mid)) = (k.open, k.mid) else { return out };
        if !(SENSITIVE.0..=SENSITIVE.1).contains(&k_mid) {
            s.insensitive += 1;
            return out;
        }
        let dir = ret_bps.signum();
        let onset = s
            .trail
            .after(t - W_NS)
            .find(|(_, v)| dir * (v / ago).ln() * 1e4 >= ret_bps.abs() * ONSET_FRACTION)
            .map_or(t, |(ti, _)| *ti);
        let mut shock = Shock {
            t0: t,
            onset,
            dir,
            ret_bps,
            thr_bps,
            open,
            k_mid0: k_mid,
            base: [None; 2],
            lag: [None; 2],
            against: [false; 2],
        };
        for (f, fol) in asset.followers.iter().enumerate() {
            if fol.open != Some(open) {
                continue;
            }
            shock.base[f] = fol.trail.at(onset - W_NS);
            for &(ti, m) in fol.trail.after(onset - W_NS) {
                shock.follow(f, ti, m);
            }
        }
        s.shocks += 1;
        s.pending = Some(shock);
        out.extend(self.expire(a, t));
        out
    }

    /// A follower's YES mid (0 Kalshi, 1 Polymarket) for the window opening at
    /// `open`, received at `t`. Unchanged mids are ignored.
    pub fn on_follow(&mut self, a: usize, f: usize, t: i64, open: i64, mid: Option<f64>) -> Vec<Value> {
        let Some(asset) = self.state.get_mut(a) else { return Vec::new() };
        let fol = &mut asset.followers[f];
        if fol.open == Some(open) && fol.mid == mid {
            return self.expire(a, t);
        }
        if fol.open != Some(open) {
            fol.trail.clear();
        }
        fol.open = Some(open);
        fol.mid = mid;
        if let Some(m) = mid {
            fol.trail.push(t, m, FOLLOW_KEEP_NS);
            for s in &mut asset.sources {
                if let Some(sh) = s.pending.as_mut().filter(|sh| sh.open == open) {
                    sh.follow(f, t, m);
                }
            }
        }
        self.expire(a, t)
    }

    /// End the asset's shocks that are answered, timed out or rolled over.
    fn expire(&mut self, a: usize, t: i64) -> Vec<Value> {
        let mut out = Vec::new();
        let Some(asset) = self.state.get_mut(a) else { return out };
        let k_open = asset.followers[0].open;
        for (src, s) in asset.sources.iter_mut().enumerate() {
            let end = s.pending.as_ref().is_some_and(|sh| {
                sh.done() || t - sh.t0 > TIMEOUT_NS || k_open.is_some_and(|o| o != sh.open)
            });
            if !end {
                continue;
            }
            let sh = s.pending.take().expect("pending");
            let lag_ms = |f: usize| sh.lag[f].map(|l| round_dp(l as f64 / 1e6, 1));
            for f in 0..2 {
                if sh.base[f].is_some() {
                    s.stats[f].add(lag_ms(f));
                    self.totals[src][f].add(lag_ms(f));
                }
            }
            let mut r = json!({
                "type": "leadlag_shock",
                "asset": self.assets[a],
                "source": SOURCES[src],
                "t_ns": sh.t0,
                "onset_ns": sh.onset,
                "confirm_ms": round_dp((sh.t0 - sh.onset) as f64 / 1e6, 1),
                "open_unix": sh.open,
                "sec_into": round_dp(sh.t0 as f64 / 1e9 - sh.open as f64, 3),
                "ret_bps": round_dp(sh.ret_bps, 2),
                "threshold_bps": round_dp(sh.thr_bps, 2),
                "kalshi_mid0": sh.k_mid0,
                "kalshi_base": sh.base[0],
                "poly_base": sh.base[1],
                "kalshi_lag_ms": lag_ms(0),
                "poly_lag_ms": lag_ms(1),
                "kalshi_against": sh.against[0],
                "poly_against": sh.against[1],
            });
            if let Some(o) = r.as_object_mut() {
                o.extend(self.stamp.clone());
            }
            out.push(r);
        }
        out
    }

    /// End whatever has timed out by `t` (call it when nothing else arrives).
    pub fn tick(&mut self, t: i64) -> Vec<Value> {
        (0..self.assets.len()).flat_map(|a| self.expire(a, t)).collect()
    }

    pub fn snapshot(&self) -> Value {
        let mut assets = Map::new();
        for (name, st) in self.assets.iter().zip(&self.state) {
            let mut per = Map::new();
            for (src, s) in st.sources.iter().enumerate() {
                if s.samples == 0 && s.trail.0.is_empty() {
                    continue;
                }
                per.insert(
                    SOURCES[src].into(),
                    json!({
                        "feed_lag_ms_p50": round_opt(s.feed_lag_ms.quantile(0.5), 1),
                        "feed_lag_ms_p90": round_opt(s.feed_lag_ms.quantile(0.9), 1),
                        "typical_1s_bps": round_opt(s.ewma_bps, 2),
                        "warm": s.samples >= WARMUP,
                        "shocks": s.shocks,
                        "insensitive": s.insensitive,
                        "kalshi": s.stats[0].json(),
                        "poly": s.stats[1].json(),
                    }),
                );
            }
            assets.insert(name.clone(), Value::Object(per));
        }
        let totals: Map<String, Value> = SOURCES
            .iter()
            .enumerate()
            .map(|(i, s)| {
                let [k, p] = &self.totals[i];
                ((*s).into(), json!({"kalshi": k.json(), "poly": p.json()}))
            })
            .collect();
        json!({
            "params": {
                "shock_k": SHOCK_K,
                "min_shock_bps": MIN_SHOCK_BPS,
                "onset_fraction": ONSET_FRACTION,
                "timeout_ms": TIMEOUT_NS / 1_000_000,
                "cooldown_ms": COOLDOWN_NS / 1_000_000,
                "step": 0.01,
                "kalshi_mid_range": [SENSITIVE.0, SENSITIVE.1],
            },
            "assets": assets,
            "totals": totals,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const S: i64 = 1_000_000_000;
    const MS: i64 = 1_000_000;

    /// 120 s of a quiet market (±1 bp a second), Kalshi at 0.50 and Polymarket at 0.49.
    fn warmed() -> LeadLag {
        let mut ll = LeadLag::new(vec!["BTC".into()], Map::new());
        ll.on_follow(0, 0, 0, 900, Some(0.50));
        ll.on_follow(0, 1, 0, 900, Some(0.49));
        for i in 0..120 {
            let px = if i % 2 == 0 { 100.0 } else { 100.01 };
            assert!(ll.on_ref(0, 1, i * S, px, None).is_empty());
        }
        ll
    }

    #[test]
    fn times_the_followers_from_the_onset() {
        let mut ll = warmed();
        // The move starts at 120.2 s (+2.5 bp, under the 3 bp floor) and is
        // confirmed at 120.5 s (+6 bp); the onset is the first third of it.
        let onset = 120 * S + 200 * MS;
        assert!(ll.on_ref(0, 1, onset, 100.035, None).is_empty());
        let t0 = 120 * S + 500 * MS;
        assert!(ll.on_ref(0, 1, t0, 100.07, Some(t0 / MS - 7)).is_empty());
        // Polymarket follows 300 ms after the onset; Kalshi 620 ms after it.
        assert!(ll.on_follow(0, 1, t0, 900, Some(0.495)).is_empty()); // half a cent: not yet
        assert!(ll.on_follow(0, 1, onset + 300 * MS, 900, Some(0.50)).is_empty());
        let out = ll.on_follow(0, 0, onset + 620 * MS, 900, Some(0.52));
        assert_eq!(out.len(), 1);
        let r = &out[0];
        assert_eq!((r["asset"].as_str(), r["source"].as_str()), (Some("BTC"), Some("perp")));
        assert_eq!((r["poly_lag_ms"].as_f64(), r["kalshi_lag_ms"].as_f64()), (Some(300.0), Some(620.0)));
        assert_eq!(r["confirm_ms"].as_f64(), Some(300.0));
        let snap = ll.snapshot();
        let btc = &snap["assets"]["BTC"]["perp"];
        assert_eq!((btc["shocks"].as_u64(), btc["kalshi"]["responded"].as_u64()), (Some(1), Some(1)));
        assert_eq!(btc["kalshi"]["before_binance"].as_u64(), Some(0));
        assert_eq!(btc["feed_lag_ms_p50"].as_f64(), Some(7.0));
        assert_eq!(snap["totals"]["perp"]["poly"]["lag_ms_p50"].as_f64(), Some(300.0));
    }

    #[test]
    fn a_follower_that_moved_first_has_a_negative_lag() {
        let mut ll = warmed();
        // Kalshi moves up a cent 400 ms before Binance's onset.
        ll.on_follow(0, 0, 120 * S + 100 * MS, 900, Some(0.51));
        let t0 = 120 * S + 500 * MS;
        assert!(ll.on_ref(0, 1, t0, 100.2, None).is_empty());
        // A second shock inside the cooldown is ignored; then the first times out.
        assert!(ll.on_ref(0, 1, t0 + 100 * MS, 100.5, None).is_empty());
        let out = ll.tick(t0 + TIMEOUT_NS + 1);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0]["kalshi_lag_ms"].as_f64(), Some(-400.0));
        assert!(out[0]["poly_lag_ms"].is_null());
        assert_eq!(ll.snapshot()["totals"]["perp"]["kalshi"]["before_binance"], 1);
        assert!(ll.tick(t0 + 2 * TIMEOUT_NS).is_empty());
    }

    #[test]
    fn quiet_seconds_count_as_zero_moves() {
        let mut ll = LeadLag::new(vec!["BTC".into()], Map::new());
        ll.on_ref(0, 0, 0, 100.0, None);
        ll.on_ref(0, 0, S, 100.1, None);
        ll.on_ref(0, 0, 2 * S, 100.1, None); // the +10 bp second is sampled here
        let busy = ll.snapshot()["assets"]["BTC"]["spot"]["typical_1s_bps"].as_f64().unwrap();
        // 90 s without a new best bid or ask: warm, and the typical move decays.
        ll.on_ref(0, 0, 92 * S, 100.1, None);
        let snap = ll.snapshot();
        let quiet = &snap["assets"]["BTC"]["spot"];
        assert_eq!(quiet["warm"], true);
        assert!(quiet["typical_1s_bps"].as_f64().unwrap() < busy);
    }

    #[test]
    fn each_reference_is_timed_on_its_own() {
        let mut ll = warmed();
        // Kraken (source 2) warms on its own clock; Binance perp stays quiet.
        for i in 0..120 {
            let px = if i % 2 == 0 { 100.0 } else { 100.01 };
            ll.on_ref(0, 2, i * S + 50 * MS, px, Some((i * S + 50 * MS) / MS - 5));
        }
        let t0 = 120 * S + 500 * MS;
        assert!(ll.on_ref(0, 2, t0, 100.2, None).is_empty());
        let out = ll.on_follow(0, 1, t0 + 80 * MS, 900, Some(0.51));
        assert!(out.is_empty()); // Kalshi has not answered yet
        let out = ll.on_follow(0, 0, t0 + 200 * MS, 900, Some(0.52));
        assert_eq!(out.len(), 1);
        assert_eq!(out[0]["source"], "kraken");
        let snap = ll.snapshot();
        assert_eq!(snap["assets"]["BTC"]["kraken"]["feed_lag_ms_p50"].as_f64(), Some(5.0));
        assert_eq!(snap["totals"]["kraken"]["poly"]["responded"], 1);
        assert_eq!(snap["totals"]["perp"]["poly"]["n"], 0);
        assert_eq!(snap["totals"]["bitstamp"]["kalshi"]["n"], 0);
    }

    #[test]
    fn ignores_insensitive_books_and_ends_on_a_roll() {
        let mut ll = warmed();
        ll.on_follow(0, 0, 120 * S, 900, Some(0.95));
        assert!(ll.on_ref(0, 1, 120 * S + 500 * MS, 100.2, None).is_empty());
        assert_eq!(ll.snapshot()["assets"]["BTC"]["perp"]["insensitive"], 1);
        // Back in range, a shock down, then the window rolls before anyone moves.
        ll.on_follow(0, 0, 123 * S, 900, Some(0.50));
        ll.on_ref(0, 1, 126 * S, 100.2, None);
        assert!(ll.on_ref(0, 1, 126 * S + 900 * MS, 99.9, None).is_empty());
        let out = ll.on_follow(0, 0, 127 * S, 1800, Some(0.50));
        assert_eq!(out.len(), 1);
        assert!(out[0]["ret_bps"].as_f64().unwrap() < 0.0);
        assert!(out[0]["kalshi_lag_ms"].is_null());
    }
}
