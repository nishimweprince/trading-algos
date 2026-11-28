//! What the monitoring page shows: the run's results so far, rebuilt from the
//! journal at start and kept up to date from every record written after.
//! The per-second `pu_sec` lines are skipped (they are the forward tape, not
//! results), so the summary stays small however long the run.
//!
//! The statistics are `pu_review`'s: net ¢ per share after fees, with a
//! standard error clustered by window (positions in one window share its
//! outcome), and the same bands.

use std::collections::{BTreeMap, HashMap, VecDeque};
use std::path::Path;

use serde_json::{Value, json};

use crate::engine::NS;

/// Cost-per-share bands for the per-dollar tables.
const PRICE_BANDS: [(f64, f64); 4] = [(0.0, 0.30), (0.30, 0.50), (0.50, 0.70), (0.70, 1.01)];

fn price_label(lo: f64, hi: f64) -> String {
    format!("cost {:.2}–{:.2}", lo, hi.min(1.0))
}

/// The quick-exit pre-registration (README, epoch 7): the 2 s exit, settled
/// fills needed, and the t it must reach.
pub const EXIT_PRIMARY_MS: i64 = 2_000;
pub const EXIT_NEED: usize = 300;
pub const EXIT_T: f64 = 2.0;

/// The bar before going live (README).
pub const GATE_SETTLED: usize = 150;
pub const GATE_DAYS: f64 = 3.0;
pub const GATE_T: f64 = 2.0;
pub const GATE_AGREE: f64 = 0.99;
const KEEP_CLOSES: usize = 7 * 96;
const RECENT: usize = 40;
const SERIES_POINTS: usize = 1_500;

/// Results are read per epoch (PREREG.md): the current epoch's records drive
/// the page; earlier epochs' settled positions are kept as the baseline.
#[derive(Default)]
pub struct Summary {
    epoch: String,
    epoch_start_ns: Option<i64>,
    baseline: Vec<Value>,
    decisions: BTreeMap<u64, Value>,
    fills: HashMap<u64, Value>,
    no_fills: HashMap<u64, Value>,
    settles: Vec<Value>,
    resolved_both: u64,
    agree: u64,
    closes: VecDeque<Value>,
    halts: Vec<Value>,
    shadows: Vec<Value>,
    /// Delay study: `pu_fill_probe` of this epoch's decisions, and their settles.
    probes: Vec<Value>,
    probe_settles: Vec<Value>,
    windows: u64,
    first_ns: Option<i64>,
    /// Gap calibration: settled shadows of `cal_epochs`, with their entry's
    /// seconds left and near-side gap.
    cal_epochs: Vec<String>,
    cal_meta: HashMap<(String, i64), (f64, f64, f64)>,
    cal: Vec<CalRow>,
    /// The per-dollar gate's settled shadows (this epoch).
    rpd: Vec<Value>,
    /// Quick-exit study: probes and their settles (this epoch).
    exits: Vec<Value>,
    exit_settles: Vec<Value>,
}

/// One settled shadow: predicted gap to Kalshi's mid (x), to its near side
/// (x_cons), seconds left at entry, realized gap (won − cost).
#[derive(Clone)]
struct CalRow {
    open: i64,
    /// Cost per share (ask + fee).
    c: f64,
    x: f64,
    x_cons: f64,
    left: f64,
    r: f64,
}

/// β of r on x through the origin, its window-clustered s.e., and n.
fn fit(rows: &[&CalRow], x_of: impl Fn(&CalRow) -> f64) -> (f64, f64, usize) {
    let rows: Vec<(&CalRow, f64)> = rows.iter().map(|c| (*c, x_of(c))).filter(|(_, x)| x.is_finite()).collect();
    let sxx: f64 = rows.iter().map(|(_, x)| x * x).sum();
    if sxx <= 0.0 {
        return (f64::NAN, f64::NAN, 0);
    }
    let b = rows.iter().map(|(c, x)| x * c.r).sum::<f64>() / sxx;
    let mut by: HashMap<i64, f64> = HashMap::new();
    for (c, x) in &rows {
        *by.entry(c.open).or_default() += x * (c.r - b * x);
    }
    let se = if by.len() >= 20 { by.values().map(|v| v * v).sum::<f64>().sqrt() / sxx } else { f64::NAN };
    (b, se, rows.len())
}

fn f(v: &Value, k: &str) -> f64 {
    v.get(k).and_then(Value::as_f64).unwrap_or(f64::NAN)
}

fn i(v: &Value, k: &str) -> i64 {
    v.get(k).and_then(Value::as_i64).unwrap_or(0)
}

fn round(x: f64, dp: i32) -> Value {
    if x.is_finite() {
        let m = 10f64.powi(dp);
        ((x * m).round() / m).into()
    } else {
        Value::Null
    }
}

struct Row {
    key: (String, i64),
    shares: f64,
    pnl: f64,
}

/// Share-weighted ¢/share, its window-clustered s.e., windows, P&L.
fn clustered(rows: &[Row]) -> Value {
    let q: f64 = rows.iter().map(|r| r.shares).sum();
    let pnl: f64 = rows.iter().map(|r| r.pnl).sum();
    if q <= 0.0 {
        return json!({"n": 0, "windows": 0, "net_c": null, "se_c": null, "t": null, "pnl": 0.0});
    }
    let mean = pnl / q;
    let mut by: HashMap<&(String, i64), f64> = HashMap::new();
    for r in rows {
        *by.entry(&r.key).or_default() += r.pnl - mean * r.shares;
    }
    let windows = by.len();
    let se = if windows > 1 { by.values().map(|v| v * v).sum::<f64>().sqrt() / q } else { f64::NAN };
    // Under 20 windows the clustered s.e. means little (all wins give s.e. ≈ 0).
    let t = if windows >= 20 && se > 0.0 { mean / se } else { f64::NAN };
    json!({"n": rows.len(), "windows": windows, "net_c": round(100.0 * mean, 2), "se_c": round(100.0 * se, 2),
           "t": round(t, 2), "pnl": round(pnl, 2)})
}

/// Within US trading hours (13:30–20:00 UTC, weekdays).
fn us_hours(t_s: i64) -> bool {
    let dow = (t_s.div_euclid(86_400) + 4).rem_euclid(7); // 0 = Sunday
    let m = t_s.rem_euclid(86_400) / 60;
    (1..=5).contains(&dow) && (13 * 60 + 30..20 * 60).contains(&m)
}

