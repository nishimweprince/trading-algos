//! Paper results from the journal: per-day table, equity curve, trades,
//! breakdowns, execution and data quality.
//!
//! `signal` is the first thing to read when the curve goes negative. Over
//! settled trades it compares the win rate, the probability Kalshi's mid gave
//! the leg at entry, and the price paid. Win rate tracking Kalshi above the
//! price: the signal works. Tracking the price: the edge is gone. Far below the
//! price: suspect a bug before the market.
//!
//! **Days.** Entries, fills and no-fills count on the day of the decision;
//! settled trades, wins and P&L on the day they settled (UTC), the day cash
//! moved, which is the ledger's `realised_today` basis too.

use std::collections::{BTreeMap, BTreeSet};

use indexmap::IndexMap;
use pk_core::WINDOW_S;
use serde_json::{Map, Value, json};

use crate::shadow_table::shadow_table;
use crate::stats::{Key, f, fmean, i, key, median, opt, s, truthy, utc_day_ns};

pub const CURVE_POINTS: usize = 400;
/// Seconds from window close to Polymarket's resolution when the journal holds
/// no measured delay yet.
pub const SETTLE_DELAY_DEFAULT_S: f64 = 240.0;
const MARKOUT_S: [i64; 3] = [1, 5, 30];
const SEC_BUCKETS: [(i64, i64); 4] = [(0, 60), (60, 180), (180, 420), (420, 900)];

fn pct(values: &[f64], q: f64) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    let mut o = values.to_vec();
    o.sort_by(f64::total_cmp);
    Some(o[((q * o.len() as f64) as usize).min(o.len() - 1)])
}

fn kalshi_implied(d: &Value, side: &str) -> Option<f64> {
    let k = d.get("kalshi")?;
    let (b, a) = (f(k, "bid")?, f(k, "ask")?);
    let mid = (b + a) / 2.0;
    Some(if side == "yes" { mid } else { 1.0 - mid })
}

struct Row {
    asset: String,
    sec: f64,
    win: f64,
    vwap: f64,
    implied: Option<f64>,
    cents: f64,
    pnl: f64,
    fee: f64,
    t: i64,
}

fn group(rows: &[&Row]) -> Value {
    let implied: Vec<f64> = rows.iter().filter_map(|r| r.implied).collect();
    let col = |g: fn(&Row) -> f64| rows.iter().map(|r| g(r)).collect::<Vec<f64>>();
    json!({
        "n": rows.len(),
        "win_rate": opt(fmean(&col(|r| r.win))),
        "avg_price": opt(fmean(&col(|r| r.vwap))),
        "kalshi_implied": opt(fmean(&implied)),
        "net_cents": opt(fmean(&col(|r| r.cents))),
        "pnl": pk_core::num::round_dp(rows.iter().map(|r| r.pnl).sum::<f64>(), 2),
    })
}

fn quality(rows: &[Row]) -> Value {
    let pnl: Vec<f64> = rows.iter().map(|r| r.pnl).collect();
    let wins: Vec<f64> = pnl.iter().copied().filter(|p| *p > 0.0).collect();
    let losses: Vec<f64> = pnl.iter().copied().filter(|p| *p <= 0.0).collect();
    let (mut streak, mut longest) = (0, 0);
    for p in &pnl {
        streak = if *p <= 0.0 { streak + 1 } else { 0 };
        longest = longest.max(streak);
    }
    let fees: f64 = rows.iter().map(|r| r.fee).sum();
    let gross = pnl.iter().sum::<f64>() + fees;
    let loss_sum: f64 = losses.iter().sum();
    json!({
        "n": pnl.len(),
        "wins": wins.len(),
        "losses": losses.len(),
        "avg_win": opt(fmean(&wins)),
        "avg_loss": opt(fmean(&losses)),
        "profit_factor": (!losses.is_empty() && loss_sum < 0.0).then(|| wins.iter().sum::<f64>() / -loss_sum),
        "expectancy": opt(fmean(&pnl)),
        "largest_win": wins.iter().copied().reduce(f64::max),
        "largest_loss": losses.iter().copied().reduce(f64::min),
        "longest_losing_streak": longest,
        "gross_pnl": pk_core::num::round_dp(gross, 2),
        "fees": pk_core::num::round_dp(fees, 2),
        "fees_pct_of_gross": (gross > 0.0).then(|| 100.0 * fees / gross),
    })
}

fn drawdown(pnl: &[f64]) -> Value {
    let (mut peak, mut running, mut worst) = (0.0f64, 0.0f64, 0.0f64);
    for p in pnl {
        running += p;
        peak = peak.max(running);
        worst = worst.max(peak - running);
    }
    json!({"max": pk_core::num::round_dp(worst, 2), "current": pk_core::num::round_dp(peak - running, 2)})
}

