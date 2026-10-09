//! PLAN 3.3 on the Helix tape: the sweeps, the null battery and the price-side latency curve.
//!
//! ```text
//! pk helix sweeps --split discovery   # parameters are chosen here
//! pk helix sweeps --split earlier     # the spot regime: reported only
//! pk helix sweeps --split holdout     # refuses until research/PREREG.md exists
//! ```
//!
//! Reads `research/data/helix/<day>/` ([`super::replay`]), the venue
//! resolutions and the inventory's day roles; writes `helix_sweeps_<split>.json`.
//! Everything is net of the fee and settled on Polymarket's own outcome.
//!
//! - `variants`: per variant, entries, win rate, average price, net ¢/share at
//!   the touch with its s.e., day-t, days and assets positive, per asset.
//! - `thresholds`: how often each uncensored first crossing fires.
//! - `nulls`: the null battery on the base rule.
//! - `latency`: per latency and limit offset, the share of entries whose cheap
//!   ask was still at or below the limit, the net there, and the trade-through proxy.
//! - `spreads`, `sizing`, and `regime` (epoch 4's questions A1–A3 and A5).
//!
//! Means follow pandas: `groupby(...).mean()` sums with Kahan compensation
//! ([`group_mean`]), a column's `.mean()` (also inside `for k, g in groupby`)
//! sums pairwise ([`np_mean`]).

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};

use pk_core::fees::{DEFAULT_EXPONENT, DEFAULT_RATE, taker_fee};
use pk_core::num::round_dp;
use pk_core::shadow::US_NIGHT_UTC;
use pk_results::stats::{fmean, stdev};
use serde_json::{Map, Value, json};

use super::pd::{
    SPREAD_EDGES, SPREAD_LABELS, cut, group_by, group_mean, kahan_sum, median, np_mean, np_std, np_sum,
    quantile,
};
use crate::frame::Frame;
use crate::nulls::{Settled, arms, summarise};

pub const LATENCIES_MS: [i64; 7] = [0, 25, 50, 100, 150, 250, 400];
pub const OFFSETS_C: [i64; 4] = [0, 1, 2, 3];
const EPOCH4_NET_CENTS: f64 = -31.0;

fn fee(p: f64) -> f64 {
    taker_fee(p, 1.0, DEFAULT_RATE, DEFAULT_EXPONENT)
}

fn r(x: f64, n: usize) -> Value {
    if x.is_nan() { Value::Null } else { round_dp(x, n).into() }
}

fn ro(x: Option<f64>, n: usize) -> Value {
    x.map(|x| r(x, n)).unwrap_or(Value::Null)
}

/// `statistics`' t of a mean against zero, None under two values or no spread.
fn t_stat(v: &[f64]) -> Option<f64> {
    if v.len() < 2 {
        return None;
    }
    let sd = stdev(v)?;
    (sd != 0.0).then(|| fmean(v).expect("non-empty") / (sd / (v.len() as f64).sqrt()))
}

/// One settled crossing.
#[derive(Clone, Debug)]
pub struct Cross {
    pub variant: String,
    pub asset: String,
    pub open_unix: i64,
    pub t_ns: i64,
    pub day: String,
    pub side: String,
    pub enter: bool,
    pub entry_px: f64,
    pub gap: f64,
    pub touch_size: f64,
    pub k_bid: f64,
    pub k_ask: f64,
    pub y_bid: f64,
    pub y_ask: f64,
    pub n_bid: f64,
    pub n_ask: f64,
    pub win: f64,
    pub net: f64,
}

/// The windows' uncensored first crossings, over all days.
#[derive(Default)]
pub struct Windows {
    pub n: usize,
    /// `first_cross_s_<t>` columns in file order, NaN where it did not fire.
    pub first_cross: indexmap::IndexMap<String, Vec<f64>>,
    pub max_abs_gap_full: Vec<f64>,
}

/// The venue resolutions: (asset, open) → (day, Polymarket's outcome).
pub type Res = Vec<(String, i64, String, Option<String>)>;

pub fn load_res(p: &Path) -> anyhow::Result<Res> {
    let f = Frame::read_parquet(p)?;
    let (a, o, d) = (f.col("asset"), f.col("open_unix"), f.col("day"));
    let out = f.cols.get("poly_outcome");
    Ok((0..f.len())
        .map(|i| {
            (
                a.str(i).unwrap_or_default().to_owned(),
                o.i64(i).unwrap_or(0),
                d.str(i).unwrap_or_default().to_owned(),
                out.and_then(|c| c.str(i)).map(str::to_owned),
            )
        })
        .collect())
}

