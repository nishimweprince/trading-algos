//! Binance spot as a second signal next to Kalshi.
//!
//! Spot leads Kalshi (a Kalshi jump is followed by ≤ 1 bp of spot, 10-23), and
//! Kalshi reaches Tokyo ~90 ms later than Binance does, so Predict's fills were
//! picked off within 5 s (−2.1¢ ± 0.5 markout, 10-27). Three uses:
//!
//! 1. **Guard**, directional: a Binance *fall* of `pause_bps` within
//!    `GUARD_WINDOW_MS` takes a coin's Up (YES) first leg off, a *rise* its
//!    Down (NO) one: falling spot is what sellers dump into an Up bid, while
//!    the Down bid is the side it favours. Likewise while the Kalshi fair
//!    corrected by Binance strays `pause_gap` below or above Kalshi's own.
//! 2. **Adjusted fair**: Kalshi's fair moved by the Binance log move since
//!    Kalshi's fair last changed, `Φ(Φ⁻¹(k) + Δ ln S / (σ √τ))`.
//! 3. **Spot fair**: `Φ(ln(S / K) / (σ √τ))` from Binance alone, K being
//!    Predict's own start price for the window.
//!
//! σ is the larger of Binance's realised volatility (an EWMA of squared 1 s
//! log returns) and the volatility Kalshi's own price implies (`implied_sigma`,
//! smoothed per asset). Realised alone ran ~2.4× under Kalshi's in a calm
//! warm-up (10-27 12:17Z), which made every spot move look 2.4× too big. With
//! no Kalshi-implied value yet there is no Binance-informed fair at all.
//! Which fair prices first legs and pairing legs is configuration;
//! `FairScores` keeps the evidence for choosing.

use std::collections::{HashMap, VecDeque};

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

/// The span of the guard's move.
pub const GUARD_WINDOW_MS: i64 = 1_000;
/// A Binance mid older than this is no signal (the guard and both fairs fall back).
pub const SPOT_MAX_AGE_MS: i64 = 5_000;
/// σ needs this many 1 s returns (a few minutes) before it is used.
const MIN_VOL_SAMPLES: u32 = 120;
/// EWMA half-life of the variance, in 1 s samples (5 minutes).
const VOL_HALF_LIFE: f64 = 300.0;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum FairSource {
    #[default]
    Kalshi,
    Adjusted,
    Binance,
}

impl FairSource {
    pub fn parse(text: &str) -> anyhow::Result<Self> {
        match text.trim().to_ascii_lowercase().as_str() {
            "kalshi" => Ok(Self::Kalshi),
            "adjusted" | "kalshi+binance" => Ok(Self::Adjusted),
            "binance" | "spot" => Ok(Self::Binance),
            other => anyhow::bail!("fair source must be kalshi, adjusted or binance, got {other:?}"),
        }
    }
}

/// The three fairs of one market at one moment (YES probability).
#[derive(Clone, Copy, Debug, Default)]
pub struct Fairs {
    pub kalshi: f64,
    pub adjusted: Option<f64>,
    pub spot: Option<f64>,
}

impl Fairs {
    /// The chosen source, Kalshi when it is not available.
    pub fn pick(&self, source: FairSource) -> f64 {
        match source {
            FairSource::Kalshi => self.kalshi,
            FairSource::Adjusted => self.adjusted.unwrap_or(self.kalshi),
            FairSource::Binance => self.spot.unwrap_or(self.kalshi),
        }
    }
}

#[derive(Default)]
struct AssetSpot {
    mid: f64,
    ms: i64,
    /// `(ms, ln mid)` over the guard window.
    trail: VecDeque<(i64, f64)>,
    move_bps: f64,
    /// Signed log move over the guard window, in bp (positive: rising).
    signed_bps: f64,
    /// Until when the Up bid (a fall) or the Down bid (a rise) is held off.
    fall_until: i64,
    rise_until: i64,
    pauses: u64,
    last_sample: Option<(i64, f64)>,
    var_per_s: f64,
    samples: u32,
}

