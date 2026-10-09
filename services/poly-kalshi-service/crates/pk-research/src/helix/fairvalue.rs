//! PLAN 4.3 on the Helix tape: a Chainlink fair-value signal against the Kalshi gap.
//!
//! ```text
//! pk helix fairvalue --split discovery
//! ```
//!
//! Reads the per-second grids of [`super::replay`] (each leg's touch and
//! Chainlink's price `s` at the end of every second) and the resolutions.
//!
//! 1. **Which Chainlink number settles the market**: since 2026-08-07 the 60 s
//!    TWAP at the close against the 60 s TWAP at the open (`k_twap`, the
//!    previous window's last-minute mean), not the tape's strike.
//! 2. **Fair value**: `P(up) = Φ((ln E[X] − ln K) / sd)`, X the closing 60 s
//!    average of a driftless log-normal walk, σ a trailing per-second realised
//!    volatility of Chainlink over `W` seconds.
//! 3. **The variant**, per second (first second it fires, inside the entry
//!    window, at or below the cap): YES when `P(up) − ask − fee > edge`, NO
//!    when `(1 − P(up)) − ask − fee > edge`; the Kalshi gap is scored on the
//!    same grid like for like.
//!
//! σ is pandas' rolling standard deviation (Welford with Kahan compensation,
//! as `Rolling.std` computes it), so the signal fires on the same seconds.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};

use pk_core::num::round_dp;
use serde_json::{Map, Value, json};

use super::pd::{DECILE_LABELS, cut, decile_edges, group_by, group_mean, median, np_mean, np_std};
use super::sweeps::{Res, load_res, split_days};
use crate::frame::Frame;

pub const WINDOWS_W: [usize; 3] = [120, 300, 900];
pub const EDGES: [f64; 5] = [0.02, 0.04, 0.06, 0.08, 0.10];
const GAP_THRESHOLDS: [f64; 4] = [0.08, 0.10, 0.12, 0.15];
const EARLY: f64 = 20.0;
const LATE: f64 = 60.0;
const CAP: f64 = 0.60;
const AVG_S: f64 = 60.0;

fn r(x: f64, n: usize) -> Value {
    if x.is_nan() { Value::Null } else { round_dp(x, n).into() }
}

/// The schedule on every Helix window.
fn fee(px: f64) -> f64 {
    0.07 * px * (1.0 - px)
}

/// One second of one window's grid.
#[derive(Clone, Debug)]
pub struct Sec {
    pub asset: String,
    pub open_unix: i64,
    pub sec: i64,
    pub day: String,
    pub k_bid: f64,
    pub k_ask: f64,
    pub y_bid: f64,
    pub y_ask: f64,
    pub n_bid: f64,
    pub n_ask: f64,
    pub s: f64,
    pub k_twap: f64,
    pub avg_sum: f64,
    pub p_up: f64,
}

#[derive(Clone, Debug)]
pub struct Win {
    pub asset: String,
    pub open_unix: i64,
    pub strike: f64,
    pub cl_twap60: f64,
    pub cl_close_last: f64,
    pub k_twap: f64,
    pub last_prev: f64,
}

/// One asset's grid seconds that have a Chainlink price, and its windows, over `days`.
pub fn load(data: &Path, days: &[String], asset: &str) -> anyhow::Result<(Vec<Sec>, Vec<Win>)> {
    let (mut grid, mut wins) = (Vec::new(), Vec::new());
    for day in days {
        let d = data.join(day);
        if !d.join("summary.json").is_file() {
            continue;
        }
        let g = Frame::read_parquet(&d.join("grid.parquet"))?;
        let f = |c: &str, i: usize| g.cols.get(c).and_then(|col| col.f64(i)).unwrap_or(f64::NAN);
        for i in 0..g.len() {
            if g.col("asset").str(i) != Some(asset) || f("s", i).is_nan() {
                continue;
            }
            grid.push(Sec {
                asset: asset.to_owned(),
                open_unix: g.col("open_unix").i64(i).unwrap_or(0),
                sec: g.col("sec").i64(i).unwrap_or(0),
                day: day.clone(),
                k_bid: f("k_bid", i),
                k_ask: f("k_ask", i),
                y_bid: f("y_bid", i),
                y_ask: f("y_ask", i),
                n_bid: f("n_bid", i),
                n_ask: f("n_ask", i),
                s: f("s", i),
                k_twap: f64::NAN,
                avg_sum: 0.0,
                p_up: f64::NAN,
            });
        }
        let w = Frame::read_parquet(&d.join("windows.parquet"))?;
        let f = |c: &str, i: usize| w.cols.get(c).and_then(|col| col.f64(i)).unwrap_or(f64::NAN);
        for i in 0..w.len() {
            if w.col("asset").str(i) != Some(asset) {
                continue;
            }
            wins.push(Win {
                asset: asset.to_owned(),
                open_unix: w.col("open_unix").i64(i).unwrap_or(0),
                strike: f("strike", i),
                cl_twap60: f("cl_twap60", i),
                cl_close_last: f("cl_close_last", i),
                k_twap: f64::NAN,
                last_prev: f64::NAN,
            });
        }
    }
    Ok((grid, wins))
}

