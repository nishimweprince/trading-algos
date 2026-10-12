//! Lead–lag: does Binance move before Kalshi and Polymarket?
//!
//! A **shock** is a Binance mid move over the last second of at least
//! [`SHOCK_K`] × the asset's typical one-second move (an EWMA of |1 s log
//! return| per asset and source), and never less than [`MIN_SHOCK_BPS`]. From
//! the shock it times how long each follower (Kalshi's YES mid, Polymarket's
//! YES mid) takes to move at least 1¢ the same way (price up = YES up), and
//! gives up after [`TIMEOUT_NS`]. A follower that had already moved 1¢ that way
//! in the second before the shock *moved first*.
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

pub const SOURCES: [&str; 2] = ["spot", "perp"];
pub const FOLLOWERS: [&str; 2] = ["kalshi", "poly"];
pub const SHOCK_K: f64 = 3.0;
pub const MIN_SHOCK_BPS: f64 = 3.0;
pub const TIMEOUT_NS: i64 = 3_000_000_000;
pub const COOLDOWN_NS: i64 = 5_000_000_000;
const W_NS: i64 = 1_000_000_000;
/// One-second samples before a source may fire.
const WARMUP: u32 = 60;
/// About five minutes of one-second samples.
const EWMA_ALPHA: f64 = 1.0 / 300.0;
const LAGS_KEPT: usize = 500;
const STEP: f64 = 0.01 - 1e-9;
const SENSITIVE: (f64, f64) = (0.10, 0.90);

/// `(t_ns, value)` covering the last second, plus the one just before it.
#[derive(Debug, Default)]
struct Trail(VecDeque<(i64, f64)>);

impl Trail {
    fn push(&mut self, t: i64, v: f64) {
        self.0.push_back((t, v));
        while self.0.len() > 1 && self.0[1].0 <= t - W_NS {
            self.0.pop_front();
        }
    }

    /// The value one second ago (the last one at or before `t - 1 s`).
    fn second_ago(&self, t: i64) -> Option<f64> {
        self.0.front().filter(|(t0, _)| *t0 <= t - W_NS).map(|(_, v)| *v)
    }

    fn clear(&mut self) {
        self.0.clear();
    }
}

#[derive(Debug)]
struct Stat {
    n: u64,
    responded: u64,
    first: u64,
    lags: Rolling,
}

impl Default for Stat {
    fn default() -> Self {
        Self { n: 0, responded: 0, first: 0, lags: Rolling::new(LAGS_KEPT) }
    }
}

impl Stat {
    fn add(&mut self, lag_ms: Option<f64>, first: bool) {
        self.n += 1;
        if first {
            self.first += 1;
        }
        if let Some(l) = lag_ms {
            self.responded += 1;
            self.lags.push(l);
        }
    }

    fn json(&self) -> Value {
        json!({
            "n": self.n,
            "responded": self.responded,
            "moved_first": self.first,
            "lag_ms_p50": round_opt(self.lags.quantile(0.5), 0),
            "lag_ms_p90": round_opt(self.lags.quantile(0.9), 0),
        })
    }
}

#[derive(Debug)]
struct Shock {
    t0: i64,
    dir: f64,
    ret_bps: f64,
    thr_bps: f64,
    open: i64,
    k_mid0: f64,
    base: [Option<f64>; 2],
    first: [bool; 2],
    lag: [Option<i64>; 2],
    against: [bool; 2],
}

impl Shock {
    fn done(&self) -> bool {
        (0..2).all(|f| self.base[f].is_none() || self.first[f] || self.lag[f].is_some())
    }
}

#[derive(Debug)]
struct Source {
    trail: Trail,
    sec: Option<i64>,
    sec_mid: Option<f64>,
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
            sec_mid: None,
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
    sources: [Source; 2],
    followers: [Follower; 2],
}

pub struct LeadLag {
    pub assets: Vec<String>,
    state: Vec<Asset>,
    totals: [[Stat; 2]; 2],
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