/// `crossings` settled against `res` (an inner join, crossings' order kept),
/// and the windows, over the days that have a replay.
pub fn load(data: &Path, days: &[String], res: &Res) -> anyhow::Result<(Vec<Cross>, Windows)> {
    let outcome: HashMap<(&str, i64), &Option<String>> =
        res.iter().map(|(a, o, _, p)| ((a.as_str(), *o), p)).collect();
    let mut cross = Vec::new();
    let mut win = Windows::default();
    for day in days {
        let d = data.join(day);
        if !d.join("summary.json").is_file() {
            continue;
        }
        let c = Frame::read_parquet(&d.join("crossings.parquet"))?;
        let f = |name: &str, i: usize| c.cols.get(name).and_then(|col| col.f64(i)).unwrap_or(f64::NAN);
        let s =
            |name: &str, i: usize| c.cols.get(name).and_then(|col| col.str(i)).unwrap_or_default().to_owned();
        for i in 0..c.len() {
            let (asset, open) = (s("asset", i), c.col("open_unix").i64(i).unwrap_or(0));
            let Some(Some(out)) = outcome.get(&(asset.as_str(), open)) else { continue };
            let side = s("side", i);
            let entry_px = f("entry_px", i);
            let w = if side == *out { 1.0 } else { 0.0 };
            cross.push(Cross {
                variant: s("variant", i),
                asset,
                open_unix: open,
                t_ns: c.col("t_ns").i64(i).unwrap_or(0),
                day: day.clone(),
                side,
                enter: c.col("enter").f64(i) == Some(1.0),
                entry_px,
                gap: f("gap", i),
                touch_size: f("touch_size", i),
                k_bid: f("k_bid", i),
                k_ask: f("k_ask", i),
                y_bid: f("y_bid", i),
                y_ask: f("y_ask", i),
                n_bid: f("n_bid", i),
                n_ask: f("n_ask", i),
                win: w,
                net: 100.0 * (w - entry_px - fee(entry_px)),
            });
        }
        let w = Frame::read_parquet(&d.join("windows.parquet"))?;
        win.n += w.len();
        for (name, col) in &w.cols {
            if name.starts_with("first_cross_s_") {
                win.first_cross
                    .entry(name.clone())
                    .or_default()
                    .extend((0..w.len()).map(|i| col.f64(i).unwrap_or(f64::NAN)));
            }
        }
        if let Some(col) = w.cols.get("max_abs_gap_full") {
            win.max_abs_gap_full.extend((0..w.len()).filter_map(|i| col.f64(i)).filter(|x| !x.is_nan()));
        }
    }
    Ok((cross, win))
}

/// Per-group statistics of entries: `_stats` in the Python.
pub fn stats(rows: &[&Cross], days: usize) -> Value {
    if rows.is_empty() {
        return json!({"n": 0});
    }
    let col = |f: fn(&Cross) -> f64| rows.iter().map(|x| f(x)).collect::<Vec<f64>>();
    let (net, win, px) = (col(|x| x.net), col(|x| x.win), col(|x| x.entry_px));
    let daily: Vec<f64> =
        group_by(rows.iter().map(|x| (x.day.as_str(), x.net))).values().map(|v| group_mean(v)).collect();
    let by_asset = group_by(rows.iter().map(|x| (x.asset.as_str(), *x)));
    let touch = |rs: &[&Cross]| median(&rs.iter().map(|x| x.touch_size * x.entry_px).collect::<Vec<_>>());
    let n = rows.len();
    let mean = |v: &[f64]| np_mean(v).unwrap_or(f64::NAN);
    json!({
        "n": n,
        "per_day": round_dp(n as f64 / days.max(1) as f64, 1),
        "win_rate": r(mean(&win), 4),
        "avg_px": r(mean(&px), 4),
        "edge_pp": r(100.0 * (mean(&win) - mean(&px)), 2),
        "net_cents": r(mean(&net), 2),
        "se": if n > 1 { ro(np_std(&net).map(|s| s / (n as f64).sqrt()), 2) } else { Value::Null },
        "day_t": ro(t_stat(&daily), 2),
        "days_positive": format!("{}/{}", daily.iter().filter(|d| **d > 0.0).count(), daily.len()),
        "assets_positive": format!(
            "{}/{}",
            by_asset.values().filter(|g| group_mean(&g.iter().map(|x| x.net).collect::<Vec<_>>()) > 0.0).count(),
            by_asset.len()
        ),
        "touch_usd_median": ro(touch(rows), 2),
        "by_asset": by_asset.iter().map(|(a, g)| {
            let net: Vec<f64> = g.iter().map(|x| x.net).collect();
            let win: Vec<f64> = g.iter().map(|x| x.win).collect();
            ((*a).to_owned(), json!({
                "n": g.len(),
                "net_cents": r(np_mean(&net).unwrap_or(f64::NAN), 2),
                "win_rate": r(np_mean(&win).unwrap_or(f64::NAN), 3),
                "touch_usd_median": ro(touch(g), 2),
            }))
        }).collect::<Map<_, _>>(),
    })
}