/// Each window's `k_twap` and `last_prev`: the previous window's 60 s TWAP and last tick.
pub fn with_reference(wins: &mut [Win]) {
    let prev: HashMap<(String, i64), (f64, f64)> =
        wins.iter().map(|w| ((w.asset.clone(), w.open_unix + 900), (w.cl_twap60, w.cl_close_last))).collect();
    for w in wins.iter_mut() {
        if let Some((t, l)) = prev.get(&(w.asset.clone(), w.open_unix)) {
            w.k_twap = *t;
            w.last_prev = *l;
        }
    }
}

/// Which Chainlink comparison reproduces Polymarket's outcome.
pub fn settlement_rules(wins: &[Win], res: &HashMap<(String, i64), Option<String>>) -> Value {
    let w: Vec<(&Win, bool)> = wins
        .iter()
        .filter_map(|w| {
            let out = res.get(&(w.asset.clone(), w.open_unix))?.as_deref()?;
            (!w.cl_twap60.is_nan() && !w.k_twap.is_nan()).then_some((w, out == "yes"))
        })
        .collect();
    let rate = |rule: &dyn Fn(&Win) -> bool| {
        r(
            np_mean(&w.iter().map(|(x, up)| if rule(x) == *up { 1.0 } else { 0.0 }).collect::<Vec<_>>())
                .unwrap_or(f64::NAN),
            4,
        )
    };
    json!({
        "windows": w.len(),
        "last_tick >= strike (tape)": rate(&|x| x.cl_close_last >= x.strike),
        "twap60 >= strike (tape)": rate(&|x| x.cl_twap60 >= x.strike),
        "last_tick >= last_tick at open": rate(&|x| x.cl_close_last >= x.last_prev),
        "twap60 >= twap60 at open": rate(&|x| x.cl_twap60 >= x.k_twap),
    })
}

/// Abramowitz and Stegun 7.1.26 (|error| < 1.5e-7).
pub fn ndtr(x: f64) -> f64 {
    let z = x.abs() / std::f64::consts::SQRT_2;
    let t = 1.0 / (1.0 + 0.3275911 * z);
    let poly =
        t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))));
    let erf = 1.0 - poly * (-z * z).exp();
    let sign = if x > 0.0 {
        1.0
    } else if x < 0.0 {
        -1.0
    } else {
        0.0
    };
    0.5 * (1.0 + sign * erf)
}

/// P(average of the last 60 s ≥ K) with `sec` seconds elapsed, driftless log walk.
pub fn p_up(s: f64, k: f64, sigma: f64, sec: f64, avg_sum: f64) -> f64 {
    let tau = 900.0 - sec;
    let in_avg = tau < AVG_S;
    let var = if in_avg {
        sigma.powi(2) * tau.powf(3.0) / (3.0 * AVG_S * AVG_S)
    } else {
        sigma.powi(2) * ((tau - AVG_S) + AVG_S / 3.0)
    };
    let mean = if in_avg { (avg_sum + s * tau) / AVG_S } else { s };
    let sd = var.max(1e-12).sqrt();
    ndtr((mean.ln() - k.ln()) / sd)
}

