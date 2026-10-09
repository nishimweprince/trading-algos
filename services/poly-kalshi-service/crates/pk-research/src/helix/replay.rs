//! PLAN 0.1b / 3.3: the Helix tape through the live `decide()`, per day.
//!
//! ```text
//! pk helix replay [--days 20260801-20260930] [--jobs 3] [--out research/data/helix] [--shift-ms k=0,p=0]
//! ```
//!
//! For every 15-minute window, the three legs the live engine reads (Kalshi
//! YES, Polymarket YES and NO) are merged by time, and **the same `decide()`**
//! the engine calls is evaluated for every variant in [`variants`]: a pass over
//! the merged ticks finds where a variant *could* fire (both legs valid, inside
//! its timing, `|gap|` within a hair of its threshold), and `decide()` is called
//! on those ticks, in order, until one consumes the window. Its float
//! arithmetic decides every boundary case, as live.
//!
//! - **Leg validity** (`Book::quote`): both sides present, not crossed, updated
//!   since the open. Kalshi counts only from its contract switch
//!   ([`series::kalshi_contract_start`]).
//! - **Prices** are 1e-4 ticks, as the book rounds them.
//! - **Evaluation on every update** of any of the three legs.
//!
//! Settlement is Polymarket's own outcome (`resolutions.parquet`). Per day,
//! under `<out>/<day>/`: `grid.parquet` (per window per second, the legs'
//! touch at the end of the second and Chainlink's price `s`),
//! `crossings.parquet` (per variant per window, the consuming decision),
//! `windows.parquet` (the uncensored gap, Chainlink at open and close, the
//! Kalshi switch), `slices.parquet` (every BBO row and Polymarket trade from
//! −2 s to +5 s around each crossing) and `summary.json`.

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};

use pk_core::StrategyParams;
use pk_core::book::Quote;
use pk_core::decision::{Action, WINDOW_S, decide};
use pk_core::num::round_dp;
use serde_json::{Map, Value, json};

use crate::frame::Row;

use super::records::{PolyWindow, iter_poly_trades, iter_windows};
use super::series::{
    Extra, MonoToWall, PX, Series, asof, chainlink_prices, kalshi_contract_start, kalshi_quote_series,
    poly_bbo_series,
};
use super::{CHAINLINK_SPOT, HELIX_15M, NS, asset_name, path};
use crate::frame::{Col, Frame};

pub const THRESHOLDS: [f64; 5] = [0.08, 0.10, 0.12, 0.15, 0.20];
const SLICE_BEFORE_NS: i64 = 2 * NS;
const SLICE_AFTER_NS: i64 = 5 * NS;
pub const MIN_FREE_GB: f64 = 30.0;

pub fn base() -> StrategyParams {
    StrategyParams { gap_threshold: 0.10, max_entry_px: 0.60, notional_usd: 50.0, ..Default::default() }
}

/// PLAN 1.2's shadow set plus the 3.3 sweeps; `base` is the live rule.
pub fn variants() -> Vec<(&'static str, StrategyParams)> {
    let b = base();
    let v = |f: &dyn Fn(&mut StrategyParams)| {
        let mut p = b.clone();
        f(&mut p);
        p
    };
    vec![
        ("base", b.clone()),
        ("t08", v(&|p| p.gap_threshold = 0.08)),
        ("t12", v(&|p| p.gap_threshold = 0.12)),
        ("t15", v(&|p| p.gap_threshold = 0.15)),
        ("t20", v(&|p| p.gap_threshold = 0.20)),
        ("early60", v(&|p| p.entry_early_s = 60)),
        ("early120", v(&|p| p.entry_early_s = 120)),
        ("early180", v(&|p| p.entry_early_s = 180)),
        ("kspread03", v(&|p| p.max_kalshi_spread = Some(0.03))),
        ("kspread05", v(&|p| p.max_kalshi_spread = Some(0.05))),
        ("kspread10", v(&|p| p.max_kalshi_spread = Some(0.10))),
        ("cap50", v(&|p| p.max_entry_px = 0.50)),
        ("pspread05", v(&|p| p.max_poly_spread = Some(0.05))),
        ("pspread10", v(&|p| p.max_poly_spread = Some(0.10))),
    ]
}