#[derive(Default)]
pub struct Spot {
    assets: HashMap<String, AssetSpot>,
}

impl Spot {
    /// A Binance `bookTicker` frame (bare or under the combined stream's
    /// `data`); the asset it updated.
    pub fn frame(&mut self, text: &str, now: i64, pause_bps: f64, pause_ms: i64) -> Option<String> {
        let value: Value = serde_json::from_str(text).ok()?;
        let data = value.get("data").unwrap_or(&value);
        let symbol = data.get("s")?.as_str()?;
        let asset = symbol.strip_suffix("USDT")?.to_owned();
        let bid: f64 = data.get("b")?.as_str()?.parse().ok()?;
        let ask: f64 = data.get("a")?.as_str()?.parse().ok()?;
        if !(bid > 0.0 && ask >= bid) {
            return None;
        }
        self.update(&asset, (bid + ask) / 2.0, now, pause_bps, pause_ms);
        Some(asset)
    }

    fn update(&mut self, asset: &str, mid: f64, now: i64, pause_bps: f64, pause_ms: i64) {
        let s = self.assets.entry(asset.to_owned()).or_default();
        let ln = mid.ln();
        s.mid = mid;
        s.ms = now;
        s.trail.push_back((now, ln));
        while s.trail.front().is_some_and(|(t, _)| now - t > GUARD_WINDOW_MS) {
            s.trail.pop_front();
        }
        let low = s.trail.iter().map(|(_, x)| *x).fold(f64::INFINITY, f64::min);
        let high = s.trail.iter().map(|(_, x)| *x).fold(f64::NEG_INFINITY, f64::max);
        let (rise, fall) = ((ln - low) * 1e4, (high - ln) * 1e4);
        s.move_bps = rise.max(fall);
        s.signed_bps = s.trail.front().map_or(0.0, |(_, x)| (ln - x) * 1e4);
        if pause_bps > 0.0 && (rise >= pause_bps || fall >= pause_bps) {
            if s.fall_until.max(s.rise_until) < now {
                s.pauses += 1;
            }
            if rise >= pause_bps {
                s.rise_until = now + pause_ms;
            }
            if fall >= pause_bps {
                s.fall_until = now + pause_ms;
            }
        }
        match s.last_sample {
            None => s.last_sample = Some((now, ln)),
            Some((t0, l0)) if now - t0 >= 1_000 => {
                let dt = (now - t0) as f64 / 1_000.0;
                let x = (ln - l0).powi(2) / dt;
                let lambda = 0.5_f64.powf(1.0 / VOL_HALF_LIFE);
                s.var_per_s = if s.samples == 0 { x } else { lambda * s.var_per_s + (1.0 - lambda) * x };
                s.samples = s.samples.saturating_add(1);
                s.last_sample = Some((now, ln));
            }
            Some(_) => {}
        }
    }

    fn fresh(&self, asset: &str, now: i64) -> Option<&AssetSpot> {
        self.assets.get(asset).filter(|s| now - s.ms <= SPOT_MAX_AGE_MS)
    }

    /// The assets seen on Binance.
    pub fn assets(&self) -> impl Iterator<Item = &String> {
        self.assets.keys()
    }

    /// The fresh Binance mid.
    pub fn mid(&self, asset: &str, now: i64) -> Option<f64> {
        self.fresh(asset, now).map(|s| s.mid)
    }

    /// σ of the log price per √second, once warmed up.
    pub fn sigma(&self, asset: &str, now: i64) -> Option<f64> {
        self.fresh(asset, now).filter(|s| s.samples >= MIN_VOL_SAMPLES && s.var_per_s > 0.0).map(|s| s.var_per_s.sqrt())
    }

    /// Which first legs a recent fast move holds off: `(up, down)`. A fall
    /// threatens the Up bid, a rise the Down bid.
    pub fn guard(&self, asset: &str, now: i64) -> (bool, bool) {
        self.fresh(asset, now).map_or((false, false), |s| (now < s.fall_until, now < s.rise_until))
    }

