//! PLAN 0.1b: one row per Helix stream per day, and each day usable or excluded.
//!
//! ```text
//! pk helix inventory [--checksums kpc_tier2.sha256.log]
//! ```
//!
//! Writes `research/data/helix_inventory.json`:
//!
//! - `streams`: per stream per day, from the block headers alone: records,
//!   blocks, sub-files (recorder restarts), the time span and the largest
//!   internal gap (in the block clock), the size, and the checksum result
//!   where the manifest covers the file.
//! - `assets`: per day, the windows and BBO rows per asset, from the replay's
//!   `windows.parquet`.
//! - `days`: per day, the settlement regime, the role in research
//!   (`discovery`, `holdout`, `earlier_regime`) and `usable` with the reason when not.
//!
//! The TWAP-regime days except the most recent 14 are discovery; the most
//! recent 14 are the holdout; the earlier regime's days never set a parameter.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};

use pk_core::num::round_dp;
use serde_json::{Map, Value, json};

use super::container::{FOOT_MAGIC, FOOTER_PRESENT, index_entry, open, walk};
use super::{STREAMS, days, path};
use crate::frame::Frame;

pub const HOLDOUT_DAYS: usize = 14;
pub const MIN_KALSHI_HOURS: f64 = 20.0;
pub const WALK_MAX_BYTES: u64 = 300_000_000;

type Span = (i64, i64);

/// The blocks of a single-sub-file day from its footer index (two small reads),
/// if the indexed blocks tile the file from byte 32 to the index.
fn footer_blocks(p: &Path) -> Option<Vec<(u32, u8, Span)>> {
    let mut f = std::fs::File::open(p).ok()?;
    let size = f.metadata().ok()?.len();
    let mut head = [0u8; 32];
    if f.read_exact(&mut head).is_err() || size < 32 + 24 {
        return None;
    }
    if u32::from_le_bytes(head[20..24].try_into().ok()?) & FOOTER_PRESENT == 0 {
        return None;
    }
    let mut tr = [0u8; 24];
    f.seek(SeekFrom::Start(size - 24)).ok()?;
    f.read_exact(&mut tr).ok()?;
    let (fmag, n, ilen) = (
        u64::from_le_bytes(tr[0..8].try_into().ok()?),
        u32::from_le_bytes(tr[8..12].try_into().ok()?),
        u32::from_le_bytes(tr[12..16].try_into().ok()?),
    );
    if fmag != FOOT_MAGIC || ilen != n * 40 {
        return None;
    }
    let idx_off = size - 24 - ilen as u64;
    let mut raw = vec![0u8; ilen as usize];
    f.seek(SeekFrom::Start(idx_off)).ok()?;
    f.read_exact(&mut raw).ok()?;
    let mut out = Vec::new();
    let mut expect = 32u64;
    for k in 0..n as usize {
        let (off, comp, count, tag, first, last) = index_entry(&raw, k * 40);
        if off != expect {
            return None;
        }
        out.push((count, tag, (first, last)));
        expect = off + 40 + comp as u64;
    }
    (expect == idx_off).then_some(out)
}

fn summary(
    size: u64,
    records: u64,
    blocks: usize,
    subs: usize,
    tags: &indexmap::IndexMap<u8, u64>,
    spans: &[Span],
) -> Value {
    let mut gap = 0i64;
    let mut sorted = spans.to_vec();
    sorted.sort();
    if let Some(first) = sorted.first() {
        let mut reach = first.1;
        for (f, l) in &sorted[1..] {
            gap = gap.max(f - reach);
            reach = reach.max(*l);
        }
    }
    json!({
        "bytes": size,
        "records": records,
        "blocks": blocks,
        "subfiles": subs,
        "tags": tags.iter().map(|(t, n)| (t.to_string(), json!(n))).collect::<Map<_, _>>(),
        "span_s": sorted.first().map(|f| round_dp((sorted.iter().map(|s| s.1).max().expect("spans") - f.0) as f64 / 1e9, 1)),
        "largest_gap_s": round_dp(gap as f64 / 1e9, 1),
    })
}

