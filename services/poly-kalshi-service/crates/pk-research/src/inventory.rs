//! PLAN 0.1c: our tape, one row per recorded hour file, usable or excluded and why.
//!
//! ```text
//! pk inventory --record-dir data/raw/dev [--journal data/state/dev/journal]
//! ```
//!
//! Per file: host (from the manifest), lines per venue, sessions,
//! `out_of_order` and `truncated` (the replay's own counts), the session
//! start, the measured feed geometry (`feed_ms_k` / `feed_ms_p`, p10/p50 of
//! receive minus venue time, one line in `SAMPLE`), and two flags:
//!
//! - `ghost_levels`: the session started before `3bfa696` (Kalshi's cancelled
//!   levels left ghosts that crossed the book). Excluded from all research.
//! - `pre_seq_fix`: started before `1721a36` (Kalshi seq counted only orderbook
//!   frames). Excluded from fill research; fine for signal statistics.
//!
//! The geometry, not the host tag, says where a file was recorded:
//! `host_by_geometry` is `london` for a Polymarket p50 under 4 ms, `dublin`
//! for 6 to 20 ms.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::Path;

use chrono::{DateTime, TimeZone, Utc};
use pk_core::num::round_dp;
use serde_json::{Value, json};

pub const SAMPLE: u64 = 20;
const GHOST_FIX: (&str, (i32, u32, u32, u32, u32, u32)) = ("3bfa696", (2026, 10, 6, 17, 31, 51));
const SEQ_FIX: (&str, (i32, u32, u32, u32, u32, u32)) = ("1721a36", (2026, 10, 6, 18, 33, 3));

fn at(t: (i32, u32, u32, u32, u32, u32)) -> DateTime<Utc> {
    Utc.with_ymd_and_hms(t.0, t.1, t.2, t.3, t.4, t.5).single().expect("valid")
}

fn iso(t_ns: Option<i64>) -> Value {
    t_ns.map(|t| {
        DateTime::<Utc>::from_timestamp_nanos(t).format("%Y-%m-%dT%H:%M:%S+00:00").to_string().into()
    })
    .unwrap_or(Value::Null)
}

/// The venue's own timestamp (ms) on a Kalshi orderbook or Polymarket market frame.
fn venue_ms(venue: char, text: &str) -> Option<f64> {
    let body: Value = serde_json::from_str(text).ok()?;
    let v = if venue == 'k' {
        let b = body.as_object()?;
        let ts = b.get("msg").and_then(|m| m.get("ts_ms")).filter(|v| truthy(v));
        ts.or_else(|| b.get("sending_ts_ms")).cloned()?
    } else {
        let msg = match &body {
            Value::Array(a) => a.first()?.clone(),
            other => other.clone(),
        };
        msg.get("timestamp")?.clone()
    };
    if !truthy(&v) {
        return None;
    }
    match v {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
}

fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        _ => true,
    }
}

fn pcts(mut v: Vec<f64>) -> Value {
    if v.len() < 20 {
        return Value::Null;
    }
    v.sort_by(f64::total_cmp);
    json!({"n": v.len(), "p10": round_dp(v[v.len() / 10], 2), "p50": round_dp(v[v.len() / 2], 2)})
}