    /// The fresh log mid, and its signed move over the guard window (bp).
    pub fn ln_and_move(&self, asset: &str, now: i64) -> Option<(f64, f64)> {
        self.fresh(asset, now).map(|s| (s.mid.ln(), s.signed_bps))
    }

    pub fn status(&self, now: i64) -> Value {
        let mut out = serde_json::Map::new();
        for (asset, s) in &self.assets {
            out.insert(
                asset.clone(),
                json!({
                    "mid": s.mid,
                    "age_ms": now - s.ms,
                    "move_bps": s.move_bps,
                    "sigma_bps_per_min": (s.samples >= MIN_VOL_SAMPLES).then(|| s.var_per_s.sqrt() * 1e4 * 60_f64.sqrt()),
                    "vol_samples": s.samples,
                    "paused": now < s.fall_until.max(s.rise_until),
                    "up_off": now < s.fall_until,
                    "down_off": now < s.rise_until,
                    "move_signed_bps": s.signed_bps,
                    "pauses": s.pauses,
                }),
            );
        }
        Value::Object(out)
    }
}

/// Spot must be this far from the strike (log) for Kalshi's price to imply a
/// stable σ: Binance and the Chainlink start price differ by a few bp.
const IMPLIED_MIN_LN: f64 = 8e-4;
/// … and Kalshi this far from 0.5 (z), where σ = ln(S/K) / (z √τ) blows up.
const IMPLIED_MIN_Z: f64 = 0.25;
/// Half-life of the per-asset implied σ, in seconds.
pub const IMPLIED_HALF_LIFE_S: f64 = 300.0;
/// An implied σ older than this is not used.
pub const IMPLIED_MAX_AGE_MS: i64 = 10 * 60_000;

/// The σ (per √s) that makes `spot_fair(spot, strike, σ, τ)` equal Kalshi's
/// fair, where that is well determined.
pub fn implied_sigma(kalshi: f64, spot: f64, strike: f64, tau_s: f64) -> Option<f64> {
    if tau_s < 60.0 || !(spot > 0.0 && strike > 0.0) {
        return None;
    }
    let z = norm_inv(kalshi.clamp(0.02, 0.98));
    let x = (spot / strike).ln();
    if z.abs() < IMPLIED_MIN_Z || x.abs() < IMPLIED_MIN_LN || z.signum() != x.signum() {
        return None;
    }
    let sigma = x / (z * tau_s.sqrt());
    (sigma.is_finite() && sigma > 0.0).then_some(sigma)
}

/// Kalshi's fair moved by `d_ln` of spot since it last changed.
pub fn adjusted_fair(kalshi: f64, d_ln: f64, sigma: f64, tau_s: f64) -> f64 {
    let z = norm_inv(kalshi.clamp(0.005, 0.995));
    norm_cdf(z + d_ln / (sigma * tau_s.max(1.0).sqrt()))
}

/// P(spot ends above `strike`) for driftless log spot with `sigma` per √s.
pub fn spot_fair(spot: f64, strike: f64, sigma: f64, tau_s: f64) -> f64 {
    norm_cdf((spot / strike).ln() / (sigma * tau_s.max(1.0).sqrt()))
}

/// Standard normal CDF (Abramowitz–Stegun 7.1.26 erf, |error| < 1.5e-7).
pub fn norm_cdf(x: f64) -> f64 {
    let z = x.abs() / std::f64::consts::SQRT_2;
    let t = 1.0 / (1.0 + 0.327_591_1 * z);
    let poly = t * (0.254_829_592 + t * (-0.284_496_736 + t * (1.421_413_741 + t * (-1.453_152_027 + t * 1.061_405_429))));
    let erf = 1.0 - poly * (-z * z).exp();
    if x >= 0.0 { 0.5 * (1.0 + erf) } else { 0.5 * (1.0 - erf) }
}