impl Summary {
    /// Every record already in the journal (oldest file first).
    pub fn load(dir: &Path, epoch: &str, cal_epochs: &[String]) -> Self {
        let mut s =
            Summary { epoch: epoch.to_owned(), cal_epochs: cal_epochs.to_vec(), ..Summary::default() };
        let Ok(rd) = std::fs::read_dir(dir) else { return s };
        let mut files: Vec<_> = rd
            .filter_map(Result::ok)
            .map(|e| e.path())
            .filter(|p| p.extension().is_some_and(|x| x == "jsonl"))
            .collect();
        files.sort();
        for p in files {
            let Ok(text) = std::fs::read_to_string(&p) else { continue };
            for line in text.lines() {
                if line.contains("\"type\":\"pu_sec\"") {
                    continue;
                }
                if let Ok(v) = serde_json::from_str::<Value>(line) {
                    s.ingest(&v);
                }
            }
        }
        s
    }

    pub fn ingest(&mut self, r: &Value) {
        let kind = r.get("type").and_then(Value::as_str).unwrap_or_default();
        let id = r.get("id").and_then(Value::as_u64).unwrap_or(0);
        // Records from before epochs were written are epoch "1".
        let ep = r.get("epoch").and_then(Value::as_str).unwrap_or("1");
        let mine = ep == self.epoch;
        if self.cal_epochs.iter().any(|e| e == ep) {
            let key = (r["asset"].as_str().unwrap_or_default().to_owned(), i(r, "open"));
            match kind {
                "pu_shadow" => {
                    // Epoch 4 shadows have no x_cons: the near side is half Kalshi's spread under the mid.
                    let xc = r
                        .get("x_cons")
                        .and_then(Value::as_f64)
                        .unwrap_or_else(|| f(r, "edge") - f(r, "kalshi_spread") / 2.0);
                    self.cal_meta.insert(key, (f(r, "left"), xc, f(r, "ask") + f(r, "fee")));
                }
                "pu_shadow_settle" => {
                    let (left, xc, c) = self.cal_meta.remove(&key).unwrap_or((f64::NAN, f64::NAN, f64::NAN));
                    self.cal.push(CalRow {
                        open: key.1,
                        c: r.get("cost").and_then(Value::as_f64).unwrap_or(c),
                        x: f(r, "edge"),
                        x_cons: r.get("x_cons").and_then(Value::as_f64).unwrap_or(xc),
                        left,
                        r: f(r, "per_share"),
                    });
                }
                _ => {}
            }
        }
        match kind {
            "pu_window" => {
                self.windows += 1;
                self.first_ns.get_or_insert(i(r, "t_ns"));
                if mine {
                    self.epoch_start_ns.get_or_insert(i(r, "t_ns"));
                }
            }
            "pu_decision" if mine => {
                self.decisions.insert(id, r.clone());
            }
            // Fills belong to the epoch of their decision.
            "pu_fill" if self.decisions.contains_key(&id) => {
                self.fills.insert(id, r.clone());
            }
            "pu_no_fill" if self.decisions.contains_key(&id) => {
                self.no_fills.insert(id, r.clone());
            }
            "pu_settle" if mine => self.settles.push(r.clone()),
            "pu_shadow_settle" if mine => self.shadows.push(r.clone()),
            "pu_rpd_settle" if mine => self.rpd.push(r.clone()),
            "pu_exit_probe" if mine && r.get("error").is_none() => self.exits.push(r.clone()),
            "pu_exit_settle" if mine => self.exit_settles.push(r.clone()),
            "pu_fill_probe" if self.decisions.contains_key(&id) && r.get("error").is_none() => {
                self.probes.push(r.clone());
            }
            "pu_probe_settle" if mine => self.probe_settles.push(r.clone()),
            "pu_settle" => self.baseline.push(r.clone()),
            "pu_resolution" => {
                if let Some(a) = r.get("agree").and_then(Value::as_bool) {
                    self.resolved_both += 1;
                    self.agree += u64::from(a);
                }
            }
            "pu_close" if mine => {
                self.closes.push_back(r.clone());
                while self.closes.len() > KEEP_CLOSES {
                    self.closes.pop_front();
                }
            }
            "pu_halt" => self.halts.push(r.clone()),
            _ => {}
        }
    }

