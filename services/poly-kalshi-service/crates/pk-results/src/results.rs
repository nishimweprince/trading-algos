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
//!
//! **Exits.** A position the active exit sold has a `settle` record with
//! `outcome: "exit"`. Net ¢/share is the ledger's P&L over the shares filled,
//! so exits, partial exits and both fees count; an exit is a win when its
//! P&L is positive. `exits` compares each exit with holding to settlement.

use std::collections::{BTreeMap, BTreeSet};

use indexmap::IndexMap;
use pk_core::WINDOW_S;
use serde_json::{Map, Value, json};

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

/// Net ¢/share of a settled position over the shares the entry filled:
/// payout − cost − fee for a plain settlement; the ledger's P&L (which counts
/// the active exit's proceeds and fee) when the position was sold, wholly
/// (`outcome: "exit"`) or in part (P&L off the plain formula by over 1¢).
pub fn net_cents(settle: &Value, fill: &Value) -> f64 {
    let shares = f(fill, "shares").filter(|s| *s > 0.0).unwrap_or(1.0);
    let plain =
        f(settle, "payout").unwrap_or(0.0) - f(fill, "cost").unwrap_or(0.0) - f(fill, "fee").unwrap_or(0.0);
    let pnl = f(settle, "pnl").unwrap_or(plain);
    let sold = s(settle, "outcome") == Some("exit") || (pnl - plain).abs() > 0.01;
    100.0 * if sold { pnl } else { plain } / shares
}

/// A win: paid out at settlement, or sold at a profit by the active exit.
pub fn won(settle: &Value) -> bool {
    if s(settle, "outcome") == Some("exit") {
        f(settle, "pnl").unwrap_or(0.0) > 0.0
    } else {
        f(settle, "payout").unwrap_or(0.0) > 0.0
    }
}

/// Each exit against holding the same shares to settlement, ¢/share net of fees.
pub fn exits_vs_hold(records: &[Value], limit: usize) -> Value {
    let mut fills: IndexMap<Key, &Value> = IndexMap::new();
    let mut outcomes: BTreeMap<Key, (Option<String>, Option<String>)> = BTreeMap::new();
    let mut exits: Vec<&Value> = Vec::new();
    for r in records {
        match s(r, "type") {
            Some("fill") if f(r, "shares").unwrap_or(0.0) > 0.0 => {
                fills.insert(key(r), r);
            }
            Some("resolution") => {
                let o = outcomes.entry(key(r)).or_default();
                let out = s(r, "outcome").map(str::to_owned);
                if s(r, "venue") == Some("poly") { o.0 = out } else { o.1 = out }
            }
            Some("exit") if s(r, "reason") == Some("fair") => exits.push(r),
            _ => {}
        }
    }
    let mut rows = Vec::new();
    let (mut exit_c, mut hold_c, mut held) = (Vec::new(), Vec::new(), Vec::new());
    for e in &exits {
        let k = key(e);
        let Some(fl) = fills.get(&k) else { continue };
        let entry = f(e, "entry_vwap").or_else(|| f(fl, "vwap")).unwrap_or(0.0);
        let shares = f(e, "shares").unwrap_or(0.0);
        if shares <= 0.0 {
            continue;
        }
        let entry_fee = f(fl, "fee").unwrap_or(0.0) / f(fl, "shares").unwrap_or(1.0);
        let exit_px = f(e, "exit_px").unwrap_or(0.0);
        let exit_cents = 100.0 * (exit_px - entry - entry_fee - f(e, "fee").unwrap_or(0.0) / shares);
        let outcome = outcomes.get(&k).and_then(|o| o.0.clone().or_else(|| o.1.clone()));
        let side = s(e, "side").unwrap_or_default();
        let hold_cents =
            outcome.as_deref().map(|o| 100.0 * ((if o == side { 1.0 } else { 0.0 }) - entry - entry_fee));
        exit_c.push(exit_cents);
        if let Some(h) = hold_cents {
            hold_c.push(h);
        }
        if let Some(h) = f(e, "held_s") {
            held.push(h);
        }
        rows.push(json!({
            "asset": k.0, "open_unix": k.1, "t_ns": e.get("t_ns"), "side": side, "venue": e.get("venue"),
            "shares": shares, "entry_vwap": entry, "exit_px": exit_px, "held_s": e.get("held_s"),
            "exit_cents": pk_core::num::round_dp(exit_cents, 2),
            "outcome": outcome, "hold_cents": hold_cents.map(|h| pk_core::num::round_dp(h, 2)),
        }));
    }
    let both: Vec<(f64, f64)> = rows
        .iter()
        .filter_map(|r| Some((f(r, "exit_cents")?, f(r, "hold_cents")?)))
        .collect();
    json!({
        "n": rows.len(),
        "exit_cents_mean": opt(fmean(&exit_c)),
        "hold_cents_mean": opt(fmean(&hold_c)),
        "exit_minus_hold_cents": opt(fmean(&both.iter().map(|(e, h)| e - h).collect::<Vec<_>>())),
        "compared": both.len(),
        "held_s_median": opt(median(&held)),
        "rows": rows.into_iter().rev().take(limit).collect::<Vec<_>>(),
    })
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
            on.wins += u64::from(won(st));
            on.pnl += f(st, "pnl").unwrap_or(0.0);
            on.cents.push(net_cents(st, fl));
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
            win: if won(st) { 1.0 } else { 0.0 },
            vwap: f(fl, "vwap").unwrap_or(0.0),
            implied: kalshi_implied(d, side),
            cents: net_cents(st, fl),
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
    let mut out = json!({
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
        "curve": curve,
        "trades": trades.into_iter().rev().take(trades_limit).map(|t| t.1).collect::<Vec<_>>(),
        "signal": group(&all_rows),
        "by_asset": by_asset,
        "by_sec": by_sec,
        "execution": execution,
    });
    // Only journals with active exits carry the comparison.
    let exits = exits_vs_hold(records, trades_limit);
    if exits.get("n").and_then(Value::as_u64).unwrap_or(0) > 0
        && let Some(o) = out.as_object_mut()
    {
        o.insert("exits".into(), exits);
    }
    out
}