/// `Series.rolling(w, min_periods).std()`, as pandas computes it.
pub fn rolling_std(values: &[f64], w: usize, min_periods: usize) -> Vec<f64> {
    let minp = min_periods.max(1) as f64;
    let (mut nobs, mut mean, mut ssq, mut comp_add, mut comp_rem) = (0.0f64, 0.0f64, 0.0f64, 0.0f64, 0.0f64);
    let mut same = 0usize;
    let mut prev = values.first().copied().unwrap_or(0.0);
    let mut out = Vec::with_capacity(values.len());
    let add = |v: f64,
               nobs: &mut f64,
               mean: &mut f64,
               ssq: &mut f64,
               comp: &mut f64,
               same: &mut usize,
               prev: &mut f64| {
        if v.is_nan() {
            return;
        }
        *nobs += 1.0;
        if v == *prev {
            *same += 1;
        } else {
            *same = 1;
        }
        *prev = v;
        let prev_mean = *mean - *comp;
        let y = v - *comp;
        let t = y - *mean;
        *comp = t + *mean - y;
        *mean = if *nobs != 0.0 { *mean + t / *nobs } else { 0.0 };
        *ssq += (v - prev_mean) * (v - *mean);
    };
    for i in 0..values.len() {
        let s = (i + 1).saturating_sub(w);
        if i == 0 {
            for &v in &values[s..=i] {
                add(v, &mut nobs, &mut mean, &mut ssq, &mut comp_add, &mut same, &mut prev);
            }
        } else {
            let ps = i.saturating_sub(w);
            for &v in &values[ps..s] {
                if v.is_nan() {
                    continue;
                }
                nobs -= 1.0;
                if nobs != 0.0 {
                    let prev_mean = mean - comp_rem;
                    let y = v - comp_rem;
                    let t = y - mean;
                    comp_rem = t + mean - y;
                    mean -= t / nobs;
                    ssq -= (v - prev_mean) * (v - mean);
                } else {
                    mean = 0.0;
                    ssq = 0.0;
                }
            }
            add(values[i], &mut nobs, &mut mean, &mut ssq, &mut comp_add, &mut same, &mut prev);
        }
        let var = if nobs >= minp && nobs > 1.0 {
            if nobs == 1.0 || same as f64 >= nobs { 0.0 } else { ssq / (nobs - 1.0) }
        } else {
            f64::NAN
        };
        out.push(if var < 0.0 { 0.0 } else { var.sqrt() });
    }
    out
}

/// The trailing per-second log-return s.d. of Chainlink over `w` seconds, on
/// `grid` sorted by (open, sec); a missing second is not a 1 s return.
pub fn sigma(grid: &[Sec], w: usize) -> Vec<f64> {
    let mut ret = vec![f64::NAN; grid.len()];
    for i in 1..grid.len() {
        let (a, b) = (&grid[i - 1], &grid[i]);
        let gap = (b.open_unix + b.sec) - (a.open_unix + a.sec);
        if gap == 1 {
            ret[i] = b.s.ln() - a.s.ln();
        }
    }
    rolling_std(&ret, w, w / 3)
}

/// One decision per window: the first second the signal fires, if its price is at or below the cap.
#[derive(Clone, Debug)]
pub struct Entry {
    pub asset: String,
    pub open_unix: i64,
    pub sec: i64,
    pub day: String,
    pub side: &'static str,
    pub px: f64,
}

