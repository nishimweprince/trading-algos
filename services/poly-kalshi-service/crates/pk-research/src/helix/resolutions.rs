//! PLAN 0.1b: every window's settlement, from the venues, joined to the tape.
//!
//! ```text
//! pk helix resolutions [--out research/data]
//! ```
//!
//! Writes `resolutions.parquet`, one row per (asset, open_unix) over the Helix days:
//!
//! - **Polymarket** (Gamma, paged by series and day): `poly_outcome` (Up =
//!   `yes`), `poly_source` (the market's `resolutionSource`), `regime` (`spot`
//!   or `twap`), and the window's `fee_rate`/`fee_exponent`/`fees_enabled`.
//! - **Kalshi** (`/markets` settled, paged by series and day):
//!   `kalshi_outcome`, `kalshi_strike` and `kalshi_close_value`.
//! - **Tape**: `tape_strike`, `tape_close` (the resolution record's price, the
//!   next window's strike) and `tape_outcome`. Helix's own derivation, not a
//!   settlement: `poly_outcome` is what every replay settles on.
//!
//! Responses are cached per (venue, asset, day) under `<out>/venue_cache/`.

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};
use std::time::Duration;

use chrono::{DateTime, NaiveDate, TimeDelta, Utc};
use serde_json::{Map, Value, json};

use super::gamma::{GAMMA, client, prices};
use super::records::{iter_resolutions, iter_windows};
use super::{HELIX_15M, asset_name};
use crate::frame::{Frame, Row};

pub const KALSHI: &str = "https://api.elections.kalshi.com/trade-api/v2";

