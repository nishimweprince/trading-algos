//! PLAN 0.1a: validate the Helix decoder on a day before any number depends on it.
//!
//! ```text
//! pk helix validate --day 20260920 [--gamma 40] [--out research/data]
//! ```
//!
//! Each check is reported with its numbers in `helix_validation_<day>.json`:
//!
//! 1. **Identity**: the array path against the typed records, field for field.
//! 2. **Mirror**: Polymarket `yes_bid ≈ 1 − no_ask` at every YES tick; Kalshi YES against NO.
//! 3. **Window span**: where each token's ticks fall against its window.
//! 4. **Convergence**: the YES book 5 s before close against the resolution
//!    record, and that against Gamma for a sample of windows (cached).
//! 5. **Tick rates** per stream, raw and deduplicated.
//! 6. **Clocks**: wall minus monotonic, and venue-to-receive latency per venue.
//! 7. **Kalshi roll**: the old contract's terminal book after each open.
//!
//! The Gamma sample is drawn from a seeded generator of its own, not numpy's.

use std::collections::HashMap;
use std::path::Path;

use pk_core::num::round_dp;
use rand::SeedableRng;
use rand::seq::index::sample;
use serde_json::{Map, Value, json};

use super::container::{blocks, tags};
use super::gamma::GammaCache;
use super::pd::quantile;
use super::records::{
    TAG_POLY_BBO_NS, TAG_QUOTE_NS, asset_of, iter_poly_bbo, iter_poly_trades, iter_quotes, iter_resolutions,
    iter_trades, iter_windows,
};
use super::series::{
    Extra, MonoToWall, PX, Series, asof, interp, kalshi_contract_start, kalshi_quote_series, poly_bbo_series,
};
use super::{HELIX_15M, NS, asset_name, path};

fn pct(values: &[f64], qs: &[f64]) -> Value {
    if values.is_empty() {
        return json!({});
    }
    let mut m: Map<String, Value> = qs
        .iter()
        .map(|q| {
            (
                format!("p{}", (q * 100.0).round_ties_even()),
                json!(round_dp(quantile(values, *q).expect("values"), 4)),
            )
        })
        .collect();
    m.insert("n".into(), values.len().into());
    Value::Object(m)
}

const Q3: [f64; 3] = [0.01, 0.5, 0.99];

fn i64_at(d: &[u8], o: usize) -> i64 {
    i64::from_le_bytes(d[o..o + 8].try_into().expect("8 bytes"))
}

fn f64_at(d: &[u8], o: usize) -> f64 {
    f64::from_le_bytes(d[o..o + 8].try_into().expect("8 bytes"))
}

/// The first `n` records of each BBO file: the typed path against the block path.
pub fn check_identity(day: &str, root: &Path, n: usize) -> anyhow::Result<Value> {
    // Raw records of the block path, in file order, with the first block's
    // scale applied to all (as the Python check does).
    let block_path =
        |file: &Path, canonical: u8, rsz: usize, limit: usize| -> anyhow::Result<(Vec<Vec<u8>>, i64)> {
            let mut out = Vec::new();
            let mut scale = None;
            for raw in blocks(file, &tags(canonical), rsz)? {
                scale.get_or_insert(raw.scale);
                // The block's tag is appended to each record, so
                // the asset check can apply the tag's rule.
                out.extend(raw.records().map(|r| {
                    let mut v = r.to_vec();
                    v.push(raw.tag);
                    v
                }));
                if out.len() >= limit {
                    break;
                }
            }
            Ok((out, scale.unwrap_or(1)))
        };
    let p = path(root, "Polymarket_CLOB_Yes_BBO", day);
    let typed: Vec<_> = iter_poly_bbo(&p)?.into_iter().take(n).collect();
    let (mut a, scale) = block_path(&p, TAG_POLY_BBO_NS, 224, n)?;
    a.truncate(n);
    let eq = a.len() == typed.len();
    let all = |f: &dyn Fn(usize) -> bool| eq && (0..a.len()).all(f);
    let px = |e8: i64| (e8 as f64 * (PX as f64 / 1e8)).round_ties_even() as i16 as f64 / PX as f64;
    let close = |x: f64, y: f64| (x - y).abs() <= 1e-8 + 1e-5 * y.abs();
    let poly = json!({
        "records": typed.len(),
        "wall": all(&|i| i64_at(&a[i], 8) * scale == typed[i].ts_wall_ns),
        "recv": all(&|i| i64_at(&a[i], 0) * scale == typed[i].ts_received_ns),
        "bid": all(&|i| px(i64_at(&a[i], 24)) == round_dp(typed[i].bid_px, 4)),
        "ask": all(&|i| px(i64_at(&a[i], 40)) == round_dp(typed[i].ask_px, 4)),
        "sizes": all(&|i| close(i64_at(&a[i], 32) as f64 / 1e8, typed[i].bid_sz) && close(i64_at(&a[i], 48) as f64 / 1e8, typed[i].ask_sz)),
        "token": all(&|i| {
            let mut k = [0u8; 24];
            let t = typed[i].token_id.as_bytes();
            k[..t.len().min(24)].copy_from_slice(&t[..t.len().min(24)]);
            a[i][136..160] == k
        }),
    });
    let p = path(root, "Kalshi_Yes_BBO", day);
    let typed: Vec<_> = iter_quotes(&p, false)?.into_iter().take(n).collect();
    let (mut k, scale) = block_path(&p, TAG_QUOTE_NS, 64, n)?;
    k.truncate(typed.len());
    let eq = k.len() == typed.len();
    let all = |f: &dyn Fn(usize) -> bool| eq && (0..k.len()).all(f);
    let kalshi = json!({
        "records": typed.len(),
        "wall": all(&|i| i64_at(&k[i], 40) * scale == typed[i].ts_wall_ns),
        "bid_ask": all(&|i| f64_at(&k[i], 0) == typed[i].bid) && all(&|i| f64_at(&k[i], 8) == typed[i].ask),
        "asset": all(&|i| {
            let r = &k[i];
            asset_of(r[r.len() - 1], r[58], u16::from_le_bytes([r[60], r[61]])) == typed[i].asset_id
        }),
        "changed": all(&|i| k[i][59] == typed[i].changed),
    });
    let pass = [&poly, &kalshi]
        .iter()
        .all(|part| part.as_object().expect("object").iter().all(|(kk, v)| kk == "records" || v == true));
    Ok(json!({"poly_bbo": poly, "kalshi_bbo": kalshi, "pass": pass}))
}