/// One leg's rows that may count for a window.
#[derive(Clone, Debug, Default)]
pub struct Leg {
    pub t: Vec<i64>,
    pub bid: Vec<i16>,
    pub ask: Vec<i16>,
    pub bsz: Vec<f32>,
    pub asz: Vec<f32>,
}

impl Leg {
    pub fn of(s: &Series, rows: std::ops::Range<usize>, shift_ns: i64) -> Leg {
        Leg {
            t: s.t[rows.clone()].iter().map(|t| t + shift_ns).collect(),
            bid: s.bid[rows.clone()].to_vec(),
            ask: s.ask[rows.clone()].to_vec(),
            bsz: s.bsz[rows.clone()].to_vec(),
            asz: s.asz[rows].to_vec(),
        }
    }

    pub fn clip(&self, lo: i64, hi: i64) -> Leg {
        let a = self.t.partition_point(|&x| x < lo);
        let b = self.t.partition_point(|&x| x < hi).max(a);
        Leg {
            t: self.t[a..b].to_vec(),
            bid: self.bid[a..b].to_vec(),
            ask: self.ask[a..b].to_vec(),
            bsz: self.bsz[a..b].to_vec(),
            asz: self.asz[a..b].to_vec(),
        }
    }

    /// The row in force at `t`, and whether it is a valid two-sided touch.
    fn at(&self, t: i64) -> (Option<usize>, bool) {
        let i = asof(&self.t, t);
        let valid = i.is_some_and(|i| self.bid[i] > 0 && self.ask[i] > 0 && self.bid[i] < self.ask[i]);
        (i, valid)
    }

    fn quote(&self, i: Option<usize>, valid: bool) -> Quote {
        match i {
            Some(i) if valid => Quote {
                valid: true,
                bid: Some(self.bid[i] as f64 / PX as f64),
                ask: Some(self.ask[i] as f64 / PX as f64),
            },
            _ => Quote::INVALID,
        }
    }
}

/// One asset's Kalshi rows from the window's own contract on (the switch is
/// found on the recorded times, before any shift).
pub fn kalshi_leg(ky: &Series, key: usize, open_ns: i64, shift_ns: i64) -> Leg {
    let rows = ky.rows(key);
    let start = kalshi_contract_start(&ky.t[rows.clone()], &ky.seq[rows.clone()], open_ns);
    Leg::of(ky, rows.start + start..rows.end, shift_ns)
}

fn px(v: i16) -> f64 {
    v as f64 / PX as f64
}

