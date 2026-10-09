//! PLAN 3.2: decide once, fill many, on our own tape (full depth).
//!
//! ```text
//! pk fills run --record-dir data/raw/dev --out research/data/fills_dublin
//! pk fills grid --run research/data/fills_dublin
//! ```
//!
//! A decision depends on neither latency nor the limit, so the engine runs
//! once. `run` replays the tape and, for every entry, taps the cheap leg's
//! ask ladder at each line for the next second. The engine resolves an order
//! on the first line at or past `decision + latency`, against the book
//! *before* that line, and the tap records exactly that state, so a fill at
//! any latency can be computed from it. Written: `fills.jsonl.zst` (one row
//! per entry per line), the replay's `journal/` and `summary.json`.
//!
//! `grid` fills every latency × limit offset, walking the ladder as the paper
//! venue does, settles on the journal's Polymarket resolutions, and first
//! checks the engine: at the replay's own latency and offset 0, every entry
//! must fill exactly as the replay's `fill` record did.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::io::{BufRead, Write};
use std::path::Path;

use pk_core::fees::taker_fee;
use pk_core::num::round_dp;
use pk_engine::Engine;
use pk_results::stats::{fmean, stdev};
use serde_json::{Value, json};

pub const HORIZON_MS: i64 = 1000;
pub const LATENCIES_MS: [i64; 7] = [0, 25, 50, 100, 150, 250, 400];
pub const OFFSETS_C: [i64; 4] = [0, 1, 2, 3];
/// Levels kept above the limit: offsets reach +3¢.
pub const LADDER_ABOVE: f64 = 0.05;

struct Capture {
    asset: String,
    open_unix: i64,
    decision_ns: i64,
    token: String,
    limit: f64,
}

/// Snapshots open captures before each line, starts new ones after.
#[derive(Default)]
pub struct LadderTap {
    open: Vec<Capture>,
    pub rows: Vec<Value>,
    seen: HashMap<usize, (i64, i64)>,
}

impl LadderTap {
    pub fn before(&mut self, engine: &Engine, recv_ns: i64) {
        if self.open.is_empty() {
            return;
        }
        let mut still = Vec::new();
        for c in self.open.drain(..) {
            let ladder: Vec<(f64, f64)> = engine
                .books
                .get(&c.token)
                .filter(|b| b.snapshot_ns.is_some())
                .map(|b| b.asks_upto(c.limit + LADDER_ABOVE).collect())
                .unwrap_or_default();
            let dt = recv_ns - c.decision_ns;
            self.rows.push(json!({
                "asset": c.asset, "open_unix": c.open_unix, "decision_ns": c.decision_ns, "dt_ns": dt,
                "px": ladder.iter().map(|x| x.0).collect::<Vec<_>>(), "sz": ladder.iter().map(|x| x.1).collect::<Vec<_>>(),
            }));
            if dt <= HORIZON_MS * 1_000_000 {
                still.push(c);
            }
        }
        self.open = still;
    }

    pub fn after(&mut self, engine: &Engine, recv_ns: i64) {
        for (idx, st) in engine.state.iter().enumerate() {
            let Some(d) = &st.decision else { continue };
            let id = (
                d.get("open_unix").and_then(Value::as_i64).unwrap_or(0),
                d.get("t_ns").and_then(Value::as_i64).unwrap_or(0),
            );
            if self.seen.get(&idx) == Some(&id) {
                continue;
            }
            self.seen.insert(idx, id);
            let blocked = d.get("blocked").is_some_and(|b| !b.is_null() && b != "");
            if d.get("action").and_then(Value::as_str) != Some("enter") || blocked || id.1 != recv_ns {
                continue;
            }
            let Some(w) = &st.window else { continue };
            let side = d.get("side").and_then(Value::as_str).unwrap_or("yes");
            let limit = d.get("entry_px").and_then(Value::as_f64).unwrap_or(0.0);
            self.open.push(Capture {
                asset: engine.assets[idx].clone(),
                open_unix: w.open_unix,
                decision_ns: recv_ns,
                token: if side == "yes" { w.yes_token.clone() } else { w.no_token.clone() },
                limit,
            });
            self.rows.push(json!({
                "asset": engine.assets[idx], "open_unix": w.open_unix, "decision_ns": recv_ns, "dt_ns": null,
                "side": side, "limit": limit, "contracts": d.get("contracts"), "fee_rate": w.fee_rate,
                "fee_exponent": w.fee_exponent, "sec_into": d.get("sec_into"), "gap": d.get("gap"),
            }));
        }
    }

