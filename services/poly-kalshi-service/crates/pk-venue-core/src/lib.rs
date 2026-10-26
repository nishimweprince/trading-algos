//! Primitives shared only by the isolated external-venue processes.
//!
//! `pk-trader`, `pk-engine` and `pk-core` deliberately do not depend on this
//! crate. Keeping the dependency one-way is the mechanical guarantee that a
//! new venue cannot add work to the existing decision path.

use std::collections::{HashMap, VecDeque};
use std::path::{Path, PathBuf};

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

pub mod service;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum VenueMode {
    #[default]
    Shadow,
    Live,
}

impl VenueMode {
    pub fn parse(value: &str) -> anyhow::Result<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "shadow" | "paper" => Ok(Self::Shadow),
            "live" => Ok(Self::Live),
            other => anyhow::bail!("mode {other:?} is not shadow or live"),
        }
    }

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Shadow => "shadow",
            Self::Live => "live",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Lane {
    Maker,
    Taker,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub enum MatchGrade {
    /// Settlement-equivalent.
    E0,
    /// Explicit, bounded settlement-basis difference.
    E1,
    /// Informational similarity only; never executable.
    E2,
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct SideMapping {
    pub kalshi_yes_is_venue_side: u8,
    pub venue_side_names: [String; 2],
}

/// A canonical description of the rules which decide a contract.
///
/// Numeric values which participate in settlement are strings. This preserves
/// the venue's exact decimal representation and avoids an f64 round-trip from
/// changing a fingerprint.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct NormalizedContract {
    pub venue: String,
    pub venue_market_id: String,
    pub kalshi_ticker: String,
    pub event_class: String,
    pub underlying: String,
    pub outcome: String,
    pub operator: String,
    pub threshold: Option<String>,
    pub open_ms: Option<i64>,
    pub close_ms: Option<i64>,
    pub decision_ms: Option<i64>,
    pub resolution_source: String,
    pub averaging_window_s: Option<u64>,
    pub tie_rule: String,
    pub correction_rule: String,
}

impl NormalizedContract {
    pub fn fingerprint(&self) -> String {
        let bytes = serde_json::to_vec(self).expect("NormalizedContract is serializable");
        let digest = Sha256::digest(bytes);
        digest.iter().map(|b| format!("{b:02x}")).collect()
    }

    pub fn exact_rules(&self, other: &Self) -> bool {
        self.event_class == other.event_class
            && self.underlying == other.underlying
            && self.outcome == other.outcome
            && self.operator == other.operator
            && self.threshold == other.threshold
            && self.open_ms == other.open_ms
            && self.close_ms == other.close_ms
            && self.decision_ms == other.decision_ms
            && self.resolution_source == other.resolution_source
            && self.averaging_window_s == other.averaging_window_s
            && self.tie_rule == other.tie_rule
            && self.correction_rule == other.correction_rule
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct MatchDecision {
    pub grade: MatchGrade,
    pub kalshi_ticker: String,
    pub venue_market_id: String,
    pub kalshi_fingerprint: String,
    pub venue_fingerprint: String,
    pub side_mapping: SideMapping,
    pub basis_reason: Option<String>,
    pub max_basis: f64,
    pub maker: bool,
    pub taker: bool,
    pub rejected: Vec<String>,
}

impl MatchDecision {
    pub fn executable(&self, lane: Lane) -> bool {
        self.grade != MatchGrade::E2
            && self.rejected.is_empty()
            && match lane {
                Lane::Maker => self.maker,
                Lane::Taker => self.taker,
            }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AllowlistEntry {
    pub venue: String,
    pub venue_market_id: String,
    pub kalshi_ticker: String,
    pub venue_fingerprint: String,
    pub kalshi_fingerprint: String,
    pub side_mapping: SideMapping,
    pub max_basis: f64,
    pub expires_at: Option<DateTime<Utc>>,
    #[serde(default = "yes")]
    pub maker: bool,
    #[serde(default)]
    pub taker: bool,
    pub reason: String,
}

const fn yes() -> bool {
    true
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Allowlist {
    #[serde(default)]
    pub version: u32,
    #[serde(default)]
    pub entries: Vec<AllowlistEntry>,
}

impl Allowlist {
    pub fn load(path: &Path) -> anyhow::Result<Self> {
        if !path.exists() {
            return Ok(Self::default());
        }
        let parsed: Self = serde_json::from_slice(&std::fs::read(path)?)?;
        anyhow::ensure!(parsed.version == 1, "{}: allowlist version must be 1", path.display());
        Ok(parsed)
    }

    pub fn decide(
        &self,
        venue: &NormalizedContract,
        kalshi: &NormalizedContract,
        now: DateTime<Utc>,
    ) -> MatchDecision {
        let venue_fp = venue.fingerprint();
        let kalshi_fp = kalshi.fingerprint();
        if venue.exact_rules(kalshi) {
            return MatchDecision {
                grade: MatchGrade::E0,
                kalshi_ticker: kalshi.kalshi_ticker.clone(),
                venue_market_id: venue.venue_market_id.clone(),
                kalshi_fingerprint: kalshi_fp,
                venue_fingerprint: venue_fp,
                side_mapping: SideMapping {
                    kalshi_yes_is_venue_side: 0,
                    venue_side_names: ["Yes".into(), "No".into()],
                },
                basis_reason: None,
                max_basis: 0.0,
                maker: true,
                taker: true,
                rejected: Vec::new(),
            };
        }
        let found = self.entries.iter().find(|e| {
            e.venue == venue.venue
                && e.venue_market_id == venue.venue_market_id
                && e.kalshi_ticker == kalshi.kalshi_ticker
        });
        let Some(entry) = found else {
            return rejected(venue, kalshi, venue_fp, kalshi_fp, "no exact rules or allowlist entry");
        };
        let mut reasons = Vec::new();
        if entry.venue_fingerprint != venue_fp {
            reasons.push("venue rule fingerprint changed".into());
        }
        if entry.kalshi_fingerprint != kalshi_fp {
            reasons.push("Kalshi rule fingerprint changed".into());
        }
        if entry.expires_at.is_some_and(|at| at <= now) {
            reasons.push("allowlist entry expired".into());
        }
        MatchDecision {
            grade: if reasons.is_empty() { MatchGrade::E1 } else { MatchGrade::E2 },
            kalshi_ticker: kalshi.kalshi_ticker.clone(),
            venue_market_id: venue.venue_market_id.clone(),
            kalshi_fingerprint: kalshi_fp,
            venue_fingerprint: venue_fp,
            side_mapping: entry.side_mapping.clone(),
            basis_reason: Some(entry.reason.clone()),
            max_basis: entry.max_basis,
            maker: entry.maker && reasons.is_empty(),
            taker: entry.taker && reasons.is_empty(),
            rejected: reasons,
        }
    }
}

fn rejected(
    venue: &NormalizedContract,
    kalshi: &NormalizedContract,
    venue_fp: String,
    kalshi_fp: String,
    why: &str,
) -> MatchDecision {
    MatchDecision {
        grade: MatchGrade::E2,
        kalshi_ticker: kalshi.kalshi_ticker.clone(),
        venue_market_id: venue.venue_market_id.clone(),
        kalshi_fingerprint: kalshi_fp,
        venue_fingerprint: venue_fp,
        side_mapping: SideMapping::default(),
        basis_reason: None,
        max_basis: 0.0,
        maker: false,
        taker: false,
        rejected: vec![why.into()],
    }
}

#[derive(Clone, Copy, Debug, Default, Serialize, Deserialize)]
pub struct Book {
    pub bid: Option<f64>,
    pub bid_size: f64,
    pub ask: Option<f64>,
    pub ask_size: f64,
    pub update_ms: i64,
}

impl Book {
    pub fn mid(self) -> Option<f64> {
        Some((self.bid? + self.ask?) / 2.0)
    }

    pub fn valid(self) -> bool {
        self.bid.is_none_or(|p| (0.0..=1.0).contains(&p))
            && self.ask.is_none_or(|p| (0.0..=1.0).contains(&p))
            && self.bid.zip(self.ask).is_none_or(|(b, a)| b <= a)
    }

    pub fn age_ms(self, now_ms: i64) -> i64 {
        now_ms.saturating_sub(self.update_ms)
    }
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
pub struct RiskLimits {
    pub order_usd: f64,
    pub max_open_usd: f64,
    pub daily_loss_usd: f64,
    pub max_markets: usize,
    pub max_orders: usize,
    pub max_rejections: u64,
}

impl RiskLimits {
    pub fn validate(self) -> anyhow::Result<Self> {
        anyhow::ensure!(self.order_usd > 0.0, "order USD cap must be positive");
        anyhow::ensure!(self.max_open_usd >= self.order_usd, "open USD cap must cover one order");
        anyhow::ensure!(self.daily_loss_usd > 0.0, "daily loss cap must be positive");
        anyhow::ensure!(self.max_markets > 0 && self.max_orders > 0, "market/order caps must be positive");
        Ok(self)
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct RiskState {
    pub utc_day: String,
    pub realised_pnl: f64,
    pub marked_pnl: f64,
    pub open_usd: f64,
    pub active_markets: usize,
    pub open_orders: usize,
    pub consecutive_rejections: u64,
    pub halted: bool,
    pub halt_reason: Option<String>,
}

impl Default for RiskState {
    fn default() -> Self {
        Self {
            utc_day: Utc::now().format("%Y-%m-%d").to_string(),
            realised_pnl: 0.0,
            marked_pnl: 0.0,
            open_usd: 0.0,
            active_markets: 0,
            open_orders: 0,
            consecutive_rejections: 0,
            halted: false,
            halt_reason: None,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct RiskGuard {
    pub limits: RiskLimits,
    pub state: RiskState,
    pub state_path: PathBuf,
}

impl RiskGuard {
    pub fn open(limits: RiskLimits, state_path: PathBuf) -> anyhow::Result<Self> {
        let limits = limits.validate()?;
        let state = if state_path.is_file() {
            serde_json::from_slice(&std::fs::read(&state_path)?)?
        } else {
            RiskState::default()
        };
        Ok(Self { limits, state, state_path })
    }

    pub fn reset_day(&mut self, now: DateTime<Utc>) {
        let day = now.format("%Y-%m-%d").to_string();
        if self.state.utc_day != day {
            self.state.utc_day = day;
            self.state.realised_pnl = 0.0;
            self.state.marked_pnl = 0.0;
            self.state.consecutive_rejections = 0;
        }
    }

    pub fn check_order(&mut self, cost: f64, new_market: bool) -> Result<(), String> {
        self.check_latches()?;
        if !cost.is_finite() || cost <= 0.0 || cost > self.limits.order_usd + 1e-9 {
            return Err("order cost exceeds cap".into());
        }
        if self.state.open_usd + cost > self.limits.max_open_usd + 1e-9 {
            return Err("aggregate open notional exceeds cap".into());
        }
        if new_market && self.state.active_markets >= self.limits.max_markets {
            return Err("maximum active markets reached".into());
        }
        if self.state.open_orders >= self.limits.max_orders {
            return Err("maximum open orders reached".into());
        }
        Ok(())
    }

    pub fn check_latches(&mut self) -> Result<(), String> {
        if self.state.realised_pnl + self.state.marked_pnl <= -self.limits.daily_loss_usd {
            self.halt("daily loss limit reached");
        }
        if self.state.consecutive_rejections >= self.limits.max_rejections {
            self.halt("consecutive rejection limit reached");
        }
        if self.state.halted {
            Err(self.state.halt_reason.clone().unwrap_or_else(|| "risk halted".into()))
        } else {
            Ok(())
        }
    }

    pub fn halt(&mut self, why: impl Into<String>) {
        self.state.halted = true;
        self.state.halt_reason = Some(why.into());
        self.save();
    }

    pub fn acknowledge(&mut self) {
        self.state.halted = false;
        self.state.halt_reason = None;
        self.state.consecutive_rejections = 0;
        self.save();
    }

    pub fn save(&self) {
        if let Err(error) =
            atomic_write(&self.state_path, &serde_json::to_vec_pretty(&self.state).unwrap_or_default())
        {
            tracing_fallback("risk state save failed", &error);
        }
    }

    pub fn status(&self) -> Value {
        json!({"limits": self.limits, "state": self.state})
    }
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Series {
    points: HashMap<String, VecDeque<Value>>,
    max_points: usize,
}

impl Series {
    pub fn new(max_points: usize) -> Self {
        Self { points: HashMap::new(), max_points: max_points.max(1) }
    }

    pub fn push(&mut self, market: &str, point: Value) {
        let points = self.points.entry(market.to_owned()).or_default();
        while points.len() >= self.max_points {
            points.pop_front();
        }
        points.push_back(point);
    }

    pub fn since(&self, since: i64) -> Value {
        Value::Object(
            self.points
                .iter()
                .map(|(market, points)| {
                    let filtered = points
                        .iter()
                        .filter(|p| p.get(0).and_then(Value::as_i64).is_some_and(|t| t > since))
                        .cloned()
                        .collect();
                    (market.clone(), Value::Array(filtered))
                })
                .collect(),
        )
    }
}

pub fn atomic_write(path: &Path, bytes: &[u8]) -> anyhow::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension(format!("tmp-{}", std::process::id()));
    std::fs::write(&tmp, bytes)?;
    std::fs::rename(tmp, path)?;
    Ok(())
}

fn tracing_fallback(message: &str, error: &dyn std::fmt::Display) {
    eprintln!("{message}: {error}");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn contract(source: &str) -> NormalizedContract {
        NormalizedContract {
            venue: "hip4".into(),
            venue_market_id: "17".into(),
            kalshi_ticker: "KXBTC-17".into(),
            event_class: "binary_price".into(),
            underlying: "BTC".into(),
            outcome: "above".into(),
            operator: ">".into(),
            threshold: Some("80000".into()),
            decision_ms: Some(1_800_000_000_000),
            resolution_source: source.into(),
            averaging_window_s: Some(90),
            tie_rule: "no".into(),
            ..Default::default()
        }
    }

    #[test]
    fn exact_and_allowlisted_matches_fail_closed() {
        let a = contract("pyth");
        let mut b = a.clone();
        b.venue = "kalshi".into();
        assert_eq!(Allowlist::default().decide(&a, &b, Utc::now()).grade, MatchGrade::E0);

        b.resolution_source = "brti".into();
        let entry = AllowlistEntry {
            venue: a.venue.clone(),
            venue_market_id: a.venue_market_id.clone(),
            kalshi_ticker: b.kalshi_ticker.clone(),
            venue_fingerprint: a.fingerprint(),
            kalshi_fingerprint: b.fingerprint(),
            side_mapping: SideMapping {
                kalshi_yes_is_venue_side: 0,
                venue_side_names: ["Yes".into(), "No".into()],
            },
            max_basis: 0.02,
            expires_at: None,
            maker: true,
            taker: false,
            reason: "Pyth versus BRTI".into(),
        };
        let allow = Allowlist { version: 1, entries: vec![entry] };
        let decision = allow.decide(&a, &b, Utc::now());
        assert_eq!(decision.grade, MatchGrade::E1);
        assert!(decision.executable(Lane::Maker));
        assert!(!decision.executable(Lane::Taker));

        let mut changed = a.clone();
        changed.averaging_window_s = Some(60);
        assert_eq!(allow.decide(&changed, &b, Utc::now()).grade, MatchGrade::E2);
    }

    fn test_limits() -> RiskLimits {
        RiskLimits {
            order_usd: 10.0,
            max_open_usd: 20.0,
            daily_loss_usd: 5.0,
            max_markets: 2,
            max_orders: 4,
            max_rejections: 3,
        }
    }

    #[test]
    fn risk_halts_at_loss_and_caps_orders() {
        let dir = std::env::temp_dir().join(format!("pk-venue-risk-basic-{}", std::process::id()));
        let mut r = RiskGuard::open(test_limits(), dir.join("risk.json")).unwrap();
        assert!(r.check_order(11.0, false).is_err());
        r.state.realised_pnl = -5.0;
        assert!(r.check_latches().is_err());
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn invalid_limits_are_refused_at_open() {
        let dir = std::env::temp_dir().join(format!("pk-venue-risk-invalid-{}", std::process::id()));
        let bad = RiskLimits { order_usd: 0.0, ..test_limits() };
        assert!(RiskGuard::open(bad, dir.join("risk.json")).is_err());
        let bad = RiskLimits { max_open_usd: 1.0, ..test_limits() };
        assert!(RiskGuard::open(bad, dir.join("risk.json")).is_err());
    }

    #[test]
    fn halt_persists_across_reopen_and_acknowledge_clears() {
        let dir = std::env::temp_dir().join(format!("pk-venue-risk-halt-{}", std::process::id()));
        let path = dir.join("halt.json");
        let _ = std::fs::remove_dir_all(&dir);
        let mut first = RiskGuard::open(test_limits(), path.clone()).unwrap();
        first.state.realised_pnl = -3.0;
        first.state.marked_pnl = -2.5;
        assert!(first.check_latches().is_err(), "realised plus marked trips the latch");
        let mut second = RiskGuard::open(test_limits(), path.clone()).unwrap();
        assert!(second.check_latches().is_err(), "halt survives a restart");
        assert!(second.check_order(1.0, false).is_err(), "halted guard rejects orders");
        second.acknowledge();
        second.state.realised_pnl = 0.0;
        second.state.marked_pnl = 0.0;
        assert!(second.check_latches().is_ok());
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn rejections_latch_and_day_rollover_resets_pnl() {
        let dir = std::env::temp_dir().join(format!("pk-venue-risk-reject-{}", std::process::id()));
        let path = dir.join("rej.json");
        let _ = std::fs::remove_dir_all(&dir);
        let mut r = RiskGuard::open(test_limits(), path).unwrap();
        r.state.consecutive_rejections = 3;
        assert!(r.check_latches().is_err());
        r.state.utc_day = "2000-01-01".into();
        r.reset_day(Utc::now());
        assert_eq!(r.state.consecutive_rejections, 0);
        assert_eq!(r.state.realised_pnl, 0.0);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn book_rejects_crossed_or_out_of_range() {
        assert!(Book { bid: Some(0.4), ask: Some(0.5), ..Default::default() }.valid());
        assert!(!Book { bid: Some(0.6), ask: Some(0.5), ..Default::default() }.valid());
        assert!(!Book { bid: Some(-0.1), ask: None, ..Default::default() }.valid());
    }

    #[test]
    #[allow(clippy::type_complexity)]
    fn any_rule_difference_breaks_e0_without_an_entry() {
        let a = contract("pyth");
        let variants: Vec<(&str, fn(&mut NormalizedContract))> = vec![
            ("oracle", |b| b.resolution_source = "brti".into()),
            ("window", |b| b.averaging_window_s = Some(60)),
            ("deadline", |b| b.decision_ms = Some(1_800_000_000_001)),
            ("threshold", |b| b.threshold = Some("80001".into())),
            ("operator", |b| b.operator = ">=".into()),
            ("tie", |b| b.tie_rule = "yes".into()),
            ("correction", |b| b.correction_rule = "other".into()),
            ("outcome", |b| b.outcome = "below".into()),
        ];
        for (name, mutate) in variants {
            let mut b = a.clone();
            b.venue = "kalshi".into();
            mutate(&mut b);
            let d = Allowlist::default().decide(&a, &b, Utc::now());
            assert_eq!(d.grade, MatchGrade::E2, "{name}");
            assert!(!d.executable(Lane::Maker), "{name}");
            assert!(!d.executable(Lane::Taker), "{name}");
        }
    }

    #[test]
    fn similar_tickers_alone_never_execute() {
        let mut a = contract("pyth");
        a.kalshi_ticker = "KXBTC-17-MORNING".into();
        let mut b = a.clone();
        b.venue = "kalshi".into();
        b.kalshi_ticker = "KXBTC-17-MORNING".into();
        b.resolution_source = "something-else".into();
        let d = Allowlist::default().decide(&a, &b, Utc::now());
        assert_eq!(d.grade, MatchGrade::E2);
        assert!(!d.rejected.is_empty());
    }

    #[test]
    fn allowlist_fails_closed_on_expiry_and_either_fingerprint() {
        let a = contract("pyth");
        let mut b = a.clone();
        b.venue = "kalshi".into();
        b.resolution_source = "brti".into();
        let entry = AllowlistEntry {
            venue: a.venue.clone(),
            venue_market_id: a.venue_market_id.clone(),
            kalshi_ticker: b.kalshi_ticker.clone(),
            venue_fingerprint: a.fingerprint(),
            kalshi_fingerprint: b.fingerprint(),
            side_mapping: SideMapping {
                kalshi_yes_is_venue_side: 0,
                venue_side_names: ["Yes".into(), "No".into()],
            },
            max_basis: 0.02,
            expires_at: None,
            maker: true,
            taker: true,
            reason: "test basis".into(),
        };
        let allow = Allowlist { version: 1, entries: vec![entry.clone()] };
        assert!(allow.decide(&a, &b, Utc::now()).executable(Lane::Taker));

        let mut expired = entry.clone();
        expired.expires_at = Some(Utc::now() - chrono::Duration::seconds(1));
        let allow = Allowlist { version: 1, entries: vec![expired] };
        let d = allow.decide(&a, &b, Utc::now());
        assert_eq!(d.grade, MatchGrade::E2);
        assert!(d.rejected.iter().any(|r| r.contains("expired")));

        let mut changed = a.clone();
        changed.threshold = Some("80001".into());
        let d = Allowlist { version: 1, entries: vec![entry.clone()] }.decide(&changed, &b, Utc::now());
        assert_eq!(d.grade, MatchGrade::E2);

        let mut changed_kalshi = b.clone();
        changed_kalshi.tie_rule = "yes".into();
        let d = Allowlist { version: 1, entries: vec![entry] }.decide(&a, &changed_kalshi, Utc::now());
        assert_eq!(d.grade, MatchGrade::E2);
    }

    #[test]
    fn allowlist_load_rejects_unknown_versions_and_missing_files_are_empty() {
        let dir = std::env::temp_dir().join(format!("pk-venue-allow-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let missing = dir.join("absent.json");
        assert!(Allowlist::load(&missing).unwrap().entries.is_empty());
        std::fs::create_dir_all(&dir).unwrap();
        let bad = dir.join("bad.json");
        std::fs::write(&bad, r#"{"version":2,"entries":[]}"#).unwrap();
        assert!(Allowlist::load(&bad).is_err());
        let _ = std::fs::remove_dir_all(dir);
    }
}
