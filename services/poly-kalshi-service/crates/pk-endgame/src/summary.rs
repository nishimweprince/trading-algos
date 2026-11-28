//! What the monitoring page shows: the run's results so far, rebuilt from the
//! journal at start and kept up to date from every record written after.
//! The per-second `eg_sec` lines are skipped (they are the forward tape, not
//! results), so the summary stays small however long the run.
//!
//! The statistics are `eg_review.py`'s: net ¢ per share after fees, with a
//! standard error clustered by window (positions in one window share its
//! outcome), and the same bands.

use std::collections::{BTreeMap, HashMap, VecDeque};
use std::path::Path;

use serde_json::{Value, json};

use crate::engine::NS;

/// The bar before going live (README).
pub const GATE_SETTLED: usize = 150;
pub const GATE_DAYS: f64 = 3.0;
pub const GATE_T: f64 = 2.0;
pub const GATE_AGREE: f64 = 0.99;
/// The Kalshi-lead rule's read (PREREG "KG-1"): the traded exit over this many settled fills.
pub const KG_NEED: usize = 300;
pub const KG_T: f64 = 2.0;
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
    /// Shadow rules: rule → (decisions, fills, misses, settles).
    shadows: BTreeMap<String, ShadowTally>,
    kg_decisions: Vec<Value>,
    kg_fills: u64,
    kg_no_fills: u64,
    kg_settles: Vec<Value>,
    windows: u64,
    first_ns: Option<i64>,
}

