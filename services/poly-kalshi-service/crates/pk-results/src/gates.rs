//! The pre-registered gates, read from the journal (README §Gates).
//!
//! - `day_t`: day-t ≥ 2 at n ≥ 300 *filled* entries at or below the cap, net
//!   of fees, at the real fill (VWAP). Kill if not met.
//! - `markout_5s`: the median 5 s markout on fills is no worse than −3¢.
//! - `fill_price`: average and p90 fill at or below the cap.
//! - `capacity_usd_per_entry`: median $ filled per entry, per asset (an asset
//!   under $10 cannot carry live size, whatever its edge).
//!
//! Per entry, net ¢/share = payout/share − VWAP − fee/share. Day-t is the mean
//! of daily net ¢/share over its standard error.

use std::collections::BTreeMap;

use indexmap::IndexMap;
use serde_json::{Map, Value, json};

use crate::stats::{Key, f, fmean, i, key, median, opt, s, stdev, truthy, utc_day_s};

pub const BANDS: [(f64, f64); 8] =
    [(0.0, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0)];
pub const GATE_MIN_N: usize = 300;
pub const GATE_MIN_DAY_T: f64 = 2.0;
pub const GATE_MARKOUT_5S_CENTS: f64 = -3.0;
pub const GATE_MARKOUT_MIN_N: usize = 30;
pub const CAPACITY_MIN_USD: f64 = 10.0;

pub fn band_of(price: f64) -> String {
    for (lo, hi) in BANDS {
        if lo <= price && price < hi {
            return format!("[{lo:.1},{hi:.1})");
        }
    }
    "[0.9,1.0)".into()
}

/// The q-quantile as `ordered[ceil(q n) - 1]`.
fn pctl(values: &[f64], q: f64) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    let mut o = values.to_vec();
    o.sort_by(f64::total_cmp);
    let idx = ((q * o.len() as f64).ceil() as i64 - 1).clamp(0, o.len() as i64 - 1) as usize;
    Some(o[idx])
}

fn t_stat(values: &[f64]) -> Option<f64> {
    let sd = stdev(values)?;
    if sd == 0.0 {
        return None;
    }
    Some(fmean(values)? / (sd / (values.len() as f64).sqrt()))
}

