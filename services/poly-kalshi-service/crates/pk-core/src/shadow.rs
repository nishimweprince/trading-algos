//! Shadow variants: other parameter sets of the rule, decided live, never ordered.
//!
//! `decide()` is pure, so every book update can also be put to a list of
//! variants (`PK_SHADOW_VARIANTS`) at one call each. Each variant has its own
//! first-crossing-decides state per window and costs nothing once consumed.
//!
//! Records: `shadow` (one per variant per window at the roll) and
//! `shadow_settle` (one per consumed variant when Polymarket resolves: net
//! ¢/share at the touch after the fee). The live rule runs alongside as
//! variant `live`, so every variant is compared on the same basis.

use std::collections::{HashMap, HashSet};

use indexmap::IndexMap;
use serde_json::{Map, Value, json};

use crate::book::Quote;
use crate::decision::{Decision, StrategyParams, decide};
use crate::fees::taker_fee;
use crate::num::round_dp;

/// 01:00-04:59 ET, Kalshi's thinnest hours (EDT; one hour later in winter).
pub const US_NIGHT_UTC: [u32; 6] = [5, 6, 7, 8, 9, 10];

/// PLAN 1.2's set and the PREREG part 2-4 additions.
pub const DEFAULT_VARIANTS: &str = r#"[
 {"name": "t08", "gap": 0.08},
 {"name": "t10", "gap": 0.10},
 {"name": "t12", "gap": 0.12},
 {"name": "t15", "gap": 0.15},
 {"name": "t20", "gap": 0.20},
 {"name": "early120", "early": 120},
 {"name": "kspread05", "max_kalshi_spread": 0.05},
 {"name": "cap50", "cap": 0.50},
 {"name": "pspread05", "max_poly_spread": 0.05},
 {"name": "pspread10", "max_poly_spread": 0.10},
 {"name": "kext", "kalshi_extreme": true},
 {"name": "kext_t15", "kalshi_extreme": true, "gap": 0.15},
 {"name": "floor15", "min_px": 0.15},
 {"name": "usnight", "skip_hours_utc": [5, 6, 7, 8, 9, 10]},
 {"name": "pspread01", "max_poly_spread": 0.01},
 {"name": "plag250", "max_poly_lag_ms": 250},
 {"name": "t20_cap50", "gap": 0.20, "cap": 0.50}
]"#;

#[derive(Clone, Debug, PartialEq)]
pub struct Variant {
    pub name: String,
    pub params: StrategyParams,
}

const KEYS: [&str; 10] = [
    "gap",
    "cap",
    "early",
    "late",
    "max_kalshi_spread",
    "max_poly_spread",
    "min_px",
    "kalshi_extreme",
    "skip_hours_utc",
    "max_poly_lag_ms",
];

/// `[{name, gap, cap, early, late, max_kalshi_spread, max_poly_spread, min_px,
/// kalshi_extreme, skip_hours_utc, max_poly_lag_ms}, ...]`; a missing key keeps
/// the live value. An empty string means no variants.
pub fn parse_variants(text: &str, base: &StrategyParams) -> anyhow::Result<Vec<Variant>> {
    if text.trim().is_empty() {
        return Ok(Vec::new());
    }
    let items: Vec<Map<String, Value>> = serde_json::from_str(text)
        .map_err(|e| anyhow::anyhow!("PK_SHADOW_VARIANTS must be a JSON list: {e}"))?;
    let mut out = Vec::new();
    let mut names = HashSet::new();
    for item in items {
        let name = item.get("name").and_then(Value::as_str).unwrap_or_default().to_owned();
        anyhow::ensure!(
            !name.is_empty() && name != "live" && !names.contains(&name),
            "shadow variant name {name:?} is empty, repeated or reserved"
        );
        let unknown: Vec<&String> =
            item.keys().filter(|k| *k != "name" && !KEYS.contains(&k.as_str())).collect();
        anyhow::ensure!(unknown.is_empty(), "shadow variant {name}: unknown keys {unknown:?}");
        let mut p = base.clone();
        let f = |k: &str| item.get(k).and_then(Value::as_f64);
        if let Some(v) = f("gap") {
            p.gap_threshold = v;
        }
        if let Some(v) = f("cap") {
            p.max_entry_px = v;
        }
        if let Some(v) = item.get("early").and_then(Value::as_i64) {
            p.entry_early_s = v;
        }
        if let Some(v) = item.get("late").and_then(Value::as_i64) {
            p.entry_late_s = v;
        }
        for (key, slot) in [
            ("max_kalshi_spread", &mut p.max_kalshi_spread),
            ("max_poly_spread", &mut p.max_poly_spread),
            ("min_px", &mut p.min_entry_px),
            ("max_poly_lag_ms", &mut p.max_poly_lag_ms),
        ] {
            if let Some(v) = item.get(key) {
                *slot = v.as_f64();
            }
        }
        if let Some(v) = item.get("kalshi_extreme") {
            p.kalshi_extreme = v.as_bool();
        }
        if let Some(v) = item.get("skip_hours_utc") {
            p.skip_hours_utc =
                v.as_array().map(|a| a.iter().filter_map(Value::as_u64).map(|h| h as u32).collect());
        }
        names.insert(name.clone());
        out.push(Variant { name, params: p });
    }
    Ok(out)
}

pub type Key = (String, i64);

#[derive(Debug)]
struct Fired {
    record: Map<String, Value>,
    entry_px: Option<f64>,
    side: &'static str,
    action: &'static str,
    fee_rate: f64,
    fee_exponent: f64,
}

