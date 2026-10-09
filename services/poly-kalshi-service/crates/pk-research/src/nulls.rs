//! The null battery (manual §5), on our own tape.
//!
//! ```text
//! pk nulls --run research/data/run1
//! ```
//!
//! Reads a `pk replay run` output (`grid.parquet`, `journal/`) and books, at
//! the recorded touch with the real fee curve, held to Polymarket's outcome:
//!
//! | arm | when | leg |
//! |---|---|---|
//! | `signal` | the engine's first crossing, every one (cap ignored) | the gap's cheap leg |
//! | `signal_capped` | the same, only entries at or below the cap | same |
//! | `random_same_rule` | a random second in the same window | the leg the gap's sign points to |
//! | `random_cheap` | a random second | the cheaper of YES / NO |
//! | `fixed_cheap` | second 300 | the cheaper of YES / NO |
//!
//! Reproduce-or-stop: `signal` edge about +5 pp and `random_same_rule` about
//! 0 pp. Edge (pp) = win rate − average price; net ¢/share subtracts the fee.
//! The random arms use a seeded ChaCha generator, not numpy's: same design,
//! different draws from the Python service's.

use std::collections::{BTreeMap, HashMap};

use pk_core::fees::{DEFAULT_EXPONENT, DEFAULT_RATE, taker_fee};
use pk_core::num::round_dp;
use pk_results::gates::{BANDS, band_of};
use pk_results::stats::{fmean, stdev};
use rand::{Rng, SeedableRng};
use serde_json::{Value, json};

use crate::frame::Frame;

pub const FIXED_SEC: i64 = 300;

#[derive(Clone, Debug)]
pub struct Booking {
    pub asset: String,
    pub open_unix: i64,
    pub side: String,
    pub px: f64,
}

#[derive(Clone, Debug)]
pub struct Settled {
    pub asset: String,
    pub px: f64,
    pub win: f64,
    pub net_cents: f64,
    pub gross_cents: f64,
    pub day: String,
    pub band: String,
}

fn grid_bookings(grid: &Frame, seed: u64, early: i64, late: i64) -> BTreeMap<&'static str, Vec<Booking>> {
    let (asset, open, sec) = (grid.col("asset"), grid.col("open_unix"), grid.col("sec"));
    let g = |c: &str, i: usize| grid.col(c).f64(i).unwrap_or(f64::NAN);
    let mut groups: indexmap::IndexMap<(String, i64), Vec<usize>> = indexmap::IndexMap::new();
    let mut fixed = Vec::new();
    for i in 0..grid.len() {
        let s = sec.i64(i).unwrap_or(-1);
        if s < early || s > 900 - late {
            continue;
        }
        groups
            .entry((asset.str(i).unwrap_or_default().to_owned(), open.i64(i).unwrap_or(0)))
            .or_default()
            .push(i);
        if s == FIXED_SEC {
            fixed.push(i);
        }
    }
    let mut rng = rand::rngs::StdRng::seed_from_u64(seed);
    let picks: Vec<usize> = groups.values().map(|rows| rows[rng.gen_range(0..rows.len())]).collect();
    let gap = |i: usize| (g("k_bid", i) + g("k_ask", i)) / 2.0 - (g("y_bid", i) + g("y_ask", i)) / 2.0;
    let booking = |i: usize, yes: bool| Booking {
        asset: asset.str(i).unwrap_or_default().to_owned(),
        open_unix: open.i64(i).unwrap_or(0),
        side: if yes { "yes" } else { "no" }.into(),
        px: if yes { g("y_ask", i) } else { g("n_ask", i) },
    };
    let same_rule = |rows: &[usize]| rows.iter().map(|&i| booking(i, gap(i) > 0.0)).collect::<Vec<_>>();
    let cheap =
        |rows: &[usize]| rows.iter().map(|&i| booking(i, g("y_ask", i) <= g("n_ask", i))).collect::<Vec<_>>();
    BTreeMap::from([
        ("random_same_rule", same_rule(&picks)),
        ("random_cheap", cheap(&picks)),
        ("fixed_cheap", cheap(&fixed)),
    ])
}