/// Inverse standard normal CDF (Acklam, relative error < 1.2e-9).
pub fn norm_inv(p: f64) -> f64 {
    const A: [f64; 6] = [-3.969_683_028_665_376e1, 2.209_460_984_245_205e2, -2.759_285_104_469_687e2, 1.383_577_518_672_69e2, -3.066_479_806_614_716e1, 2.506_628_277_459_239];
    const B: [f64; 5] = [-5.447_609_879_822_406e1, 1.615_858_368_580_409e2, -1.556_989_798_598_866e2, 6.680_131_188_771_972e1, -1.328_068_155_288_572e1];
    const C: [f64; 6] = [-7.784_894_002_430_293e-3, -3.223_964_580_411_365e-1, -2.400_758_277_161_838, -2.549_732_539_343_734, 4.374_664_141_464_968, 2.938_163_982_698_783];
    const D: [f64; 4] = [7.784_695_709_041_462e-3, 3.224_671_290_700_398e-1, 2.445_134_137_142_996, 3.754_408_661_907_416];
    let p = p.clamp(1e-12, 1.0 - 1e-12);
    let low = 0.024_25;
    if p < low {
        let q = (-2.0 * p.ln()).sqrt();
        (((((C[0] * q + C[1]) * q + C[2]) * q + C[3]) * q + C[4]) * q + C[5]) / ((((D[0] * q + D[1]) * q + D[2]) * q + D[3]) * q + 1.0)
    } else if p <= 1.0 - low {
        let q = p - 0.5;
        let r = q * q;
        (((((A[0] * r + A[1]) * r + A[2]) * r + A[3]) * r + A[4]) * r + A[5]) * q
            / (((((B[0] * r + B[1]) * r + B[2]) * r + B[3]) * r + B[4]) * r + 1.0)
    } else {
        -norm_inv(1.0 - p)
    }
}

/// Mean and standard error by Welford's method.
#[derive(Clone, Copy, Debug, Default, Serialize, Deserialize)]
pub struct Stat {
    pub n: u64,
    pub mean: f64,
    m2: f64,
}

impl Stat {
    pub fn push(&mut self, x: f64) {
        if !x.is_finite() {
            return;
        }
        self.n += 1;
        let d = x - self.mean;
        self.mean += d / self.n as f64;
        self.m2 += d * (x - self.mean);
    }
    pub fn se(&self) -> Option<f64> {
        (self.n > 1).then(|| (self.m2 / (self.n - 1) as f64 / self.n as f64).sqrt())
    }
    pub fn json(&self) -> Value {
        let se = self.se();
        json!({"n": self.n, "mean": self.mean, "se": se, "t": se.filter(|s| *s > 0.0).map(|s| self.mean / s)})
    }
}

/// One scoring sample: the three fairs and Predict's own YES mid.
#[derive(Clone, Copy, Debug)]
pub struct Sample {
    pub ms: i64,
    pub fairs: [f64; 4], // kalshi, adjusted, spot, predict mid
}

pub const SOURCES: [&str; 4] = ["kalshi", "adjusted", "binance", "predict_mid"];

#[derive(Default)]
struct Pending {
    close_ms: i64,
    samples: Vec<Sample>,
    /// |fair − Predict's mid 5 s later|, per source.
    lead: Vec<[f64; 4]>,
}

/// Window-level evidence: each settled window contributes one value per
/// metric (its samples share one outcome, so they are not independent).
#[derive(Default, Serialize, Deserialize)]
pub struct FairScores {
    pub windows: u64,
    pub brier: [Stat; 4],
    pub lead: [Stat; 4],
    /// Kalshi's score minus the source's (positive: the source beats Kalshi).
    pub brier_gain: [Stat; 4],
    pub lead_gain: [Stat; 4],
    #[serde(skip)]
    pending: HashMap<String, Pending>,
}

