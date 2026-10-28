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
//! With the USDⓈ-M perpetual connected (`perp_primary`, off by default) its
//! mid drives all three. It is Binance's busiest book (~930 updates a second
//! against spot's ~280, Tokyo 10-28) but did not move first: spot fired the
//! 5 bp guard first on 76% of shared moves. The perp trades a few bp off
//! spot, so it is brought to spot's level by the basis `ln perp − ln spot`, an
//! EWMA (`BASIS_HALF_LIFE_MS`) sampled on each spot frame: the spot fair keeps
//! spot's level against Predict's start price and moves at the perp's speed.
//! With the perp stale for `PERP_MAX_AGE_MS`, spot frames drive as before.
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
/// A perp mid older than this neither samples the basis nor stops spot
/// frames from driving.
const PERP_MAX_AGE_MS: i64 = 1_000;
/// EWMA half-life of the perp–spot basis.
const BASIS_HALF_LIFE_MS: f64 = 60_000.0;
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
    Blend,
    Anchored,
}

impl FairSource {
    pub fn parse(text: &str) -> anyhow::Result<Self> {
        match text.trim().to_ascii_lowercase().as_str() {
            "kalshi" => Ok(Self::Kalshi),
            "adjusted" | "kalshi+binance" => Ok(Self::Adjusted),
            "binance" | "spot" => Ok(Self::Binance),
            "blend" => Ok(Self::Blend),
            "anchored" | "binance_anchored" => Ok(Self::Anchored),
            other => anyhow::bail!(
                "fair source must be kalshi, adjusted, binance, blend or anchored, got {other:?}"
            ),
        }
    }
}

/// The fairs of one market at one moment (YES probability).
#[derive(Clone, Copy, Debug, Default)]
pub struct Fairs {
    pub kalshi: f64,
    pub adjusted: Option<f64>,
    pub spot: Option<f64>,
    pub blend: Option<f64>,
    pub anchored: Option<f64>,
}

