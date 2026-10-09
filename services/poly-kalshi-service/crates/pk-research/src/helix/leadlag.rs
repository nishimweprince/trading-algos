//! PLAN 4.2 on the Helix tape: who moves first, spot, Kalshi or Polymarket, at ~20 ms.
//!
//! ```text
//! pk helix leadlag --days 20260820,20260901 [--bin-ms 20]
//! ```
//!
//! Per asset and day, inside every window's entry period (20 s to 840 s), each
//! series is sampled at the end of every `bin_ms` bin and differenced:
//!
//! - **spot**: Binance spot best bid/ask mid (`BinanceSpot_Book`, a 100 ms
//!   depth stream; monotonic receive time mapped to wall through the
//!   Polymarket rows);
//! - **chainlink**: the Chainlink reference price (the settlement source);
//! - **kalshi**: Kalshi YES mid, from the window's own contract;
//! - **poly**: Polymarket YES mid of the window's token.
//!
//! `xcorr[a→b]` is corr(Δa(t), Δb(t + lag)) for lags of ±3 s: a peak at a
//! positive lag means a leads b. `incremental` regresses Polymarket's move over
//! the next 500 ms on spot's and Kalshi's over the past second: R² with spot
//! alone and with both, and Kalshi's t-stat given spot.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use pk_core::num::round_dp;
use rustfft::FftPlanner;
use rustfft::num_complex::Complex;
use serde_json::{Map, Value, json};

use super::pd::{np_sum, np_var};
use super::records::iter_book;
use super::series::{
    Extra, MonoToWall, PX, Timeline, asof, chainlink_prices, kalshi_contract_start, kalshi_quote_series,
    poly_bbo_series,
};
use super::{CHAINLINK_SPOT, HELIX_15M, NS, asset_name, chainlink_id, path};

pub const MAX_LAG_S: f64 = 3.0;
const SPOT_ID: [(&str, i32); 6] =
    [("BTC", 15), ("ETH", 16), ("SOL", 17), ("XRP", 18), ("BNB", 19), ("DOGE", 20)];
const PAIRS: [(&str, &str); 5] =
    [("spot", "poly"), ("spot", "kalshi"), ("kalshi", "poly"), ("chainlink", "poly"), ("spot", "chainlink")];

/// The value in force at each bin end; NaN before the first row at or after `lo`.
fn sample(t: &[i64], v: &[f64], ends: &[i64], lo: i64) -> Vec<f64> {
    let first = t.partition_point(|&x| x < lo);
    ends.iter().map(|&e| asof(t, e).filter(|&i| i >= first).map(|i| v[i]).unwrap_or(f64::NAN)).collect()
}

fn spot_books(root: &Path, day: &str, clock: &MonoToWall) -> anyhow::Result<BTreeMap<i32, Timeline>> {
    let p = path(root, "BinanceSpot_Book", day);
    if !p.exists() {
        return Ok(BTreeMap::new());
    }
    let mut rows: BTreeMap<i32, Vec<(i64, f64)>> = BTreeMap::new();
    for b in iter_book(&p)? {
        if !SPOT_ID.iter().any(|(_, a)| *a == b.asset_id) {
            continue;
        }
        if let Some(ask) = b.best_ask {
            rows.entry(b.asset_id)
                .or_default()
                .push((clock.wall(b.ts_received_ns), ((b.best_bid + ask) / 2.0).ln()));
        }
    }
    Ok(rows
        .into_iter()
        .map(|(aid, mut r)| {
            r.sort_by_key(|x| x.0);
            (aid, r.into_iter().unzip())
        })
        .collect())
}