    fn rows<'a>(&self, keep: impl Fn(&Value) -> bool + 'a) -> Vec<Row> {
        Self::rows_of(&self.settles, keep)
    }

    fn rows_of<'a>(settles: &[Value], keep: impl Fn(&Value) -> bool + 'a) -> Vec<Row> {
        settles
            .iter()
            .filter(|s| keep(s))
            .map(|s| Row {
                key: (s["asset"].as_str().unwrap_or_default().to_owned(), i(s, "open")),
                shares: f(s, "shares"),
                pnl: f(s, "pnl"),
            })
            .collect()
    }

    /// The page's data. `live` is the engine's status at this instant.
    pub fn snapshot(&self, live: Value, now_ns: i64) -> Value {
        let now_s = now_ns / NS;
        let all = clustered(&self.rows(|_| true));
        let n = self.settles.len();
        let t_first = self.settles.iter().map(|s| i(s, "t_ns")).min();
        let t_last = self.settles.iter().map(|s| i(s, "t_ns")).max();
        let days = match (t_first, t_last) {
            (Some(a), Some(b)) => (b - a) as f64 / (86_400.0 * NS as f64),
            _ => 0.0,
        };
        let us = self.settles.iter().filter(|s| us_hours(i(s, "open") + 900)).count();
        let agree_rate = (self.resolved_both > 0).then(|| self.agree as f64 / self.resolved_both as f64);
        let decisions = self.decisions.len();
        let fill_rate = (decisions > 0).then(|| self.fills.len() as f64 / decisions as f64);
        let filled_sh: f64 = self.fills.values().map(|x| f(x, "shares")).filter(|x| x.is_finite()).sum();
        let instant_sh: f64 =
            self.decisions.values().map(|x| f(x, "instant_shares")).filter(|x| x.is_finite()).sum();
        let today = now_s - now_s.rem_euclid(86_400);
        let decisions_today = self.decisions.values().filter(|d| i(d, "t_ns") / NS >= today).count();
        // Booked by entry time, like the engine's daily loss.
        let pnl_today: f64 = self
            .settles
            .iter()
            .filter(|s| {
                self.decisions.get(&s["id"].as_u64().unwrap_or(0)).map_or(i(s, "t_ns"), |d| i(d, "t_ns")) / NS
                    >= today
            })
            .map(|s| f(s, "pnl"))
            .filter(|x| x.is_finite())
            .sum();

        // Cumulative P&L after each settle, thinned to at most SERIES_POINTS.
        let mut ordered: Vec<&Value> = self.settles.iter().collect();
        ordered.sort_by_key(|s| i(s, "t_ns"));
        let mut cum = 0.0;
        let mut series: Vec<Value> = Vec::with_capacity(ordered.len());
        for s in &ordered {
            cum += f(s, "pnl");
            series.push(json!([i(s, "t_ns") / 1_000_000, round(cum, 2)]));
        }
        if series.len() > SERIES_POINTS {
            let step = series.len().div_ceil(SERIES_POINTS);
            let last = series.last().cloned();
            series = series.into_iter().step_by(step).collect();
            if let Some(l) = last {
                series.push(l);
            }
        }

        let dec_of = |s: &Value| self.decisions.get(&s.get("id").and_then(Value::as_u64).unwrap_or(0));
        let by_asset: Vec<Value> = {
            let mut assets: Vec<String> =
                self.settles.iter().filter_map(|s| s["asset"].as_str().map(str::to_owned)).collect();
            assets.sort();
            assets.dedup();
            assets
                .into_iter()
                .map(|a| {
                    let mut v = clustered(&self.rows(|s| s["asset"].as_str() == Some(a.as_str())));
                    v["label"] = a.into();
                    v
                })
                .collect()
        };
        let band = |label: String, keep: &dyn Fn(&Value) -> bool| {
            let mut v = clustered(&self.rows(|s| keep(s)));
            v["label"] = label.into();
            v
        };
        let by_left: Vec<Value> =
            [(0.0, 120.0), (120.0, 300.0), (300.0, 480.0), (480.0, 660.0), (660.0, 901.0)]
                .iter()
                .map(|&(lo, hi)| {
                    band(format!("{lo:.0}–{hi:.0} s"), &|s| {
                        dec_of(s).is_some_and(|d| (lo..hi).contains(&f(d, "left")))
                    })
                })
                .filter(|v| v["n"].as_u64().unwrap_or(0) > 0)
                .collect();
        let calibration: Vec<Value> = [(0.5, 0.8), (0.8, 0.9), (0.9, 0.95), (0.95, 0.99), (0.99, 1.01)]
            .iter()
            .filter_map(|&(lo, hi)| {
                let rows: Vec<(&Value, &Value)> = self
                    .settles
                    .iter()
                    .filter_map(|s| dec_of(s).map(|d| (s, d)))
                    .filter(|(_, d)| (lo..hi).contains(&f(d, "p_side")))
                    .collect();
                if rows.is_empty() {
                    return None;
                }
                let k = rows.len() as f64;
                let won = rows.iter().filter(|(s, _)| s["won"].as_bool() == Some(true)).count() as f64;
                let model = rows.iter().map(|(_, d)| f(d, "p_side")).sum::<f64>() / k;
                let paid: Vec<f64> = rows
                    .iter()
                    .filter_map(|(s, _)| self.fills.get(&s["id"].as_u64().unwrap_or(0)).map(|x| f(x, "avg")))
                    .filter(|x| x.is_finite())
                    .collect();
                let paid =
                    if paid.is_empty() { f64::NAN } else { paid.iter().sum::<f64>() / paid.len() as f64 };
                Some(json!({"label": format!("{lo:.2}–{:.2}", hi.min(1.0)), "n": rows.len(),
                            "model": round(model, 3), "won": round(won / k, 3), "paid": round(paid, 3)}))
            })
            .collect();

        // Why the windows of the last 24 h did not trade.
        let mut gates: BTreeMap<String, u64> = BTreeMap::new();
        let mut closes_24h = 0;
        for c in self.closes.iter().filter(|c| i(c, "t_ns") / NS >= now_s - 86_400) {
            closes_24h += 1;
            *gates.entry(c["last_gate"].as_str().unwrap_or("not reached").to_owned()).or_default() += 1;
        }
        let mut gates: Vec<(String, u64)> = gates.into_iter().collect();
        gates.sort_by_key(|g| std::cmp::Reverse(g.1));

        let settle_of: HashMap<u64, &Value> =
            self.settles.iter().filter_map(|s| Some((s.get("id")?.as_u64()?, s))).collect();
        let recent: Vec<Value> = self
            .decisions
            .values()
            .rev()
            .take(RECENT)
            .map(|d| {
                let id = d["id"].as_u64().unwrap_or(0);
                let fill = self.fills.get(&id);
                let s = settle_of.get(&id);
                json!({
                    "id": id, "t_ns": d["t_ns"], "asset": d["asset"], "side": d["side"], "left": d["left"],
                    "p_side": d["p_side"], "kalshi_side": d["kalshi_side"], "ask": d["ask"], "edge": d["edge"], "limit": d["limit"],
                    "filled": fill.map(|x| x["shares"].clone()), "avg": fill.map(|x| x["avg"].clone()),
                    "ask_now": self.no_fills.get(&id).or(fill).map(|x| x["ask_now"].clone()),
                    "status": if s.is_some() { "settled" } else if fill.is_some() { "open" }
                              else if self.no_fills.contains_key(&id) { "missed" } else { "waiting" },
                    "won": s.map(|x| x["won"].clone()), "pnl": s.map(|x| x["pnl"].clone()),
                })
            })
            .collect();

        let sizing = self.sizing();
        let delays = self.delays();
        let gap_cal = self.gap_cal();
        let by_price = self.by_price();
        let gate_cmp = self.gate_compare();
        let all_usd = clustered(&Self::rows_usd(&self.settles, |_| true));
        let exit_study = self.exit_study();
        let min_edge = self.min_edge_compare();
        let mut base_epochs: Vec<&str> =
            self.baseline.iter().map(|s| s["epoch"].as_str().unwrap_or("1")).collect();
        base_epochs.sort_unstable();
        base_epochs.dedup();
        let t_ok =
            all["t"].as_f64().is_some_and(|t| t >= GATE_T) && all["net_c"].as_f64().is_some_and(|m| m > 0.0);
        json!({
            "now_ms": now_ns / 1_000_000,
            "live": live,
            "kpi": {
                "settled": n, "days": round(days, 2), "us_hours_settled": us, "decisions": decisions,
                "decisions_today": decisions_today, "pnl_today": round(pnl_today, 2),
                "fill_rate": fill_rate.map(|x| round(x, 3)),
                "delay_kept": if instant_sh > 0.0 { round(filled_sh / instant_sh, 3) } else { Value::Null },
                "agree": agree_rate.map(|x| round(x, 4)), "resolved": self.resolved_both,
                "windows": self.windows, "closes_24h": closes_24h,
                "since_ms": self.first_ns.map(|t| t / 1_000_000),
            },
            "all": all,
            "epoch": self.epoch,
            "epoch_since_ms": self.epoch_start_ns.map(|t| t / 1_000_000),
            "baseline": {
                "epochs": base_epochs,
                "all": clustered(&Self::rows_of(&self.baseline, |_| true)),
            },
            "gate": {
                "settled": {"need": GATE_SETTLED, "have": n, "ok": n >= GATE_SETTLED},
                "days": {"need": GATE_DAYS, "have": round(days, 2), "ok": days >= GATE_DAYS},
                "us_hours": {"have": us, "ok": us >= 30},
                "edge": {"need_t": GATE_T, "ok": t_ok},
                "agree": {"need": GATE_AGREE, "have": agree_rate.map(|x| round(x, 4)),
                          "ok": agree_rate.is_some_and(|a| a >= GATE_AGREE) && self.resolved_both >= 100},
            },
            "series": series,
            "by_asset": by_asset,
            "by_left": by_left,
            "calibration": calibration,
            "gates_24h": gates.iter().map(|(g, c)| json!({"gate": g, "n": c})).collect::<Vec<_>>(),
            "recent": recent,
            "halts": self.halts,
            "sizing": sizing,
            "min_edge": min_edge,
            "delays": delays,
            "gap_cal": gap_cal,
            "by_price": by_price,
            "gate_cmp": gate_cmp,
            "all_usd": all_usd,
            "exits": exit_study,
        })
    }

    /// The settled shadows that fit calibrated Kelly's β, as (x, r), oldest first.
    pub fn cal_seed(&self) -> Vec<(f64, f64)> {
        self.cal.iter().map(|c| (c.x, c.r)).collect()
    }

    /// Does a gap to Kalshi pay? Settled shadows of `cal_epochs` by predicted
    /// gap and by seconds left: mean predicted gap to the mid and to the near
    /// side, mean realized gap (won − cost) with a window-clustered s.e., and
    /// β (realized ÷ predicted, fitted through the origin) for both.
    fn gap_cal(&self) -> Value {
        let row = |label: String, rs: Vec<&CalRow>| {
            let k = rs.len() as f64;
            let mean = |g: &dyn Fn(&CalRow) -> f64| {
                let v: Vec<f64> = rs.iter().map(|c| g(c)).filter(|x| x.is_finite()).collect();
                if v.is_empty() { f64::NAN } else { v.iter().sum::<f64>() / v.len() as f64 }
            };
            let rows: Vec<Row> =
                rs.iter().map(|c| Row { key: (String::new(), c.open), shares: 1.0, pnl: c.r }).collect();
            let cl = clustered(&rows);
            let (b, b_se, _) = fit(&rs, |c| c.x);
            let (bc, _, _) = fit(&rs, |c| c.x_cons);
            let cs: Vec<&&CalRow> = rs.iter().filter(|c| c.c.is_finite()).collect();
            let csum: f64 = cs.iter().map(|c| c.c).sum();
            let per_usd = |g: &dyn Fn(&CalRow) -> f64| {
                if csum > 0.0 { round(100.0 * cs.iter().map(|c| g(c)).sum::<f64>() / csum, 1) } else { Value::Null }
            };
            json!({"pred_pd": per_usd(&|c| c.x), "real_pd": per_usd(&|c| c.r), "cost": round(mean(&|c| c.c), 3),
                   "label": label, "n": k as u64, "windows": cl["windows"], "pred_c": round(100.0 * mean(&|c| c.x), 2),
                   "cons_c": round(100.0 * mean(&|c| c.x_cons), 2), "real_c": cl["net_c"], "se_c": cl["se_c"],
                   "t": cl["t"], "beta": round(b, 2), "beta_se": round(b_se, 2), "beta_cons": round(bc, 2)})
        };
        let all: Vec<&CalRow> = self.cal.iter().collect();
        let mut by_gap = Vec::new();
        for (lo, hi) in [(0.03, 0.05), (0.05, 0.07), (0.07, 0.09), (0.09, 0.11), (0.11, 0.151)] {
            let rs: Vec<&CalRow> = all.iter().copied().filter(|c| (lo..hi).contains(&c.x)).collect();
            if !rs.is_empty() {
                by_gap.push(row(format!("{:.0}–{:.0}¢", 100.0 * lo, (100.0 * hi).floor()), rs));
            }
        }
        let mut by_left = Vec::new();
        for (lo, hi) in [(0.0, 300.0), (300.0, 600.0), (600.0, 901.0)] {
            let rs: Vec<&CalRow> = all.iter().copied().filter(|c| (lo..hi).contains(&c.left)).collect();
            if !rs.is_empty() {
                by_left.push(row(format!("{lo:.0}–{:.0} s left", hi.min(900.0)), rs));
            }
        }
        let mut by_price = Vec::new();
        for (lo, hi) in PRICE_BANDS {
            let rs: Vec<&CalRow> = all.iter().copied().filter(|c| (lo..hi).contains(&c.c)).collect();
            if !rs.is_empty() {
                by_price.push(row(price_label(lo, hi), rs));
            }
        }
        json!({"epochs": self.cal_epochs, "all": if all.is_empty() { Value::Null } else { row("All".into(), all) },
               "by_gap": by_gap, "by_left": by_left, "by_price": by_price})
    }

    /// The quick-exit study: per exit delay, how much of the position the bid
    /// took, net per share settled (exit, with any unsold remainder held), the
    /// same positions held, and the paired difference, clustered by window.
    fn exit_study(&self) -> Value {
        let mut ds: Vec<i64> = self.exits.iter().map(|p| i(p, "delay_ms")).collect();
        ds.sort_unstable();
        ds.dedup();
        let rows_of = |d: i64, k: &str| -> Vec<Row> {
            self.exit_settles
                .iter()
                .filter(|s| i(s, "delay_ms") == d)
                .map(|s| Row { key: (String::new(), i(s, "open")), shares: f(s, "shares"), pnl: f(s, k) })
                .filter(|r| r.shares.is_finite() && r.shares > 0.0 && r.pnl.is_finite())
                .collect()
        };
        let mut primary = Value::Null;
        let rows: Vec<Value> = ds
            .iter()
            .map(|&d| {
                let ps: Vec<&Value> = self.exits.iter().filter(|p| i(p, "delay_ms") == d).collect();
                let full = ps.iter().filter(|p| f(p, "sold") >= f(p, "shares") - 1e-9).count();
                let sold: f64 = ps.iter().map(|p| f(p, "sold")).filter(|x| x.is_finite()).sum();
                let want: f64 = ps.iter().map(|p| f(p, "shares")).filter(|x| x.is_finite()).sum();
                let ex = clustered(&rows_of(d, "pnl"));
                let hold = clustered(&rows_of(d, "hold_pnl"));
                let diff = clustered(&rows_of(d, "diff"));
                let v = json!({"label": format!("{:.0} s", d as f64 / 1000.0), "delay_ms": d, "probes": ps.len(),
                       "full_rate": if ps.is_empty() { Value::Null } else { round(full as f64 / ps.len() as f64, 3) },
                       "sold_frac": if want > 0.0 { round(sold / want, 3) } else { Value::Null },
                       "n": ex["n"], "windows": ex["windows"], "exit_c": ex["net_c"], "exit_se": ex["se_c"], "exit_t": ex["t"],
                       "hold_c": hold["net_c"], "diff_c": diff["net_c"], "diff_se": diff["se_c"], "diff_t": diff["t"]});
                if d == EXIT_PRIMARY_MS {
                    let n = ex["n"].as_u64().unwrap_or(0) as usize;
                    let t = ex["t"].as_f64();
                    let m = ex["net_c"].as_f64();
                    primary = json!({"delay_ms": d, "need": EXIT_NEED, "have": n, "need_t": EXIT_T, "t": ex["t"],
                        "verdict": if n < EXIT_NEED { "collecting" }
                                   else if t.is_some_and(|t| t >= EXIT_T) && m.is_some_and(|m| m > 0.0) { "pass" }
                                   else { "fail" }});
                }
                v
            })
            .collect();
        json!({"rows": rows, "primary": primary})
    }

    /// Settled positions weighted by dollars: `clustered` with shares = cost,
    /// so its "net_c" is return per dollar in percent.
    fn rows_usd<'a>(settles: &[Value], keep: impl Fn(&Value) -> bool + 'a) -> Vec<Row> {
        settles
            .iter()
            .filter(|s| keep(s))
            .map(|s| Row {
                key: (s["asset"].as_str().unwrap_or_default().to_owned(), i(s, "open")),
                shares: f(s, "usd") + f(s, "fee"),
                pnl: f(s, "pnl"),
            })
            .filter(|r| r.shares.is_finite() && r.pnl.is_finite())
            .collect()
    }

    /// Per share and per dollar for one group of settled positions.
    fn per_usd(rows_sh: &[Row], rows_usd: &[Row], label: String) -> Value {
        let mut v = clustered(rows_sh);
        let u = clustered(rows_usd);
        v["label"] = label.into();
        v["cost"] = round(rows_usd.iter().map(|r| r.shares).sum(), 2);
        v["per_usd_pct"] = u["net_c"].clone();
        v["per_usd_se"] = u["se_c"].clone();
        v["t_usd"] = u["t"].clone();
        v
    }

    /// Settled positions by cost per share: per share and per dollar.
    fn by_price(&self) -> Vec<Value> {
        let cost_sh = |s: &Value| (f(s, "usd") + f(s, "fee")) / f(s, "shares");
        PRICE_BANDS
            .iter()
            .map(|&(lo, hi)| {
                let keep = |s: &Value| (lo..hi).contains(&cost_sh(s));
                Self::per_usd(&self.rows(keep), &Self::rows_usd(&self.settles, keep), price_label(lo, hi))
            })
            .filter(|v| v["n"].as_u64().unwrap_or(0) > 0)
            .collect()
    }

    /// The traded 5¢ gate against the per-dollar gate, both as shadows costed at
    /// the ask plus fee for one share, per share and per dollar, clustered by window.
    fn gate_compare(&self) -> Vec<Value> {
        let group = |label: &str, src: &[Value], keep: &dyn Fn(&Value) -> bool| {
            let pick: Vec<&Value> =
                src.iter().filter(|s| keep(s) && f(s, "cost").is_finite() && f(s, "per_share").is_finite()).collect();
            let key = |s: &Value| (String::new(), i(s, "open"));
            let sh: Vec<Row> = pick.iter().map(|s| Row { key: key(s), shares: 1.0, pnl: f(s, "per_share") }).collect();
            let usd: Vec<Row> =
                pick.iter().map(|s| Row { key: key(s), shares: f(s, "cost"), pnl: f(s, "per_share") }).collect();
            let mut v = Self::per_usd(&sh, &usd, label.to_owned());
            v["avg_cost"] =
                if pick.is_empty() { Value::Null } else { round(f(&v, "cost") / pick.len() as f64, 3) };
            v
        };
        vec![
            group("5¢ gate (traded rule)", &self.shadows, &|s| s["real"].as_bool() == Some(true)),
            group("Per-dollar gate", &self.rpd, &|_| true),
            group("Per-dollar, also ≥ 5¢", &self.rpd, &|s| s["passes_5c"].as_bool() == Some(true)),
            group("Per-dollar only (< 5¢)", &self.rpd, &|s| s["passes_5c"].as_bool() == Some(false)),
        ]
    }

    /// Each counterfactual sizing rule over the settled positions (settle order):
    /// P&L, cost risked, return on cost, worst drawdown, window-clustered t.
    fn sizing(&self) -> Vec<Value> {
        let mut ordered: Vec<&Value> =
            self.settles.iter().filter(|s| s.get("alt").is_some_and(Value::is_object)).collect();
        ordered.sort_by_key(|s| i(s, "t_ns"));
        // Kelly rows count only positions sized under the current Kelly caps.
        let cap_now = ordered.iter().rev().find_map(|s| s.get("kelly_cap").filter(|v| v.is_object())).cloned();
        let same_cap = |s: &Value| match &cap_now {
            Some(c) => s.get("kelly_cap") == Some(c),
            None => true,
        };
        crate::engine::RULES
            .iter()
            .map(|rule| {
                let (mut cum, mut peak, mut dd, mut risked) = (0.0f64, 0.0f64, 0.0f64, 0.0);
                let mut rows = Vec::new();
                let kelly = rule.starts_with("kelly");
                for s in ordered.iter().filter(|s| !kelly || same_cap(s)) {
                    let a = &s["alt"][*rule];
                    let (sh, pnl, cost) = (f(a, "shares"), f(a, "pnl"), f(a, "cost"));
                    if !(sh.is_finite() && pnl.is_finite() && cost.is_finite()) {
                        continue;
                    }
                    cum += pnl;
                    peak = peak.max(cum);
                    dd = dd.max(peak - cum);
                    risked += cost;
                    if sh > 1e-9 {
                        rows.push(Row {
                            key: (s["asset"].as_str().unwrap_or_default().to_owned(), i(s, "open")),
                            shares: sh,
                            pnl,
                        });
                    }
                }
                // Positions in one window move together across coins: cluster by window open.
                for r in &mut rows {
                    r.key.0.clear();
                }
                let mut v = clustered(&rows);
                v["label"] = (*rule).into();
                v["risked"] = round(risked, 2);
                v["ret_pct"] = if risked > 0.0 { round(100.0 * cum / risked, 2) } else { Value::Null };
                v["max_dd"] = round(dd, 2);
                v["shares"] = round(rows.iter().map(|r| r.shares).sum(), 1);
                if kelly {
                    v["cap"] = cap_now.clone().unwrap_or(Value::Null);
                }
                v
            })
            .collect()
    }

    /// The delay study: per probe delay, overall and per coin, how much of the
    /// order filled, the slippage against the decision's ask, and settled net
    /// ¢/share clustered by window.
    fn delays(&self) -> Vec<Value> {
        let mut ds: Vec<i64> = self.probes.iter().map(|p| i(p, "delay_ms")).collect();
        ds.sort_unstable();
        ds.dedup();
        let mut assets: Vec<String> =
            self.probes.iter().filter_map(|p| p["asset"].as_str().map(str::to_owned)).collect();
        assets.sort();
        assets.dedup();
        let mut out = Vec::new();
        for d in ds {
            for a in std::iter::once(None).chain(assets.iter().map(Some)) {
                let keep =
                    |v: &Value| i(v, "delay_ms") == d && a.is_none_or(|a| v["asset"].as_str() == Some(a));
                let ps: Vec<&Value> = self.probes.iter().filter(|p| keep(p)).collect();
                if ps.is_empty() {
                    continue;
                }
                let filled: Vec<&&Value> = ps.iter().filter(|p| f(p, "shares") > 1e-9).collect();
                let want: f64 = ps.iter().map(|p| f(p, "want")).filter(|x| x.is_finite()).sum();
                let got: f64 = filled.iter().map(|p| f(p, "shares")).sum();
                let slip: Vec<f64> = filled.iter().map(|p| f(p, "slip")).filter(|x| x.is_finite()).collect();
                let rows = Self::rows_of(&self.probe_settles, |s| keep(s));
                let mut v = clustered(&rows);
                v["label"] = format!("{d} ms").into();
                v["delay_ms"] = d.into();
                v["asset"] = a.map_or("All".to_owned(), Clone::clone).into();
                v["probes"] = ps.len().into();
                v["fill_rate"] = round(filled.len() as f64 / ps.len() as f64, 3);
                v["share_fill"] = if want > 0.0 { round(got / want, 3) } else { Value::Null };
                v["slip_c"] = if slip.is_empty() {
                    Value::Null
                } else {
                    round(100.0 * slip.iter().sum::<f64>() / slip.len() as f64, 2)
                };
                out.push(v);
            }
        }
        out
    }

    /// The min-edge-3¢ rule (every shadow) against its 3–5¢ part and the ≥ 5¢ part,
    /// per share at top of book, clustered by window.
    fn min_edge_compare(&self) -> Vec<Value> {
        let group = |label: &str, keep: &dyn Fn(&Value) -> bool| {
            let rows: Vec<Row> = self
                .shadows
                .iter()
                .filter(|s| keep(s))
                .map(|s| Row { key: (String::new(), i(s, "open")), shares: 1.0, pnl: f(s, "per_share") })
                .filter(|r| r.pnl.is_finite())
                .collect();
            let mut v = clustered(&rows);
            v["label"] = label.into();
            v
        };
        vec![
            group("All gaps ≥ 3¢ (old rule)", &|_| true),
            group("3–5¢ only (dropped)", &|s| s["real"].as_bool() == Some(false)),
            group("≥ 5¢ (kept)", &|s| s["real"].as_bool() == Some(true)),
        ]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn settle(id: u64, asset: &str, open: i64, pnl: f64, won: bool, t_ns: i64) -> Value {
        json!({"type": "pu_settle", "id": id, "asset": asset, "open": open, "shares": 10.0, "pnl": pnl, "won": won, "t_ns": t_ns})
    }

    #[test]
    fn scores_and_bands() {
        let mut s = Summary { epoch: "1".into(), ..Summary::default() };
        for k in 0..30u64 {
            let open = 1_800_000_000 + 900 * k as i64;
            let t = (open + 930) * NS;
            s.ingest(
                &json!({"type": "pu_decision", "id": k + 1, "asset": "BTC", "left": 25.0, "p_side": 0.85,
                             "instant_shares": 10.0, "t_ns": t - 30 * NS}),
            );
            s.ingest(
                &json!({"type": "pu_fill", "id": k + 1, "shares": 10.0, "avg": 0.85, "t_ns": t - 30 * NS}),
            );
            let won = k % 10 != 0;
            s.ingest(&settle(k + 1, "BTC", open, if won { 1.4 } else { -8.6 }, won, t));
            s.ingest(&json!({"type": "pu_resolution", "agree": true, "t_ns": t}));
        }
        let v = s.snapshot(json!({}), (1_800_000_000 + 900 * 31) * NS);
        assert_eq!(v["kpi"]["settled"], 30);
        assert_eq!(v["all"]["windows"], 30);
        // 27 × 1.4 − 3 × 8.6 = 12 over 300 shares = 4¢.
        assert!((v["all"]["net_c"].as_f64().unwrap() - 4.0).abs() < 1e-9);
        assert!(v["all"]["t"].as_f64().is_some());
        assert_eq!(v["kpi"]["fill_rate"], 1.0);
        assert_eq!(v["calibration"][0]["label"], "0.80–0.90");
        assert_eq!(v["calibration"][0]["won"], 0.9);
        assert_eq!(v["by_left"][0]["label"], "0–120 s");
        assert_eq!(v["series"].as_array().unwrap().len(), 30);
        assert_eq!(v["recent"].as_array().unwrap().len(), 30);
        assert_eq!(v["gate"]["settled"]["ok"], false);
    }

    #[test]
    fn sizing_and_min_edge_tables() {
        let mut s = Summary { epoch: "1".into(), ..Summary::default() };
        let mut a = settle(1, "BTC", 0, 5.0, true, NS);
        a["alt"] = json!({"flat": {"shares": 10.0, "cost": 5.0, "pnl": 5.0}, "tiers": {"shares": 20.0, "cost": 10.0, "pnl": 10.0}});
        let mut b = settle(2, "ETH", 900, -5.0, false, 2 * NS);
        b["alt"] = json!({"flat": {"shares": 10.0, "cost": 5.0, "pnl": -5.0}, "tiers": {"shares": 10.0, "cost": 5.0, "pnl": -5.0}});
        s.ingest(&a);
        s.ingest(&b);
        s.ingest(&json!({"type": "pu_shadow_settle", "open": 0, "real": false, "per_share": -0.4}));
        s.ingest(&json!({"type": "pu_shadow_settle", "open": 900, "real": true, "per_share": 0.5}));
        let v = s.snapshot(json!({}), 3 * NS);
        let tiers = v["sizing"].as_array().unwrap().iter().find(|r| r["label"] == "tiers").unwrap().clone();
        assert_eq!(tiers["pnl"], 5.0);
        assert_eq!(tiers["risked"], 15.0);
        assert_eq!(tiers["max_dd"], 5.0);
        assert_eq!(v["min_edge"][1]["n"], 1);
        assert_eq!(v["min_edge"][0]["pnl"], 0.1);
    }

    #[test]
    fn gap_calibration_pools_cal_epochs() {
        let mut s = Summary::load(Path::new("/nonexistent"), "5", &["4".into(), "5".into()]);
        // Epoch 4 shadow: no x_cons, so the near side is half Kalshi's spread under the mid.
        s.ingest(&json!({"type": "pu_shadow", "epoch": "4", "asset": "BTC", "open": 0, "left": 700.0,
                         "edge": 0.06, "kalshi_spread": 0.02}));
        s.ingest(&json!({"type": "pu_shadow_settle", "epoch": "4", "asset": "BTC", "open": 0, "edge": 0.06,
                         "per_share": 0.5}));
        s.ingest(&json!({"type": "pu_shadow", "epoch": "5", "asset": "ETH", "open": 900, "left": 200.0,
                         "edge": 0.04, "x_cons": 0.02}));
        s.ingest(&json!({"type": "pu_shadow_settle", "epoch": "5", "asset": "ETH", "open": 900, "edge": 0.04,
                         "x_cons": 0.02, "per_share": -0.5}));
        // Epoch 3 is not a calibration epoch.
        s.ingest(&json!({"type": "pu_shadow_settle", "epoch": "3", "asset": "SOL", "open": 0, "edge": 0.1,
                         "per_share": 0.9}));
        assert_eq!(s.cal_seed(), vec![(0.06, 0.5), (0.04, -0.5)]);
        let v = s.snapshot(json!({}), NS);
        let g = &v["gap_cal"];
        assert_eq!(g["all"]["n"], 2);
        assert_eq!(g["all"]["cons_c"], 3.5); // (5 + 2) / 2 ¢
        assert_eq!(g["by_gap"][0]["label"], "3–5¢");
        assert_eq!(g["by_gap"][1]["label"], "5–7¢");
        assert_eq!(g["by_left"][0]["label"], "0–300 s left");
        // β = Σxr / Σx² = (0.03 − 0.02) / (0.0036 + 0.0016)
        assert!((g["all"]["beta"].as_f64().unwrap() - 1.92).abs() < 0.01);
    }

    #[test]
    fn per_dollar_tables() {
        let mut s = Summary { epoch: "6".into(), ..Summary::default() };
        // A cheap winner and an expensive loser: same shares, very different dollars.
        let mut a = settle(1, "BTC", 0, 8.5, true, NS);
        a["epoch"] = "6".into();
        a["shares"] = 10.0.into();
        a["usd"] = 1.4.into();
        a["fee"] = 0.1.into();
        let mut b = settle(2, "ETH", 900, -8.6, false, 2 * NS);
        b["epoch"] = "6".into();
        b["shares"] = 10.0.into();
        b["usd"] = 8.5.into();
        b["fee"] = 0.1.into();
        s.ingest(&a);
        s.ingest(&b);
        s.ingest(&json!({"type": "pu_rpd_settle", "epoch": "6", "open": 0, "cost": 0.2, "per_share": 0.8, "passes_5c": false}));
        s.ingest(&json!({"type": "pu_rpd_settle", "epoch": "6", "open": 900, "cost": 0.8, "per_share": -0.8, "passes_5c": true}));
        s.ingest(&json!({"type": "pu_shadow_settle", "epoch": "6", "open": 0, "cost": 0.5, "per_share": 0.5, "real": true}));
        let v = s.snapshot(json!({}), 3 * NS);
        // −0.1 USD on 10.1 USD of cost.
        assert_eq!(v["all_usd"]["net_c"], -0.99);
        let p = v["by_price"].as_array().unwrap();
        assert_eq!(p.len(), 2);
        assert_eq!(p[0]["label"], "cost 0.00–0.30");
        assert_eq!(p[0]["per_usd_pct"], 566.67); // 8.5 / 1.5
        let g = v["gate_cmp"].as_array().unwrap();
        assert_eq!(g[0]["per_usd_pct"], 100.0);
        assert_eq!(g[1]["n"], 2);
        assert_eq!(g[1]["per_usd_pct"], 0.0); // (0.8 − 0.8) / 1.0
        assert_eq!(g[1]["avg_cost"], 0.5);
        assert_eq!(g[3]["per_usd_pct"], 400.0);
    }

    #[test]
    fn kelly_rows_count_only_the_current_cap() {
        let mut s = Summary { epoch: "7".into(), ..Summary::default() };
        let alt = json!({"flat": {"shares": 10.0, "cost": 5.0, "pnl": 1.0}, "kelly_q": {"shares": 30.0, "cost": 15.0, "pnl": 3.0}});
        let mut a = settle(1, "BTC", 0, 1.0, true, NS);
        a["epoch"] = "7".into();
        a["alt"] = alt.clone();
        let mut b = settle(2, "ETH", 900, 1.0, true, 2 * NS);
        b["epoch"] = "7".into();
        b["alt"] = alt;
        b["kelly_cap"] = json!({"trade": 50.0, "window": 100.0});
        s.ingest(&a);
        s.ingest(&b);
        let v = s.snapshot(json!({}), 3 * NS);
        let rows = v["sizing"].as_array().unwrap();
        let get = |l: &str| rows.iter().find(|r| r["label"] == l).unwrap().clone();
        assert_eq!(get("flat")["n"], 2);
        assert_eq!(get("kelly_q")["n"], 1);
        assert_eq!(get("kelly_q")["cap"]["trade"], 50.0);
    }

    #[test]
    fn exit_study_pairs_exits_with_holding() {
        let mut s = Summary { epoch: "7".into(), ..Summary::default() };
        for (id, open, sold, pnl, hold) in [(1, 0, 10.0, 0.3, -4.4), (2, 900, 6.0, 4.5, 4.2)] {
            s.ingest(&json!({"type": "pu_exit_probe", "epoch": "7", "id": id, "open": open, "delay_ms": 2000,
                             "shares": 10.0, "sold": sold}));
            s.ingest(&json!({"type": "pu_exit_settle", "epoch": "7", "id": id, "open": open, "delay_ms": 2000,
                             "shares": 10.0, "pnl": pnl, "hold_pnl": hold, "diff": pnl - hold}));
        }
        s.ingest(&json!({"type": "pu_exit_probe", "epoch": "7", "id": 3, "delay_ms": 2000, "error": "timeout"}));
        let v = s.snapshot(json!({}), NS);
        let r = &v["exits"]["rows"][0];
        assert_eq!((r["label"].as_str(), r["probes"].as_u64()), (Some("2 s"), Some(2)));
        assert_eq!(r["full_rate"], 0.5);
        assert_eq!(r["sold_frac"], 0.8);
        assert_eq!(r["exit_c"], 24.0); // (0.3 + 4.5) / 20 shares
        assert_eq!(r["hold_c"], -1.0);
        assert_eq!(r["diff_c"], 25.0);
        assert_eq!(v["exits"]["primary"]["verdict"], "collecting");
    }

    #[test]
    fn epochs_are_scored_apart() {
        let mut s = Summary { epoch: "2".into(), ..Summary::default() };
        s.ingest(&settle(1, "BTC", 0, 1.0, true, NS)); // no epoch: "1"
        s.ingest(&json!({"type": "pu_decision", "id": 2, "epoch": "2", "t_ns": NS}));
        s.ingest(&json!({"type": "pu_fill", "id": 2, "shares": 10.0, "avg": 0.5, "t_ns": NS}));
        let mut two = settle(2, "BTC", 900, -2.0, false, 2 * NS);
        two["epoch"] = "2".into();
        s.ingest(&two);
        let v = s.snapshot(json!({}), 3 * NS);
        assert_eq!(v["all"]["n"], 1);
        assert_eq!(v["all"]["pnl"], -2.0);
        assert_eq!(v["baseline"]["all"]["pnl"], 1.0);
        assert_eq!(v["baseline"]["epochs"][0], "1");
        assert_eq!(v["kpi"]["fill_rate"], 1.0);
    }

    #[test]
    fn us_hours_are_weekday_afternoons_utc() {
        // 2026-11-24 is a Tuesday.
        let tue = 1_795_478_400; // 2026-11-24T00:00Z
        assert!(us_hours(tue + 15 * 3600));
        assert!(!us_hours(tue + 3 * 3600));
        assert!(!us_hours(tue - 2 * 86_400 + 15 * 3600)); // Sunday
    }

    #[test]
    fn delay_study_by_delay_and_coin() {
        let mut s = Summary { epoch: "3".into(), ..Summary::default() };
        s.ingest(&json!({"type": "pu_decision", "id": 1, "epoch": "3", "t_ns": NS}));
        for (d, sh, slip) in [(25, 10.0, 0.0), (150, 4.0, 0.01)] {
            s.ingest(&json!({"type": "pu_fill_probe", "id": 1, "asset": "BTC", "open": 0, "delay_ms": d,
                             "want": 10.0, "shares": sh, "slip": slip, "epoch": "3"}));
            s.ingest(&json!({"type": "pu_probe_settle", "id": 1, "asset": "BTC", "open": 0, "delay_ms": d,
                             "shares": sh, "pnl": sh * 0.4, "epoch": "3"}));
        }
        // A failed fetch and another epoch's decision are left out.
        s.ingest(&json!({"type": "pu_fill_probe", "id": 1, "delay_ms": 50, "error": "timeout"}));
        s.ingest(&json!({"type": "pu_fill_probe", "id": 9, "asset": "BTC", "delay_ms": 25, "shares": 1.0}));
        let v = s.snapshot(json!({}), 2 * NS);
        let rows = v["delays"].as_array().unwrap();
        assert_eq!(rows.len(), 4); // 25 and 150 ms, All and BTC
        let slow = rows.iter().find(|r| r["delay_ms"] == 150 && r["asset"] == "All").unwrap();
        assert_eq!(slow["share_fill"], 0.4);
        assert_eq!(slow["slip_c"], 1.0);
        assert_eq!(slow["net_c"], 40.0);
    }
}