#[derive(Debug, Default)]
pub struct Shadow {
    pub variants: Vec<Variant>,
    fired: HashMap<Key, IndexMap<String, Fired>>,
    done: HashMap<Key, HashSet<String>>,
}

pub fn quote_json(q: &Quote) -> Value {
    json!({"valid": q.valid, "bid": q.bid, "ask": q.ask})
}

impl Shadow {
    pub fn new(variants: Vec<Variant>, live: &StrategyParams) -> Self {
        let variants = if variants.is_empty() {
            Vec::new()
        } else {
            std::iter::once(Variant { name: "live".into(), params: live.clone() }).chain(variants).collect()
        };
        Self { variants, ..Default::default() }
    }

    pub fn is_active(&self) -> bool {
        !self.variants.is_empty()
    }

    /// Run every variant not yet consumed in this window. `depth(buy_yes, px)` is
    /// the shares offered at or below `px` on that leg.
    #[allow(clippy::too_many_arguments)]
    pub fn evaluate(
        &mut self,
        key: &Key,
        quotes: (&Quote, &Quote, &Quote),
        sec_into: f64,
        recv_ns: i64,
        depth: impl Fn(bool, f64) -> f64,
        fee: (f64, f64),
        lags: (Option<f64>, Option<f64>),
    ) {
        let done = self.done.entry(key.clone()).or_default();
        if done.len() == self.variants.len() {
            return;
        }
        let (k, y, n) = quotes;
        let utc_hour = ((key.1 as f64 + sec_into) as i64).div_euclid(3600).rem_euclid(24) as u32;
        for v in &self.variants {
            if done.contains(&v.name) {
                continue;
            }
            let d: Decision = decide(k, y, n, &v.params, sec_into, false, Some(utc_hour), Some(lags));
            if !d.consumes_window() {
                continue;
            }
            done.insert(v.name.clone());
            let side = if d.buy_yes == Some(true) { "yes" } else { "no" };
            let mut record = Map::new();
            record.insert("variant".into(), v.name.clone().into());
            record.insert("action".into(), d.action.as_str().into());
            record.insert("reason".into(), d.reason.into());
            record.insert("t_ns".into(), recv_ns.into());
            record.insert("sec_into".into(), round_dp(sec_into, 3).into());
            record.insert("gap".into(), round_dp(d.gap.unwrap_or(0.0), 4).into());
            record.insert("side".into(), side.into());
            record.insert("entry_px".into(), d.entry_px.into());
            record.insert(
                "depth_at_limit".into(),
                d.entry_px.map(|px| round_dp(depth(d.buy_yes == Some(true), px), 2)).into(),
            );
            record.insert("kalshi".into(), quote_json(k));
            record.insert("poly_yes".into(), quote_json(y));
            record.insert("poly_no".into(), quote_json(n));
            self.fired.entry(key.clone()).or_default().insert(
                v.name.clone(),
                Fired {
                    record,
                    entry_px: d.entry_px,
                    side,
                    action: d.action.as_str(),
                    fee_rate: fee.0,
                    fee_exponent: fee.1,
                },
            );
        }
    }

    /// The window's `shadow` records, one per variant, at the roll.
    pub fn close(&mut self, key: &Key) -> Vec<Value> {
        if self.variants.is_empty() {
            return Vec::new();
        }
        self.done.remove(key);
        let fired = self.fired.get(key);
        self.variants
            .iter()
            .map(|v| {
                let mut out = Map::new();
                out.insert("type".into(), "shadow".into());
                out.insert("asset".into(), key.0.clone().into());
                out.insert("open_unix".into(), key.1.into());
                match fired.and_then(|f| f.get(&v.name)) {
                    Some(f) => out.extend(f.record.clone()),
                    None => {
                        out.insert("variant".into(), v.name.clone().into());
                        out.insert("action".into(), "none".into());
                    }
                }
                Value::Object(out)
            })
            .collect()
    }

    /// `shadow_settle` records for the window's consumed variants; forgets it.
    pub fn settle(&mut self, key: &Key, outcome: &str) -> Vec<Value> {
        let Some(fired) = self.fired.remove(key) else { return Vec::new() };
        fired
            .into_iter()
            .filter_map(|(name, f)| {
                let px = f.entry_px?;
                let win = if f.side == outcome { 1.0 } else { 0.0 };
                let net = 100.0 * (win - px - taker_fee(px, 1.0, f.fee_rate, f.fee_exponent));
                Some(json!({
                    "type": "shadow_settle",
                    "asset": key.0,
                    "open_unix": key.1,
                    "variant": name,
                    "action": f.action,
                    "side": f.side,
                    "entry_px": px,
                    "outcome": outcome,
                    "win": win,
                    "net_cents_touch": round_dp(net, 3),
                }))
            })
            .collect()
    }

    pub fn forget_before(&mut self, open_unix: i64) {
        self.fired.retain(|k, _| k.1 >= open_unix);
        self.done.retain(|k, _| k.1 >= open_unix);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_variants_parse() {
        let base = StrategyParams { gap_threshold: 0.15, ..Default::default() };
        let v = parse_variants(DEFAULT_VARIANTS, &base).unwrap();
        assert_eq!(v.len(), 17);
        assert_eq!(v[0].params.gap_threshold, 0.08);
        assert_eq!(v[13].params.skip_hours_utc, Some(US_NIGHT_UTC.to_vec()));
        assert_eq!(v[15].params.max_poly_lag_ms, Some(250.0));
        assert!(parse_variants(r#"[{"name":"live"}]"#, &base).is_err());
        assert!(parse_variants(r#"[{"name":"x","bogus":1}]"#, &base).is_err());
        assert!(parse_variants("  ", &base).unwrap().is_empty());
    }
}