/// Per asset, per series name, the differenced bins of every window of the day.
pub fn day_series(
    root: &Path,
    day: &str,
    bin_ms: i64,
) -> anyhow::Result<BTreeMap<String, BTreeMap<String, Vec<f64>>>> {
    let windows = super::records::iter_windows(day, root)?;
    let tokens: Vec<String> = windows.iter().map(|w| w.token_yes.clone()).collect();
    let yes = poly_bbo_series(
        root,
        "Polymarket_CLOB_Yes_BBO",
        day,
        &tokens,
        Extra { recv: true, ..Default::default() },
    )?;
    let aids: Vec<u16> = HELIX_15M.iter().map(|(a, _)| *a).collect();
    let ky =
        kalshi_quote_series(root, "Kalshi_Yes_BBO", day, &aids, Extra { seq: true, ..Default::default() })?;
    let clock = MonoToWall::new(&yes, 100_000);
    let spot = spot_books(root, day, &clock)?;
    let cl = chainlink_prices(root, day, &CHAINLINK_SPOT.iter().map(|(_, a)| *a).collect::<Vec<_>>())?;
    let step = bin_ms * 1_000_000;
    let mut out: BTreeMap<String, BTreeMap<String, Vec<f64>>> = BTreeMap::new();
    for (k, w) in windows.iter().enumerate() {
        let asset = asset_name(w.asset).expect("15-minute asset");
        let o = w.open_unix * NS;
        let ends: Vec<i64> = (0..)
            .map(|i| o + 20 * NS + i * step)
            .take_while(|&s| s < o + 840 * NS)
            .map(|s| s + step - 1)
            .collect();
        let mid = |b: i16, a: i16| (b as i64 + a as i64) as f64 / (2 * PX) as f64;
        let (mut pt, mut pv) = (Vec::new(), Vec::new());
        for i in yes.rows(k) {
            if yes.bid[i] > 0 && yes.ask[i] > yes.bid[i] {
                pt.push(yes.t[i]);
                pv.push(mid(yes.bid[i], yes.ask[i]));
            }
        }
        let mut series: Vec<(&str, Vec<f64>)> = vec![("poly", sample(&pt, &pv, &ends, o))];
        let ks = ky.rows(aids.iter().position(|a| *a == w.asset).expect("listed"));
        let start = kalshi_contract_start(&ky.t[ks.clone()], &ky.seq[ks.clone()], o);
        let (mut kt, mut kv) = (Vec::new(), Vec::new());
        for i in ks.start + start..ks.end {
            if ky.bid[i] > 0 && ky.ask[i] > ky.bid[i] {
                kt.push(ky.t[i]);
                kv.push(mid(ky.bid[i], ky.ask[i]));
            }
        }
        series.push(("kalshi", sample(&kt, &kv, &ends, o)));
        if let Some((_, sid)) = SPOT_ID.iter().find(|(a, _)| *a == asset)
            && let Some((st, sv)) = spot.get(sid)
        {
            series.push(("spot", sample(st, sv, &ends, o - 10 * NS)));
        }
        if let Some((ct, cv)) = chainlink_id(asset).and_then(|c| cl.get(&c)) {
            let logs: Vec<f64> = cv.iter().map(|x| x.ln()).collect();
            series.push(("chainlink", sample(ct, &logs, &ends, o - 10 * NS)));
        }
        for (name, values) in series {
            let d = out.entry(asset.to_owned()).or_default().entry(name.to_owned()).or_default();
            let mut prev = f64::NAN;
            for v in values {
                let x = v - prev;
                prev = v;
                // np.nan_to_num: NaN → 0, ±inf → the largest finite value.
                d.push(if x.is_nan() {
                    0.0
                } else if x.is_infinite() {
                    x.signum() * f64::MAX
                } else {
                    x
                });
            }
        }
    }
    Ok(out)
}

