//! The reverse direction's summary (Polymarket signal, Kalshi execution):
//! `pk replay run --reverse` writes the journal; this reads it.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use pk_core::num::round_dp;
use serde_json::{Value, json};

pub fn summarize(records: &[Value]) -> Value {
    let of = |t: &str| records.iter().filter(|r| r["type"] == t).collect::<Vec<_>>();
    let (decisions, fills, settles) = (of("decision"), of("fill"), of("settle"));
    let mut resolutions: HashMap<(String, i64), BTreeMap<String, String>> = HashMap::new();
    for r in of("resolution") {
        if let (Some(a), Some(o), Some(v), Some(out)) =
            (r["asset"].as_str(), r["open_unix"].as_i64(), r["venue"].as_str(), r["outcome"].as_str())
        {
            resolutions.entry((a.into(), o)).or_default().insert(v.into(), out.into());
        }
    }
    let f = |r: &Value, k: &str| r[k].as_f64().unwrap_or(0.0);
    let filled: Vec<&&Value> = fills.iter().filter(|r| f(r, "shares") > 0.0).collect();
    let won = |s: &Value| s["side"] == s["outcome"];
    let wins = settles.iter().filter(|s| won(s)).count();
    let assets: BTreeSet<&str> =
        settles.iter().chain(filled.iter().copied()).filter_map(|r| r["asset"].as_str()).collect();
    let mut by_asset = serde_json::Map::new();
    for a in assets {
        let sa: Vec<&&Value> = settles.iter().filter(|s| s["asset"] == a).collect();
        let fa: Vec<&&&Value> = filled.iter().filter(|x| x["asset"] == a).collect();
        let nets: Vec<f64> = sa
            .iter()
            .filter(|s| f(s, "shares") != 0.0)
            .map(|s| {
                let fee =
                    fa.iter().find(|x| x["open_unix"] == s["open_unix"]).map(|x| f(x, "fee")).unwrap_or(0.0);
                100.0 * (f(s, "payout") - f(s, "vwap") * f(s, "shares") - fee) / f(s, "shares")
            })
            .collect();
        by_asset.insert(
            a.into(),
            json!({
                "fills": fa.len(),
                "settled": sa.len(),
                "wins": sa.iter().filter(|s| won(s)).count(),
                "pnl_usd": round_dp(sa.iter().map(|s| f(s, "pnl")).sum::<f64>(), 2),
                "net_cents_per_share": (!nets.is_empty()).then(|| round_dp(nets.iter().sum::<f64>() / nets.len() as f64, 2)),
            }),
        );
    }
    let traded: BTreeSet<(String, i64)> = settles
        .iter()
        .filter_map(|s| Some((s["asset"].as_str()?.to_owned(), s["open_unix"].as_i64()?)))
        .collect();
    let disagree = traded
        .iter()
        .filter(|k| {
            resolutions.get(*k).is_some_and(|r| {
                r.contains_key("poly") && r.contains_key("kalshi") && r["poly"] != r["kalshi"]
            })
        })
        .count();
    json!({
        "decisions": decisions.len(),
        "enters": decisions.iter().filter(|d| d["action"] == "enter").count(),
        "skips": decisions.iter().filter(|d| d["action"] == "skip").count(),
        "fills": filled.len(),
        "no_fills": fills.len() - filled.len(),
        "fill_rate": (!fills.is_empty()).then(|| round_dp(filled.len() as f64 / fills.len() as f64, 4)),
        "settled": settles.len(),
        "open_unsettled": filled.len() as i64 - settles.len() as i64,
        "wins": wins,
        "win_rate": (!settles.is_empty()).then(|| round_dp(wins as f64 / settles.len() as f64, 4)),
        "pnl_usd": round_dp(settles.iter().map(|s| f(s, "pnl")).sum::<f64>(), 2),
        "fees_usd": round_dp(filled.iter().map(|x| f(x, "fee")).sum::<f64>(), 2),
        "venue_disagreement_on_settled": disagree,
        "by_asset": by_asset,
    })
}