/// Open paper trades with settlement ETAs: window close plus the median
/// measured Polymarket resolution delay. Kalshi's outcome, when recorded, is
/// an early hint only (the venues can disagree).
pub fn open_trade_etas(records: &[Value]) -> Value {
    let mut fills: IndexMap<Key, &Value> = IndexMap::new();
    let mut settled: BTreeSet<Key> = BTreeSet::new();
    let mut kalshi: BTreeMap<Key, String> = BTreeMap::new();
    let mut delays: Vec<f64> = Vec::new();
    for r in records {
        match s(r, "type") {
            Some("fill") if f(r, "shares").unwrap_or(0.0) > 0.0 => {
                fills.insert(key(r), r);
            }
            Some("settle") => {
                settled.insert(key(r));
            }
            Some("resolution") => {
                if s(r, "venue") == Some("poly") && f(r, "delay_s").is_some() {
                    delays.push(f(r, "delay_s").unwrap_or(0.0));
                } else if s(r, "venue") == Some("kalshi") && truthy(r, "outcome") {
                    kalshi.insert(key(r), s(r, "outcome").unwrap_or_default().to_owned());
                }
            }
            _ => {}
        }
    }
    let basis = median(&delays).unwrap_or(SETTLE_DELAY_DEFAULT_S);
    let mut trades: Vec<(f64, Value)> = Vec::new();
    for (k, fl) in &fills {
        let Some(open) = k.1 else { continue };
        if settled.contains(k) {
            continue;
        }
        let close = open + WINDOW_S;
        let eta = close as f64 + basis;
        trades.push((
            eta,
            json!({
                "asset": k.0, "side": fl.get("side"), "shares": fl.get("shares"), "vwap": fl.get("vwap"),
                "cost": fl.get("cost"), "open_unix": open, "close_unix": close, "eta_unix": eta,
                "kalshi_outcome": kalshi.get(k),
            }),
        ));
    }
    trades.sort_by(|a, b| a.0.total_cmp(&b.0));
    json!({
        "trades": trades.into_iter().map(|t| t.1).collect::<Vec<_>>(),
        "settle_delay_s": basis,
        "delay_samples": delays.len(),
        "delay_default": delays.is_empty(),
    })
}

fn markout_summary(rows: &[&Value]) -> Value {
    let mut out = Map::new();
    out.insert("n".into(), rows.len().into());
    for h in MARKOUT_S {
        let v: Vec<f64> = rows.iter().filter_map(|r| f(r, &format!("markout_{h}s"))).collect();
        out.insert(format!("{h}s"), json!({"n": v.len(), "mean": opt(fmean(&v)), "median": opt(median(&v))}));
    }
    Value::Object(out)
}

#[derive(Default)]
struct Day {
    windows: u64,
    enter: u64,
    skip: u64,
    blocked: u64,
    filled: u64,
    no_fill: u64,
    settled: u64,
    wins: u64,
    vwaps: Vec<f64>,
    cents: Vec<f64>,
    pnl: f64,
    fees: f64,
}