impl Fairs {
    /// The chosen source, Kalshi when it is not available.
    pub fn pick(&self, source: FairSource) -> f64 {
        match source {
            FairSource::Kalshi => self.kalshi,
            FairSource::Adjusted => self.adjusted.unwrap_or(self.kalshi),
            FairSource::Binance => self.spot.unwrap_or(self.kalshi),
            FairSource::Blend => self.blend.unwrap_or(self.kalshi),
            FairSource::Anchored => self.anchored.unwrap_or(self.kalshi),
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
    /// The last raw perp log mid, and when.
    perp_ln: f64,
    perp_ms: i64,
    /// `ln perp − ln spot` (EWMA) and when it was last sampled.
    basis: Option<(f64, i64)>,
}

#[derive(Default)]
pub struct Spot {
    assets: HashMap<String, AssetSpot>,
    /// The perpetual drives (spot sets the level); otherwise spot alone.
    pub perp_primary: bool,
}

impl Spot {
    /// A Binance `bookTicker` frame (bare or under the combined stream's
    /// `data`) from spot or, with `perp`, the USDⓈ-M perpetual; the asset
    /// whose mid it moved.
    pub fn frame(&mut self, text: &str, perp: bool, now: i64, pause_bps: f64, pause_ms: i64) -> Option<String> {
        let value: Value = serde_json::from_str(text).ok()?;
        let data = value.get("data").unwrap_or(&value);
        let symbol = data.get("s")?.as_str()?;
        let asset = symbol.strip_suffix("USDT")?.to_owned();
        let bid: f64 = data.get("b")?.as_str()?.parse().ok()?;
        let ask: f64 = data.get("a")?.as_str()?.parse().ok()?;
        if !(bid > 0.0 && ask >= bid) {
            return None;
        }
        let ln = ((bid + ask) / 2.0).ln();
        if !self.perp_primary {
            if perp {
                return None;
            }
            self.update(&asset, ln.exp(), now, pause_bps, pause_ms);
            return Some(asset);
        }
        let s = self.assets.entry(asset.clone()).or_default();
        if perp {
            s.perp_ln = ln;
            s.perp_ms = now;
            let (basis, _) = s.basis?;
            self.update(&asset, (ln - basis).exp(), now, pause_bps, pause_ms);
            return Some(asset);
        }
        if now - s.perp_ms > PERP_MAX_AGE_MS {
            self.update(&asset, ln.exp(), now, pause_bps, pause_ms);
            return Some(asset);
        }
        let x = s.perp_ln - ln;
        s.basis = Some(match s.basis {
            None => (x, now),
            Some((b, t)) => {
                let lambda = 0.5_f64.powf((now - t).max(0) as f64 / BASIS_HALF_LIFE_MS);
                (lambda * b + (1.0 - lambda) * x, now)
            }
        });
        None
    }

    /// The perp–spot basis in bp, once sampled.
    #[cfg(test)]
    pub fn basis_bps(&self, asset: &str) -> Option<f64> {
        self.assets.get(asset).and_then(|s| s.basis).map(|(b, _)| b * 1e4)
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
                    "driver": if self.perp_primary && now - s.perp_ms <= PERP_MAX_AGE_MS && s.basis.is_some() { "perp" } else { "spot" },
                    "basis_bps": s.basis.map(|(b, _)| b * 1e4),
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

/// P(Up) from Binance's own move since the window opened: `d` is
/// `ln(S_now / S_open)`, and `offset` the steady Binance-vs-strike log gap
/// (0 until learned). Near the close a few bp of level gap between Binance
/// USDT and Predict's Chainlink start price dominates the strike formula;
/// the anchored fair only moves when Binance itself moves.
pub fn anchored_fair(mid: f64, s_open: f64, offset: f64, sigma: f64, tau_s: f64) -> f64 {
    norm_cdf(((mid / s_open).ln() + offset) / (sigma * tau_s.max(1.0).sqrt()))
}

/// Logit-space blend of the Binance fair with Predict's own mid (which
/// carries what other traders know): `σ(w·logit(b) + (1−w)·logit(m))`.
pub fn blend_fair(binance: f64, predict_mid: f64, w: f64) -> f64 {
    let logit = |p: f64| {
        let p = p.clamp(0.02, 0.98);
        (p / (1.0 - p)).ln()
    };
    let z = w.clamp(0.0, 1.0) * logit(binance) + (1.0 - w.clamp(0.0, 1.0)) * logit(predict_mid);
    1.0 / (1.0 + (-z).exp())
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
#[derive(Clone, Copy, Debug, Default, PartialEq, Serialize, Deserialize)]
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

/// One scoring sample: the fairs and Predict's own YES mid (excluding our
/// orders). A blend or anchored fair that is unavailable at the moment is
/// NaN; the window still scores every other source.
#[derive(Clone, Copy, Debug)]
pub struct Sample {
    pub ms: i64,
    pub fairs: [f64; N_SOURCES],
}

pub const N_SOURCES: usize = SOURCES.len();
pub const SOURCES: [&str; 6] = ["kalshi", "adjusted", "binance", "blend", "binance_anchored", "predict_mid"];
pub const IDX_KALSHI: usize = 0;
pub const IDX_MID: usize = 5;

/// Opening bids are placed in the window's first 5 minutes; the scorecard
/// splits there so each source is judged where it is actually used.
pub const EARLY_BUCKET_MS: i64 = 300_000;
const WINDOW_MS: i64 = 900_000;

#[derive(Default)]
struct Pending {
    close_ms: i64,
    samples: Vec<Sample>,
    /// The earlier sample's ms with |fair − Predict's mid 5 s later|, per source.
    lead: Vec<(i64, [f64; N_SOURCES])>,
}

/// One time bucket's window-level evidence.
#[derive(Default, Serialize, Deserialize)]
pub struct BucketScores {
    pub windows: u64,
    #[serde(deserialize_with = "widen", default)]
    pub brier: [Stat; N_SOURCES],
    #[serde(deserialize_with = "widen", default)]
    pub lead: [Stat; N_SOURCES],
    /// Kalshi's score minus the source's (positive: the source beats Kalshi).
    #[serde(deserialize_with = "widen", default)]
    pub brier_gain: [Stat; N_SOURCES],
    #[serde(deserialize_with = "widen", default)]
    pub lead_gain: [Stat; N_SOURCES],
}

/// A saved stat array, widened to today's sources: a missing column (a saved
/// scorecard from before the blend and anchored fairs existed) is no data,
/// so the existing history stays.
fn widen<'de, D>(deserializer: D) -> Result<[Stat; N_SOURCES], D::Error>
where
    D: serde::Deserializer<'de>,
{
    let saved = Vec::<Stat>::deserialize(deserializer)?;
    let mut out = [Stat::default(); N_SOURCES];
    for (slot, stat) in out.iter_mut().zip(saved) {
        *slot = stat;
    }
    Ok(out)
}

/// Window-level evidence: each settled window contributes one value per
/// metric (its samples share one outcome, so they are not independent).
#[derive(Default, Serialize, Deserialize)]
pub struct FairScores {
    pub windows: u64,
    #[serde(deserialize_with = "widen")]
    pub brier: [Stat; N_SOURCES],
    #[serde(deserialize_with = "widen")]
    pub lead: [Stat; N_SOURCES],
    /// Kalshi's score minus the source's (positive: the source beats Kalshi).
    #[serde(deserialize_with = "widen")]
    pub brier_gain: [Stat; N_SOURCES],
    #[serde(deserialize_with = "widen")]
    pub lead_gain: [Stat; N_SOURCES],
    /// The window's first 5 minutes (when opening bids are placed) …
    #[serde(default)]
    pub early: BucketScores,
    /// … and the rest.
    #[serde(default)]
    pub late: BucketScores,
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
            let mid = sample.fairs[IDX_MID];
            p.lead.push((prev.ms, prev.fairs.map(|f| (f - mid).abs())));
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

    /// Score `market` at its outcome, overall and per time bucket. Each
    /// source averages over the samples where it has a value; gains stay
    /// paired with Kalshi on the same samples.
    pub fn resolve(&mut self, market: &str, yes_won: bool) {
        let Some(p) = self.pending.remove(market) else { return };
        if p.samples.is_empty() {
            return;
        }
        let outcome = if yes_won { 1.0 } else { 0.0 };
        let cut = p.close_ms - WINDOW_MS + EARLY_BUCKET_MS;
        let early_samples: Vec<Sample> = p.samples.iter().copied().filter(|s| s.ms < cut).collect();
        let late_samples: Vec<Sample> = p.samples.iter().copied().filter(|s| s.ms >= cut).collect();
        let early_lead: Vec<(i64, [f64; N_SOURCES])> =
            p.lead.iter().copied().filter(|(ms, _)| *ms < cut).collect();
        let late_lead: Vec<(i64, [f64; N_SOURCES])> =
            p.lead.iter().copied().filter(|(ms, _)| *ms >= cut).collect();
        Self::score_bucket(
            &mut self.brier,
            &mut self.lead,
            &mut self.brier_gain,
            &mut self.lead_gain,
            &p.samples,
            &p.lead,
            outcome,
        );
        self.windows += 1;
        if Self::score_bucket(
            &mut self.early.brier,
            &mut self.early.lead,
            &mut self.early.brier_gain,
            &mut self.early.lead_gain,
            &early_samples,
            &early_lead,
            outcome,
        ) {
            self.early.windows += 1;
        }
        if Self::score_bucket(
            &mut self.late.brier,
            &mut self.late.lead,
            &mut self.late.brier_gain,
            &mut self.late.lead_gain,
            &late_samples,
            &late_lead,
            outcome,
        ) {
            self.late.windows += 1;
        }
    }

    /// Push one window's means over `samples`/`leads`; true when anything scored.
    fn score_bucket(
        brier: &mut [Stat; N_SOURCES],
        lead: &mut [Stat; N_SOURCES],
        brier_gain: &mut [Stat; N_SOURCES],
        lead_gain: &mut [Stat; N_SOURCES],
        samples: &[Sample],
        leads: &[(i64, [f64; N_SOURCES])],
        outcome: f64,
    ) -> bool {
        if samples.is_empty() {
            return false;
        }
        let mut scored = false;
        for i in 0..N_SOURCES {
            let mut n = 0u32;
            let (mut sum, mut gain) = (0.0, 0.0);
            for s in samples {
                let (fair, kalshi) = (s.fairs[i], s.fairs[IDX_KALSHI]);
                if fair.is_finite() && kalshi.is_finite() {
                    n += 1;
                    sum += (fair - outcome).powi(2);
                    gain += (kalshi - outcome).powi(2) - (fair - outcome).powi(2);
                }
            }
            if n > 0 {
                scored = true;
                brier[i].push(sum / n as f64);
                brier_gain[i].push(gain / n as f64);
            }
        }
        for i in 0..N_SOURCES {
            let mut n = 0u32;
            let (mut sum, mut gain) = (0.0, 0.0);
            for (_, errs) in leads {
                let (err, base) = (errs[i], errs[IDX_KALSHI]);
                if err.is_finite() && base.is_finite() {
                    n += 1;
                    sum += err;
                    gain += base - err;
                }
            }
            if n > 0 {
                lead[i].push(sum / n as f64);
                lead_gain[i].push(gain / n as f64);
            }
        }
        scored
    }

    pub fn status(&self) -> Value {
        let per = |stats: &[Stat; N_SOURCES]| -> Value {
            Value::Object(SOURCES.iter().zip(stats).map(|(k, s)| ((*k).to_owned(), s.json())).collect())
        };
        let bucket = |windows: u64,
                      brier: &[Stat; N_SOURCES],
                      lead: &[Stat; N_SOURCES],
                      brier_gain: &[Stat; N_SOURCES],
                      lead_gain: &[Stat; N_SOURCES]| {
            json!({
                "windows": windows,
                "brier": per(brier),
                "lead_5s": per(lead),
                "brier_gain_vs_kalshi": per(brier_gain),
                "lead_gain_vs_kalshi": per(lead_gain),
            })
        };
        json!({
            "windows": self.windows,
            "pending": self.pending.len(),
            "brier": per(&self.brier),
            "lead_5s": per(&self.lead),
            "brier_gain_vs_kalshi": per(&self.brier_gain),
            "lead_gain_vs_kalshi": per(&self.lead_gain),
            "early": bucket(self.early.windows, &self.early.brier, &self.early.lead, &self.early.brier_gain, &self.early.lead_gain),
            "late": bucket(self.late.windows, &self.late.brier, &self.late.lead, &self.late.brier_gain, &self.late.lead_gain),
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
        assert_eq!(s.frame(&frame(100_000.0), false, 0, 5.0, 2_000).as_deref(), Some("BTC"));
        assert_eq!(s.guard("BTC", 100), (false, false));
        s.frame(&frame(100_060.0), false, 500, 5.0, 2_000); // +6 bp in 0.5 s
        assert_eq!(s.guard("BTC", 600), (false, true), "a rise holds the Down bid off, not the Up bid");
        assert_eq!(s.guard("BTC", 2_600), (false, false), "over after pause_ms");
        assert!(s.ln_and_move("BTC", 600).unwrap().1 > 5.0);
        assert!(s.sigma("BTC", 600).is_none(), "not warmed up");
        for i in 1..=MIN_VOL_SAMPLES as i64 {
            s.frame(&frame(100_060.0 + (i % 2) as f64 * 10.0), false, 500 + i * 1_000, 5.0, 2_000);
        }
        assert!(s.sigma("BTC", 500 + MIN_VOL_SAMPLES as i64 * 1_000).is_some());
        assert!(s.mid("BTC", 10_000_000).is_none(), "stale");
    }

    #[test]
    fn the_perp_drives_at_spot_level() {
        let mut s = Spot { perp_primary: true, ..Default::default() };
        let frame = |px: f64| format!(r#"{{"stream":"btcusdt@bookTicker","data":{{"s":"BTCUSDT","b":"{px}","a":"{px}"}}}}"#);
        assert_eq!(s.frame(&frame(100_050.0), true, 0, 5.0, 2_000), None, "no basis yet");
        assert_eq!(s.frame(&frame(100_000.0), false, 10, 5.0, 2_000), None, "spot only samples the basis");
        assert!((s.basis_bps("BTC").unwrap() - 5.0).abs() < 0.01, "perp 5 bp over spot");
        // The perp's move lands at spot's level.
        assert_eq!(s.frame(&frame(100_110.0), true, 20, 5.0, 2_000).as_deref(), Some("BTC"));
        assert!((s.mid("BTC", 20).unwrap() - 100_060.0).abs() < 0.1);
        assert_eq!(s.guard("BTC", 30), (false, false), "6 bp from the level's first print, inside one window");
        assert_eq!(s.frame(&frame(100_170.0), true, 40, 5.0, 2_000).as_deref(), Some("BTC"));
        assert_eq!(s.guard("BTC", 50), (false, true), "+6 bp on the perp is a rise");
        // A stale perp hands driving back to spot.
        assert_eq!(s.frame(&frame(100_200.0), false, 2_000, 5.0, 2_000).as_deref(), Some("BTC"));
        assert!((s.mid("BTC", 2_000).unwrap() - 100_200.0).abs() < 1e-6);
        // Off: perp frames are ignored.
        let mut off = Spot::default();
        assert_eq!(off.frame(&frame(100_000.0), true, 0, 5.0, 2_000), None);
    }

    #[test]
    fn scores_count_windows_and_gains() {
        let mut f = FairScores::default();
        // Adjusted is right (0.9) where Kalshi was not (0.5); YES wins.
        f.sample("7", 900_000, Sample { ms: 890_000, fairs: [0.5, 0.9, 0.7, 0.8, 0.75, 0.5] });
        f.sample("7", 900_000, Sample { ms: 895_000, fairs: [0.5, 0.9, 0.7, 0.8, 0.75, 0.8] });
        assert_eq!(f.due(900_000 + 60_000, 60_000), vec![("7".to_string(), 900_000)]);
        f.resolve("7", true);
        assert_eq!(f.windows, 1);
        assert!(f.brier_gain[1].mean > 0.0 && f.lead_gain[1].mean > 0.0);
        assert!(f.due(1 << 40, 0).is_empty());
    }

    #[test]
    fn blend_sits_between_its_inputs_and_clamps() {
        let mid = blend_fair(0.7, 0.5, 0.5);
        assert!(mid > 0.5 && mid < 0.7, "{mid}");
        assert!((blend_fair(0.7, 0.5, 1.0) - 0.7).abs() < 1e-9, "all Binance");
        assert!((blend_fair(0.7, 0.5, 0.0) - 0.5).abs() < 1e-9, "all Predict mid");
        assert!(blend_fair(0.0, 1.0, 0.5).is_finite(), "extremes clamp, never NaN");
        assert_eq!(FairSource::parse("blend").unwrap(), FairSource::Blend);
        assert_eq!(FairSource::parse("binance_anchored").unwrap(), FairSource::Anchored);
    }

    #[test]
    fn anchored_moves_with_binance_not_the_level_gap() {
        let (sigma, tau) = (1.5e-4, 600.0);
        // +20 bp from the open prices exactly like the strike formula at the strike.
        let moved = anchored_fair(100.2, 100.0, 0.0, sigma, tau);
        assert!((moved - spot_fair(100.2, 100.0, sigma, tau)).abs() < 1e-9, "{moved}");
        // A 6 bp Binance-vs-strike gap no longer moves a late-window fair.
        let late = anchored_fair(100.06, 100.06, 0.0, sigma, 10.0);
        assert!((late - 0.5).abs() < 1e-9, "{late}");
        assert!(spot_fair(100.06, 100.0, sigma, 10.0) > 0.85, "the strike formula blows up");
    }

    #[test]
    fn old_four_wide_scorecards_load_with_empty_new_columns() {
        let dir = std::env::temp_dir().join(format!("pk-predict-scores-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("fair_scores.json");
        let stat = serde_json::json!({"n": 287u64, "mean": 0.174, "m2": 1.0});
        let four = vec![stat.clone(), stat.clone(), stat.clone(), stat.clone()];
        std::fs::write(
            &path,
            serde_json::to_vec(&serde_json::json!({
                "windows": 287u64, "brier": four, "lead": four, "brier_gain": four, "lead_gain": four,
            }))
            .unwrap(),
        )
        .unwrap();
        let loaded = FairScores::load(&path);
        assert_eq!(loaded.windows, 287);
        assert_eq!(loaded.brier[IDX_MID], Stat { n: 0, mean: 0.0, m2: 0.0 });
        assert_eq!(loaded.brier[0].n, 287);
        assert!((loaded.brier[0].mean - 0.174).abs() < 1e-12);
        assert_eq!(loaded.early.windows, 0, "buckets start empty");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn buckets_split_early_and_late_and_skip_nan_sources() {
        let mut f = FairScores::default();
        // Window closes at t=900 s; samples at 60 s (early) and 600 s (late).
        // The anchored fair is missing early (NaN); YES wins.
        f.sample("7", 900_000, Sample { ms: 60_000, fairs: [0.5, 0.6, 0.6, 0.6, f64::NAN, 0.5] });
        f.sample("7", 900_000, Sample { ms: 65_000, fairs: [0.5, 0.6, 0.6, 0.6, f64::NAN, 0.6] });
        f.sample("7", 900_000, Sample { ms: 600_000, fairs: [0.5, 0.6, 0.6, 0.6, 0.6, 0.5] });
        f.sample("7", 900_000, Sample { ms: 605_000, fairs: [0.5, 0.6, 0.6, 0.6, 0.6, 0.6] });
        f.resolve("7", true);
        assert_eq!((f.windows, f.early.windows, f.late.windows), (1, 1, 1));
        assert_eq!(f.early.brier[4].n, 0, "anchored had no early value");
        assert_eq!(f.late.brier[4].n, 1);
        assert_eq!(f.early.brier[1].n, 1, "other sources still score early");
        assert!(f.early.brier_gain[1].mean > 0.0);
    }

    #[test]
    fn tickers_name_their_asset() {
        assert_eq!(asset_of_ticker("KXBTC15M-26OCT262115-15"), Some("BTC"));
        assert_eq!(asset_of_ticker("KXBNB15M-X"), Some("BNB"));
        assert_eq!(asset_of_ticker("OTHER"), None);
    }
}