pub fn variants(cross: &[Cross], days: usize) -> Value {
    let entries = group_by(cross.iter().filter(|x| x.enter).map(|x| (x.variant.as_str(), x)));
    let mut out = Map::new();
    for (name, g) in entries {
        let mut v = stats(&g, days);
        let all = cross.iter().filter(|x| x.variant == name).count();
        v["crossings"] = all.into();
        v["skips"] = cross.iter().filter(|x| x.variant == name && !x.enter).count().into();
        out.insert(name.to_owned(), v);
    }
    Value::Object(out)
}

pub fn thresholds(w: &Windows) -> Value {
    let mut out = Map::new();
    out.insert("windows".into(), w.n.into());
    for (col, v) in &w.first_cross {
        let key = format!("0.{}", col.rsplit('_').next().unwrap_or_default());
        let fired: Vec<f64> = v.iter().copied().filter(|x| !x.is_nan()).collect();
        out.insert(
            key,
            json!({
                "fires_share": r(fired.len() as f64 / v.len() as f64, 4),
                "median_first_s": ro(median(&fired), 1),
            }),
        );
    }
    out.insert(
        "max_abs_gap_full_quantiles".into(),
        [10, 25, 50, 75, 90, 99]
            .iter()
            .map(|q| (format!("p{q}"), ro(quantile(&w.max_abs_gap_full, *q as f64 / 100.0), 3)))
            .collect::<Map<_, _>>()
            .into(),
    );
    Value::Object(out)
}

/// The null battery on the base rule, per day on its grid, merged.
pub fn nulls(data: &Path, days: &[String], cross: &[Cross], res: &Res) -> anyhow::Result<Value> {
    let mut merged: BTreeMap<&'static str, Vec<Settled>> = BTreeMap::new();
    for day in days {
        let gpath = data.join(day).join("grid.parquet");
        if !gpath.is_file() {
            continue;
        }
        let grid = Frame::read_parquet(&gpath)?;
        let mut journal: Vec<Value> = cross
            .iter()
            .filter(|x| x.variant == "base" && x.day == *day)
            .map(|x| {
                json!({"type": "decision", "asset": x.asset, "open_unix": x.open_unix, "side": x.side,
                       "entry_px": x.entry_px})
            })
            .collect();
        journal.extend(res.iter().filter(|(_, _, d, p)| d == day && p.as_deref().is_some_and(|p| !p.is_empty())).map(
            |(a, o, _, p)| json!({"type": "resolution", "venue": "poly", "asset": a, "open_unix": o, "outcome": p}),
        ));
        let seed: u64 = day.parse().unwrap_or(7);
        for (name, book) in arms(&grid, &journal, 0.60, 20, 60, seed) {
            merged.entry(name).or_default().extend(book);
        }
    }
    if merged.is_empty() {
        return Ok(json!({}));
    }
    let mut out = summarise(&merged);
    let (s, n) = (&merged["signal"], &merged["random_same_rule"]);
    let m = |v: &[Settled], f: fn(&Settled) -> f64| {
        np_mean(&v.iter().map(f).collect::<Vec<_>>()).unwrap_or(f64::NAN)
    };
    let excess = 100.0 * (m(s, |x| x.win) - m(s, |x| x.px)) - 100.0 * (m(n, |x| x.win) - m(n, |x| x.px));
    let var = |v: &[Settled]| super::pd::np_var(&v.iter().map(|x| x.gross_cents).collect::<Vec<_>>(), 1);
    let se = match (var(s), var(n)) {
        (Some(a), Some(b)) => (a / s.len() as f64 + b / n.len() as f64).sqrt(),
        _ => f64::NAN,
    };
    out["signal_minus_random_same_rule_pp"] = json!({"excess": r(excess, 2), "se": r(se, 2)});
    Ok(out)
}