pub fn arms(
    grid: &Frame,
    journal: &[Value],
    cap: f64,
    early: i64,
    late: i64,
    seed: u64,
) -> BTreeMap<&'static str, Vec<Settled>> {
    let outcomes: HashMap<(String, i64), String> = journal
        .iter()
        .filter(|r| r["type"] == "resolution" && r["venue"] == "poly")
        .filter_map(|r| {
            Some((
                (r["asset"].as_str()?.to_owned(), r["open_unix"].as_i64()?),
                r["outcome"].as_str()?.to_owned(),
            ))
        })
        .collect();
    let decisions: Vec<Booking> = journal
        .iter()
        .filter(|r| r["type"] == "decision")
        .filter_map(|r| {
            Some(Booking {
                asset: r["asset"].as_str()?.to_owned(),
                open_unix: r["open_unix"].as_i64()?,
                side: r["side"].as_str()?.to_owned(),
                px: r["entry_px"].as_f64()?,
            })
        })
        .collect();
    let mut books: BTreeMap<&'static str, Vec<Booking>> = BTreeMap::new();
    books.insert("signal_capped", decisions.iter().filter(|b| b.px <= cap).cloned().collect());
    books.insert("signal", decisions);
    books.extend(grid_bookings(grid, seed, early, late));
    books
        .into_iter()
        .map(|(name, rows)| {
            let settled = rows
                .into_iter()
                .filter_map(|b| {
                    let outcome = outcomes.get(&(b.asset.clone(), b.open_unix))?;
                    if !(b.px > 0.0 && b.px < 1.0) {
                        return None;
                    }
                    let win = if &b.side == outcome { 1.0 } else { 0.0 };
                    let fee = taker_fee(b.px, 1.0, DEFAULT_RATE, DEFAULT_EXPONENT);
                    Some(Settled {
                        day: chrono::DateTime::<chrono::Utc>::from_timestamp(b.open_unix, 0)
                            .map(|t| t.format("%Y-%m-%d").to_string())
                            .unwrap_or_default(),
                        band: band_of(b.px),
                        net_cents: 100.0 * (win - b.px - fee),
                        gross_cents: 100.0 * (win - b.px),
                        asset: b.asset,
                        px: b.px,
                        win,
                    })
                })
                .collect();
            (name, settled)
        })
        .collect()
}

fn arm_row(rows: &[&Settled]) -> Value {
    if rows.is_empty() {
        return json!({"n": 0});
    }
    let mut daily: BTreeMap<&str, Vec<f64>> = BTreeMap::new();
    let mut daily_net: BTreeMap<&str, Vec<f64>> = BTreeMap::new();
    for r in rows {
        daily.entry(&r.day).or_default().push(r.gross_cents);
        daily_net.entry(&r.day).or_default().push(r.net_cents);
    }
    let day_gross: Vec<f64> = daily.values().map(|v| v.iter().sum::<f64>() / v.len() as f64).collect();
    let day_net: Vec<f64> = daily_net.values().map(|v| v.iter().sum::<f64>() / v.len() as f64).collect();
    let t = match stdev(&day_net) {
        Some(sd) if day_net.len() > 1 && sd > 0.0 => {
            fmean(&day_net).map(|m| m / (sd / (day_net.len() as f64).sqrt()))
        }
        _ => None,
    };
    let mean = |f: fn(&Settled) -> f64| {
        pk_stats::np_mean(&rows.iter().map(|r| f(r)).collect::<Vec<_>>()).unwrap_or(f64::NAN)
    };
    json!({
        "n": rows.len(),
        "avg_px": round_dp(mean(|r| r.px), 4),
        "win_rate": round_dp(mean(|r| r.win), 4),
        "edge_pp": round_dp(100.0 * (mean(|r| r.win) - mean(|r| r.px)), 2),
        "net_cents": round_dp(mean(|r| r.net_cents), 2),
        "days_positive": format!("{}/{}", day_gross.iter().filter(|g| **g > 0.0).count(), day_gross.len()),
        "day_t_net": t.map(|t| round_dp(t, 2)),
    })
}

pub fn summarise(books: &BTreeMap<&'static str, Vec<Settled>>) -> Value {
    let (signal, null) = (&books["signal"], &books["random_same_rule"]);
    let mut bands = serde_json::Map::new();
    for (lo, _) in BANDS {
        let name = band_of(lo);
        let s: Vec<f64> = signal.iter().filter(|r| r.band == name).map(|r| r.gross_cents).collect();
        let n: Vec<f64> = null.iter().filter(|r| r.band == name).map(|r| r.gross_cents).collect();
        let excess = (!s.is_empty() && !n.is_empty()).then(|| {
            round_dp(s.iter().sum::<f64>() / s.len() as f64 - n.iter().sum::<f64>() / n.len() as f64, 2)
        });
        bands.insert(name, json!({"n_signal": s.len(), "n_null": n.len(), "excess_cents": excess}));
    }
    let mut per_asset: BTreeMap<&str, Vec<&Settled>> = BTreeMap::new();
    for r in signal {
        per_asset.entry(&r.asset).or_default().push(r);
    }
    json!({
        "arms": books.iter().map(|(k, v)| ((*k).to_owned(), arm_row(&v.iter().collect::<Vec<_>>()))).collect::<serde_json::Map<_, _>>(),
        "bands_signal_vs_random_same_rule": bands,
        "signal_by_asset": per_asset.into_iter().map(|(a, v)| (a.to_owned(), arm_row(&v))).collect::<serde_json::Map<_, _>>(),
    })
}