    pub fn write(&self, path: &Path) -> anyhow::Result<()> {
        let mut enc = zstd::stream::write::Encoder::new(std::fs::File::create(path)?, 3)?;
        for r in &self.rows {
            writeln!(enc, "{r}")?;
        }
        enc.finish()?;
        Ok(())
    }
}

/// `(shares, cost, fee)` against the ladder before the first line at or past the latency.
pub fn fill_at(
    events: &[&Value],
    latency_ms: f64,
    limit: f64,
    contracts: f64,
    rate: f64,
    exp: f64,
) -> (f64, f64, f64) {
    let due = (latency_ms * 1e6).round() as i64;
    for e in events {
        if e["dt_ns"].as_i64().unwrap_or(i64::MIN) >= due {
            let (mut remaining, mut cost, mut fee) = (contracts, 0.0, 0.0);
            let px = e["px"].as_array().cloned().unwrap_or_default();
            let sz = e["sz"].as_array().cloned().unwrap_or_default();
            for (p, s) in px.iter().zip(&sz) {
                let (p, s) = (p.as_f64().unwrap_or(0.0), s.as_f64().unwrap_or(0.0));
                if p > limit + 1e-9 || remaining <= 0.0 {
                    break;
                }
                let take = s.min(remaining);
                cost += take * p;
                fee += taker_fee(p, take, rate, exp);
                remaining -= take;
            }
            return (contracts - remaining, cost, fee);
        }
    }
    (0.0, 0.0, 0.0) // the tape ended before the order would have landed
}

struct Row {
    asset: String,
    shares: f64,
    ordered: f64,
    cost: f64,
    fee: f64,
    win: Option<f64>,
}

fn cell(rows: &[&Row]) -> Value {
    let filled: Vec<&&Row> = rows.iter().filter(|r| r.shares > 0.0).collect();
    let settled: Vec<&&&Row> = filled.iter().filter(|r| r.win.is_some()).collect();
    let shares: f64 = filled.iter().map(|r| r.shares).sum();
    let nets: Vec<f64> = settled
        .iter()
        .map(|r| 100.0 * (r.win.unwrap_or(0.0) * r.shares - r.cost - r.fee) / r.shares)
        .collect();
    json!({
        "entries": rows.len(),
        "fill_rate": (!rows.is_empty()).then(|| filled.len() as f64 / rows.len() as f64),
        "fill_ratio": fmean(&filled.iter().map(|r| r.shares / r.ordered).collect::<Vec<_>>()),
        "usd_filled_per_entry": fmean(&rows.iter().map(|r| r.cost).collect::<Vec<_>>()),
        "vwap": (!filled.is_empty()).then(|| filled.iter().map(|r| r.cost).sum::<f64>() / shares),
        "settled": settled.len(),
        "win_rate": fmean(&settled.iter().map(|r| r.win.unwrap_or(0.0)).collect::<Vec<_>>()),
        "net_cents": fmean(&nets),
        "net_se": stdev(&nets).map(|sd| sd / (nets.len() as f64).sqrt()),
        "pnl_usd": round_dp(settled.iter().map(|r| r.win.unwrap_or(0.0) * r.shares - r.cost - r.fee).sum::<f64>(), 2),
    })
}

pub fn read_rows(path: &Path) -> anyhow::Result<Vec<Value>> {
    let dec = zstd::stream::read::Decoder::new(std::fs::File::open(path)?)?;
    std::io::BufReader::new(dec).lines().map(|l| Ok(serde_json::from_str(&l?)?)).collect()
}