/// One entry's cheap leg after each latency.
#[derive(Clone, Debug)]
struct LatRow {
    variant: String,
    asset: String,
    day: String,
    win: f64,
    px: f64,
    ask: [f64; 7],
    asz: [f64; 7],
    thru: [bool; 7],
}

type SliceKey = (String, i64, i64, String);

fn latency_rows(data: &Path, days: &[String], entries: &[&Cross]) -> anyhow::Result<Vec<LatRow>> {
    let mut rows = Vec::new();
    for day in days {
        let spath = data.join(day).join("slices.parquet");
        if !spath.is_file() {
            continue;
        }
        let sl = Frame::read_parquet(&spath)?;
        // (dt_ms, ask, asz, trade_px) per (asset, open, t_cross, kind), dt ≥ 0, by dt.
        let mut legs: HashMap<SliceKey, Vec<(f64, f64, f64, f64)>> = HashMap::new();
        let g = |c: &str, i: usize| sl.cols.get(c).and_then(|col| col.f64(i)).unwrap_or(f64::NAN);
        for i in 0..sl.len() {
            let dt = g("dt_ms", i);
            if dt.is_nan() || dt < 0.0 {
                continue;
            }
            let key = (
                sl.col("asset").str(i).unwrap_or_default().to_owned(),
                sl.col("open_unix").i64(i).unwrap_or(0),
                sl.col("t_cross").i64(i).unwrap_or(0),
                sl.col("kind").str(i).unwrap_or_default().to_owned(),
            );
            legs.entry(key).or_default().push((dt, g("ask", i), g("asz", i), g("trade_px", i)));
        }
        for v in legs.values_mut() {
            v.sort_by(|a, b| a.0.total_cmp(&b.0));
        }
        for x in entries.iter().filter(|x| x.day == *day) {
            let leg = if x.side == "yes" { "y" } else { "n" };
            let k = |kind: String| (x.asset.clone(), x.open_unix, x.t_ns, kind);
            let bbo = legs.get(&k(format!("bbo_{leg}")));
            let trades = legs.get(&k(format!("trade_{leg}")));
            let mut row = LatRow {
                variant: x.variant.clone(),
                asset: x.asset.clone(),
                day: day.clone(),
                win: x.win,
                px: x.entry_px,
                ask: [x.entry_px; 7],
                asz: [x.touch_size; 7],
                thru: [false; 7],
            };
            for (j, lat) in LATENCIES_MS.iter().enumerate() {
                let lat = *lat as f64;
                if let Some(b) = bbo
                    && let Some(i) = b.partition_point(|r| r.0 <= lat).checked_sub(1)
                {
                    row.ask[j] = b[i].1;
                    row.asz[j] = b[i].2;
                }
                row.thru[j] =
                    trades.is_some_and(|t| t.iter().any(|r| r.0 <= lat && r.3 >= x.entry_px - 1e-9));
            }
            rows.push(row);
        }
    }
    Ok(rows)
}

/// The order (limit = touch + `off` ¢, at most the cap) against the ask after
/// `lat`: whether it filled, and the net ¢/share at that ask (None if not).
fn fill(x: &LatRow, j: usize, off: i64) -> (bool, Option<f64>) {
    let limit = (x.px + off as f64 / 100.0).min(0.60);
    let ask = x.ask[j];
    let ok = ask > 0.0 && ask <= limit + 1e-9;
    (ok, ok.then(|| 100.0 * (x.win - ask - fee(ask))))
}

fn lat_index(lat: i64) -> usize {
    LATENCIES_MS.iter().position(|l| *l == lat).expect("listed latency")
}

