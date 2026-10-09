//! Per shadow variant: windows, crossings, entries, settled n, win rate,
//! average price, net ¢/share at the touch and its standard error, the number
//! of distinct windows the entries came from with the window-clustered s.e.,
//! and the same split by US night and day. Entries only.

use std::collections::{BTreeMap, BTreeSet};

use pk_core::shadow::US_NIGHT_UTC;
use serde_json::{Value, json};

use crate::stats::{f, i, opt, s};

/// Standard error of the mean net with entries clustered by window start
/// (the sandwich estimator with the C/(C−1) factor): the assets move
/// together inside a window, so its entries are not independent.
fn cluster_se(rows: &[&Value], mean: f64) -> Option<f64> {
    let mut sums: BTreeMap<i64, f64> = BTreeMap::new();
    for r in rows {
        *sums.entry(i(r, "open_unix").unwrap_or(0)).or_default() +=
            f(r, "net_cents_touch").unwrap_or(0.0) - mean;
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
        let avg =
            |k: &str| (n > 0).then(|| rows.iter().map(|r| f(r, k).unwrap_or(0.0)).sum::<f64>() / n as f64);
        out.insert(
            v.clone(),
            json!({
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
            }),
        );
    }
    Value::Object(out)
}