fn iso(t: DateTime<Utc>) -> String {
    t.format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

fn day_bounds(day: &str) -> anyhow::Result<(DateTime<Utc>, DateTime<Utc>)> {
    let start = NaiveDate::parse_from_str(day, "%Y%m%d")?.and_hms_opt(0, 0, 0).expect("midnight").and_utc();
    Ok((start, start + TimeDelta::days(1)))
}

pub struct Fetcher {
    cache: PathBuf,
    http: reqwest::blocking::Client,
    series_ids: HashMap<String, String>,
}

impl Fetcher {
    pub fn new(cache: &Path) -> anyhow::Result<Self> {
        Ok(Self { cache: cache.to_path_buf(), http: client(30)?, series_ids: HashMap::new() })
    }

    /// A GET with up to six tries (429 and transport errors back off 0.5 s × 2ⁿ).
    fn get(&self, url: &str, params: &[(&str, String)]) -> anyhow::Result<Value> {
        let mut attempt = 0;
        loop {
            let r = self
                .http
                .get(url)
                .query(params)
                .send()
                .and_then(|r| r.error_for_status())
                .and_then(|r| r.json());
            match r {
                Ok(v) => return Ok(v),
                Err(e) if attempt == 5 => return Err(e.into()),
                Err(_) => {
                    std::thread::sleep(Duration::from_millis(500 << attempt));
                    attempt += 1;
                }
            }
        }
    }

    fn cached(&self, name: &str, fetch: impl FnOnce() -> anyhow::Result<Value>) -> anyhow::Result<Value> {
        let p = self.cache.join(format!("{name}.json"));
        if p.is_file() {
            return Ok(serde_json::from_slice(&std::fs::read(&p)?)?);
        }
        let body = fetch()?;
        std::fs::create_dir_all(&self.cache)?;
        std::fs::write(&p, serde_json::to_string(&body)?)?;
        Ok(body)
    }

    fn series_id(&mut self, asset: &str) -> anyhow::Result<String> {
        if let Some(id) = self.series_ids.get(asset) {
            return Ok(id.clone());
        }
        let slug = format!("{}-up-or-down-15m", asset.to_lowercase());
        let body = self
            .cached(&format!("series_{asset}"), || self.get(&format!("{GAMMA}/series"), &[("slug", slug)]))?;
        let id = match &body[0]["id"] {
            Value::String(s) => s.clone(),
            other => other.to_string(),
        };
        self.series_ids.insert(asset.to_owned(), id.clone());
        Ok(id)
    }

    /// Windows closing on `day` (by end date), one compact row each.
    pub fn poly_day(&mut self, asset: &str, day: &str) -> anyhow::Result<Vec<Value>> {
        let name = format!("poly_{asset}_{day}");
        if !self.cache.join(format!("{name}.json")).is_file() {
            self.series_id(asset)?;
        }
        let series = self.series_ids.get(asset).cloned().unwrap_or_default();
        let (start, end) = day_bounds(day)?;
        let body = self.cached(&name, || {
            let mut rows = Vec::new();
            let mut offset = 0;
            loop {
                let page = self.get(
                    &format!("{GAMMA}/events"),
                    &[
                        ("series_id", series.clone()),
                        ("end_date_min", iso(start + TimeDelta::seconds(1))),
                        ("end_date_max", iso(end)),
                        ("limit", "200".into()),
                        ("offset", offset.to_string()),
                    ],
                )?;
                let page = page.as_array().cloned().unwrap_or_default();
                for e in &page {
                    for m in e["markets"].as_array().into_iter().flatten() {
                        rows.push(json!({
                            "slug": e.get("slug"),
                            "outcomePrices": m.get("outcomePrices"),
                            "closed": m.get("closed"),
                            "resolutionSource": m.get("resolutionSource"),
                            "feeSchedule": m.get("feeSchedule"),
                            "feesEnabled": m.get("feesEnabled"),
                            "outcomes": m.get("outcomes"),
                        }));
                    }
                }
                if page.len() < 200 {
                    return Ok(Value::Array(rows));
                }
                offset += 200;
            }
        })?;
        Ok(body.as_array().cloned().unwrap_or_default())
    }

    pub fn kalshi_day(&self, asset: &str, day: &str) -> anyhow::Result<Vec<Value>> {
        let (start, end) = day_bounds(day)?;
        let body = self.cached(&format!("kalshi_{asset}_{day}"), || {
            let mut rows = Vec::new();
            let mut cursor: Option<String> = None;
            loop {
                let mut params = vec![
                    ("series_ticker", format!("KX{asset}15M")),
                    ("min_close_ts", (start.timestamp() + 1).to_string()),
                    ("max_close_ts", end.timestamp().to_string()),
                    ("limit", "1000".into()),
                ];
                if let Some(c) = &cursor {
                    params.push(("cursor", c.clone()));
                }
                let body = self.get(&format!("{KALSHI}/markets"), &params)?;
                for m in body["markets"].as_array().into_iter().flatten() {
                    let keep = [
                        "ticker",
                        "open_time",
                        "close_time",
                        "result",
                        "status",
                        "floor_strike",
                        "expiration_value",
                    ];
                    rows.push(Value::Object(
                        keep.iter()
                            .map(|k| ((*k).to_owned(), m.get(*k).cloned().unwrap_or(Value::Null)))
                            .collect(),
                    ));
                }
                cursor = body["cursor"].as_str().filter(|c| !c.is_empty()).map(str::to_owned);
                if cursor.is_none() {
                    return Ok(Value::Array(rows));
                }
            }
        })?;
        Ok(body.as_array().cloned().unwrap_or_default())
    }
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

fn poly_outcome(m: &Value) -> Option<&'static str> {
    let prices = prices(m.get("outcomePrices"))?;
    let outcomes: Vec<String> = serde_json::from_str::<Vec<String>>(m["outcomes"].as_str().unwrap_or("[]"))
        .ok()?
        .into_iter()
        .map(|o| o.to_lowercase())
        .collect();
    if !truthy(m.get("closed")) || outcomes != ["up", "down"] {
        return None;
    }
    match prices.as_slice() {
        [a, b] if *a == 1.0 && *b == 0.0 => Some("yes"),
        [a, b] if *a == 0.0 && *b == 1.0 => Some("no"),
        _ => None,
    }
}

/// Kalshi sends `expiration_value` as text, sometimes with a thousands separator.
fn number(v: Option<&Value>) -> Option<f64> {
    match v? {
        Value::Null => None,
        Value::String(s) if s.is_empty() => None,
        Value::String(s) => s.replace(',', "").trim().parse().ok(),
        Value::Number(n) => n.as_f64(),
        _ => None,
    }
}

fn regime(source: Option<&Value>) -> Value {
    match source.and_then(Value::as_str).filter(|s| !s.is_empty()) {
        None => Value::Null,
        Some(s) => if s.to_lowercase().contains("twap") { "twap" } else { "spot" }.into(),
    }
}

/// One row per (asset, open_unix) over `days`, sorted by (open, asset).
pub fn build(days: &[String], cache: &Path, root: &Path) -> anyhow::Result<Vec<Row>> {
    let mut fetcher = Fetcher::new(cache)?;
    let mut rows: indexmap::IndexMap<(String, i64), Row> = indexmap::IndexMap::new();
    let new_row = |asset: &str, open: i64, day: &str| -> Row {
        [("asset", json!(asset)), ("open_unix", json!(open)), ("day", json!(day))]
            .into_iter()
            .map(|(k, v)| (k.to_owned(), v))
            .collect()
    };
    for day in days {
        let windows = iter_windows(day, root)?;
        let mut tape_res = HashMap::new();
        for r in iter_resolutions(day, root)? {
            if let Some(name) = u8::try_from(r.asset).ok().and_then(asset_name) {
                tape_res.insert((name, r.open_unix), r);
            }
        }
        for w in &windows {
            let asset = asset_name(w.asset).expect("15-minute asset");
            let r = tape_res.get(&(asset, w.open_unix));
            let strike = (!w.strike.is_nan() && w.strike > 0.0).then_some(w.strike);
            let mut row = new_row(asset, w.open_unix, day);
            row.insert("tape_strike".into(), strike.into());
            row.insert("tape_close".into(), r.map(|r| r.close_px).into());
            row.insert(
                "tape_outcome".into(),
                match (r, strike) {
                    (Some(r), Some(s)) => if r.close_px >= s { "yes" } else { "no" }.into(),
                    _ => Value::Null,
                },
            );
            rows.insert((asset.to_owned(), w.open_unix), row);
        }
        for (_, asset) in HELIX_15M {
            for m in fetcher.poly_day(asset, day)? {
                let slug = m["slug"].as_str().unwrap_or_default();
                let Some(open) = slug.rsplit_once('-').and_then(|(_, o)| o.parse::<i64>().ok()) else {
                    continue;
                };
                let row = rows.entry((asset.to_owned(), open)).or_insert_with(|| new_row(asset, open, day));
                let fees = m.get("feeSchedule").filter(|f| truthy(Some(f))).cloned().unwrap_or(json!({}));
                let enabled = m.get("feesEnabled");
                let fees_on = enabled.is_none() || truthy(enabled);
                row.insert("poly_outcome".into(), poly_outcome(&m).into());
                row.insert("poly_source".into(), m.get("resolutionSource").cloned().unwrap_or(Value::Null));
                row.insert("regime".into(), regime(m.get("resolutionSource")));
                row.insert("fees_enabled".into(), enabled.cloned().unwrap_or(Value::Null));
                row.insert(
                    "fee_rate".into(),
                    if fees_on { fees.get("rate").cloned().unwrap_or(Value::Null) } else { json!(0.0) },
                );
                row.insert("fee_exponent".into(), fees.get("exponent").cloned().unwrap_or(Value::Null));
            }
            for m in fetcher.kalshi_day(asset, day)? {
                let Some(close) = m["close_time"].as_str().and_then(|s| DateTime::parse_from_rfc3339(s).ok())
                else {
                    continue;
                };
                let open = close.timestamp() - 900;
                let row = rows.entry((asset.to_owned(), open)).or_insert_with(|| new_row(asset, open, day));
                let result = m["result"].as_str().filter(|r| *r == "yes" || *r == "no");
                row.insert("kalshi_ticker".into(), m.get("ticker").cloned().unwrap_or(Value::Null));
                row.insert("kalshi_outcome".into(), result.into());
                row.insert("kalshi_strike".into(), m.get("floor_strike").cloned().unwrap_or(Value::Null));
                row.insert("kalshi_close_value".into(), number(m.get("expiration_value")).into());
            }
        }
        println!("{day} {}", rows.len());
    }
    let mut out: Vec<Row> = rows.into_values().collect();
    out.sort_by(|a, b| {
        (a["open_unix"].as_i64(), a["asset"].as_str()).cmp(&(b["open_unix"].as_i64(), b["asset"].as_str()))
    });
    Ok(out)
}

/// Python's `str()` of a JSON scalar (`None`, `0.07`, `0.0`).
fn py_str(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => "None".into(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(b)) => if *b { "True" } else { "False" }.into(),
        Some(Value::Number(n)) if n.is_f64() => {
            let f = n.as_f64().expect("f64");
            if f.fract() == 0.0 && f.abs() < 1e16 { format!("{f:.1}") } else { format!("{f}") }
        }
        Some(other) => other.to_string(),
    }
}