pub fn latency(data: &Path, days: &[String], entries: &[&Cross]) -> anyhow::Result<Value> {
    let df = latency_rows(data, days, entries)?;
    if df.is_empty() {
        return Ok(json!({}));
    }
    let base: Vec<&LatRow> = df.iter().filter(|x| x.variant == "base").collect();
    let touch_net: Vec<f64> = base.iter().map(|x| 100.0 * (x.win - x.px - fee(x.px))).collect();
    let mut out = Map::new();
    out.insert("entries".into(), base.len().into());
    let mean = |v: &[f64]| np_mean(v).unwrap_or(f64::NAN);
    let assets: BTreeSet<&str> = base.iter().map(|x| x.asset.as_str()).collect();
    for lat in LATENCIES_MS {
        let j = lat_index(lat);
        for off in OFFSETS_C {
            let cells: Vec<(bool, Option<f64>)> = base.iter().map(|x| fill(x, j, off)).collect();
            let ok: Vec<f64> = cells.iter().map(|c| if c.0 { 1.0 } else { 0.0 }).collect();
            let filled: Vec<f64> = cells.iter().filter_map(|c| c.1).collect();
            let any = cells.iter().any(|c| c.0);
            let all = cells.iter().all(|c| c.0);
            let missed: Vec<f64> =
                cells.iter().zip(&touch_net).filter(|(c, _)| !c.0).map(|(_, t)| *t).collect();
            let per_entry: Vec<f64> = cells.iter().map(|c| c.1.unwrap_or(0.0)).collect();
            let usd: Vec<f64> =
                base.iter().zip(&cells).filter(|(_, c)| c.0).map(|(x, _)| x.asz[j] * x.ask[j]).collect();
            out.insert(
                format!("{lat}ms+{off}c"),
                json!({
                    "fill_share": r(mean(&ok), 4),
                    "net_cents_filled": if any { r(mean(&filled), 2) } else { Value::Null },
                    "net_cents_missed_at_touch": if all { Value::Null } else { r(mean(&missed), 2) },
                    "net_cents_per_entry": r(mean(&per_entry), 2),
                    "touch_usd_median": if any { ro(median(&usd), 2) } else { Value::Null },
                    "by_asset_fill_share": assets.iter().map(|a| {
                        let v: Vec<f64> = base.iter().zip(&ok).filter(|(x, _)| x.asset == *a).map(|(_, o)| *o).collect();
                        ((*a).to_owned(), r(mean(&v), 3))
                    }).collect::<Map<_, _>>(),
                }),
            );
        }
    }
    for lat in LATENCIES_MS {
        let j = lat_index(lat);
        let t: Vec<f64> = base.iter().map(|x| if x.thru[j] { 1.0 } else { 0.0 }).collect();
        let w =
            |through: bool| base.iter().filter(|x| x.thru[j] == through).map(|x| x.win).collect::<Vec<_>>();
        let (any, all) = (base.iter().any(|x| x.thru[j]), base.iter().all(|x| x.thru[j]));
        out.insert(
            format!("trade_through_{lat}ms"),
            json!({
                "share": r(mean(&t), 4),
                "win_rate_if_through": if any { r(mean(&w(true)), 4) } else { Value::Null },
                "win_rate_if_not": if all { Value::Null } else { r(mean(&w(false)), 4) },
            }),
        );
    }
    let ndays = base.iter().map(|x| x.day.as_str()).collect::<BTreeSet<_>>().len().max(1);
    let j150 = lat_index(150);
    let mut per_variant = Map::new();
    for (name, g) in group_by(df.iter().map(|x| (x.variant.as_str(), x))) {
        let mut cells = Map::new();
        for off in [0, 2] {
            let c: Vec<(bool, Option<f64>)> = g.iter().map(|x| fill(x, j150, off)).collect();
            let per_entry = mean(&c.iter().map(|c| c.1.unwrap_or(0.0)).collect::<Vec<_>>());
            let filled: Vec<f64> = c.iter().filter_map(|c| c.1).collect();
            cells.insert(
                format!("150ms+{off}c"),
                json!({
                    "fill_share": r(mean(&c.iter().map(|c| if c.0 { 1.0 } else { 0.0 }).collect::<Vec<_>>()), 4),
                    "net_cents_filled": if filled.is_empty() { Value::Null } else { r(mean(&filled), 2) },
                    "net_cents_per_entry": r(per_entry, 2),
                    "net_cents_per_day": r(per_entry * g.len() as f64 / ndays as f64, 0),
                }),
            );
        }
        per_variant.insert(name.to_owned(), Value::Object(cells));
    }
    out.insert("variants_150ms".into(), Value::Object(per_variant));
    out.insert("prereg".into(), prereg_comparisons(&df));
    Ok(Value::Object(out))
}