pub fn evaluate(records: &[Value], max_entry_px: f64) -> Value {
    let mut fills: IndexMap<Key, &Value> = IndexMap::new();
    let mut settles: IndexMap<Key, &Value> = IndexMap::new();
    let mut decisions: IndexMap<String, u64> = IndexMap::new();
    let mut no_fills = 0u64;
    let mut resolutions: IndexMap<Key, BTreeMap<String, String>> = IndexMap::new();
    let mut windows: BTreeMap<(String, String), u64> = BTreeMap::new();
    let mut markouts_5s: Vec<f64> = Vec::new();
    for r in records {
        let k = key(r);
        match s(r, "type") {
            Some("decision") => {
                let label = if truthy(r, "blocked") {
                    format!("blocked_{}", s(r, "blocked").unwrap_or_default())
                } else {
                    s(r, "action").unwrap_or_default().to_owned()
                };
                *decisions.entry(label).or_default() += 1;
            }
            Some("fill") => {
                if f(r, "shares").unwrap_or(0.0) > 0.0 {
                    fills.insert(k, r);
                } else {
                    no_fills += 1;
                }
            }
            Some("settle") => {
                settles.insert(k, r);
            }
            Some("resolution") => {
                resolutions.entry(k).or_default().insert(
                    s(r, "venue").unwrap_or_default().into(),
                    s(r, "outcome").unwrap_or_default().into(),
                );
            }
            Some("window") => {
                let day = utc_day_s(i(r, "open_unix").unwrap_or(0));
                *windows.entry((s(r, "asset").unwrap_or_default().into(), day)).or_default() += 1;
            }
            Some("markout") if truthy(r, "filled") => {
                if let Some(m) = f(r, "markout_5s") {
                    markouts_5s.push(m);
                }
            }
            _ => {}
        }
    }
    let vwaps: Vec<f64> = fills.values().filter_map(|r| f(r, "vwap")).collect();
    let mut settled: Vec<(Key, &Value, &Value, f64)> = Vec::new();
    for (k, s_) in &settles {
        let Some(fl) = fills.get(k) else { continue };
        let per_share = 100.0
            * ((f(s_, "payout").unwrap_or(0.0) - f(fl, "cost").unwrap_or(0.0) - f(fl, "fee").unwrap_or(0.0))
                / f(fl, "shares").unwrap_or(1.0));
        settled.push((k.clone(), fl, s_, per_share));
    }
    let mut bands: BTreeMap<String, Vec<f64>> = BTreeMap::new();
    let mut daily: BTreeMap<String, Vec<f64>> = BTreeMap::new();
    for (k, fl, _s, cents) in &settled {
        let vwap = f(fl, "vwap").unwrap_or(0.0);
        bands.entry(band_of(vwap)).or_default().push(*cents);
        if vwap <= max_entry_px {
            daily.entry(utc_day_s(k.1.unwrap_or(0))).or_default().push(*cents);
        }
    }
    let day_means: Vec<f64> = daily.values().filter_map(|v| fmean(v)).collect();
    let in_band: Vec<f64> = settled
        .iter()
        .filter(|(_, fl, _, _)| f(fl, "vwap").unwrap_or(0.0) <= max_entry_px)
        .map(|t| t.3)
        .collect();
    let pnl: Vec<f64> = settled.iter().map(|(_, _, s_, _)| f(s_, "pnl").unwrap_or(0.0)).collect();
    let slip: Vec<f64> =
        fills.values().map(|fl| f(fl, "vwap").unwrap_or(0.0) - f(fl, "touch_px").unwrap_or(0.0)).collect();
    let fill_ratio: Vec<f64> = fills
        .values()
        .filter(|fl| truthy(fl, "ordered"))
        .map(|fl| f(fl, "shares").unwrap_or(0.0) / f(fl, "ordered").unwrap_or(1.0))
        .collect();
    let both: Vec<&BTreeMap<String, String>> =
        resolutions.values().filter(|v| v.contains_key("poly") && v.contains_key("kalshi")).collect();
    let disagree = both.iter().filter(|v| v["poly"] != v["kalshi"]).count();
    let day_t = t_stat(&day_means);
    let avg_vwap = fmean(&vwaps);
    let p90 = pctl(&vwaps, 0.9);
    let markout_med = median(&markouts_5s);
    let mut by_asset: BTreeMap<String, Vec<f64>> = BTreeMap::new();
    for (k, fl) in &fills {
        by_asset.entry(k.0.clone().unwrap_or_default()).or_default().push(f(fl, "cost").unwrap_or(0.0));
    }
    let capacity: BTreeMap<String, f64> =
        by_asset.iter().filter_map(|(a, v)| Some((a.clone(), median(v)?))).collect();
    let gates = json!({
        "fill_price": avg_vwap.map(|a| a <= max_entry_px && p90.unwrap_or(0.0) <= max_entry_px),
        "day_t": if in_band.len() < GATE_MIN_N { None } else { day_t.map(|t| t >= GATE_MIN_DAY_T) },
        "markout_5s": if markouts_5s.len() < GATE_MARKOUT_MIN_N { None } else { markout_med.map(|m| m >= GATE_MARKOUT_5S_CENTS) },
    });
    let mut band_out = Map::new();
    for (lo, _) in BANDS {
        let b = band_of(lo);
        let v = bands.get(&b).cloned().unwrap_or_default();
        band_out.insert(b, json!({"n": v.len(), "net_cents": opt(fmean(&v))}));
    }
    let n_fills = fills.len() as u64;
    json!({
        "decisions": decisions,
        "fills": n_fills,
        "no_fills": no_fills,
        "fill_rate": if n_fills + no_fills == 0 { None } else { Some(n_fills as f64 / (n_fills + no_fills) as f64) },
        "avg_fill_ratio": opt(fmean(&fill_ratio)),
        "vwap": {"avg": opt(avg_vwap), "p10": opt(pctl(&vwaps, 0.1)), "p50": opt(pctl(&vwaps, 0.5)), "p90": opt(p90)},
        "slippage_vs_touch": opt(fmean(&slip)),
        "settled": settled.len(),
        "net_cents_per_share": opt(fmean(&settled.iter().map(|t| t.3).collect::<Vec<_>>())),
        "dollars_per_entry": opt(fmean(&pnl)),
        "markout_5s_median": opt(markout_med),
        "markout_5s_n": markouts_5s.len(),
        "capacity_usd_per_entry": capacity,
        "capacity_below_min": capacity.iter().filter(|(_, v)| **v < CAPACITY_MIN_USD).map(|(a, _)| a.clone()).collect::<Vec<_>>(),
        "pnl_total": pnl.iter().sum::<f64>(),
        "bands": band_out,
        "in_band_n": in_band.len(),
        "days": day_means.len(),
        "days_positive": day_means.iter().filter(|m| **m > 0.0).count(),
        "day_t": opt(day_t),
        "venue_disagreement": if both.is_empty() { None } else { Some(disagree as f64 / both.len() as f64) },
        "resolved_windows": both.len(),
        "windows_per_asset_day": windows.iter().map(|((a, d), n)| (format!("{a} {d}"), *n)).collect::<BTreeMap<_, _>>(),
        "gates": gates,
        "gate_min_n": GATE_MIN_N,
    })
}