/// Every variant's consuming decision in one window, and the uncensored gap.
/// `k`, `y`, `n` hold only rows that may count: Kalshi from the contract
/// switch, Polymarket from the open, all before the close.
pub fn scan_window(
    k: &Leg,
    y: &Leg,
    n: &Leg,
    open_unix: i64,
    variants: &[(&'static str, StrategyParams)],
) -> (Vec<Row>, Row) {
    let o = open_unix * NS;
    let mut times: Vec<i64> = k.t.iter().chain(&y.t).chain(&n.t).copied().collect();
    times.sort_unstable();
    times.dedup();
    let mut stats = Row::new();
    stats.insert("events".into(), times.len().into());
    if times.is_empty() {
        return (Vec::new(), stats);
    }
    let kat: Vec<_> = times.iter().map(|&t| k.at(t)).collect();
    let yat: Vec<_> = times.iter().map(|&t| y.at(t)).collect();
    let nat: Vec<_> = times.iter().map(|&t| n.at(t)).collect();
    let sec: Vec<f64> = times.iter().map(|&t| (t - o) as f64 / NS as f64).collect();
    let both: Vec<bool> = (0..times.len()).map(|j| kat[j].1 && yat[j].1).collect();
    // Doubled mids in ticks: the gap is exact in integers, |gap| = |g2| / (2 PX).
    let g2: Vec<i64> = (0..times.len())
        .map(|j| {
            if !both[j] {
                return 0;
            }
            let (ki, yi) = (kat[j].0.expect("valid"), yat[j].0.expect("valid"));
            (k.bid[ki] as i64 + k.ask[ki] as i64) - (y.bid[yi] as i64 + y.ask[yi] as i64)
        })
        .collect();
    let abs_g: Vec<f64> = g2.iter().map(|g| g.abs() as f64 / (2 * PX) as f64).collect();
    let b = base();
    let inside = |j: usize, p: &StrategyParams| {
        sec[j] >= p.entry_early_s as f64 && sec[j] <= (WINDOW_S - p.entry_late_s) as f64
    };
    let entry: Vec<bool> = (0..times.len()).map(|j| both[j] && inside(j, &b)).collect();
    let max_full = (0..times.len())
        .filter(|&j| entry[j])
        .map(|j| abs_g[j])
        .fold(None, |m: Option<f64>, x| Some(m.map_or(x, |m| m.max(x))));
    stats.insert("max_abs_gap_full".into(), max_full.into());
    for thr in THRESHOLDS {
        let hit = (0..times.len()).find(|&j| entry[j] && abs_g[j] >= thr - 1e-9);
        let key = &format!("{thr:.2}")[2..];
        stats.insert(format!("first_cross_s_{key}"), hit.map(|j| sec[j]).into());
        stats.insert(
            format!("first_cross_side_{key}"),
            hit.map(|j| if g2[j] > 0 { "yes" } else { "no" }).into(),
        );
    }
    let mut out = Vec::new();
    for (name, p) in variants {
        for j in 0..times.len() {
            if !(both[j] && inside(j, p) && abs_g[j] >= p.gap_threshold - 2e-4) {
                continue;
            }
            let kq = k.quote(kat[j].0, kat[j].1);
            let yq = y.quote(yat[j].0, yat[j].1);
            let nq = n.quote(nat[j].0, nat[j].1);
            let d = decide(&kq, &yq, &nq, p, sec[j], false, None, None);
            if !d.consumes_window() {
                continue;
            }
            let buy_yes = d.buy_yes == Some(true);
            let (leg, li) = if buy_yes { (y, yat[j].0) } else { (n, nat[j].0) };
            let ki = kat[j].0.expect("valid");
            let mut r = Row::new();
            r.insert("variant".into(), (*name).into());
            r.insert("open_unix".into(), open_unix.into());
            r.insert("t_ns".into(), times[j].into());
            r.insert("sec_into".into(), round_dp(sec[j], 3).into());
            r.insert("action".into(), d.action.as_str().into());
            r.insert("side".into(), if buy_yes { "yes" } else { "no" }.into());
            r.insert("entry_px".into(), d.entry_px.into());
            r.insert("gap".into(), round_dp(d.gap.unwrap_or(0.0), 4).into());
            r.insert("touch_size".into(), li.map(|i| leg.asz[i] as f64).into());
            r.insert("k_bid".into(), kq.bid.into());
            r.insert("k_ask".into(), kq.ask.into());
            r.insert("k_bsz".into(), (k.bsz[ki] as f64).into());
            r.insert("k_asz".into(), (k.asz[ki] as f64).into());
            r.insert("y_bid".into(), yq.bid.into());
            r.insert("y_ask".into(), yq.ask.into());
            r.insert("n_bid".into(), nq.bid.into());
            r.insert("n_ask".into(), nq.ask.into());
            r.insert("enter".into(), (d.action == Action::Enter).into());
            out.push(r);
            break;
        }
    }
    (out, stats)
}

/// Per second of the window, the three legs' touch at the end of the second
/// where all three are valid, and Chainlink's price in force.
#[derive(Default)]
struct Grid {
    open_unix: Vec<Option<i64>>,
    sec: Vec<Option<i64>>,
    cols: [Vec<Option<f64>>; 9],
    asset: Vec<Option<String>>,
}

const GRID_COLS: [&str; 9] = ["k_bid", "k_ask", "y_bid", "y_ask", "n_bid", "n_ask", "y_asz", "n_asz", "s"];

impl Grid {
    fn add(
        &mut self,
        k: &Leg,
        y: &Leg,
        n: &Leg,
        open_unix: i64,
        asset: &str,
        cl: Option<&(Vec<i64>, Vec<f64>)>,
    ) {
        let o = open_unix * NS;
        for s in 0..WINDOW_S {
            let end = o + (s + 1) * NS - 1;
            let ((ki, kv), (yi, yv), (ni, nv)) = (k.at(end), y.at(end), n.at(end));
            if !(kv && yv && nv) {
                continue;
            }
            let (ki, yi, ni) = (ki.expect("valid"), yi.expect("valid"), ni.expect("valid"));
            self.open_unix.push(Some(open_unix));
            self.sec.push(Some(s));
            let s_px = match cl {
                Some((t, p)) if !t.is_empty() => asof(t, end).map(|j| p[j]),
                _ => None,
            };
            let vals = [
                px(k.bid[ki]),
                px(k.ask[ki]),
                px(y.bid[yi]),
                px(y.ask[yi]),
                px(n.bid[ni]),
                px(n.ask[ni]),
                y.asz[yi] as f64,
                n.asz[ni] as f64,
            ];
            for (c, v) in self.cols.iter_mut().zip(vals) {
                c.push(Some(v));
            }
            self.cols[8].push(s_px);
            self.asset.push(Some(asset.to_owned()));
        }
    }

    fn frame(self) -> Frame {
        let mut f = Frame::default();
        f.push("open_unix", Col::I64(self.open_unix));
        f.push("sec", Col::I64(self.sec));
        for (name, c) in GRID_COLS.iter().zip(self.cols) {
            f.push(name, Col::F64(c));
        }
        f.push("asset", Col::Str(self.asset));
        f
    }
}

fn chainlink_window(cl: Option<&(Vec<i64>, Vec<f64>)>, open_unix: i64) -> Row {
    let mut m = Row::new();
    let Some((t, px)) = cl.filter(|c| !c.0.is_empty()) else { return m };
    let (o, c) = (open_unix * NS, (open_unix + WINDOW_S) * NS);
    let last60: Vec<f64> =
        t.iter().zip(px).filter(|(t, _)| **t >= c - 60 * NS && **t < c).map(|(_, p)| *p).collect();
    m.insert("cl_open".into(), asof(t, o).map(|i| px[i]).into());
    m.insert("cl_close_last".into(), asof(t, c).map(|i| px[i]).into());
    m.insert("cl_twap60".into(), pk_stats::np_mean(&last60).into());
    m
}

/// Polymarket trades on `tokens`, receive time mapped to wall, per token in wall order.
type Trades = HashMap<String, Vec<(i64, f64, f64, String)>>;

fn poly_trades(root: &Path, day: &str, tokens: &[&str], clock: &MonoToWall) -> anyhow::Result<Trades> {
    let mut out: Trades = HashMap::new();
    for stream in ["Polymarket_CLOB_Trade", "Polymarket_CLOB_No_Trade"] {
        let p = path(root, stream, day);
        if !p.exists() {
            continue;
        }
        for r in iter_poly_trades(&p)? {
            if tokens.contains(&r.asset_id.as_str()) {
                out.entry(r.asset_id.clone()).or_default().push((
                    clock.wall(r.ts_received_ns),
                    r.price,
                    r.size,
                    r.side,
                ));
            }
        }
    }
    for rows in out.values_mut() {
        rows.sort_by_key(|r| r.0);
    }
    Ok(out)
}

pub type Resolutions = HashMap<(String, i64), Option<String>>;

/// `resolutions.parquet` as (asset, open_unix) → Polymarket's outcome.
pub fn load_resolutions(p: &Path) -> anyhow::Result<Resolutions> {
    if !p.is_file() {
        anyhow::bail!("{} is missing: run `pk helix resolutions` first", p.display());
    }
    let f = Frame::read_parquet(p)?;
    let (a, o, out) = (f.col("asset"), f.col("open_unix"), f.cols.get("poly_outcome"));
    Ok((0..f.len())
        .filter_map(|i| {
            Some(((a.str(i)?.to_owned(), o.i64(i)?), out.and_then(|c| c.str(i)).map(str::to_owned)))
        })
        .collect())
}

/// One day: the four parquet outputs and the summary, written atomically
/// (a temporary directory renamed into place).
pub fn run_day(
    day: &str,
    out_dir: &Path,
    root: &Path,
    resolutions: &Resolutions,
    shift_ms: &BTreeMap<char, f64>,
) -> anyhow::Result<Value> {
    let started = std::time::Instant::now();
    let mut timings = Map::new();
    let missing: Vec<&str> = ["Polymarket_CLOB_Yes_BBO", "Polymarket_CLOB_No_BBO", "Kalshi_Yes_BBO"]
        .into_iter()
        .filter(|s| !path(root, s, day).exists())
        .collect();
    if !missing.is_empty() {
        return Ok(json!({"day": day, "skipped": format!("missing {}", missing.join(", "))}));
    }
    let windows = iter_windows(day, root)?;
    if windows.is_empty() {
        return Ok(json!({"day": day, "skipped": "no windows"}));
    }
    let yes_tokens: Vec<String> = windows.iter().map(|w| w.token_yes.clone()).collect();
    let no_tokens: Vec<String> = windows.iter().map(|w| w.token_no.clone()).collect();
    let t0 = std::time::Instant::now();
    let yes = poly_bbo_series(
        root,
        "Polymarket_CLOB_Yes_BBO",
        day,
        &yes_tokens,
        Extra { recv: true, ..Default::default() },
    )?;
    let no = poly_bbo_series(root, "Polymarket_CLOB_No_BBO", day, &no_tokens, Extra::default())?;
    timings.insert("poly".into(), round_dp(t0.elapsed().as_secs_f64(), 1).into());
    let t0 = std::time::Instant::now();
    let aids: Vec<u16> = HELIX_15M.iter().map(|(a, _)| *a).collect();
    let ky =
        kalshi_quote_series(root, "Kalshi_Yes_BBO", day, &aids, Extra { seq: true, ..Default::default() })?;
    timings.insert("kalshi".into(), round_dp(t0.elapsed().as_secs_f64(), 1).into());
    let t0 = std::time::Instant::now();
    let spot: Vec<u16> = CHAINLINK_SPOT.iter().map(|(_, a)| *a).collect();
    let cl_by_id = chainlink_prices(root, day, &spot)?;
    let chainlink: HashMap<&str, &(Vec<i64>, Vec<f64>)> =
        CHAINLINK_SPOT.iter().filter_map(|(name, aid)| cl_by_id.get(aid).map(|v| (*name, v))).collect();
    let clock = MonoToWall::new(&yes, 100_000);
    let all_tokens: Vec<&str> = yes_tokens.iter().chain(&no_tokens).map(String::as_str).collect();
    let trades = poly_trades(root, day, &all_tokens, &clock)?;
    timings.insert("trades_chainlink".into(), round_dp(t0.elapsed().as_secs_f64(), 1).into());

    let shift_k = (shift_ms.get(&'k').copied().unwrap_or(0.0) * 1e6).round_ties_even() as i64;
    let shift_p = (shift_ms.get(&'p').copied().unwrap_or(0.0) * 1e6).round_ties_even() as i64;
    let vars = variants();
    let mut crossings: Vec<Row> = Vec::new();
    let mut window_rows: Vec<Row> = Vec::new();
    let mut grid = Grid::default();
    let mut slices: Vec<Row> = Vec::new();
    let t0 = std::time::Instant::now();
    for (idx, w) in windows.iter().enumerate() {
        let asset = asset_name(w.asset).expect("15-minute asset");
        let (o, c) = (w.open_unix * NS, (w.open_unix + WINDOW_S) * NS);
        let k_all = kalshi_leg(&ky, aids.iter().position(|a| *a == w.asset).expect("listed"), o, shift_k);
        let k = k_all.clip(o, c);
        let y = Leg::of(&yes, yes.rows(idx), shift_p).clip(o, c);
        let n = Leg::of(&no, no.rows(idx), shift_p).clip(o, c);
        let (found, stats) = scan_window(&k, &y, &n, w.open_unix, &vars);
        let mut row = Row::new();
        row.insert("asset".into(), asset.into());
        row.insert("open_unix".into(), w.open_unix.into());
        row.insert("strike".into(), (!w.strike.is_nan()).then_some(w.strike).into());
        row.insert(
            "poly_outcome".into(),
            resolutions.get(&(asset.to_owned(), w.open_unix)).cloned().flatten().into(),
        );
        row.insert("kalshi_switch_ms".into(), k_all.t.first().map(|t| (t - o) as f64 / 1e6).into());
        row.insert("k_rows".into(), k.t.len().into());
        row.insert("y_rows".into(), y.t.len().into());
        row.insert("n_rows".into(), n.t.len().into());
        row.extend(stats);
        row.extend(chainlink_window(chainlink.get(asset).copied(), w.open_unix));
        window_rows.push(row);
        grid.add(&k, &y, &n, w.open_unix, asset, chainlink.get(asset).copied());
        let mut seen = Vec::new();
        for mut f in found {
            f.insert("asset".into(), asset.into());
            let t_x = f["t_ns"].as_i64().expect("t_ns");
            crossings.push(f);
            if seen.contains(&t_x) {
                continue;
            }
            seen.push(t_x);
            slices.extend(slice_rows(asset, w, t_x, [("k", &k), ("y", &y), ("n", &n)], &trades));
        }
    }
    timings.insert("scan".into(), round_dp(t0.elapsed().as_secs_f64(), 1).into());

    let tmp = out_dir.join(format!(".{day}.tmp"));
    std::fs::remove_dir_all(&tmp).ok();
    std::fs::create_dir_all(&tmp)?;
    Frame::from_records(&crossings).write_parquet(&tmp.join("crossings.parquet"))?;
    Frame::from_records(&window_rows).write_parquet(&tmp.join("windows.parquet"))?;
    Frame::from_records(&slices).write_parquet(&tmp.join("slices.parquet"))?;
    let grid = grid.frame();
    let grid_rows = grid.len();
    grid.write_parquet(&tmp.join("grid.parquet"))?;
    let summary = json!({
        "day": day,
        "windows": windows.len(),
        "crossings": crossings.len(),
        "slices_rows": slices.len(),
        "grid_rows": grid_rows,
        "rows": {"poly_yes_raw": yes.raw_rows, "poly_yes": yes.len(), "poly_no": no.len(),
                 "kalshi_raw": ky.raw_rows, "kalshi": ky.len()},
        "shift_ms": shift_ms.iter().map(|(k, v)| (k.to_string(), json!(v))).collect::<Map<_, _>>(),
        "timings_s": timings,
        "seconds": round_dp(started.elapsed().as_secs_f64(), 1),
        "variants": vars.iter().map(|(n, p)| ((*n).to_owned(), json!(p))).collect::<Map<_, _>>(),
    });
    std::fs::write(tmp.join("summary.json"), serde_json::to_string_pretty(&summary)?)?;
    let day_dir = out_dir.join(day);
    std::fs::remove_dir_all(&day_dir).ok();
    std::fs::rename(&tmp, &day_dir)?;
    Ok(summary)
}

fn slice_rows(asset: &str, w: &PolyWindow, t_x: i64, legs: [(&str, &Leg); 3], trades: &Trades) -> Vec<Row> {
    let (lo, hi) = (t_x - SLICE_BEFORE_NS, t_x + SLICE_AFTER_NS);
    let mut out = Vec::new();
    let row = |kind: String, dt: i64| {
        let mut r = Row::new();
        r.insert("asset".into(), asset.into());
        r.insert("open_unix".into(), w.open_unix.into());
        r.insert("t_cross".into(), t_x.into());
        r.insert("kind".into(), kind.into());
        r.insert("dt_ms".into(), ((dt - t_x) as f64 / 1e6).into());
        r
    };
    for (name, leg) in legs {
        let part = leg.clip(lo, hi);
        for j in 0..part.t.len() {
            let mut r = row(format!("bbo_{name}"), part.t[j]);
            r.insert("bid".into(), px(part.bid[j]).into());
            r.insert("ask".into(), px(part.ask[j]).into());
            r.insert("bsz".into(), (part.bsz[j] as f64).into());
            r.insert("asz".into(), (part.asz[j] as f64).into());
            for k in ["trade_px", "trade_sz", "trade_side"] {
                r.insert(k.into(), Value::Null);
            }
            out.push(r);
        }
    }
    for (name, token) in [("y", &w.token_yes), ("n", &w.token_no)] {
        let Some(tr) = trades.get(token) else { continue };
        let a = tr.partition_point(|r| r.0 < lo);
        let b = tr.partition_point(|r| r.0 < hi);
        for (t, price, size, side) in &tr[a..b.max(a)] {
            let mut r = row(format!("trade_{name}"), *t);
            for k in ["bid", "ask", "bsz", "asz"] {
                r.insert(k.into(), Value::Null);
            }
            r.insert("trade_px".into(), (*price).into());
            r.insert("trade_sz".into(), (*size).into());
            r.insert("trade_side".into(), side.clone().into());
            out.push(r);
        }
    }
    out
}

pub struct RunArgs {
    pub root: PathBuf,
    pub days: Option<String>,
    pub out: PathBuf,
    pub resolutions: PathBuf,
    pub jobs: usize,
    pub shift_ms: BTreeMap<char, f64>,
    pub force: bool,
    /// Stop before the disk falls under this (default [`MIN_FREE_GB`]).
    pub min_free_gb: f64,
}

/// Days in parallel (`--jobs`, about 3 GB each on a real day), stopping
/// before the disk falls under `min_free_gb`. One bad day does not stop the batch.
pub fn run(args: &RunArgs) -> anyhow::Result<()> {
    std::fs::create_dir_all(&args.out)?;
    let resolutions = load_resolutions(&args.resolutions)?;
    let todo: Vec<String> = super::select_days(&args.root, "Polymarket_Window", args.days.as_deref())
        .into_iter()
        .filter(|d| args.force || !args.out.join(d).join("summary.json").is_file())
        .collect();
    println!("{} days to run", todo.len());
    let queue = std::sync::Mutex::new(todo.into_iter());
    let stop = std::sync::atomic::AtomicBool::new(false);
    std::thread::scope(|s| {
        for _ in 0..args.jobs.max(1) {
            s.spawn(|| {
                loop {
                    if stop.load(std::sync::atomic::Ordering::Relaxed) {
                        return;
                    }
                    let free =
                        fs2::available_space(&args.out).map(|b| b as f64 / 1e9).unwrap_or(f64::INFINITY);
                    if free < args.min_free_gb {
                        eprintln!("under {} GB free: stopping (PLAN 3.3)", args.min_free_gb);
                        stop.store(true, std::sync::atomic::Ordering::Relaxed);
                        return;
                    }
                    let Some(day) = queue.lock().expect("queue").next() else { return };
                    match run_day(&day, &args.out, &args.root, &resolutions, &args.shift_ms) {
                        Ok(s) => {
                            let keys = ["day", "skipped", "crossings", "seconds", "timings_s"];
                            let m: Map<String, Value> = keys
                                .iter()
                                .filter_map(|k| s.get(*k).map(|v| ((*k).to_owned(), v.clone())))
                                .collect();
                            println!("{}", Value::Object(m));
                        }
                        Err(e) => println!("{}", json!({"day": day, "error": format!("{e:#}")})),
                    }
                }
            });
        }
    });
    if stop.load(std::sync::atomic::Ordering::Relaxed) {
        anyhow::bail!("under {} GB free", args.min_free_gb);
    }
    Ok(())
}