/// Day-by-day difference a − b on the days both have: mean, day-t, days positive.
fn paired(a: &BTreeMap<String, f64>, b: &BTreeMap<String, f64>) -> Map<String, Value> {
    let d: Vec<f64> = a.iter().filter_map(|(day, x)| b.get(day).map(|y| x - y)).collect();
    let mut m = Map::new();
    m.insert("mean_daily_diff".into(), r(np_mean(&d).unwrap_or(f64::NAN), 2));
    m.insert("day_t".into(), ro(t_stat(&d), 2));
    m.insert(
        "days_positive".into(),
        format!("{}/{}", d.iter().filter(|x| **x > 0.0).count(), d.len()).into(),
    );
    m
}

/// research/PREREG.md C1 and C2, at 150 ms, price side; an entry the limit misses is 0.
fn prereg_comparisons(df: &[LatRow]) -> Value {
    let j = lat_index(150);
    let per_entry = |variant: &str, off: i64| -> Vec<(String, f64)> {
        df.iter()
            .filter(|x| x.variant == variant)
            .map(|x| (x.day.clone(), fill(x, j, off).1.unwrap_or(0.0)))
            .collect()
    };
    let by_day = |v: &[(String, f64)], f: fn(&[f64]) -> f64| -> BTreeMap<String, f64> {
        group_by(v.iter().map(|(d, x)| (d.clone(), *x))).into_iter().map(|(d, xs)| (d, f(&xs))).collect()
    };
    let (base0, base2, t15) = (per_entry("base", 0), per_entry("base", 2), per_entry("t15", 0));
    let all = |v: &[(String, f64)]| v.iter().map(|x| x.1).collect::<Vec<_>>();
    let mut c1 = paired(&by_day(&base2, group_mean), &by_day(&base0, group_mean));
    c1.insert("per_entry_plus2c".into(), r(np_mean(&all(&base2)).unwrap_or(f64::NAN), 2));
    c1.insert("per_entry_touch".into(), r(np_mean(&all(&base0)).unwrap_or(f64::NAN), 2));
    let (s15, s0) = (by_day(&t15, kahan_sum), by_day(&base0, kahan_sum));
    let mut c2 = paired(&s15, &s0);
    let daily_mean =
        |m: &BTreeMap<String, f64>| np_mean(&m.values().copied().collect::<Vec<_>>()).unwrap_or(f64::NAN);
    c2.insert("per_day_t15".into(), r(daily_mean(&s15), 0));
    c2.insert("per_day_base".into(), r(daily_mean(&s0), 0));
    json!({"C1_offset_plus2c_vs_touch": c1, "C2_t15_vs_t10_per_day": c2})
}

pub fn spreads(entries: &[&Cross]) -> Value {
    let base: Vec<&&Cross> = entries.iter().filter(|x| x.variant == "base").collect();
    let p_spread = |x: &Cross| if x.side == "yes" { x.y_ask - x.y_bid } else { x.n_ask - x.n_bid };
    let k_spread = |x: &Cross| x.k_ask - x.k_bid;
    let mut out = Map::new();
    for (name, f) in [("p_spread", &p_spread as &dyn Fn(&Cross) -> f64), ("k_spread", &k_spread)] {
        let groups = group_by(base.iter().filter_map(|x| cut(f(x), &SPREAD_EDGES).map(|b| (b, x.net))));
        out.insert(
            name.into(),
            groups
                .into_iter()
                .map(|(b, v)| {
                    (
                        SPREAD_LABELS[b].to_owned(),
                        json!({"n": v.len(), "net_cents": r(np_mean(&v).unwrap_or(f64::NAN), 2)}),
                    )
                })
                .collect::<Map<_, _>>()
                .into(),
        );
    }
    Value::Object(out)
}

pub fn sizing(entries: &[&Cross]) -> Value {
    let base: Vec<&&Cross> = entries.iter().filter(|x| x.variant == "base").collect();
    type Shares<'a> = &'a dyn Fn(&Cross) -> f64;
    let plans: [(&str, Shares); 3] = [
        ("notional_50", &|x| (50.0 / x.entry_px).floor()),
        ("fixed_100", &|_| 100.0),
        ("min_50_touch", &|x| (50.0 / x.entry_px).floor().min(x.touch_size)),
    ];
    let mut out = Map::new();
    for (name, shares) in plans {
        let pnl: Vec<f64> = base.iter().map(|x| x.net / 100.0 * shares(x)).collect();
        let daily: Vec<f64> = group_by(base.iter().zip(&pnl).map(|(x, p)| (x.day.as_str(), *p)))
            .values()
            .map(|v| kahan_sum(v))
            .collect();
        let top = group_by(base.iter().zip(&pnl).map(|(x, p)| (x.asset.as_str(), *p)));
        out.insert(
            name.into(),
            json!({
                "total_usd": r(np_sum(&pnl), 2),
                "daily_mean": r(np_mean(&daily).unwrap_or(f64::NAN), 2),
                "daily_sd": r(np_std(&daily).unwrap_or(f64::NAN), 2),
                "by_asset_usd": top.iter().map(|(a, v)| ((*a).to_owned(), r(kahan_sum(v), 2))).collect::<Map<_, _>>(),
            }),
        );
    }
    Value::Object(out)
}