/// corr(a(t), b(t + lag)) for lag in [−max_lag, max_lag] bins, via FFT.
pub fn xcorr(a: &[f64], b: &[f64], max_lag: usize) -> Vec<f64> {
    let n = a.len();
    let size = 1usize << (usize::BITS - (2 * n - 1).leading_zeros());
    let mut planner = FftPlanner::<f64>::new();
    let fwd = planner.plan_fft_forward(size);
    let inv = planner.plan_fft_inverse(size);
    let pad = |x: &[f64]| {
        let mut v: Vec<Complex<f64>> = x.iter().map(|&r| Complex::new(r, 0.0)).collect();
        v.resize(size, Complex::new(0.0, 0.0));
        v
    };
    let (mut fa, mut fb) = (pad(a), pad(&b[..n.min(b.len())]));
    fwd.process(&mut fa);
    fwd.process(&mut fb);
    let mut prod: Vec<Complex<f64>> = fa.iter().zip(&fb).map(|(x, y)| x.conj() * y).collect();
    inv.process(&mut prod);
    let full: Vec<f64> = prod.iter().map(|c| c.re / size as f64).collect();
    let lags: Vec<f64> = full[size - max_lag..].iter().chain(&full[..=max_lag]).copied().collect();
    let sq = |x: &[f64]| np_sum(&x.iter().map(|v| v * v).collect::<Vec<_>>());
    let denom = (sq(a) * sq(b)).sqrt();
    lags.iter().map(|x| if denom > 0.0 { x / denom } else { x * 0.0 }).collect()
}

/// Least squares with an intercept: β, R², and β's standard errors.
fn fit(y: &[f64], cols: &[&[f64]]) -> (f64, Vec<f64>, Vec<f64>) {
    let k = cols.len() + 1;
    let row = |i: usize| -> Vec<f64> { std::iter::once(1.0).chain(cols.iter().map(|c| c[i])).collect() };
    let mut xtx = vec![vec![0.0; k]; k];
    let mut xty = vec![0.0; k];
    for (i, &yi) in y.iter().enumerate() {
        let r = row(i);
        for a in 0..k {
            xty[a] += r[a] * yi;
            for b in 0..k {
                xtx[a][b] += r[a] * r[b];
            }
        }
    }
    let inv = invert(&xtx);
    let beta: Vec<f64> = (0..k).map(|a| (0..k).map(|b| inv[a][b] * xty[b]).sum()).collect();
    let resid: Vec<f64> = y
        .iter()
        .enumerate()
        .map(|(i, yi)| yi - row(i).iter().zip(&beta).map(|(x, b)| x * b).sum::<f64>())
        .collect();
    let (rv, yv) = (np_var(&resid, 0).unwrap_or(0.0), np_var(y, 0).unwrap_or(0.0));
    let r2 = if yv > 0.0 { 1.0 - rv / yv } else { 0.0 };
    let se = (0..k).map(|a| (inv[a][a] * rv).sqrt()).collect();
    (r2, beta, se)
}

/// Gauss–Jordan with partial pivoting (the matrices here are 2×2 and 3×3).
fn invert(m: &[Vec<f64>]) -> Vec<Vec<f64>> {
    let n = m.len();
    let mut a: Vec<Vec<f64>> = m.to_vec();
    let mut inv: Vec<Vec<f64>> =
        (0..n).map(|i| (0..n).map(|j| if i == j { 1.0 } else { 0.0 }).collect()).collect();
    for c in 0..n {
        let p = (c..n).max_by(|&x, &y| a[x][c].abs().total_cmp(&a[y][c].abs())).expect("rows");
        a.swap(c, p);
        inv.swap(c, p);
        let d = a[c][c];
        if d == 0.0 {
            continue;
        }
        for j in 0..n {
            a[c][j] /= d;
            inv[c][j] /= d;
        }
        for r in 0..n {
            if r != c {
                let f = a[r][c];
                for j in 0..n {
                    a[r][j] -= f * a[c][j];
                    inv[r][j] -= f * inv[c][j];
                }
            }
        }
    }
    inv
}