pub fn grid(rows: &[Value], journal: &[Value], own_latency: i64, cap: f64) -> Value {
    let outcomes: HashMap<(String, i64), String> = journal
        .iter()
        .filter(|r| r["type"] == "resolution" && r["venue"] == "poly")
        .filter_map(|r| {
            Some((
                (r["asset"].as_str()?.to_owned(), r["open_unix"].as_i64()?),
                r["outcome"].as_str()?.to_owned(),
            ))
        })
        .collect();
    let engine_fills: HashMap<(String, i64), &Value> = journal
        .iter()
        .filter(|r| r["type"] == "fill")
        .filter_map(|r| Some(((r["asset"].as_str()?.to_owned(), r["open_unix"].as_i64()?), r)))
        .collect();
    // Grouped as pandas would: by (asset, open_unix, decision_ns), sorted.
    let mut groups: BTreeMap<(String, i64, i64), (Option<&Value>, Vec<&Value>)> = BTreeMap::new();
    for r in rows {
        let key = (
            r["asset"].as_str().unwrap_or_default().to_owned(),
            r["open_unix"].as_i64().unwrap_or(0),
            r["decision_ns"].as_i64().unwrap_or(0),
        );
        let g = groups.entry(key).or_default();
        if r["dt_ns"].is_null() {
            g.0.get_or_insert(r);
        } else {
            g.1.push(r)
        }
    }
    let entries: Vec<(String, i64, &Value, Vec<&Value>)> = groups
        .into_iter()
        .filter_map(|((a, o, _), (head, mut events))| {
            events.sort_by_key(|e| e["dt_ns"].as_i64().unwrap_or(0));
            Some((a, o, head?, events))
        })
        .collect();
    let num = |v: &Value, k: &str| v[k].as_f64().unwrap_or(0.0);
    let mut mismatches = Vec::new();
    for (asset, open, h, events) in &entries {
        let (shares, cost, _) = fill_at(
            events,
            own_latency as f64,
            num(h, "limit"),
            num(h, "contracts"),
            num(h, "fee_rate"),
            num(h, "fee_exponent"),
        );
        let f = engine_fills.get(&(asset.clone(), *open));
        let same = f.is_some_and(|f| (num(f, "shares") - shares).abs() <= 1e-6);
        if !same || f.is_some_and(|f| (num(f, "cost") - round_dp(cost, 4)).abs() > 1e-4) {
            mismatches.push(json!({"asset": asset, "open_unix": open, "grid": [shares, cost],
                                   "engine": f.map(|f| json!([f["shares"], f["cost"]]))}));
        }
    }
    let mut cells = serde_json::Map::new();
    for latency in LATENCIES_MS {
        for offset in OFFSETS_C {
            let rows: Vec<Row> = entries
                .iter()
                .map(|(asset, open, h, events)| {
                    let limit = round_dp(num(h, "limit") + offset as f64 / 100.0, 4).min(cap);
                    let (shares, cost, fee) = fill_at(
                        events,
                        latency as f64,
                        limit,
                        num(h, "contracts"),
                        num(h, "fee_rate"),
                        num(h, "fee_exponent"),
                    );
                    let win = outcomes
                        .get(&(asset.clone(), *open))
                        .map(|o| if Some(o.as_str()) == h["side"].as_str() { 1.0 } else { 0.0 });
                    Row { asset: asset.clone(), shares, ordered: num(h, "contracts"), cost, fee, win }
                })
                .collect();
            let all: Vec<&Row> = rows.iter().collect();
            let assets: BTreeSet<&str> = rows.iter().map(|r| r.asset.as_str()).collect();
            let by_asset: serde_json::Map<String, Value> = assets
                .into_iter()
                .map(|a| (a.to_owned(), cell(&rows.iter().filter(|r| r.asset == a).collect::<Vec<_>>())))
                .collect();
            cells.insert(format!("{latency}ms+{offset}c"), json!({"all": cell(&all), "by_asset": by_asset}));
        }
    }
    json!({
        "entries": entries.len(),
        "engine_check": {
            "latency_ms": own_latency,
            "agreement": (!entries.is_empty()).then(|| 1.0 - mismatches.len() as f64 / entries.len() as f64),
            "mismatches": mismatches,
        },
        "cells": cells,
    })
}