/// The s.e. of the mean net with entries clustered by window start (all assets).
fn cluster_se(rows: &[&Cross]) -> Option<f64> {
    let windows = group_by(rows.iter().map(|x| (x.open_unix, x.net)));
    let c = windows.len();
    if c < 2 {
        return None;
    }
    let m = np_mean(&rows.iter().map(|x| x.net).collect::<Vec<_>>())?;
    let sums: Vec<f64> =
        windows.values().map(|v| kahan_sum(&v.iter().map(|x| x - m).collect::<Vec<_>>())).collect();
    let sq: Vec<f64> = sums.iter().map(|s| s * s).collect();
    Some((c as f64 / (c - 1) as f64 * np_sum(&sq)).sqrt() / rows.len() as f64)
}

pub fn regime(entries: &[&Cross], days: usize) -> Value {
    let base: Vec<&Cross> = entries.iter().filter(|x| x.variant == "base").copied().collect();
    if base.is_empty() {
        return json!({});
    }
    let hours = group_by(base.iter().map(|x| (x.open_unix.div_euclid(3600), *x)));
    let busy: Vec<(usize, f64, f64)> = hours
        .values()
        .filter(|g| g.len() >= 10)
        .map(|g| {
            let wins = kahan_sum(&g.iter().map(|x| x.win).collect::<Vec<_>>());
            (g.len(), wins, group_mean(&g.iter().map(|x| x.net).collect::<Vec<_>>()))
        })
        .collect();
    let share = |f: &dyn Fn(&(usize, f64, f64)) -> bool| {
        r(
            np_mean(&busy.iter().map(|b| if f(b) { 1.0 } else { 0.0 }).collect::<Vec<_>>())
                .unwrap_or(f64::NAN),
            4,
        )
    };
    let nets: Vec<f64> = busy.iter().map(|b| b.2).collect();
    let bad_hours = json!({
        "hours_with_10_plus": busy.len(),
        "share_zero_wins": if busy.is_empty() { Value::Null } else { share(&|b| b.1 == 0.0) },
        "share_net_at_or_below_epoch4": if busy.is_empty() { Value::Null } else { share(&|b| b.2 <= EPOCH4_NET_CENTS) },
        "hour_net_quantiles": if busy.is_empty() { json!({}) } else {
            [1, 5, 10, 50].iter().map(|q| (format!("p{q}"), ro(quantile(&nets, *q as f64 / 100.0), 1)))
                .collect::<Map<_, _>>().into()
        },
    });
    let by_hour: Map<String, Value> = group_by(
        base.iter().map(|x| (x.open_unix.div_euclid(3600).rem_euclid(24), *x)),
    )
    .into_iter()
    .map(|(h, g)| {
        (
            h.to_string(),
            json!({
                "n": g.len(),
                "win_rate": r(np_mean(&g.iter().map(|x| x.win).collect::<Vec<_>>()).unwrap_or(f64::NAN), 3),
                "net_cents": r(np_mean(&g.iter().map(|x| x.net).collect::<Vec<_>>()).unwrap_or(f64::NAN), 2),
                "k_spread_median": ro(median(&g.iter().map(|x| x.k_ask - x.k_bid).collect::<Vec<_>>()), 3),
            }),
        )
    })
    .collect();
    let night = |x: &Cross| US_NIGHT_UTC.contains(&(x.open_unix.div_euclid(3600).rem_euclid(24) as u32));
    let k_ext = |x: &Cross| ((x.k_bid + x.k_ask) / 2.0 - 0.5).abs() > ((x.y_bid + x.y_ask) / 2.0 - 0.5).abs();
    let pick = |f: &dyn Fn(&Cross) -> bool| base.iter().filter(|x| f(x)).copied().collect::<Vec<_>>();
    // One entry per window (all assets): the largest |gap|, the first on a tie.
    let mut first: BTreeMap<i64, &Cross> = BTreeMap::new();
    for x in &base {
        match first.get(&x.open_unix) {
            Some(b) if x.gap.abs() <= b.gap.abs() => {}
            _ => {
                first.insert(x.open_unix, x);
            }
        }
    }
    let first: Vec<&Cross> = first.into_values().collect();
    let per_window: Vec<f64> =
        group_by(base.iter().map(|x| (x.open_unix, 1.0))).values().map(|v| v.len() as f64).collect();
    let all_stats = stats(&base, days);
    json!({
        "bad_hours": bad_hours,
        "by_hour_utc": by_hour,
        "us_night": stats(&pick(&night), days),
        "us_day": stats(&pick(&|x| !night(x)), days),
        "kalshi_extreme": {
            "kalshi_more_extreme": stats(&pick(&k_ext), days),
            "kalshi_less_extreme": stats(&pick(&|x| !k_ext(x)), days),
            "floor_0_15": stats(&pick(&|x| x.entry_px >= 0.15), days),
            "kext_and_floor": stats(&pick(&|x| k_ext(x) && x.entry_px >= 0.15), days),
        },
        "clustering": {
            "windows": per_window.len(),
            "entries_per_window_mean": r(np_mean(&per_window).unwrap_or(f64::NAN), 2),
            "share_windows_3_plus": r(np_mean(&per_window.iter().map(|n| if *n >= 3.0 { 1.0 } else { 0.0 }).collect::<Vec<_>>()).unwrap_or(f64::NAN), 4),
            "se_entry": all_stats.get("se").cloned().unwrap_or(Value::Null),
            "se_window_clustered": ro(cluster_se(&base), 2),
            "one_per_window_max_gap": stats(&first, days),
        },
    })
}