pub fn scan(df: &[Sec], signal: &str, edge: f64) -> Vec<Entry> {
    let mut fired: Vec<(usize, &'static str)> = Vec::new();
    for (i, x) in df.iter().enumerate() {
        let sec = x.sec as f64;
        if !(EARLY..=900.0 - LATE).contains(&sec) {
            continue;
        }
        let (buy_yes, buy_no) = if signal == "fair" {
            (x.p_up - x.y_ask - fee(x.y_ask) > edge, (1.0 - x.p_up) - x.n_ask - fee(x.n_ask) > edge)
        } else {
            let gap = (x.k_bid + x.k_ask) / 2.0 - (x.y_bid + x.y_ask) / 2.0;
            (gap >= edge - 1e-9, gap <= -edge + 1e-9)
        };
        if buy_yes || buy_no {
            fired.push((i, if buy_yes { "yes" } else { "no" }));
        }
    }
    fired.sort_by_key(|(i, _)| df[*i].sec);
    let mut seen: BTreeSet<(&str, i64)> = BTreeSet::new();
    let mut out = Vec::new();
    for (i, side) in fired {
        let x = &df[i];
        if !seen.insert((x.asset.as_str(), x.open_unix)) {
            continue;
        }
        let px = if side == "yes" { x.y_ask } else { x.n_ask };
        if px <= CAP {
            out.push(Entry {
                asset: x.asset.clone(),
                open_unix: x.open_unix,
                sec: x.sec,
                day: x.day.clone(),
                side,
                px,
            });
        }
    }
    out
}

fn brier(p: &[f64], up: &[f64]) -> Value {
    r(np_mean(&p.iter().zip(up).map(|(p, u)| (p - u) * (p - u)).collect::<Vec<_>>()).unwrap_or(f64::NAN), 4)
}

pub fn score(entries: &[Entry], res: &HashMap<(String, i64), Option<String>>) -> Value {
    let e: Vec<(&Entry, f64)> = entries
        .iter()
        .filter_map(|x| {
            let out = res.get(&(x.asset.clone(), x.open_unix))?.as_deref()?;
            Some((x, if x.side == out { 1.0 } else { 0.0 }))
        })
        .collect();
    if e.is_empty() {
        return json!({"n": 0});
    }
    let net: Vec<f64> = e.iter().map(|(x, w)| 100.0 * (w - x.px - fee(x.px))).collect();
    let daily: Vec<f64> = group_by(e.iter().zip(&net).map(|((x, _), n)| (x.day.as_str(), *n)))
        .values()
        .map(|v| group_mean(v))
        .collect();
    let t = if daily.len() > 1 {
        np_std(&daily)
            .filter(|sd| *sd > 0.0)
            .map(|sd| np_mean(&daily).expect("days") / (sd / (daily.len() as f64).sqrt()))
    } else {
        None
    };
    let n = e.len();
    let ndays = e.iter().map(|(x, _)| x.day.as_str()).collect::<BTreeSet<_>>().len().max(1);
    let mean = |v: &[f64]| np_mean(v).unwrap_or(f64::NAN);
    json!({
        "n": n,
        "per_day": round_dp(n as f64 / ndays as f64, 1),
        "win_rate": r(mean(&e.iter().map(|x| x.1).collect::<Vec<_>>()), 4),
        "avg_px": r(mean(&e.iter().map(|x| x.0.px).collect::<Vec<_>>()), 4),
        "net_cents": r(mean(&net), 2),
        "se": if n > 1 { np_std(&net).map(|s| r(s / (n as f64).sqrt(), 2)).unwrap_or(Value::Null) } else { Value::Null },
        "day_t": t.map(|t| r(t, 2)).unwrap_or(Value::Null),
        "median_sec": median(&e.iter().map(|x| x.0.sec as f64).collect::<Vec<_>>()).map(|m| r(m, 0)).unwrap_or(Value::Null),
        "by_asset": group_by(e.iter().zip(&net).map(|((x, _), n)| (x.asset.as_str(), *n)))
            .into_iter()
            .map(|(a, v)| (a.to_owned(), json!({"n": v.len(), "net_cents": r(np_mean(&v).unwrap_or(f64::NAN), 2)})))
            .collect::<Map<_, _>>(),
    })
}

pub struct Args {
    pub split: String,
    pub data: PathBuf,
    pub inventory: PathBuf,
    pub resolutions: PathBuf,
}

pub fn run(args: &Args) -> anyhow::Result<Value> {
    let (days, _) = split_days(&args.inventory, &args.split, true)?;
    let res_rows: Res = load_res(&args.resolutions)?;
    let res: HashMap<(String, i64), Option<String>> =
        res_rows.iter().map(|(a, o, _, p)| ((a.clone(), *o), p.clone())).collect();
    let assets: BTreeSet<&str> = res_rows.iter().map(|r| r.0.as_str()).collect();
    let mut feats: Vec<Win> = Vec::new();
    let mut cal: BTreeMap<usize, Vec<Sec>> = BTreeMap::new();
    let mut fired: BTreeMap<(String, usize, String), Vec<Entry>> = BTreeMap::new();
    for asset in assets {
        let (grid, mut wins) = load(&args.data, &days, asset)?;
        if grid.is_empty() {
            continue;
        }
        with_reference(&mut wins);
        let k_twap: HashMap<i64, f64> = wins.iter().map(|w| (w.open_unix, w.k_twap)).collect();
        feats.extend(wins);
        let mut grid: Vec<Sec> = grid
            .into_iter()
            .filter_map(|mut x| {
                x.k_twap = *k_twap.get(&x.open_unix)?;
                (x.k_twap > 0.0).then_some(x)
            })
            .collect();
        grid.sort_by_key(|x| (x.open_unix, x.sec));
        let mut acc: HashMap<i64, (f64, f64)> = HashMap::new();
        for x in grid.iter_mut() {
            let v = if x.sec >= 900 - AVG_S as i64 { x.s } else { 0.0 };
            // groupby(open).cumsum(): Kahan-compensated.
            let (sum, comp) = acc.entry(x.open_unix).or_insert((0.0, 0.0));
            let y = v - *comp;
            let t = *sum + y;
            *comp = t - *sum - y;
            *sum = t;
            x.avg_sum = t;
        }
        for w in WINDOWS_W {
            let sig = sigma(&grid, w);
            let df: Vec<Sec> = grid
                .iter()
                .zip(&sig)
                .filter(|(_, s)| !s.is_nan() && **s > 0.0)
                .map(|(x, s)| {
                    let mut x = x.clone();
                    x.p_up = p_up(x.s, x.k_twap, *s, x.sec as f64, x.avg_sum);
                    x
                })
                .collect();
            cal.entry(w).or_default().extend(df.iter().filter(|x| x.sec % 60 == 0).cloned());
            for e in EDGES {
                fired.entry(("fair".into(), w, format!("{e:.2}"))).or_default().extend(scan(&df, "fair", e));
            }
            if w == WINDOWS_W[1] {
                for t in GAP_THRESHOLDS {
                    fired
                        .entry(("gap".into(), w, format!("{t:.2}")))
                        .or_default()
                        .extend(scan(&df, "gap", t));
                }
            }
        }
        println!("{asset} done");
    }
    let mut fair = Map::new();
    let edges = decile_edges();
    for w in WINDOWS_W {
        let c: Vec<(&Sec, f64)> = cal
            .get(&w)
            .into_iter()
            .flatten()
            .filter_map(|x| {
                let out = res.get(&(x.asset.clone(), x.open_unix))?.as_deref()?;
                Some((x, if out == "yes" { 1.0 } else { 0.0 }))
            })
            .collect();
        let bins = group_by(c.iter().filter_map(|(x, up)| cut(x.p_up, &edges).map(|b| (b, *up))));
        let p: Vec<f64> = c.iter().map(|x| x.0.p_up).collect();
        let up: Vec<f64> = c.iter().map(|x| x.1).collect();
        let poly: Vec<f64> = c.iter().map(|x| (x.0.y_bid + x.0.y_ask) / 2.0).collect();
        let kal: Vec<f64> = c.iter().map(|x| (x.0.k_bid + x.0.k_ask) / 2.0).collect();
        fair.insert(
            format!("W{w}"),
            json!({
                "calibration": bins.into_iter().map(|(b, v)| (DECILE_LABELS[b].to_owned(),
                    json!({"n": v.len(), "up_rate": r(group_mean(&v), 3)}))).collect::<Map<_, _>>(),
                "brier": brier(&p, &up),
                "brier_poly_mid": brier(&poly, &up),
                "brier_kalshi_mid": brier(&kal, &up),
                "edges": EDGES.iter().map(|e| {
                    let k = format!("{e:.2}");
                    let v = fired.get(&("fair".into(), w, k.clone())).cloned().unwrap_or_default();
                    (k, score(&v, &res))
                }).collect::<Map<_, _>>(),
            }),
        );
    }
    let gap: Map<String, Value> = GAP_THRESHOLDS
        .iter()
        .map(|t| {
            let k = format!("{t:.2}");
            let v = fired.get(&("gap".into(), WINDOWS_W[1], k.clone())).cloned().unwrap_or_default();
            (k, score(&v, &res))
        })
        .collect();
    let result = json!({
        "split": args.split,
        "days": days,
        "settlement": settlement_rules(&feats, &res),
        "kalshi_gap_per_second": gap,
        "fair_value": fair,
    });
    let out =
        args.data.parent().unwrap_or(Path::new(".")).join(format!("helix_fairvalue_{}.json", args.split));
    std::fs::write(&out, serde_json::to_string_pretty(&result)?)?;
    Ok(result)
}
