//! Per shadow variant: windows, crossings, entries, settled n, win rate,
//! average price, net ¢/share at the touch and its standard error, the number
//! of distinct windows the entries came from with the window-clustered s.e.,
//! and the same split by US night and day. Entries only.
//!
//! **Realistic columns** (when the shadow scores them, `PK_SHADOW_LATENCY_MS`):
//! the share of entries a FAK at the live limit filled a latency later, the
//! real net per filled share (the active exit or settlement, both fees) with
//! its window-clustered s.e., how often the exit fired, and the median hold.

use std::collections::{BTreeMap, BTreeSet};

use pk_core::shadow::US_NIGHT_UTC;
use serde_json::{Value, json};

use crate::stats::{f, i, opt, s};

/// Standard error of the mean net with entries clustered by window start
/// (the sandwich estimator with the C/(C−1) factor): the assets move
/// together inside a window, so its entries are not independent.
fn cluster_se(rows: &[&Value], mean: f64) -> Option<f64> {
    cluster_se_of(rows, "net_cents_touch", mean)
}

fn cluster_se_of(rows: &[&Value], field: &str, mean: f64) -> Option<f64> {
    let mut sums: BTreeMap<i64, f64> = BTreeMap::new();
    for r in rows {
        *sums.entry(i(r, "open_unix").unwrap_or(0)).or_default() += f(r, field).unwrap_or(0.0) - mean;
    }
    let (c, n) = (sums.len() as f64, rows.len() as f64);
    if sums.len() < 2 {
        return None;
    }
    Some((c / (c - 1.0) * sums.values().map(|v| v * v).sum::<f64>()).sqrt() / n)
}

fn session(rows: &[&Value], night: bool) -> Value {
    let part: Vec<&&Value> = rows
        .iter()
        .filter(|r| {
            US_NIGHT_UTC.contains(&((i(r, "open_unix").unwrap_or(0).div_euclid(3600).rem_euclid(24)) as u32))
                == night
        })
        .collect();
    let n = part.len();
    let win: Vec<f64> = part.iter().map(|r| f(r, "win").unwrap_or(0.0)).collect();
    let net: Vec<f64> = part.iter().map(|r| f(r, "net_cents_touch").unwrap_or(0.0)).collect();
    json!({
        "n": n,
        "win_rate": (n > 0).then(|| win.iter().sum::<f64>() / n as f64),
        "net_cents": (n > 0).then(|| net.iter().sum::<f64>() / n as f64),
    })
}

pub fn shadow_table(records: &[Value]) -> Value {
    let mut windows: BTreeMap<String, u64> = BTreeMap::new();
    let mut crossings: BTreeMap<String, u64> = BTreeMap::new();
    let mut settled: BTreeMap<String, Vec<&Value>> = BTreeMap::new();
    for r in records {
        match s(r, "type") {
            Some("shadow") => {
                let v = s(r, "variant").unwrap_or_default().to_owned();
                *windows.entry(v.clone()).or_default() += 1;
                if matches!(s(r, "action"), Some("enter" | "skip")) {
                    *crossings.entry(v).or_default() += 1;
                }
            }
            Some("shadow_settle") if s(r, "action") == Some("enter") => {
                settled.entry(s(r, "variant").unwrap_or_default().to_owned()).or_default().push(r);
            }
            _ => {}
        }
    }
    let mut names: Vec<String> =
        windows.keys().chain(settled.keys()).cloned().collect::<BTreeSet<_>>().into_iter().collect();
    names.sort_by_key(|v| (v != "live", v.clone()));
    let mut out = serde_json::Map::new();
    for v in names {
        let rows = settled.get(&v).cloned().unwrap_or_default();
        let nets: Vec<f64> = rows.iter().map(|r| f(r, "net_cents_touch").unwrap_or(0.0)).collect();
        let n = rows.len();
        let (mut se, mut se_cluster) = (None, None);
        if n > 1 {
            let mean = nets.iter().sum::<f64>() / n as f64;
            let var = nets.iter().map(|x| (x - mean) * (x - mean)).sum::<f64>() / (n - 1) as f64;
            se = Some((var / n as f64).sqrt());
            se_cluster = cluster_se(&rows, mean);
        }
        let entry_windows: BTreeSet<i64> = rows.iter().filter_map(|r| i(r, "open_unix")).collect();
        // Realistic: only rows the shadow priced (`filled` present).
        let priced: Vec<&Value> =
            rows.iter().copied().filter(|r| r.get("filled").is_some_and(|v| !v.is_null())).collect();
        let filled: Vec<&Value> =
            priced.iter().copied().filter(|r| r.get("filled") == Some(&Value::Bool(true))).collect();
        let reals: Vec<f64> = filled.iter().filter_map(|r| f(r, "net_cents_real")).collect();
        let real_mean = (!reals.is_empty()).then(|| reals.iter().sum::<f64>() / reals.len() as f64);
        let mut held: Vec<f64> = filled.iter().filter_map(|r| f(r, "held_s")).collect();
        held.sort_by(f64::total_cmp);
        let avg =
            |k: &str| (n > 0).then(|| rows.iter().map(|r| f(r, k).unwrap_or(0.0)).sum::<f64>() / n as f64);
        let mut row = json!({
            "windows": windows.get(&v).copied().unwrap_or(0),
            "crossings": crossings.get(&v).copied().unwrap_or(0),
            "n": n,
            "win_rate": avg("win"),
            "avg_px": avg("entry_px"),
            "net_cents": (n > 0).then(|| nets.iter().sum::<f64>() / n as f64),
            "se_cents": opt(se),
            "entry_windows": entry_windows.len(),
            "se_cluster_cents": opt(se_cluster),
            "us_night": session(&rows, true),
            "us_day": session(&rows, false),
        });
        // Only journals the shadow priced have the realistic columns.
        if !priced.is_empty()
            && let Some(o) = row.as_object_mut()
        {
            o.insert("priced".into(), priced.len().into());
            o.insert("fill_rate".into(), (filled.len() as f64 / priced.len() as f64).into());
            o.insert("net_cents_real".into(), real_mean.into());
            o.insert(
                "se_cluster_cents_real".into(),
                real_mean.map_or(Value::Null, |m| opt(cluster_se_of(&filled, "net_cents_real", m))),
            );
            o.insert(
                "exit_rate".into(),
                (!filled.is_empty())
                    .then(|| {
                        let exited = filled.iter().filter(|r| f(r, "exit_bid").is_some()).count();
                        exited as f64 / filled.len() as f64
                    })
                    .into(),
            );
            o.insert("held_s_median".into(), held.get(held.len() / 2).copied().into());
        }
        out.insert(v.clone(), row);
    }
    Value::Object(out)
}
