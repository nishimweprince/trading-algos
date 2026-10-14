//! The gate read-out from a decision journal (live or replayed).
//!
//! ```text
//! pk readout --journal data/state/dev/journal [--exclude-assets BTC,SOL]
//! ```
//!
//! One results epoch (`--epoch`, default the latest in the journal): the gates
//! with the settlement QA (windows per asset per day, 96 for a full day; how
//! often the venues resolved a window differently), and:
//!
//! - `markouts`: the cheap leg 1, 5 and 30 s after fills and no-fills;
//! - `capacity_usd_at_limit_median`: median $ offered at the limit per asset;
//! - `exits`: net ¢/share per exit rule against holding, on the same orders.
//!   An exit sells at the held leg's best bid, taker fee on both legs; where
//!   there was no bid, or the gap never closed, the order is held;
//! - `assets` (with `--exclude-assets`): the read with and without them, net
//!   ¢/share over the shares each settled position filled (an exit's `settle`
//!   record has none left).

use std::collections::{BTreeMap, BTreeSet, HashMap};

use pk_core::fees::{DEFAULT_EXPONENT, DEFAULT_RATE, taker_fee};
use pk_core::num::round_dp;
use pk_engine::EXIT_S;
use pk_results::{evaluate, paper_results};
use serde_json::{Value, json};

fn fee(p: f64) -> f64 {
    taker_fee(p, 1.0, DEFAULT_RATE, DEFAULT_EXPONENT)
}

/// ¢/share from buying at `reference` and selling at `bid`, taker fee on both legs.
fn sell_net(bid: Option<f64>, reference: f64) -> Option<f64> {
    let bid = bid.filter(|b| *b > 0.0 && *b < 1.0)?;
    Some(100.0 * (bid - reference - fee(reference) - fee(bid)))
}

pub fn exit_table(records: &[Value]) -> Value {
    let outcome: HashMap<(String, i64), String> = records
        .iter()
        .filter(|r| r["type"] == "resolution" && r["venue"] == "poly")
        .filter_map(|r| {
            Some((
                (r["asset"].as_str()?.to_owned(), r["open_unix"].as_i64()?),
                r["outcome"].as_str()?.to_owned(),
            ))
        })
        .collect();
    let rules: Vec<String> = std::iter::once("hold".to_owned())
        .chain(EXIT_S.iter().map(|h| format!("exit_{h}s")))
        .chain(["exit_gap".to_owned()])
        .collect();
    let groups = ["fills", "no_fills", "all"];
    type ByRule = BTreeMap<String, Vec<(f64, bool)>>;
    let mut acc: BTreeMap<&str, ByRule> =
        groups.iter().map(|g| (*g, rules.iter().map(|r| (r.clone(), Vec::new())).collect())).collect();
    for r in records.iter().filter(|r| r["type"] == "markout") {
        let (Some(asset), Some(open)) = (r["asset"].as_str(), r["open_unix"].as_i64()) else { continue };
        let Some(out) = outcome.get(&(asset.to_owned(), open)) else { continue };
        let reference = r["ref_px"].as_f64().unwrap_or(0.0);
        let won = if r["side"].as_str() == Some(out.as_str()) { 1.0 } else { 0.0 };
        let hold = 100.0 * (won - reference - fee(reference));
        let mut nets: Vec<(String, (f64, bool))> = vec![("hold".into(), (hold, false))];
        for h in EXIT_S {
            let x = sell_net(r[format!("bid_{h}s")].as_f64(), reference);
            nets.push((format!("exit_{h}s"), x.map_or((hold, false), |x| (x, true))));
        }
        let x =
            if r["exit_gap_s"].is_null() { None } else { sell_net(r["exit_gap_bid"].as_f64(), reference) };
        nets.push(("exit_gap".into(), x.map_or((hold, false), |x| (x, true))));
        let filled = r["filled"].as_bool().unwrap_or(false);
        for g in [if filled { "fills" } else { "no_fills" }, "all"] {
            for (rule, v) in &nets {
                acc.get_mut(g).expect("group").get_mut(rule).expect("rule").push(*v);
            }
        }
    }
    let mut out = serde_json::Map::new();
    for (g, by_rule) in acc {
        let mut m = serde_json::Map::new();
        for (rule, v) in by_rule {
            m.insert(
                rule,
                json!({
                    "n": v.len(),
                    "exited": v.iter().filter(|(_, e)| *e).count(),
                    "net_cents": (!v.is_empty()).then(|| round_dp(v.iter().map(|(x, _)| x).sum::<f64>() / v.len() as f64, 3)),
                }),
            );
        }
        out.insert(g.into(), Value::Object(m));
    }
    Value::Object(out)
}