pub fn paper_results(records: &[Value], trades_limit: usize) -> Value {
    let mut decisions: IndexMap<Key, &Value> = IndexMap::new();
    let mut fills: IndexMap<Key, &Value> = IndexMap::new();
    let mut settles: IndexMap<Key, &Value> = IndexMap::new();
    let mut markouts: IndexMap<Key, &Value> = IndexMap::new();
    let mut windows: Vec<&Value> = Vec::new();
    for r in records {
        let k = key(r);
        match s(r, "type") {
            Some("markout") => {
                markouts.insert(k, r);
            }
            Some("decision") => {
                decisions.insert(k, r);
            }
            Some("fill") => {
                fills.insert(k, r);
            }
            Some("settle") => {
                settles.insert(k, r);
            }
            Some("window") => windows.push(r),
            _ => {}
        }
    }
    let mut days: BTreeMap<String, Day> = BTreeMap::new();
    for w in &windows {
        days.entry(utc_day_ns(i(w, "open_unix").unwrap_or(0) * 1_000_000_000)).or_default().windows += 1;
    }
    for (k, d) in &decisions {
        let day = days.entry(utc_day_ns(i(d, "t_ns").unwrap_or(0))).or_default();
        if s(d, "action") == Some("skip") {
            day.skip += 1;
        } else if truthy(d, "blocked") {
            day.blocked += 1;
        } else {
            day.enter += 1;
        }
        let Some(fl) = fills.get(k) else { continue };
        if f(fl, "shares").unwrap_or(0.0) <= 0.0 {
            day.no_fill += 1;
            continue;
        }
        day.filled += 1;
        day.vwaps.push(f(fl, "vwap").unwrap_or(0.0));
        day.fees += f(fl, "fee").unwrap_or(0.0);
        if let Some(st) = settles.get(k) {
            let t =
                i(st, "t_ns").filter(|t| *t != 0).unwrap_or((k.1.unwrap_or(0) + WINDOW_S) * 1_000_000_000);
            let on = days.entry(utc_day_ns(t)).or_default();
            on.settled += 1;
            on.wins += u64::from(f(st, "payout").unwrap_or(0.0) > 0.0);
            on.pnl += f(st, "pnl").unwrap_or(0.0);
            on.cents.push(
                100.0
                    * (f(st, "payout").unwrap_or(0.0)
                        - f(fl, "cost").unwrap_or(0.0)
                        - f(fl, "fee").unwrap_or(0.0))
                    / f(fl, "shares").unwrap_or(1.0),
            );
        }
    }
    let mut daily = Vec::new();
    let mut cumulative = 0.0;
    for (name, d) in &days {
        cumulative += d.pnl;
        daily.push(json!({
            "day": name, "windows": d.windows, "enter": d.enter, "skip": d.skip, "blocked": d.blocked,
            "filled": d.filled, "no_fill": d.no_fill, "settled": d.settled,
            "win_rate": (d.settled > 0).then(|| d.wins as f64 / d.settled as f64),
            "avg_vwap": opt(fmean(&d.vwaps)), "net_cents": opt(fmean(&d.cents)),
            "fees": pk_core::num::round_dp(d.fees, 2), "pnl": pk_core::num::round_dp(d.pnl, 2),
            "cumulative": pk_core::num::round_dp(cumulative, 2),
        }));
    }
    daily.reverse();

    let mut trades: Vec<(i64, Value)> = Vec::new();
    for (k, fl) in &fills {
        if f(fl, "shares").unwrap_or(0.0) <= 0.0 {
            continue;
        }
        let d = decisions.get(k).copied();
        let st = settles.get(k).copied();
        let m = markouts.get(k).copied();
        let mut t = json!({
            "asset": k.0, "open_unix": k.1, "t_ns": fl.get("t_ns"), "side": fl.get("side"),
            "gap": d.and_then(|d| d.get("gap")), "sec_into": d.and_then(|d| d.get("sec_into")),
            "touch_px": fl.get("touch_px"), "vwap": fl.get("vwap"), "shares": fl.get("shares"),
            "ordered": fl.get("ordered"), "fee": fl.get("fee"),
            "outcome": st.and_then(|s| s.get("outcome")), "pnl": st.and_then(|s| s.get("pnl")),
            "depth_at_limit": fl.get("depth_at_limit"),
        });
        for h in MARKOUT_S {
            let name = format!("markout_{h}s");
            t[&name] = m.and_then(|m| m.get(&name)).cloned().unwrap_or(Value::Null);
        }
        trades.push((i(fl, "t_ns").unwrap_or(0), t));
    }
    trades.sort_by_key(|t| t.0);
    let mut curve: Vec<Value> = Vec::new();
    let mut running = 0.0;
    for (t_ns, t) in &trades {
        if let Some(p) = f(t, "pnl") {
            running += p;
            curve.push(json!([t_ns.div_euclid(1_000_000_000), pk_core::num::round_dp(running, 2)]));
        }
    }
    if curve.len() > CURVE_POINTS {
        let step = curve.len() as f64 / CURVE_POINTS as f64;
        let last = curve.last().cloned().expect("non-empty");
        curve =
            (0..CURVE_POINTS - 1).map(|i| curve[(i as f64 * step) as usize].clone()).chain([last]).collect();
    }

    let mut rows: Vec<Row> = Vec::new();
    for (k, st) in &settles {
        let Some(fl) = fills.get(k) else { continue };
        if f(fl, "shares").unwrap_or(0.0) <= 0.0 {
            continue;
        }
        let d = decisions.get(k).copied().unwrap_or(&Value::Null);
        let side = s(fl, "side").unwrap_or_default();
        rows.push(Row {
            asset: k.0.clone().unwrap_or_default(),
            sec: f(d, "sec_into").filter(|v| *v != 0.0).unwrap_or(0.0),
            win: if f(st, "payout").unwrap_or(0.0) > 0.0 { 1.0 } else { 0.0 },
            vwap: f(fl, "vwap").unwrap_or(0.0),
            implied: kalshi_implied(d, side),
            cents: 100.0
                * (f(st, "payout").unwrap_or(0.0)
                    - f(fl, "cost").unwrap_or(0.0)
                    - f(fl, "fee").unwrap_or(0.0))
                / f(fl, "shares").unwrap_or(1.0),
            pnl: f(st, "pnl").unwrap_or(0.0),
            fee: f(fl, "fee").unwrap_or(0.0),
            t: i(fl, "t_ns").unwrap_or(0),
        });
    }
    rows.sort_by_key(|r| r.t);
    let assets: BTreeSet<&String> = rows.iter().map(|r| &r.asset).collect();
    let by_asset: Map<String, Value> = assets
        .iter()
        .map(|a| ((*a).clone(), group(&rows.iter().filter(|r| &r.asset == *a).collect::<Vec<_>>())))
        .collect();
    let by_sec: Map<String, Value> = SEC_BUCKETS
        .iter()
        .map(|(lo, hi)| {
            let g: Vec<&Row> = rows.iter().filter(|r| (*lo as f64) <= r.sec && r.sec < *hi as f64).collect();
            (format!("{lo}-{hi}s"), group(&g))
        })
        .collect();
    let filled: Vec<&&Value> = fills.values().filter(|fl| f(fl, "shares").unwrap_or(0.0) > 0.0).collect();
    let ages_k: Vec<f64> = decisions
        .values()
        .filter_map(|d| d.get("age_ms").and_then(|a| a.get("kalshi")).and_then(Value::as_f64))
        .collect();
    let latencies: Vec<f64> = fills.values().filter_map(|fl| f(fl, "latency_ms")).collect();
    let col = |g: &dyn Fn(&Value) -> f64| filled.iter().map(|fl| g(fl)).collect::<Vec<f64>>();
    let fill_markouts: Vec<&Value> = markouts.values().copied().filter(|m| truthy(m, "filled")).collect();
    let nofill_markouts: Vec<&Value> = markouts.values().copied().filter(|m| !truthy(m, "filled")).collect();
    let execution = json!({
        "orders": fills.len(),
        "filled": filled.len(),
        "fill_rate": (!fills.is_empty()).then(|| filled.len() as f64 / fills.len() as f64),
        "avg_fill_ratio": opt(fmean(&col(&|fl| f(fl, "shares").unwrap_or(0.0) / f(fl, "ordered").unwrap_or(1.0)))),
        "avg_slippage_cents": opt(fmean(&col(&|fl| 100.0 * (f(fl, "vwap").unwrap_or(0.0) - f(fl, "touch_px").unwrap_or(0.0))))),
        "avg_fee_cents_per_share": opt(fmean(&col(&|fl| 100.0 * f(fl, "fee").unwrap_or(0.0) / f(fl, "shares").unwrap_or(1.0)))),
        "order_latency_ms_p50": opt(pct(&latencies, 0.5)),
        "kalshi_age_ms_p50": opt(pct(&ages_k, 0.5)),
        "kalshi_age_ms_p90": opt(pct(&ages_k, 0.9)),
        "markouts": {"fills": markout_summary(&fill_markouts), "no_fills": markout_summary(&nofill_markouts)},
    });
    let evals: i64 = windows.iter().map(|w| i(w, "evals").unwrap_or(0)).sum();
    let invalid: i64 =
        windows.iter().map(|w| w.get("reasons").and_then(|r| i(r, "invalid_leg")).unwrap_or(0)).sum();
    let all_rows: Vec<&Row> = rows.iter().collect();
    json!({
        "quality": quality(&rows),
        "drawdown": drawdown(&rows.iter().map(|r| r.pnl).collect::<Vec<_>>()),
        "funnel": {
            "windows": windows.len(),
            "crossings": decisions.len(),
            "skipped_above_cap": decisions.values().filter(|d| s(d, "action") == Some("skip")).count(),
            "blocked": decisions.values().filter(|d| s(d, "action") == Some("enter") && truthy(d, "blocked")).count(),
            "orders": fills.len(),
            "filled": filled.len(),
            "settled": rows.len(),
        },
        "data_quality": {
            "windows": windows.len(),
            "unidentified_windows": windows.iter().filter(|w| w.get("identified") == Some(&Value::Bool(false))).count(),
            "evals": evals,
            "invalid_leg_share": (evals > 0).then(|| invalid as f64 / evals as f64),
        },
        "daily": daily,
        "daily_basis": "entries by decision day; settled, wins and P&L by settle day (UTC)",
        "shadow": shadow_table(records),
        "curve": curve,
        "trades": trades.into_iter().rev().take(trades_limit).map(|t| t.1).collect::<Vec<_>>(),
        "signal": group(&all_rows),
        "by_asset": by_asset,
        "by_sec": by_sec,
        "execution": execution,
    })
}