/// Block counts, span and largest gap: from the footer index when it covers
/// the whole file, else by walking the block headers (a restart day larger
/// than `walk_max` is reported unindexed).
pub fn scan_headers(p: &Path, walk_max: Option<u64>) -> anyhow::Result<Value> {
    let size = std::fs::metadata(p)?.len();
    if let Some(blocks) = footer_blocks(p) {
        let mut tags = indexmap::IndexMap::new();
        for (count, tag, _) in &blocks {
            *tags.entry(*tag).or_insert(0) += *count as u64;
        }
        let spans: Vec<Span> = blocks.iter().map(|b| b.2).collect();
        let records = blocks.iter().map(|b| b.0 as u64).sum();
        return Ok(summary(size, records, blocks.len(), 1, &tags, &spans));
    }
    if walk_max.is_some_and(|m| size > m) {
        return Ok(json!({"bytes": size, "indexed": false, "note": "not indexed (recorder restarts)"}));
    }
    let data = open(p)?;
    let (blocks, subs) = walk(&data);
    let mut tags = indexmap::IndexMap::new();
    for b in &blocks {
        *tags.entry(b.tag).or_insert(0) += b.count as u64;
    }
    let spans: Vec<Span> = blocks.iter().map(|b| (b.first_ts, b.last_ts)).collect();
    let records = blocks.iter().map(|b| b.count as u64).sum();
    Ok(summary(size, records, blocks.len(), subs, &tags, &spans))
}

/// The files `sha256sum --quiet -c` reported (it prints failures only); None if not run.
pub fn failures(log: Option<&Path>) -> Option<BTreeSet<String>> {
    let text = std::fs::read_to_string(log?).ok()?;
    Some(
        text.lines()
            .filter_map(|l| l.rsplit_once(": "))
            .filter(|(name, verdict)| !name.is_empty() && verdict.trim() != "OK")
            .map(|(name, _)| name.trim().to_owned())
            .collect(),
    )
}

/// The manifest's file names (`sha256sum` lines: hash, then the name).
pub fn manifest(p: &Path) -> BTreeSet<String> {
    std::fs::read_to_string(p)
        .map(|t| {
            t.lines()
                .filter(|l| !l.is_empty())
                .filter_map(|l| l.split_once(char::is_whitespace).map(|(_, n)| n.trim().to_owned()))
                .collect()
        })
        .unwrap_or_default()
}

fn checksum(key: &str, manifest: &BTreeSet<String>, failures: Option<&BTreeSet<String>>) -> &'static str {
    if !manifest.contains(key) {
        return "not in manifest";
    }
    match failures {
        None => "unchecked",
        Some(f) if f.contains(key) => "FAILED",
        Some(_) => "OK",
    }
}