pub fn inventory(record_dir: &Path, journal: Option<&Path>) -> anyhow::Result<Vec<Value>> {
    let mut stamps: Vec<(i64, String)> = journal
        .filter(|j| j.is_dir())
        .map(pk_core::journal::read_journal)
        .transpose()?
        .unwrap_or_default()
        .iter()
        .filter_map(|r| {
            Some((
                r["t_ns"].as_i64().filter(|t| *t != 0)?,
                r["build"].as_str().filter(|b| !b.is_empty())?.chars().take(7).collect(),
            ))
        })
        .collect();
    stamps.sort();
    let files = pk_tape::iter_files(record_dir, None, None);
    // A file still being written has no manifest: its session's other files name the host.
    let mut session_host: HashMap<String, String> = HashMap::new();
    for f in &files {
        let m =
            f.with_file_name(format!("{}.json", f.file_name().and_then(|n| n.to_str()).unwrap_or_default()));
        if let Some(host) = std::fs::read(&m)
            .ok()
            .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
            .and_then(|v| v["host"].as_str().map(str::to_owned))
        {
            session_host.insert(session_of(f), host);
        }
    }
    let mut rows = Vec::new();
    for path in files {
        let name = path.file_name().and_then(|n| n.to_str()).unwrap_or_default().to_owned();
        let manifest: Value = std::fs::read(path.with_file_name(format!("{name}.json")))
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or(Value::Null);
        let mut reader = pk_tape::read_lines(vec![path.clone()]);
        let mut per_venue: BTreeMap<String, u64> = BTreeMap::new();
        let mut lat: BTreeMap<char, Vec<f64>> = BTreeMap::from([('k', Vec::new()), ('p', Vec::new())]);
        let (mut first, mut last) = (None, None);
        for line in reader.by_ref() {
            let n = per_venue.entry(line.venue.to_string()).or_default();
            *n += 1;
            first.get_or_insert(line.recv_ns);
            last = Some(line.recv_ns);
            if lat.contains_key(&line.venue)
                && (*n).is_multiple_of(SAMPLE)
                && let Some(ms) = venue_ms(line.venue, &line.text)
            {
                lat.get_mut(&line.venue).expect("present").push(line.recv_ns as f64 / 1e6 - ms);
            }
        }
        let session_ns: i64 = session_of(&path).parse().unwrap_or(0);
        let session_start = DateTime::<Utc>::from_timestamp_nanos(session_ns);
        let hour: String = name.chars().skip(3).take(11).collect();
        let seen: BTreeSet<String> = stamps
            .iter()
            .filter(|(t, _)| first.is_some_and(|f| f <= *t) && *t <= last.unwrap_or(0))
            .map(|(_, b)| b.clone())
            .collect();
        let ghost = session_start < at(GHOST_FIX.1) && !seen.contains(GHOST_FIX.0);
        let pre_seq = session_start < at(SEQ_FIX.1) && !seen.contains(SEQ_FIX.0);
        let mut reasons: Vec<String> = Vec::new();
        if ghost {
            reasons.push("ghost Kalshi levels (before 3bfa696): excluded".into());
        } else if pre_seq {
            reasons.push("before the Kalshi seq fix (1721a36): signal only, no fill research".into());
        }
        let stats = reader.stats.clone();
        if stats.truncated > 0 {
            reasons.push("truncated (crash or still being written): read to the break".into());
        }
        if stats.out_of_order > 0 {
            reasons.push(format!("{} lines out of order", stats.out_of_order));
        }
        let host = manifest["host"]
            .as_str()
            .map(str::to_owned)
            .or_else(|| session_host.get(&session_of(&path)).cloned())
            .unwrap_or_else(|| "unknown".into());
        let geo_k = pcts(lat.remove(&'k').unwrap_or_default());
        let geo_p = pcts(lat.remove(&'p').unwrap_or_default());
        let by_geometry = geo_p["p50"].as_f64().map(|p| {
            if p < 4.0 {
                "london"
            } else if p < 20.0 {
                "dublin"
            } else {
                "?"
            }
        });
        if by_geometry == Some("london") {
            reasons.push("London geometry: re-time to Dublin before any fill number (PLAN 3.1)".into());
        }
        if let Some(g) = by_geometry.filter(|g| *g != host) {
            reasons.push(format!("tagged {host}, but its latencies are {g}'s"));
        }
        if reasons.is_empty() {
            reasons.push("usable".into());
        }
        rows.push(json!({
            "file": path.strip_prefix(record_dir).unwrap_or(&path).display().to_string(),
            "hour": hour,
            "host": host,
            "session_start": session_start.format("%Y-%m-%dT%H:%M:%S+00:00").to_string(),
            "manifest": !manifest.is_null(),
            "sessions": manifest.get("sessions"),
            "lines": stats.lines,
            "lines_per_venue": per_venue,
            "first_recv": iso(first),
            "last_recv": iso(last),
            "out_of_order": stats.out_of_order,
            "truncated": stats.truncated,
            "bad_lines": stats.bad,
            "journal_builds": seen,
            "feed_ms_k": geo_k,
            "feed_ms_p": geo_p,
            "host_by_geometry": by_geometry,
            "ghost_levels": ghost,
            "pre_seq_fix": pre_seq,
            "usable_for_signal": !ghost,
            "usable_for_fills": !ghost && !pre_seq && by_geometry == Some("dublin"),
            "reasons": reasons,
        }));
    }
    Ok(rows)
}

fn session_of(path: &Path) -> String {
    let stem = path.file_name().and_then(|n| n.to_str()).unwrap_or_default();
    let stem = stem.split('.').next().unwrap_or_default();
    stem.rsplit('_').next().unwrap_or_default().to_owned()
}
