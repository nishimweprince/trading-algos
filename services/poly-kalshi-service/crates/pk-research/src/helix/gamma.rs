//! Polymarket's own resolution per window, from Gamma, cached on disk.
//!
//! One request per window (`/events?slug=<asset>-updown-15m-<open>`), sent
//! with our own user agent (Gamma answers a library default with 403). Answers
//! are kept in `<dir>/outcomes.json`, so a window is fetched once, ever.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use pk_core::window::poly_slug;
use serde_json::Value;

pub const GAMMA: &str = "https://gamma-api.polymarket.com";
pub const USER_AGENT: &str = "poly-kalshi/0.2 (+trading-algos)";

pub fn client(timeout_s: u64) -> anyhow::Result<reqwest::blocking::Client> {
    Ok(reqwest::blocking::Client::builder()
        .user_agent(USER_AGENT)
        .timeout(Duration::from_secs(timeout_s))
        .build()?)
}

/// A JSON array of prices (`'["1", "0"]'`), as floats.
pub fn prices(text: Option<&Value>) -> Option<Vec<f64>> {
    let text = text.and_then(Value::as_str).unwrap_or("[]");
    let v: Vec<Value> = serde_json::from_str(text).ok()?;
    v.iter()
        .map(|p| match p {
            Value::String(s) => s.trim().parse().ok(),
            Value::Number(n) => n.as_f64(),
            _ => None,
        })
        .collect()
}

pub struct GammaCache {
    path: PathBuf,
    pub data: BTreeMap<String, Option<String>>,
    http: reqwest::blocking::Client,
    min_interval: Duration,
    last: Option<Instant>,
}

impl GammaCache {
    pub fn new(dir: &Path) -> anyhow::Result<Self> {
        let path = dir.join("outcomes.json");
        let data =
            std::fs::read(&path).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default();
        Ok(Self { path, data, http: client(15)?, min_interval: Duration::from_millis(150), last: None })
    }

    /// `"yes"` (Up) / `"no"` (Down) once resolved; None if not resolved or unknown.
    pub fn outcome(&mut self, asset: &str, open_unix: i64) -> Option<String> {
        let key = format!("{asset}:{open_unix}");
        if let Some(Some(v)) = self.data.get(&key) {
            return Some(v.clone());
        }
        if let Some(last) = self.last {
            let wait = self.min_interval.saturating_sub(last.elapsed());
            std::thread::sleep(wait);
        }
        self.last = Some(Instant::now());
        let events: Value = self
            .http
            .get(format!("{GAMMA}/events"))
            .query(&[("slug", poly_slug(asset, open_unix))])
            .send()
            .and_then(|r| r.error_for_status())
            .and_then(|r| r.json())
            .ok()?;
        let market = events.get(0).and_then(|e| e.get("markets")).and_then(|m| m.get(0));
        let result = market.and_then(|m| {
            let closed = m.get("closed").and_then(Value::as_bool).unwrap_or(false);
            match prices(m.get("outcomePrices")).as_deref() {
                Some([a, b]) if closed && *a == 1.0 && *b == 0.0 => Some("yes".to_owned()),
                Some([a, b]) if closed && *a == 0.0 && *b == 1.0 => Some("no".to_owned()),
                _ => None,
            }
        });
        self.data.insert(key, result.clone());
        result
    }

    pub fn save(&self) -> anyhow::Result<()> {
        if let Some(p) = self.path.parent() {
            std::fs::create_dir_all(p)?;
        }
        std::fs::write(&self.path, serde_json::to_string_pretty(&self.data)?)?;
        Ok(())
    }
}
