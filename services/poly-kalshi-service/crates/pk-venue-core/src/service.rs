//! Process-local state, recording, and fail-closed controls for venue adapters.

use std::collections::{HashMap, VecDeque};
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::{Book, MatchDecision, RiskGuard, Series, VenueMode};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct OrderRecord {
    pub id: String,
    pub client_id: String,
    pub market: String,
    pub lane: String,
    pub side: String,
    pub price: f64,
    pub quantity: f64,
    pub status: String,
    pub created_ms: i64,
    pub updated_ms: i64,
    #[serde(default)]
    pub simulated: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct FillRecord {
    pub id: String,
    pub order_id: String,
    pub market: String,
    pub side: String,
    pub price: f64,
    pub quantity: f64,
    pub fee: f64,
    pub time_ms: i64,
    #[serde(default)]
    pub simulated: bool,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct FeedHealth {
    pub connected: bool,
    pub last_message_ms: i64,
    pub reconnects: u64,
    pub errors: u64,
    pub sequence_gaps: u64,
    pub last_error: Option<String>,
}

#[derive(Debug)]
pub struct AdapterState {
    pub venue: String,
    pub mode: VenueMode,
    pub started_ms: i64,
    pub config_fingerprint: String,
    pub ready: bool,
    pub reconciling: bool,
    pub degraded: Vec<String>,
    pub latched: Vec<String>,
    pub clock_offset_ms: Option<i64>,
    pub feeds: HashMap<String, FeedHealth>,
    pub books: HashMap<String, Book>,
    pub kalshi_fairs: HashMap<String, f64>,
    pub matches: HashMap<String, MatchDecision>,
    pub orders: VecDeque<OrderRecord>,
    pub fills: VecDeque<FillRecord>,
    pub inventory: HashMap<String, f64>,
    pub settlements: VecDeque<Value>,
    pub markouts: VecDeque<Value>,
    pub actions: VecDeque<Value>,
    pub series: Series,
    pub risk: RiskGuard,
    pub onchain_cancel_pending: bool,
    pub deadman_armed: bool,
    pub last_heartbeat_ms: i64,
}

impl AdapterState {
    pub fn new(venue: &str, mode: VenueMode, config_fingerprint: String, risk: RiskGuard) -> Self {
        let now = now_ms();
        Self {
            venue: venue.into(),
            mode,
            started_ms: now,
            config_fingerprint,
            ready: false,
            reconciling: true,
            degraded: Vec::new(),
            latched: Vec::new(),
            clock_offset_ms: None,
            feeds: HashMap::new(),
            books: HashMap::new(),
            kalshi_fairs: HashMap::new(),
            matches: HashMap::new(),
            orders: VecDeque::new(),
            fills: VecDeque::new(),
            inventory: HashMap::new(),
            settlements: VecDeque::new(),
            markouts: VecDeque::new(),
            actions: VecDeque::new(),
            series: Series::new(3600),
            risk,
            onchain_cancel_pending: false,
            deadman_armed: false,
            last_heartbeat_ms: now,
        }
    }

    pub fn latch(&mut self, reason: impl Into<String>) {
        let reason = reason.into();
        if !self.latched.contains(&reason) {
            self.latched.push(reason.clone());
        }
        self.ready = false;
        self.risk.halt(reason);
    }

    pub fn acknowledge(&mut self) {
        self.latched.clear();
        self.risk.acknowledge();
        self.ready = !self.reconciling && self.degraded.is_empty();
    }

    pub fn push_action(&mut self, value: Value) {
        bounded_push(&mut self.actions, value, 200);
    }

    pub fn push_order(&mut self, value: OrderRecord) {
        bounded_push(&mut self.orders, value, 500);
    }

    pub fn push_fill(&mut self, value: FillRecord) {
        bounded_push(&mut self.fills, value, 500);
    }

    pub fn push_markout(&mut self, value: Value) {
        bounded_push(&mut self.markouts, value, 500);
    }

    pub fn status(&self) -> Value {
        let now = now_ms();
        json!({
            "venue": self.venue,
            "mode": self.mode,
            "ready": self.ready && self.latched.is_empty() && !self.reconciling,
            "reconciling": self.reconciling,
            "uptime_s": (now - self.started_ms).max(0) / 1000,
            "build": option_env!("GIT_COMMIT").unwrap_or("dev"),
            "config": self.config_fingerprint,
            "clock_offset_ms": self.clock_offset_ms,
            "feeds": self.feeds,
            "books": self.books,
            "kalshi_fairs": self.kalshi_fairs,
            "matches": self.matches,
            "inventory": self.inventory,
            "risk": self.risk.status(),
            "latched": self.latched,
            "degraded": self.degraded,
            "onchain_cancel_pending": self.onchain_cancel_pending,
            "deadman_armed": self.deadman_armed,
            "last_heartbeat_ms": self.last_heartbeat_ms,
            "recent_actions": self.actions,
            "recent_orders": self.orders,
            "recent_fills": self.fills,
            "settlements": self.settlements,
            "markouts": self.markouts,
        })
    }
}

fn bounded_push<T>(values: &mut VecDeque<T>, value: T, max: usize) {
    while values.len() >= max {
        values.pop_front();
    }
    values.push_back(value);
}

pub struct Journal {
    root: PathBuf,
    current_day: String,
    file: File,
}

impl Journal {
    pub fn open(root: impl Into<PathBuf>) -> anyhow::Result<Self> {
        let root = root.into();
        std::fs::create_dir_all(&root)?;
        let current_day = Utc::now().format("%Y-%m-%d").to_string();
        let file = open_day(&root, &current_day)?;
        Ok(Self { root, current_day, file })
    }

    pub fn record(&mut self, kind: &str, mut value: Value) -> anyhow::Result<()> {
        let day = Utc::now().format("%Y-%m-%d").to_string();
        if day != self.current_day {
            self.file = open_day(&self.root, &day)?;
            self.current_day = day;
        }
        let object =
            value.as_object_mut().ok_or_else(|| anyhow::anyhow!("journal value must be an object"))?;
        object.insert("type".into(), kind.into());
        object.insert("t_ms".into(), now_ms().into());
        redact_secrets(&mut value);
        serde_json::to_writer(&mut self.file, &value)?;
        self.file.write_all(b"\n")?;
        self.file.flush()?;
        Ok(())
    }
}

fn redact_secrets(value: &mut Value) {
    match value {
        Value::Object(object) => {
            for (key, value) in object {
                let key = key.to_ascii_lowercase().replace(['-', '_'], "");
                if matches!(
                    key.as_str(),
                    "authorization"
                        | "xapikey"
                        | "apikey"
                        | "jwt"
                        | "accesstoken"
                        | "idtoken"
                        | "privatekey"
                        | "signature"
                        | "signedpayload"
                ) {
                    *value = Value::String("[redacted]".into());
                } else {
                    redact_secrets(value);
                }
            }
        }
        Value::Array(values) => values.iter_mut().for_each(redact_secrets),
        _ => {}
    }
}

fn open_day(root: &Path, day: &str) -> anyhow::Result<File> {
    Ok(OpenOptions::new().create(true).append(true).open(root.join(format!("{day}.jsonl")))?)
}

pub fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

/// Post-fill markouts at 5, 30 and 60 seconds against the venue mid.
/// Callers pass the mid in the fill's own side terms (both lanes only buy),
/// so the markout is always `mid(+t) - fill_px`.
pub struct MarkoutTracker {
    pending: VecDeque<MarkoutPending>,
}

struct MarkoutPending {
    market: String,
    side: String,
    fill_px: f64,
    fill_ms: i64,
    done: [bool; 3],
}

impl MarkoutTracker {
    pub fn new() -> Self {
        Self { pending: VecDeque::new() }
    }

    pub fn watch(&mut self, market: &str, side: &str, fill_px: f64, fill_ms: i64) {
        if self.pending.len() >= 200 {
            self.pending.pop_front();
        }
        self.pending.push_back(MarkoutPending {
            market: market.into(),
            side: side.into(),
            fill_px,
            fill_ms,
            done: [false; 3],
        });
    }

    /// Returns finished markout records; drops entries older than 5 minutes.
    /// `mids` is keyed by `(market, side)` so YES and NO fills on one market
    /// each compare against their own side's mid.
    pub fn poll(&mut self, now_ms: i64, mids: &HashMap<(String, String), f64>) -> Vec<Value> {
        const HORIZONS: [i64; 3] = [5_000, 30_000, 60_000];
        let mut out = Vec::new();
        self.pending.retain(|p| now_ms - p.fill_ms < 300_000);
        for pending in self.pending.iter_mut() {
            let key = (pending.market.clone(), pending.side.clone());
            let Some(mid) = mids.get(&key).copied() else { continue };
            for (i, horizon) in HORIZONS.iter().enumerate() {
                if !pending.done[i] && now_ms - pending.fill_ms >= *horizon {
                    pending.done[i] = true;
                    out.push(json!({
                        "market": pending.market,
                        "side": pending.side,
                        "fill_px": pending.fill_px,
                        "horizon_s": horizon / 1000,
                        "markout": mid - pending.fill_px,
                    }));
                }
            }
        }
        self.pending.retain(|p| p.done.iter().any(|d| !d));
        out
    }
}

impl Default for MarkoutTracker {
    fn default() -> Self {
        Self::new()
    }
}

/// Preserve only a short prefix and never include request bodies, signed
/// payloads, credentials, signatures or authorization headers.
pub fn public_error(error: &dyn std::fmt::Display) -> String {
    let raw = error.to_string();
    let lower = raw.to_ascii_lowercase();
    if ["private", "authorization", "jwt", "signature", "api-key", "x-api-key", "signed payload"]
        .iter()
        .any(|needle| lower.contains(needle))
    {
        "redacted authentication error".into()
    } else {
        raw.chars().take(240).collect()
    }
}

/// Read chrony's latest system clock offset. A missing chronyc binary is an
/// error in production; the systemd units also gate startup with waitsync.
pub fn chrony_offset_ms() -> anyhow::Result<i64> {
    let output = std::process::Command::new("chronyc").args(["tracking", "-n"]).output()?;
    anyhow::ensure!(output.status.success(), "chronyc tracking failed");
    let text = String::from_utf8(output.stdout)?;
    let line = text
        .lines()
        .find(|line| line.starts_with("Last offset"))
        .ok_or_else(|| anyhow::anyhow!("chronyc output has no Last offset"))?;
    let value = line
        .split(':')
        .nth(1)
        .and_then(|x| x.split_whitespace().next())
        .ok_or_else(|| anyhow::anyhow!("chronyc Last offset is malformed"))?
        .parse::<f64>()?;
    Ok((value.abs() * 1000.0).round() as i64)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{RiskLimits, VenueMode};

    fn guard(path: &str) -> RiskGuard {
        RiskGuard::open(
            RiskLimits {
                order_usd: 10.0,
                max_open_usd: 20.0,
                daily_loss_usd: 5.0,
                max_markets: 2,
                max_orders: 4,
                max_rejections: 3,
            },
            std::env::temp_dir().join(format!("pk-venue-svc-{path}-{}", std::process::id())),
        )
        .unwrap()
    }

    #[test]
    fn latch_halts_and_acknowledge_restores_readiness() {
        let mut state = AdapterState::new("predict", VenueMode::Shadow, "cfg".into(), guard("latch"));
        state.reconciling = false;
        state.ready = true;
        state.latch("boom");
        assert!(!state.ready);
        assert!(!state.status()["ready"].as_bool().unwrap());
        assert!(state.risk.check_order(1.0, false).is_err());
        state.acknowledge();
        assert!(state.latched.is_empty());
        assert!(state.ready);
    }

    #[test]
    fn journal_redacts_secrets_and_stamps_every_record() {
        let dir = std::env::temp_dir().join(format!("pk-venue-journal-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let mut journal = Journal::open(&dir).unwrap();
        journal
            .record(
                "order_submitted",
                json!({
                    "order": {"id": "1", "signature": "0xdeadbeef", "signedPayload": {"x": 1}},
                    "jwt": "eyJhbGciOiJIUzI1NiJ9",
                    "privateKey": "0xcafe",
                    "authorization": "Bearer hunter2",
                    "price": 0.5,
                }),
            )
            .unwrap();
        let day = Utc::now().format("%Y-%m-%d").to_string();
        let line = std::fs::read_to_string(dir.join(format!("{day}.jsonl"))).unwrap();
        assert!(!line.contains("deadbeef"), "{line}");
        assert!(!line.contains("eyJhbGciOiJIUzI1NiJ9"), "{line}");
        assert!(!line.contains("cafe"), "{line}");
        assert!(!line.contains("hunter2"), "{line}");
        assert!(line.contains("[redacted]"), "{line}");
        assert!(line.contains(r#""type":"order_submitted""#), "{line}");
        assert!(line.contains(r#""price":0.5"#), "{line}");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn public_error_redacts_auth_material_and_truncates() {
        let secret = anyhow::anyhow!("request failed with signature 0xdeadbeef attached");
        assert_eq!(public_error(&secret), "redacted authentication error");
        let long = anyhow::anyhow!("{}", "x".repeat(500));
        assert_eq!(public_error(&long).len(), 240);
        let plain = anyhow::anyhow!("Predict HTTP 429: rate limited");
        assert_eq!(public_error(&plain), "Predict HTTP 429: rate limited");
    }

    #[test]
    fn markouts_fire_at_each_horizon_then_release() {
        let mut tracker = MarkoutTracker::new();
        tracker.watch("7", "YES", 0.40, 1_000_000);
        let mids = HashMap::from([(("7".into(), "YES".into()), 0.45)]);
        assert!(tracker.poll(1_000_000 + 4_999, &mids).is_empty());
        let five = tracker.poll(1_000_000 + 5_000, &mids);
        assert_eq!(five.len(), 1);
        assert_eq!(five[0]["horizon_s"], 5);
        assert!((five[0]["markout"].as_f64().unwrap() - 0.05).abs() < 1e-9);
        assert_eq!(tracker.poll(1_000_000 + 30_000, &mids).len(), 1);
        assert_eq!(tracker.poll(1_000_000 + 60_000, &mids).len(), 1);
        assert!(tracker.poll(1_000_000 + 61_000, &mids).is_empty());
    }

    #[test]
    fn atomic_write_creates_parents_and_lands_whole() {
        let dir = std::env::temp_dir().join(format!("pk-venue-atomic-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let path = dir.join("nested").join("state.json");
        crate::atomic_write(&path, b"{\"a\":1}").unwrap();
        assert_eq!(std::fs::read(&path).unwrap(), b"{\"a\":1}");
        assert!(std::fs::read_dir(dir.join("nested")).unwrap().count() == 1);
        let _ = std::fs::remove_dir_all(dir);
    }
}