/// The day roles of `helix_inventory.json`: (days of the role that are usable, those that are not).
pub fn split_days(
    inventory: &Path,
    role: &str,
    need_chainlink: bool,
) -> anyhow::Result<(Vec<String>, Vec<String>)> {
    let inv: Value = serde_json::from_slice(&std::fs::read(inventory)?)?;
    let (mut ok, mut excluded) = (Vec::new(), Vec::new());
    for (day, v) in inv["days"].as_object().into_iter().flatten() {
        if v["role"] != role {
            continue;
        }
        if v["usable"] == true && (!need_chainlink || v["chainlink"] == true) {
            ok.push(day.clone());
        } else if !need_chainlink {
            excluded.push(day.clone());
        }
    }
    ok.sort();
    excluded.sort();
    Ok((ok, excluded))
}

pub struct Args {
    pub split: String,
    pub data: PathBuf,
    pub inventory: PathBuf,
    pub resolutions: PathBuf,
    pub skip_slices: bool,
    pub prereg: PathBuf,
}

pub fn run(args: &Args) -> anyhow::Result<Value> {
    if args.split == "holdout" && !args.prereg.is_file() {
        anyhow::bail!("{} is missing: write the holdout comparisons first", args.prereg.display());
    }
    let role = match args.split.as_str() {
        "discovery" => "discovery",
        "holdout" => "holdout",
        "earlier" => "earlier_regime",
        other => anyhow::bail!("unknown split {other}"),
    };
    let (days, excluded) = split_days(&args.inventory, role, false)?;
    let res = load_res(&args.resolutions)?;
    let (cross, windows) = load(&args.data, &days, &res)?;
    let entries: Vec<&Cross> = cross.iter().filter(|x| x.enter).collect();
    let mut result = json!({
        "split": args.split,
        "days": days,
        "excluded_days": excluded,
        "variants": variants(&cross, days.len()),
        "thresholds": thresholds(&windows),
        "spreads": spreads(&entries),
        "sizing": sizing(&entries),
        "regime": regime(&entries, days.len()),
        "nulls": nulls(&args.data, &days, &cross, &res)?,
    });
    if !args.skip_slices {
        result["latency"] = latency(&args.data, &days, &entries)?;
    }
    let out = args.data.parent().unwrap_or(Path::new(".")).join(format!("helix_sweeps_{}.json", args.split));
    std::fs::write(&out, serde_json::to_string_pretty(&result)?)?;
    Ok(result)
}