/// Kalshi's wall − monotonic offset against Polymarket's at the same instants.
fn same_instant(poly: &Series, kalshi: &Series) -> Vec<f64> {
    let mut order: Vec<usize> = (0..poly.recv.len()).collect();
    order.sort_by_key(|&i| poly.recv[i]);
    if order.is_empty() {
        return Vec::new();
    }
    let recv: Vec<f64> = order.iter().map(|&i| poly.recv[i] as f64).collect();
    let off: Vec<f64> = order.iter().map(|&i| (poly.t[i] - poly.recv[i]) as f64).collect();
    let (lo, hi) = (poly.recv[order[0]], poly.recv[*order.last().expect("rows")]);
    (0..kalshi.recv.len())
        .filter(|&i| kalshi.recv[i] >= lo && kalshi.recv[i] <= hi)
        .map(|i| (kalshi.t[i] - kalshi.recv[i]) as f64 - interp(kalshi.recv[i] as f64, &recv, &off))
        .collect()
}

pub fn check_day(
    day: &str,
    root: &Path,
    gamma_sample: usize,
    cache_dir: &Path,
) -> anyhow::Result<Map<String, Value>> {
    let started = std::time::Instant::now();
    let windows = iter_windows(day, root)?;
    let mut res = HashMap::new();
    for r in iter_resolutions(day, root)? {
        res.insert((r.asset, r.open_unix), r);
    }
    let yes_tokens: Vec<String> = windows.iter().map(|w| w.token_yes.clone()).collect();
    let no_tokens: Vec<String> = windows.iter().map(|w| w.token_no.clone()).collect();
    let yes = poly_bbo_series(
        root,
        "Polymarket_CLOB_Yes_BBO",
        day,
        &yes_tokens,
        Extra { recv: true, publ: true, seq: false },
    )?;
    let no = poly_bbo_series(root, "Polymarket_CLOB_No_BBO", day, &no_tokens, Extra::default())?;
    let aids: Vec<u16> = HELIX_15M.iter().map(|(a, _)| *a).collect();
    let ky = kalshi_quote_series(
        root,
        "Kalshi_Yes_BBO",
        day,
        &aids,
        Extra { recv: true, publ: false, seq: true },
    )?;
    let span = match (ky.t.iter().max(), ky.t.iter().min()) {
        (Some(hi), Some(lo)) => (hi - lo) as f64 / 3.6e12,
        _ => 0.0,
    };
    let kn = kalshi_quote_series(root, "Kalshi_No_BBO", day, &aids, Extra::default())?;
    let mut out = Map::new();
    out.insert("day".into(), day.into());
    out.insert("windows".into(), windows.len().into());
    out.insert("resolutions".into(), res.len().into());
    out.insert("kalshi_span_hours".into(), round_dp(span, 2).into());

    // 2. mirror
    let (mut n_mirror, mut exact, mut tick) = (0usize, 0usize, 0usize);
    for k in 0..windows.len() {
        let (ys, ns) = (yes.rows(k), no.rows(k));
        let tn = &no.t[ns.clone()];
        if ys.is_empty() || ns.is_empty() {
            continue;
        }
        for i in ys {
            let Some(j) = asof(tn, yes.t[i]) else { continue };
            if !(yes.bid[i] > 0 && yes.ask[i] > 0) {
                continue;
            }
            let j = ns.start + j;
            let (yb, ya, nb, na) = (yes.bid[i] as i64, yes.ask[i] as i64, no.bid[j] as i64, no.ask[j] as i64);
            let d = (yb - (PX - na)).abs().max((ya - (PX - nb)).abs());
            n_mirror += 1;
            exact += (d == 0) as usize;
            tick += (d <= 100) as usize;
        }
    }
    let share = |a: usize, b: usize| round_dp(a as f64 / b.max(1) as f64, 4);
    out.insert(
        "poly_mirror".into(),
        json!({"ticks": n_mirror, "exact": share(exact, n_mirror), "within_1c": share(tick, n_mirror)}),
    );
    let (mut k_n, mut k_exact) = (0usize, 0usize);
    for k in 0..aids.len() {
        let (ys, ns) = (ky.rows(k), kn.rows(k));
        if ys.is_empty() || ns.is_empty() {
            continue;
        }
        let tn = &kn.t[ns.clone()];
        for i in ys {
            let Some(j) = asof(tn, ky.t[i]) else { continue };
            let d = (ky.bid[i] as i64 - (PX - kn.ask[ns.start + j] as i64)).abs();
            k_n += 1;
            k_exact += (d == 0) as usize;
        }
    }
    out.insert("kalshi_yes_no_mirror".into(), json!({"ticks": k_n, "exact": share(k_exact, k_n)}));

    // 3. window span, 4. convergence
    let (mut inside, mut before, mut after) = (0usize, 0usize, 0usize);
    let (mut agree, mut disagree, mut undecided) = (0usize, 0usize, 0usize);
    for (k, w) in windows.iter().enumerate() {
        let rows = yes.rows(k);
        let t = &yes.t[rows.clone()];
        let (o, c) = (w.open_unix * NS, (w.open_unix + 900) * NS);
        before += t.iter().filter(|x| **x < o).count();
        after += t.iter().filter(|x| **x >= c).count();
        inside += t.iter().filter(|x| **x >= o && **x < c).count();
        let Some(r) = res.get(&(w.asset, w.open_unix)) else { continue };
        if w.strike == 0.0 || w.strike.is_nan() {
            continue;
        }
        let up = r.close_px >= w.strike;
        let Some(j) = asof(t, c - 5 * NS) else { continue };
        // Near close the book is one-sided: 0.999 bid and no ask (Up), or no
        // bid and a 0.001 ask (Down). Read the side that is there.
        let (b, a) = (yes.bid[rows.start + j] as i64, yes.ask[rows.start + j] as i64);
        let decided = if b >= 9000 || (a != 0 && a >= 9000 && b == 0) {
            Some(true)
        } else if (a != 0 && a <= 1000) || (b != 0 && b <= 1000 && a == 0) {
            Some(false)
        } else {
            None
        };
        match decided {
            None => undecided += 1,
            Some(d) if d == up => agree += 1,
            Some(_) => disagree += 1,
        }
    }
    out.insert(
        "window_span".into(),
        json!({"inside": inside, "before_open": before, "after_close": after,
               "inside_share": share(inside, inside + before + after)}),
    );
    out.insert(
        "convergence_5s_before_close".into(),
        json!({"agree": agree, "disagree": disagree, "undecided_0.1_0.9": undecided,
               "agree_share_of_decided": share(agree, agree + disagree)}),
    );

    // 4b. Gamma sample: the tape's resolution against Polymarket's own outcome.
    if gamma_sample > 0 && !windows.is_empty() {
        let mut gamma = GammaCache::new(&cache_dir.join("gamma"))?;
        let mut rng = rand::rngs::StdRng::seed_from_u64(day.parse().unwrap_or(0));
        let picks = sample(&mut rng, windows.len(), gamma_sample.min(windows.len()));
        let mut rows = Vec::new();
        for k in picks {
            let w = &windows[k];
            let Some(r) = res.get(&(w.asset, w.open_unix)) else { continue };
            let outcome = gamma.outcome(asset_name(w.asset).expect("15-minute asset"), w.open_unix);
            rows.push((outcome, if r.close_px >= w.strike { "yes" } else { "no" }));
        }
        gamma.save()?;
        let known: Vec<_> = rows.iter().filter(|r| r.0.is_some()).collect();
        out.insert(
            "gamma_check".into(),
            json!({
                "sampled": rows.len(),
                "gamma_known": known.len(),
                "agree": known.iter().filter(|r| r.0.as_deref() == Some(r.1)).count(),
                "disagree": known.iter().filter(|r| r.0.as_deref() != Some(r.1)).map(|r| json!([r.0, r.1])).collect::<Vec<_>>(),
            }),
        );
    }

    // 5. tick rates
    let per_s = |n: usize| round_dp(n as f64 / 86400.0, 1);
    out.insert(
        "tick_rates_per_s".into(),
        json!({
            "poly_yes_raw": per_s(yes.raw_rows), "poly_yes_dedup": per_s(yes.len()),
            "poly_no_raw": per_s(no.raw_rows), "kalshi_yes_raw": per_s(ky.raw_rows),
            "kalshi_yes_dedup": per_s(ky.len()),
        }),
    );

    // 6. clocks and latency
    let off: Vec<i64> = (0..yes.len()).map(|i| yes.t[i] - yes.recv[i]).collect();
    let lat_p: Vec<f64> = (0..yes.len())
        .filter(|&i| yes.publ[i] > 0)
        .map(|i| (yes.recv[i] - yes.publ[i]) as f64 / 1e6)
        .collect();
    let mut clocks = Map::new();
    clocks.insert(
        "wall_minus_mono_poly_s".into(),
        pct(&off.iter().map(|o| (o - off[0]) as f64 / 1e9).collect::<Vec<_>>(), &Q3),
    );
    clocks.insert(
        "kalshi_vs_poly_offset_same_instant_ms".into(),
        pct(&same_instant(&yes, &ky).iter().map(|x| x / 1e6).collect::<Vec<_>>(), &Q3),
    );
    clocks.insert("poly_bbo_recv_minus_pub_ms".into(), pct(&lat_p, &Q3));
    // The monotonic clock drifts against wall (~2.4 s a day): a trade's receive
    // time maps to wall through the BBO rows nearest it.
    let clock = MonoToWall::new(&yes, 200_000);
    let lat_pt: Vec<f64> = iter_poly_trades(&path(root, "Polymarket_CLOB_Trade", day))?
        .into_iter()
        .take(200_000)
        .filter(|r| r.ts_publisher_ns > 0)
        .map(|r| {
            ((r.ts_received_ns as f64 + clock.offset(r.ts_received_ns)) - r.ts_publisher_ns as f64) / 1e6
        })
        .collect();
    let lat_kt: Vec<f64> = iter_trades(&path(root, "Kalshi_Yes_Trade", day))?
        .into_iter()
        .take(200_000)
        .map(|r| (r.ts_received_ns - r.ts_wall_ns) as f64 / 1e6)
        .collect();
    clocks.insert("poly_trade_recv_minus_venue_ms".into(), pct(&lat_pt, &Q3));
    clocks.insert("kalshi_trade_recv_minus_venue_ms".into(), pct(&lat_kt, &Q3));
    out.insert("clocks".into(), Value::Object(clocks));

    // 7. Kalshi roll: the asset-keyed stream carries the old contract's
    // terminal book past the open.
    let (mut switch_ms, mut stale, mut missing) = (Vec::new(), Vec::new(), 0usize);
    for w in &windows {
        let rows = ky.rows(aids.iter().position(|a| *a == w.asset).expect("listed"));
        let (t, seq) = (&ky.t[rows.clone()], &ky.seq[rows.clone()]);
        if t.is_empty() {
            continue;
        }
        let o = w.open_unix * NS;
        let i = kalshi_contract_start(t, seq, o);
        let at_open = t.partition_point(|&x| x < o);
        let prev = seq[at_open.saturating_sub(1)] as i64;
        if i == at_open && (at_open >= t.len() || (0..=50_000).contains(&(seq[at_open] as i64 - prev))) {
            missing += 1;
        }
        if i < t.len() {
            switch_ms.push((t[i] - o) as f64 / 1e6);
            stale.push(i.saturating_sub(at_open) as f64);
        }
    }
    out.insert(
        "kalshi_roll".into(),
        json!({
            "switch_after_open_ms": pct(&switch_ms, &[0.01, 0.5, 0.9, 0.99]),
            "old_contract_rows_after_open": pct(&stale, &[0.5, 0.99]),
            "windows_without_seq_break": missing,
        }),
    );
    out.insert("seconds".into(), round_dp(started.elapsed().as_secs_f64(), 1).into());
    Ok(out)
}

pub fn run(day: &str, root: &Path, gamma: usize, out: &Path) -> anyhow::Result<Value> {
    let mut result = Map::new();
    result.insert("identity".into(), check_identity(day, root, 500_000)?);
    result.extend(check_day(day, root, gamma, out)?);
    std::fs::create_dir_all(out)?;
    let v = Value::Object(result);
    std::fs::write(out.join(format!("helix_validation_{day}.json")), serde_json::to_string_pretty(&v)?)?;
    Ok(v)
}