pub fn incremental(s: &BTreeMap<String, Vec<f64>>, bin_ms: i64) -> Option<Value> {
    let spot = s.get("spot")?;
    let (poly, kalshi) = (s.get("poly")?, s.get("kalshi")?);
    let past = (1000.0 / bin_ms as f64).round_ties_even() as usize;
    let ahead = (500.0 / bin_ms as f64).round_ties_even() as usize;
    let window_sum = |x: &[f64], k: usize| -> Vec<f64> {
        let mut c = vec![0.0];
        let mut acc = 0.0;
        for v in x {
            acc += v;
            c.push(acc);
        }
        (k..c.len()).map(|i| c[i] - c[i - k]).collect()
    };
    let n = poly.len();
    let (wy, ws, wk) = (window_sum(poly, ahead), window_sum(spot, past), window_sum(kalshi, past));
    let (mut y, mut xs, mut xk) = (Vec::new(), Vec::new(), Vec::new());
    let mut i = past;
    while i + ahead < n {
        let (a, b, c) = (wy[i], ws[i - past], wk[i - past]);
        if a != 0.0 || b != 0.0 || c != 0.0 {
            y.push(a);
            xs.push(b);
            xk.push(c);
        }
        i += 5;
    }
    if y.len() < 100 {
        return None;
    }
    let (r2_s, _, _) = fit(&y, &[&xs]);
    let (r2_k, _, _) = fit(&y, &[&xk]);
    let (r2_sk, beta, se) = fit(&y, &[&xs, &xk]);
    Some(json!({
        "rows": y.len(),
        "r2_spot": round_dp(r2_s, 4),
        "r2_kalshi": round_dp(r2_k, 4),
        "r2_spot_kalshi": round_dp(r2_sk, 4),
        "kalshi_t_given_spot": if se[2] > 0.0 { json!(round_dp(beta[2] / se[2], 2)) } else { Value::Null },
    }))
}

pub struct Args {
    pub days: Vec<String>,
    pub root: PathBuf,
    pub bin_ms: i64,
    pub out: PathBuf,
}

pub fn run(args: &Args) -> anyhow::Result<Value> {
    let max_lag = (MAX_LAG_S * 1000.0 / args.bin_ms as f64).round_ties_even() as usize;
    let lags_ms: Vec<i64> = (-(max_lag as i64)..=max_lag as i64).map(|l| l * args.bin_ms).collect();
    let at150 = max_lag + (150.0 / args.bin_ms as f64).round_ties_even() as usize;
    let mut acc: BTreeMap<String, BTreeMap<String, Vec<f64>>> = BTreeMap::new();
    let mut inc: BTreeMap<String, Vec<Value>> = BTreeMap::new();
    for day in &args.days {
        for (asset, s) in day_series(&args.root, day, args.bin_ms)? {
            for (a, b) in PAIRS {
                if let (Some(x), Some(y)) = (s.get(a), s.get(b)) {
                    let c = xcorr(x, y, max_lag);
                    let cur = acc
                        .entry(asset.clone())
                        .or_default()
                        .entry(format!("{a}->{b}"))
                        .or_insert_with(|| vec![0.0; c.len()]);
                    for (u, v) in cur.iter_mut().zip(&c) {
                        *u += v;
                    }
                }
            }
            if let Some(mut r) = incremental(&s, args.bin_ms) {
                r["day"] = day.clone().into();
                inc.entry(asset).or_default().push(r);
            }
        }
        println!("{day} done");
    }
    let ndays = args.days.len() as f64;
    let mut assets = Map::new();
    for (asset, curves) in acc {
        let mut body = Map::new();
        for (key, c) in curves {
            let c: Vec<f64> = c.iter().map(|x| x / ndays).collect();
            let peak = c.iter().enumerate().fold(0, |best, (i, v)| if *v > c[best] { i } else { best });
            let pos = |v: &[f64]| np_sum(&v.iter().map(|x| x.max(0.0)).collect::<Vec<_>>());
            body.insert(
                key,
                json!({
                    "peak_lag_ms": lags_ms[peak],
                    "peak_corr": round_dp(c[peak], 4),
                    "corr_at_0": round_dp(c[max_lag], 4),
                    "corr_at_150ms": round_dp(c[at150], 4),
                    "share_of_mass_after_150ms": round_dp(pos(&c[at150..]) / pos(&c), 3),
                }),
            );
        }
        body.insert("incremental".into(), inc.remove(&asset).map(Value::from).unwrap_or(Value::Null));
        assets.insert(asset, Value::Object(body));
    }
    let result = json!({"days": args.days, "bin_ms": args.bin_ms, "assets": assets});
    std::fs::write(&args.out, serde_json::to_string_pretty(&result)?)?;
    Ok(result)
}
