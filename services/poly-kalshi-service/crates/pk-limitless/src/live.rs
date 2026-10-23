//! Live orders (`PK_LIM_EXECUTION_MODE=live`, refused without
//! `PK_LIM_LIVE_CONFIRM=yes`) and the order-path probe.
//!
//! An intent from the engine becomes one FAK order at its limit. The venue holds
//! it for `takerDelayMs` and answers `DELAYED`; its result is read from
//! `POST /orders/status/batch` until it is final, and the engine books what
//! actually filled. A dead-man heartbeat cancels anything left open if this
//! process stops renewing it.

use std::str::FromStr;
use std::sync::Arc;
use std::time::{Duration, Instant};

use alloy::primitives::Address;
use parking_lot::Mutex;
use pk_exec::limitless::{LimClient, LimOrder, LimSide, LimTif, filled, price_milli};
use serde_json::{Value, json};

use crate::engine::{Engine, Intent, Kind};
use crate::probes::{OrderProbeStats, TakerStats};

const FINAL: [&str; 6] = ["MATCHED", "MINED", "CONFIRMED", "UNMATCHED", "FAILED", "CANCELED"];

pub fn now_ns() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos() as i64).unwrap_or(0)
}

pub fn order_for(i: &Intent) -> LimOrder {
    let side = if i.kind == Kind::Entry { LimSide::Buy } else { LimSide::Sell };
    LimOrder {
        slug: i.window.slug.clone(),
        token_id: if i.yes { i.window.yes_token.clone() } else { i.window.no_token.clone() },
        side,
        price_milli: price_milli(i.limit, side),
        shares: i.shares.floor().max(1.0) as u64,
        tif: LimTif::Fak,
        post_only: false,
    }
}

/// Send one intent and book its result.
pub async fn execute(client: Arc<LimClient>, engine: Arc<Mutex<Engine>>, taker: Arc<Mutex<TakerStats>>, i: Intent) {
    let t_decision = Instant::now();
    let order = order_for(&i);
    let exchange = match Address::from_str(&i.window.exchange) {
        Ok(a) => a,
        Err(e) => {
            engine.lock().live_result(&i.window, 0.0, 0.0, Some(0.0), json!({"error": e.to_string()}), now_ns());
            return;
        }
    };
    let posted = match client.post(exchange, &order, t_decision).await {
        Ok(p) => p,
        Err(e) => {
            let d = json!({"kind": format!("{:?}", i.kind), "error": e.to_string()});
            engine.lock().live_result(&i.window, 0.0, 0.0, Some(0.0), d, now_ns());
            return;
        }
    };
    let ack_ms = posted.timing.decision_to_ack_us as f64 / 1e3;
    let mut detail = json!({
        "kind": format!("{:?}", i.kind).to_lowercase(), "asset": i.asset, "open_unix": i.open_unix,
        "order": order, "timing": posted.timing, "ok": posted.ok, "order_id": posted.order_id,
        "status": posted.status, "eligible_at": posted.eligible_at, "error": posted.error,
    });
    let (Some(id), true) = (posted.order_id.clone(), posted.ok) else {
        taker.lock().push(ack_ms, None, false);
        engine.lock().live_result(&i.window, 0.0, 0.0, Some(0.0), detail, now_ns());
        return;
    };
    let t_ack = Instant::now();
    let mut execution = posted.body.get("execution").cloned().unwrap_or(Value::Null);
    let mut status = posted.status.clone();
    // Held for takerDelayMs, then matched or killed: poll until final (≤ 5 s).
    while !status.as_deref().is_some_and(|s| FINAL.contains(&s)) && t_ack.elapsed() < Duration::from_secs(5) {
        tokio::time::sleep(Duration::from_millis(100)).await;
        match client.status(&id).await {
            Ok((s, e)) => {
                if s.is_some() {
                    status = s;
                    execution = e;
                }
            }
            Err(e) => detail["status_error"] = e.to_string().into(),
        }
    }
    let held_ms = t_ack.elapsed().as_secs_f64() * 1e3;
    let f = filled(&execution);
    detail["final_status"] = status.clone().into();
    detail["ack_to_final_ms"] = held_ms.into();
    detail["filled"] = serde_json::to_value(f).unwrap_or(Value::Null);
    taker.lock().push(ack_ms, Some(held_ms), f.shares > 0.0);
    if !status.as_deref().is_some_and(|s| FINAL.contains(&s)) {
        // Not final after 5 s: cancel what is left and book nothing until it is known.
        let _ = client.cancel(&id).await;
        detail["cancelled_unresolved"] = true.into();
    }
    engine.lock().live_result(&i.window, f.shares, f.usd_gross, Some(f.usd_fee), detail, now_ns());
}

/// Renew the dead-man switch every 3 s, 15 s ahead.
pub async fn heartbeat(client: Arc<LimClient>, failures: Arc<std::sync::atomic::AtomicU64>) {
    let mut tick = tokio::time::interval(Duration::from_secs(3));
    loop {
        tick.tick().await;
        let at = chrono::Utc::now().timestamp_millis() + 15_000;
        if let Err(e) = client.heartbeat(at).await {
            failures.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            tracing::warn!(error = %e, "lim_heartbeat_failed");
        }
    }
}

/// One post-only probe: a buy `back` below the best bid that can't fill, then cancel.
/// Refuses any price that is not strictly below the bid, and notional above `max_usd`.
pub async fn probe_once(
    client: &LimClient,
    window: &crate::discovery::LimWindow,
    best_bid: f64,
    back: f64,
    shares: u64,
    max_usd: f64,
) -> anyhow::Result<Value> {
    let price = price_milli(best_bid - back, LimSide::Buy);
    anyhow::ensure!(f64::from(price) / 1e3 < best_bid - 1e-9, "probe price is not below the bid");
    anyhow::ensure!(f64::from(price) / 1e3 * shares as f64 <= max_usd + 1e-9, "probe notional above the cap");
    let order = LimOrder {
        slug: window.slug.clone(),
        token_id: window.yes_token.clone(),
        side: LimSide::Buy,
        price_milli: price,
        shares,
        tif: LimTif::Gtc,
        post_only: true,
    };
    let posted = client.post(Address::from_str(&window.exchange)?, &order, Instant::now()).await?;
    let mut r = json!({"type": "lim_order_probe", "t_ns": now_ns(), "asset": window.asset, "order": order,
                       "timing": posted.timing, "ok": posted.ok, "status": posted.status, "error": posted.error});
    if let Some(id) = &posted.order_id {
        let (us, ok, reply) = client.cancel(id).await?;
        r["cancel_ms"] = (us as f64 / 1e3).into();
        r["cancel_ok"] = ok.into();
        if !ok {
            r["cancel_reply"] = reply;
        }
    }
    Ok(r)
}

pub fn record_probe(stats: &Mutex<OrderProbeStats>, r: &Value) {
    let mut s = stats.lock();
    if r["ok"].as_bool() == Some(true) {
        let ack = r.pointer("/timing/decision_to_ack_us").and_then(Value::as_f64).unwrap_or(0.0) / 1e3;
        s.push(ack, r["cancel_ms"].as_f64());
    } else {
        s.rejected += 1;
        s.last_error = r["error"].as_str().map(str::to_owned);
    }
}