impl FairScores {
    pub fn load(path: &std::path::Path) -> Self {
        std::fs::read(path).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default()
    }

    pub fn save(&self, path: &std::path::Path) {
        if let Ok(bytes) = serde_json::to_vec(self) {
            let _ = std::fs::write(path, bytes);
        }
    }

    /// A sample of `market`, which closes at `close_ms`; also scores the
    /// sample about 5 s before it against this one's Predict mid.
    pub fn sample(&mut self, market: &str, close_ms: i64, sample: Sample) {
        let p = self.pending.entry(market.to_owned()).or_default();
        p.close_ms = close_ms;
        if let Some(prev) = p.samples.last()
            && (4_000..=7_000).contains(&(sample.ms - prev.ms))
        {
            let mid = sample.fairs[3];
            p.lead.push(prev.fairs.map(|f| (f - mid).abs()));
        }
        p.samples.push(sample);
    }

    /// Markets (and their close) whose window closed `after_ms` ago and still await an outcome.
    pub fn due(&self, now: i64, after_ms: i64) -> Vec<(String, i64)> {
        self.pending.iter().filter(|(_, p)| now - p.close_ms >= after_ms).map(|(m, p)| (m.clone(), p.close_ms)).collect()
    }

    pub fn drop_market(&mut self, market: &str) {
        self.pending.remove(market);
    }

    /// Score `market` at its outcome.
    pub fn resolve(&mut self, market: &str, yes_won: bool) {
        let Some(p) = self.pending.remove(market) else { return };
        if p.samples.is_empty() {
            return;
        }
        let outcome = if yes_won { 1.0 } else { 0.0 };
        let n = p.samples.len() as f64;
        let mut brier = [0.0; 4];
        for s in &p.samples {
            for i in 0..4 {
                brier[i] += (s.fairs[i] - outcome).powi(2) / n;
            }
        }
        self.windows += 1;
        for i in 0..4 {
            self.brier[i].push(brier[i]);
            self.brier_gain[i].push(brier[0] - brier[i]);
        }
        if !p.lead.is_empty() {
            let m = p.lead.len() as f64;
            let mut lead = [0.0; 4];
            for e in &p.lead {
                for i in 0..4 {
                    lead[i] += e[i] / m;
                }
            }
            for i in 0..4 {
                self.lead[i].push(lead[i]);
                self.lead_gain[i].push(lead[0] - lead[i]);
            }
        }
    }

    pub fn status(&self) -> Value {
        let per = |stats: &[Stat; 4]| -> Value {
            Value::Object(SOURCES.iter().zip(stats).map(|(k, s)| ((*k).to_owned(), s.json())).collect())
        };
        json!({
            "windows": self.windows,
            "pending": self.pending.len(),
            "brier": per(&self.brier),
            "lead_5s": per(&self.lead),
            "brier_gain_vs_kalshi": per(&self.brier_gain),
            "lead_gain_vs_kalshi": per(&self.lead_gain),
        })
    }
}