#[derive(Default)]
struct ShadowTally {
    decisions: u64,
    fills: u64,
    no_fills: u64,
    settles: Vec<Value>,
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
    pub fn load(dir: &Path, epoch: &str) -> Self {
        let mut s = Summary { epoch: epoch.to_owned(), ..Summary::default() };
        let Ok(rd) = std::fs::read_dir(dir) else { return s };
        let mut files: Vec<_> =
            rd.filter_map(Result::ok).map(|e| e.path()).filter(|p| p.extension().is_some_and(|x| x == "jsonl")).collect();
        files.sort();
        for p in files {
            let Ok(text) = std::fs::read_to_string(&p) else { continue };
            for line in text.lines() {
                if line.contains("\"type\":\"eg_sec\"") {
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
        let mine = r.get("epoch").and_then(Value::as_str).unwrap_or("1") == self.epoch;
        match kind {
            "eg_window" => {
                self.windows += 1;
                self.first_ns.get_or_insert(i(r, "t_ns"));
                if mine {
                    self.epoch_start_ns.get_or_insert(i(r, "t_ns"));
                }
            }
            "eg_decision" if mine => {
                self.decisions.insert(id, r.clone());
            }
            // Fills belong to the epoch of their decision.
            "eg_fill" if self.decisions.contains_key(&id) => {
                self.fills.insert(id, r.clone());
            }
            "eg_no_fill" if self.decisions.contains_key(&id) => {
                self.no_fills.insert(id, r.clone());
            }
            "eg_settle" if mine => self.settles.push(r.clone()),
            "eg_settle" => self.baseline.push(r.clone()),
            "eg_resolution" => {
                if let Some(a) = r.get("agree").and_then(Value::as_bool) {
                    self.resolved_both += 1;
                    self.agree += u64::from(a);
                }
            }
            "eg_close" if mine => {
                self.closes.push_back(r.clone());
                while self.closes.len() > KEEP_CLOSES {
                    self.closes.pop_front();
                }
            }
            "eg_halt" => self.halts.push(r.clone()),
            "kg_decision" if mine => self.kg_decisions.push(r.clone()),
            "kg_fill" if mine => self.kg_fills += 1,
            "kg_no_fill" if mine => self.kg_no_fills += 1,
            "kg_settle" if mine => self.kg_settles.push(r.clone()),
            "eg_shadow_decision" | "eg_shadow_fill" | "eg_shadow_no_fill" | "eg_shadow_settle" if mine => {
                let rule = r.get("rule").and_then(Value::as_str).unwrap_or("?").to_owned();
                let t = self.shadows.entry(rule).or_default();
                match kind {
                    "eg_shadow_decision" => t.decisions += 1,
                    "eg_shadow_fill" => t.fills += 1,
                    "eg_shadow_no_fill" => t.no_fills += 1,
                    _ => t.settles.push(r.clone()),
                }
            }
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

    /// The traded rule beside each shadow rule (PREREG epoch 3): decisions,
    /// fill rate, and settled net ¢/share clustered by window.
    fn shadow_table(&self) -> Vec<Value> {
        let mut out = Vec::new();
        let mut traded = clustered(&self.rows(|_| true));
        traded["label"] = "traded".into();
        traded["decisions"] = self.decisions.len().into();
        traded["fill_rate"] = if self.decisions.is_empty() {
            Value::Null
        } else {
            round(self.fills.len() as f64 / self.decisions.len() as f64, 3)
        };
        out.push(traded);
        for (rule, t) in &self.shadows {
            let mut v = clustered(&Self::rows_of(&t.settles, |_| true));
            v["label"] = rule.clone().into();
            v["decisions"] = t.decisions.into();
            let tried = t.fills + t.no_fills;
            v["fill_rate"] = if tried > 0 { round(t.fills as f64 / tried as f64, 3) } else { Value::Null };
            out.push(v);
        }
        out
    }

    /// The Kalshi-lead rule: counts, then the traded exit, holding and each
    /// probe delay as net ¢/share over the same settled fills, clustered by window.
    fn kg_table(&self, now_s: i64) -> Value {
        let key = |s: &Value| (s["asset"].as_str().unwrap_or_default().to_owned(), i(s, "open"));
        let rows_by = |pnl: &dyn Fn(&Value) -> f64| -> Vec<Row> {
            self.kg_settles
                .iter()
                .map(|s| Row { key: key(s), shares: f(s, "shares"), pnl: pnl(s) })
                .filter(|r| r.pnl.is_finite() && r.shares > 0.0)
                .collect()
        };
        let mut rows = Vec::new();
        let mut traded = clustered(&rows_by(&|s| f(s, "pnl")));
        traded["label"] = "traded".into();
        rows.push(traded.clone());
        let mut hold = clustered(&rows_by(&|s| f(s, "hold_pnl")));
        hold["label"] = "hold".into();
        rows.push(hold);
        let mut delays: Vec<i64> = self
            .kg_settles
            .iter()
            .filter_map(|s| s["exits"].as_object())
            .flat_map(|m| m.keys().filter_map(|k| k.parse::<i64>().ok()).collect::<Vec<_>>())
            .collect();
        delays.sort_unstable();
        delays.dedup();
        for d in delays {
            let k = d.to_string();
            let mut v = clustered(&rows_by(&|s| s["exits"][&k]["pnl"].as_f64().unwrap_or(f64::NAN)));
            v["label"] = format!("exit_{d}").into();
            v["delay_ms"] = d.into();
            rows.push(v);
        }
        let n = self.kg_settles.len();
        let t = traded["t"].as_f64();
        let m = traded["net_c"].as_f64();
        let verdict = if n < KG_NEED {
            "collecting"
        } else if t.is_some_and(|t| t >= KG_T) && m.is_some_and(|m| m > 0.0) {
            "pass"
        } else {
            "fail"
        };
        let tried = self.kg_fills + self.kg_no_fills;
        let today = now_s - now_s.rem_euclid(86_400);
        let mut triggers: BTreeMap<String, u64> = BTreeMap::new();
        for d in &self.kg_decisions {
            *triggers.entry(d["trigger"].as_str().unwrap_or("?").to_owned()).or_default() += 1;
        }
        let mut gates: BTreeMap<String, u64> = BTreeMap::new();
        for c in self.closes.iter().filter(|c| i(c, "t_ns") / NS >= now_s - 86_400) {
            *gates.entry(c["kg_gate"].as_str().unwrap_or("not reached").to_owned()).or_default() += 1;
        }
        let mut gates: Vec<(String, u64)> = gates.into_iter().collect();
        gates.sort_by_key(|g| std::cmp::Reverse(g.1));
        json!({
            "decisions": self.kg_decisions.len(),
            "decisions_today": self.kg_decisions.iter().filter(|d| i(d, "t_ns") / NS >= today).count(),
            "fills": self.kg_fills,
            "fill_rate": if tried > 0 { round(self.kg_fills as f64 / tried as f64, 3) } else { Value::Null },
            "settled": n,
            "triggers": triggers,
            "rows": rows,
            "read": {"need": KG_NEED, "have": n, "need_t": KG_T, "t": traded["t"], "net_c": traded["net_c"], "verdict": verdict},
            "gates_24h": gates.iter().map(|(g, c)| json!({"gate": g, "n": c})).collect::<Vec<_>>(),
        })
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
        let instant_sh: f64 = self.decisions.values().map(|x| f(x, "instant_shares")).filter(|x| x.is_finite()).sum();
        let today = now_s - now_s.rem_euclid(86_400);
        let decisions_today = self.decisions.values().filter(|d| i(d, "t_ns") / NS >= today).count();
        let pnl_today: f64 =
            self.settles.iter().filter(|s| i(s, "t_ns") / NS >= today).map(|s| f(s, "pnl")).filter(|x| x.is_finite()).sum();

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
        let by_left: Vec<Value> = [(0.0, 10.0), (10.0, 20.0), (20.0, 30.0), (30.0, 45.0), (45.0, 61.0)]
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
                let paid = if paid.is_empty() { f64::NAN } else { paid.iter().sum::<f64>() / paid.len() as f64 };
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

        let mut base_epochs: Vec<&str> = self.baseline.iter().map(|s| s["epoch"].as_str().unwrap_or("1")).collect();
        base_epochs.sort_unstable();
        base_epochs.dedup();
        let t_ok = all["t"].as_f64().is_some_and(|t| t >= GATE_T) && all["net_c"].as_f64().is_some_and(|m| m > 0.0);
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
            "shadows": self.shadow_table(),
            "kg": self.kg_table(now_s),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn settle(id: u64, asset: &str, open: i64, pnl: f64, won: bool, t_ns: i64) -> Value {
        json!({"type": "eg_settle", "id": id, "asset": asset, "open": open, "shares": 10.0, "pnl": pnl, "won": won, "t_ns": t_ns})
    }

    #[test]
    fn scores_and_bands() {
        let mut s = Summary { epoch: "1".into(), ..Summary::default() };
        for k in 0..30u64 {
            let open = 1_800_000_000 + 900 * k as i64;
            let t = (open + 930) * NS;
            s.ingest(&json!({"type": "eg_decision", "id": k + 1, "asset": "BTC", "left": 25.0, "p_side": 0.85,
                             "instant_shares": 10.0, "t_ns": t - 30 * NS}));
            s.ingest(&json!({"type": "eg_fill", "id": k + 1, "shares": 10.0, "avg": 0.85, "t_ns": t - 30 * NS}));
            let won = k % 10 != 0;
            s.ingest(&settle(k + 1, "BTC", open, if won { 1.4 } else { -8.6 }, won, t));
            s.ingest(&json!({"type": "eg_resolution", "agree": true, "t_ns": t}));
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
        assert_eq!(v["by_left"][0]["label"], "20–30 s");
        assert_eq!(v["series"].as_array().unwrap().len(), 30);
        assert_eq!(v["recent"].as_array().unwrap().len(), 30);
        assert_eq!(v["gate"]["settled"]["ok"], false);
    }

    #[test]
    fn shadow_rules_are_tallied_per_rule() {
        let mut s = Summary { epoch: "3".into(), ..Summary::default() };
        for (kind, rule) in [("eg_shadow_decision", "edge9"), ("eg_shadow_fill", "edge9"), ("eg_shadow_decision", "fav40"),
                             ("eg_shadow_no_fill", "fav40")] {
            s.ingest(&json!({"type": kind, "rule": rule, "epoch": "3", "t_ns": NS}));
        }
        s.ingest(&json!({"type": "eg_shadow_settle", "rule": "edge9", "epoch": "3", "asset": "BTC", "open": 0,
                         "shares": 10.0, "pnl": 4.0, "t_ns": 2 * NS}));
        s.ingest(&json!({"type": "eg_shadow_settle", "rule": "edge9", "epoch": "2", "asset": "BTC", "open": 0,
                         "shares": 10.0, "pnl": 9.0, "t_ns": 2 * NS})); // another epoch: ignored
        let v = s.snapshot(json!({}), 3 * NS);
        let rows = v["shadows"].as_array().unwrap();
        assert_eq!(rows[0]["label"], "traded");
        let e9 = rows.iter().find(|r| r["label"] == "edge9").unwrap();
        assert_eq!((e9["decisions"].as_u64(), e9["fill_rate"].as_f64(), e9["pnl"].as_f64()), (Some(1), Some(1.0), Some(4.0)));
        let f40 = rows.iter().find(|r| r["label"] == "fav40").unwrap();
        assert_eq!(f40["fill_rate"], 0.0);
    }

    #[test]
    fn kalshi_lead_table_scores_traded_hold_and_each_exit() {
        let mut s = Summary { epoch: "3".into(), ..Summary::default() };
        s.ingest(&json!({"type": "kg_decision", "epoch": "3", "id": 1, "trigger": "kalshi", "t_ns": NS}));
        s.ingest(&json!({"type": "kg_fill", "epoch": "3", "id": 1, "t_ns": NS}));
        s.ingest(&json!({"type": "kg_decision", "epoch": "3", "id": 2, "trigger": "book", "t_ns": NS}));
        s.ingest(&json!({"type": "kg_no_fill", "epoch": "3", "id": 2, "t_ns": NS}));
        s.ingest(&json!({"type": "kg_settle", "epoch": "3", "id": 1, "asset": "BTC", "open": 0, "shares": 10.0,
                         "pnl": 0.5, "hold_pnl": -5.0, "exits": {"1000": {"pnl": 0.2}, "2000": {"pnl": 0.5}}, "t_ns": 2 * NS}));
        let v = s.snapshot(json!({}), 3 * NS);
        let kg = &v["kg"];
        assert_eq!((kg["decisions"].as_u64(), kg["fill_rate"].as_f64(), kg["settled"].as_u64()), (Some(2), Some(0.5), Some(1)));
        assert_eq!(kg["triggers"]["kalshi"], 1);
        let rows = kg["rows"].as_array().unwrap();
        let by = |l: &str| rows.iter().find(|r| r["label"] == l).unwrap()["pnl"].as_f64().unwrap();
        assert_eq!((by("traded"), by("hold"), by("exit_1000"), by("exit_2000")), (0.5, -5.0, 0.2, 0.5));
        assert_eq!(kg["read"]["verdict"], "collecting");
    }

    #[test]
    fn epochs_are_scored_apart() {
        let mut s = Summary { epoch: "2".into(), ..Summary::default() };
        s.ingest(&settle(1, "BTC", 0, 1.0, true, NS)); // no epoch: "1"
        s.ingest(&json!({"type": "eg_decision", "id": 2, "epoch": "2", "t_ns": NS}));
        s.ingest(&json!({"type": "eg_fill", "id": 2, "shares": 10.0, "avg": 0.5, "t_ns": NS}));
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
}