/// The journal as if `excluded` had never been traded.
pub fn without_assets(records: &[Value], excluded: &BTreeSet<String>) -> Vec<Value> {
    records.iter().filter(|r| r["asset"].as_str().is_none_or(|a| !excluded.contains(a))).cloned().collect()
}

pub fn asset_comparison(records: &[Value], excluded: &BTreeSet<String>) -> Value {
    let read = |rows: &[Value]| {
        let key = |r: &Value| (r["asset"].as_str().unwrap_or_default().to_owned(), r["open_unix"].as_i64());
        let mut filled: HashMap<(String, Option<i64>), f64> = HashMap::new();
        for r in rows.iter().filter(|r| r["type"] == "fill") {
            *filled.entry(key(r)).or_default() += r["shares"].as_f64().unwrap_or(0.0);
        }
        let settles: Vec<&Value> = rows.iter().filter(|r| r["type"] == "settle").collect();
        let shares: f64 = settles.iter().map(|r| filled.get(&key(r)).copied().unwrap_or(0.0)).sum();
        let pnl: f64 = settles.iter().map(|r| r["pnl"].as_f64().unwrap_or(0.0)).sum();
        json!({
            "settled": settles.len(),
            "pnl": round_dp(pnl, 2),
            "net_cents_per_share": (shares != 0.0).then(|| round_dp(100.0 * pnl / shares, 2)),
        })
    };
    json!({"excluded": excluded, "all": read(records), "without": read(&without_assets(records, excluded))})
}

pub fn readout(all: Vec<Value>, cap: f64, epoch: Option<&str>, exclude: &BTreeSet<String>) -> Value {
    let (mut records, excluded_n) = pk_core::journal::in_epoch(all, epoch);
    let comparison = (!exclude.is_empty()).then(|| asset_comparison(&records, exclude));
    if !exclude.is_empty() {
        records = without_assets(&records, exclude);
    }
    let mut result = evaluate(&records, cap);
    if let Some(c) = comparison {
        result["assets"] = c;
    }
    result["epoch"] = match epoch {
        Some(e) => e.into(),
        None => records
            .iter()
            .rev()
            .find_map(|r| r.get("epoch").filter(|e| !e.is_null() && *e != "").cloned())
            .unwrap_or(Value::Null),
    };
    result["excluded_records"] = excluded_n.into();
    let short: serde_json::Map<String, Value> = result["windows_per_asset_day"]
        .as_object()
        .map(|m| {
            m.iter().filter(|(_, n)| n.as_u64() != Some(96)).map(|(k, v)| (k.clone(), v.clone())).collect()
        })
        .unwrap_or_default();
    result["windows_not_96"] = Value::Object(short);
    result["markouts"] = paper_results(&records, 50)["execution"]["markouts"].clone();
    result["exits"] = exit_table(&records);
    let mut depth: BTreeMap<String, Vec<f64>> = BTreeMap::new();
    for r in records.iter().filter(|r| r["type"] == "decision") {
        if let (Some(d), Some(a)) = (r["depth_at_limit"].as_f64(), r["asset"].as_str()) {
            depth.entry(a.to_owned()).or_default().push(d * r["entry_px"].as_f64().unwrap_or(0.0));
        }
    }
    result["capacity_usd_at_limit_median"] = depth
        .into_iter()
        .map(|(a, mut v)| {
            v.sort_by(f64::total_cmp);
            (a, json!(v[v.len() / 2]))
        })
        .collect::<serde_json::Map<_, _>>()
        .into();
    result
}