pub fn summarise(rows: &[Row]) -> Value {
    let mut by_day: BTreeMap<String, Map<String, Value>> = BTreeMap::new();
    for r in rows {
        let d = by_day.entry(r["day"].as_str().unwrap_or_default().to_owned()).or_insert_with(|| {
            json!({"windows": 0, "regimes": {}, "fee_rates": {}, "poly_known": 0, "tape_vs_poly_disagree": 0,
                   "kalshi_vs_poly_disagree": 0, "both": 0})
            .as_object()
            .expect("object")
            .clone()
        });
        let inc = |d: &mut Map<String, Value>, k: &str| {
            let n = d[k].as_i64().unwrap_or(0) + 1;
            d.insert(k.to_owned(), n.into());
        };
        inc(d, "windows");
        for (field, key) in [("regime", "regimes"), ("fee_rate", "fee_rates")] {
            let m = d.get_mut(key).and_then(Value::as_object_mut).expect("map");
            let k = py_str(r.get(field));
            let n = m.get(&k).and_then(Value::as_i64).unwrap_or(0) + 1;
            m.insert(k, n.into());
        }
        if let Some(p) = r.get("poly_outcome").and_then(Value::as_str).filter(|p| !p.is_empty()) {
            inc(d, "poly_known");
            if let Some(t) = r.get("tape_outcome").and_then(Value::as_str).filter(|t| !t.is_empty())
                && t != p
            {
                inc(d, "tape_vs_poly_disagree");
            }
            if let Some(k) = r.get("kalshi_outcome").and_then(Value::as_str).filter(|k| !k.is_empty()) {
                inc(d, "both");
                if k != p {
                    inc(d, "kalshi_vs_poly_disagree");
                }
            }
        }
    }
    let count = |d: &Map<String, Value>, k: &str| d["regimes"].get(k).and_then(Value::as_i64).unwrap_or(0);
    let first_twap =
        by_day.iter().find(|(_, d)| count(d, "twap") > count(d, "spot")).map(|(day, _)| day.clone());
    json!({"first_twap_day": first_twap, "days": by_day})
}

/// Builds `resolutions.parquet` and `resolutions_summary.json` under `out`.
pub fn run(root: &Path, out: &Path) -> anyhow::Result<Value> {
    let days = super::days(root, "Polymarket_Window");
    let rows = build(&days, &out.join("venue_cache"), root)?;
    std::fs::create_dir_all(out)?;
    Frame::from_records(&rows).write_parquet(&out.join("resolutions.parquet"))?;
    let summary = summarise(&rows);
    std::fs::write(out.join("resolutions_summary.json"), serde_json::to_string_pretty(&summary)?)?;
    Ok(summary)
}