    /// A Binance mid for asset `a`, source `src` (0 spot, 1 perp), received at
    /// `t`. `venue_ms` is Binance's own stamp when the stream has one.
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
        // The typical one-second move: one sample per new second.
        let sec = t.div_euclid(W_NS);
        if s.sec != Some(sec) {
            if let Some(prev) = s.sec_mid {
                let r = (mid / prev).ln().abs() * 1e4;
                s.ewma_bps = Some(match s.ewma_bps {
                    Some(e) => e + EWMA_ALPHA * (r - e),
                    None => r,
                });
                s.samples += 1;
            }
            s.sec = Some(sec);
            s.sec_mid = Some(mid);
        }
        s.trail.push(t, mid);
        let (Some(ago), Some(ewma)) = (s.trail.second_ago(t), s.ewma_bps) else { return out };
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
        let mut shock = Shock {
            t0: t,
            dir,
            ret_bps,
            thr_bps,
            open,
            k_mid0: k_mid,
            base: [None; 2],
            first: [false; 2],
            lag: [None; 2],
            against: [false; 2],
        };
        for (f, fol) in asset.followers.iter().enumerate() {
            if fol.open != Some(open) {
                continue;
            }
            shock.base[f] = fol.mid;
            if let (Some(m), Some(ago)) = (fol.mid, fol.trail.second_ago(t)) {
                shock.first[f] = dir * (m - ago) >= STEP;
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
            fol.trail.push(t, m);
        }
        for s in &mut asset.sources {
            let Some(sh) = s.pending.as_mut() else { continue };
            if sh.open != open {
                continue; // expire() ends it: the window rolled
            }
            let (Some(base), Some(m)) = (sh.base[f], mid) else { continue };
            let moved = sh.dir * (m - base);
            if sh.lag[f].is_none() && moved >= STEP {
                sh.lag[f] = Some(t - sh.t0);
            } else if moved <= -STEP {
                sh.against[f] = true;
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
                    s.stats[f].add(lag_ms(f), sh.first[f]);
                    self.totals[src][f].add(lag_ms(f), sh.first[f]);
                }
            }
            let mut r = json!({
                "type": "leadlag_shock",
                "asset": self.assets[a],
                "source": SOURCES[src],
                "t_ns": sh.t0,
                "open_unix": sh.open,
                "sec_into": round_dp(sh.t0 as f64 / 1e9 - sh.open as f64, 3),
                "ret_bps": round_dp(sh.ret_bps, 2),
                "threshold_bps": round_dp(sh.thr_bps, 2),
                "kalshi_mid0": sh.k_mid0,
                "poly_mid0": sh.base[1],
                "kalshi_lag_ms": lag_ms(0),
                "poly_lag_ms": lag_ms(1),
                "kalshi_first": sh.first[0],
                "poly_first": sh.first[1],
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
    fn times_the_followers_after_a_shock() {
        let mut ll = warmed();
        let t0 = 120 * S + 500 * MS;
        // +20 bp in half a second: a shock up.
        assert!(ll.on_ref(0, 1, t0, 100.2, Some(t0 / MS - 7)).is_empty());
        // Polymarket follows after 200 ms; Kalshi after 420 ms.
        assert!(ll.on_follow(0, 1, t0 + 180 * MS, 900, Some(0.495)).is_empty()); // half a cent: not yet
        assert!(ll.on_follow(0, 1, t0 + 200 * MS, 900, Some(0.50)).is_empty());
        let out = ll.on_follow(0, 0, t0 + 420 * MS, 900, Some(0.52));
        assert_eq!(out.len(), 1);
        let r = &out[0];
        assert_eq!((r["asset"].as_str(), r["source"].as_str()), (Some("BTC"), Some("perp")));
        assert_eq!((r["poly_lag_ms"].as_f64(), r["kalshi_lag_ms"].as_f64()), (Some(200.0), Some(420.0)));
        assert_eq!(r["kalshi_first"], false);
        let snap = ll.snapshot();
        let btc = &snap["assets"]["BTC"]["perp"];
        assert_eq!((btc["shocks"].as_u64(), btc["kalshi"]["responded"].as_u64()), (Some(1), Some(1)));
        assert_eq!(btc["feed_lag_ms_p50"].as_f64(), Some(7.0));
        assert_eq!(snap["totals"]["perp"]["poly"]["lag_ms_p50"].as_f64(), Some(200.0));
    }

    #[test]
    fn times_out_marks_movers_first_and_cools_down() {
        let mut ll = warmed();
        // Kalshi moved up a cent in the second before the shock.
        ll.on_follow(0, 0, 120 * S + 100 * MS, 900, Some(0.51));
        let t0 = 120 * S + 500 * MS;
        assert!(ll.on_ref(0, 1, t0, 100.2, None).is_empty());
        // A second shock inside the cooldown is ignored; then the first times out.
        assert!(ll.on_ref(0, 1, t0 + 100 * MS, 100.5, None).is_empty());
        let out = ll.tick(t0 + TIMEOUT_NS + 1);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0]["kalshi_first"], true);
        assert!(out[0]["poly_lag_ms"].is_null());
        assert!(ll.tick(t0 + 2 * TIMEOUT_NS).is_empty());
    }

    #[test]
    fn ignores_insensitive_books_and_ends_on_a_roll() {
        let mut ll = warmed();
        ll.on_follow(0, 0, 120 * S, 900, Some(0.95));
        assert!(ll.on_ref(0, 1, 120 * S + 500 * MS, 100.2, None).is_empty());
        assert_eq!(ll.snapshot()["assets"]["BTC"]["perp"]["insensitive"], 1);
        // Back in range, a shock down, then the window rolls before anyone moves.
        ll.on_follow(0, 0, 126 * S, 900, Some(0.50));
        ll.on_ref(0, 1, 126 * S, 100.2, None);
        assert!(ll.on_ref(0, 1, 126 * S + 900 * MS, 99.9, None).is_empty());
        let out = ll.on_follow(0, 0, 127 * S, 1800, Some(0.50));
        assert_eq!(out.len(), 1);
        assert!(out[0]["ret_bps"].as_f64().unwrap() < 0.0);
        assert!(out[0]["kalshi_lag_ms"].is_null());
    }
}