pub fn build(
    root: &Path,
    replay_dir: &Path,
    resolutions: &Path,
    failures: Option<&BTreeSet<String>>,
    manifest: &BTreeSet<String>,
) -> anyhow::Result<Value> {
    let mut streams: indexmap::IndexMap<&str, BTreeMap<String, Value>> = indexmap::IndexMap::new();
    for stream in STREAMS {
        for day in days(root, stream) {
            let mut row = scan_headers(&path(root, stream, &day), Some(WALK_MAX_BYTES))?;
            row["checksum"] = checksum(&format!("{stream}/{day}.hlxz"), manifest, failures).into();
            streams.entry(stream).or_default().insert(day, row);
        }
    }

    // The regime of a day: its most common one (the first seen on a tie).
    let mut regimes: HashMap<String, String> = HashMap::new();
    if resolutions.is_file() {
        let res = Frame::read_parquet(resolutions)?;
        let mut counts: BTreeMap<String, indexmap::IndexMap<String, usize>> = BTreeMap::new();
        for i in 0..res.len() {
            let day = res.col("day").str(i).unwrap_or_default().to_owned();
            let c = counts.entry(day).or_default();
            if let Some(r) = res.cols.get("regime").and_then(|c| c.str(i)) {
                *c.entry(r.to_owned()).or_insert(0) += 1;
            }
        }
        for (day, c) in counts {
            let best = c.iter().fold(None, |b: Option<(&String, usize)>, (k, n)| match b {
                Some((_, bn)) if bn >= *n => b,
                _ => Some((k, *n)),
            });
            regimes.insert(day, best.map(|b| b.0.clone()).unwrap_or_else(|| "unknown".into()));
        }
    }

    let mut assets = Map::new();
    let mut kalshi_hours: HashMap<String, f64> = HashMap::new();
    let mut day_dirs: Vec<PathBuf> = std::fs::read_dir(replay_dir)
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.file_name().and_then(|n| n.to_str()).is_some_and(|n| n.starts_with('2')))
        .collect();
    day_dirs.sort();
    for dir in day_dirs {
        let wpath = dir.join("windows.parquet");
        if !wpath.is_file() {
            continue;
        }
        let day = dir.file_name().and_then(|n| n.to_str()).unwrap_or_default().to_owned();
        let w = Frame::read_parquet(&wpath)?;
        let summary: Value = serde_json::from_slice(&std::fs::read(dir.join("summary.json"))?)?;
        for (stream, key) in [("Polymarket_CLOB_Yes_BBO", "poly_yes_raw"), ("Kalshi_Yes_BBO", "kalshi_raw")] {
            if let Some(row) = streams.get_mut(stream).and_then(|s| s.get_mut(&day)) {
                row["decoded_rows_15m"] = summary["rows"].get(key).cloned().unwrap_or(Value::Null);
            }
        }
        let n = |c: &str, i: usize| w.col(c).i64(i).unwrap_or(0);
        let mut per: BTreeMap<String, [i64; 5]> = BTreeMap::new();
        let mut k_by_window: BTreeMap<i64, i64> = BTreeMap::new();
        for i in 0..w.len() {
            let a = per.entry(w.col("asset").str(i).unwrap_or_default().to_owned()).or_default();
            a[0] += 1;
            a[1] += (n("k_rows", i) > 0) as i64;
            a[2] += (n("y_rows", i) > 0 && n("n_rows", i) > 0) as i64;
            a[3] += n("k_rows", i);
            a[4] += n("y_rows", i);
            let m = k_by_window.entry(n("open_unix", i)).or_insert(i64::MIN);
            *m = (*m).max(n("k_rows", i));
        }
        assets.insert(
            day.clone(),
            per.into_iter()
                .map(|(a, v)| {
                    (
                        a,
                        json!({"windows": v[0], "with_kalshi": v[1], "with_poly": v[2], "kalshi_rows": v[3],
                               "poly_yes_rows": v[4]}),
                    )
                })
                .collect::<Map<_, _>>()
                .into(),
        );
        kalshi_hours.insert(day, k_by_window.values().filter(|m| **m > 0).count() as f64 / 4.0);
    }

    let poly_days = days(root, "Polymarket_Window");
    let twap: Vec<&String> =
        poly_days.iter().filter(|d| regimes.get(*d).map(String::as_str) == Some("twap")).collect();
    let holdout: BTreeSet<&String> = twap.iter().rev().take(HOLDOUT_DAYS).copied().collect();
    let mut out_days = Map::new();
    for day in &poly_days {
        let mut reasons: Vec<String> = Vec::new();
        for need in ["Polymarket_CLOB_Yes_BBO", "Polymarket_CLOB_No_BBO", "Kalshi_Yes_BBO"] {
            if !streams.get(need).is_some_and(|s| s.contains_key(day)) {
                reasons.push(format!("no {need}"));
            }
        }
        if let Some(h) = kalshi_hours.get(day).filter(|h| **h < MIN_KALSHI_HOURS) {
            reasons.push(format!("Kalshi covers {h:.1} h of windows"));
        }
        let bad: Vec<&str> = streams
            .iter()
            .filter(|(_, d)| d.get(day).is_some_and(|r| r["checksum"] == "FAILED"))
            .map(|(s, _)| *s)
            .collect();
        if !bad.is_empty() {
            reasons.push(format!("checksum failed: {}", bad.join(", ")));
        }
        let regime = regimes.get(day).cloned().unwrap_or_else(|| "unknown".into());
        let role = if holdout.contains(day) {
            "holdout"
        } else if regime == "twap" {
            "discovery"
        } else {
            "earlier_regime"
        };
        let usable = reasons.is_empty();
        out_days.insert(
            day.clone(),
            json!({
                "regime": regime,
                "role": role,
                "kalshi_window_hours": kalshi_hours.get(day),
                "usable": usable,
                "reasons": if usable { vec!["usable".to_owned()] } else { reasons },
                "chainlink": streams.get("PolymarketChainlink_BBO").is_some_and(|s| s.contains_key(day)),
            }),
        );
    }
    Ok(json!({
        "streams": streams.into_iter().map(|(k, v)| (k.to_owned(), json!(v))).collect::<Map<_, _>>(),
        "assets": assets,
        "days": out_days,
    }))
}