/// The asset of a Kalshi 15-minute ticker (`KXBTC15M-…` → `BTC`).
pub fn asset_of_ticker(ticker: &str) -> Option<&str> {
    ticker.strip_prefix("KX")?.split_once("15M").map(|(asset, _)| asset)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normal_functions_round_trip() {
        assert!((norm_cdf(0.0) - 0.5).abs() < 1e-7);
        assert!((norm_cdf(1.96) - 0.975).abs() < 1e-4);
        for p in [0.01, 0.2, 0.5, 0.8, 0.99] {
            assert!((norm_cdf(norm_inv(p)) - p).abs() < 1e-6, "{p}");
        }
    }

    #[test]
    fn adjusted_fair_follows_spot_and_keeps_kalshi_when_flat() {
        let sigma = 1.5e-4; // 1.5 bp per √s
        assert!((adjusted_fair(0.55, 0.0, sigma, 600.0) - 0.55).abs() < 1e-6);
        // +5 bp with 10 minutes left: σ√τ ≈ 37 bp, so about +5¢ at the middle.
        let up = adjusted_fair(0.50, 5e-4, sigma, 600.0);
        assert!(up > 0.54 && up < 0.56, "{up}");
        assert!(adjusted_fair(0.50, -5e-4, sigma, 600.0) < 0.47);
    }

    #[test]
    fn implied_sigma_inverts_spot_fair_where_stable() {
        let (k, tau) = (100.0, 600.0);
        let sigma = 2.0e-4;
        let s = k * (1.0 + 30e-4); // 30 bp over the strike
        let fair = spot_fair(s, k, sigma, tau);
        let back = implied_sigma(fair, s, k, tau).unwrap();
        assert!((back / sigma - 1.0).abs() < 1e-3, "{back}");
        assert!(implied_sigma(0.52, k * 1.0001, k, tau).is_none(), "at the money: unstable");
        assert!(implied_sigma(0.30, s, k, tau).is_none(), "Kalshi below 0.5 with spot above: basis noise");
        assert!(implied_sigma(fair, s, k, 30.0).is_none(), "under a minute left");
    }

    #[test]
    fn spot_fair_is_half_at_the_strike() {
        assert!((spot_fair(100.0, 100.0, 1.5e-4, 600.0) - 0.5).abs() < 1e-9);
        assert!(spot_fair(100.1, 100.0, 1.5e-4, 600.0) > 0.6);
    }

    #[test]
    fn a_fast_move_pauses_and_vol_warms_up() {
        let mut s = Spot::default();
        let frame = |px: f64| format!(r#"{{"stream":"btcusdt@bookTicker","data":{{"s":"BTCUSDT","b":"{px}","a":"{}"}}}}"#, px + 0.1);
        assert_eq!(s.frame(&frame(100_000.0), 0, 5.0, 2_000).as_deref(), Some("BTC"));
        assert_eq!(s.guard("BTC", 100), (false, false));
        s.frame(&frame(100_060.0), 500, 5.0, 2_000); // +6 bp in 0.5 s
        assert_eq!(s.guard("BTC", 600), (false, true), "a rise holds the Down bid off, not the Up bid");
        assert_eq!(s.guard("BTC", 2_600), (false, false), "over after pause_ms");
        assert!(s.ln_and_move("BTC", 600).unwrap().1 > 5.0);
        assert!(s.sigma("BTC", 600).is_none(), "not warmed up");
        for i in 1..=MIN_VOL_SAMPLES as i64 {
            s.frame(&frame(100_060.0 + (i % 2) as f64 * 10.0), 500 + i * 1_000, 5.0, 2_000);
        }
        assert!(s.sigma("BTC", 500 + MIN_VOL_SAMPLES as i64 * 1_000).is_some());
        assert!(s.mid("BTC", 10_000_000).is_none(), "stale");
    }

    #[test]
    fn scores_count_windows_and_gains() {
        let mut f = FairScores::default();
        // Adjusted is right (0.9) where Kalshi was not (0.5); YES wins.
        f.sample("7", 10_000, Sample { ms: 0, fairs: [0.5, 0.9, 0.7, 0.5] });
        f.sample("7", 10_000, Sample { ms: 5_000, fairs: [0.5, 0.9, 0.7, 0.8] });
        assert_eq!(f.due(10_000 + 60_000, 60_000), vec![("7".to_string(), 10_000)]);
        f.resolve("7", true);
        assert_eq!(f.windows, 1);
        assert!(f.brier_gain[1].mean > 0.0 && f.lead_gain[1].mean > 0.0);
        assert!(f.due(1 << 40, 0).is_empty());
    }

    #[test]
    fn tickers_name_their_asset() {
        assert_eq!(asset_of_ticker("KXBTC15M-26OCT262115-15"), Some("BTC"));
        assert_eq!(asset_of_ticker("KXBNB15M-X"), Some("BNB"));
        assert_eq!(asset_of_ticker("OTHER"), None);
    }
}
