//! Chainlink prices from Polymarket's public real-time data socket (RTDS):
//! topic `crypto_prices_chainlink`, one update per asset per second, stamped
//! on whole seconds (`payload.timestamp`, ms). No credentials.
//!
//! ```json
//! {"topic":"crypto_prices_chainlink","type":"update","timestamp":1795531023109,
//!  "payload":{"symbol":"btc/usd","timestamp":1795531022000,"value":60156.927}}
//! ```

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use tokio_tungstenite::tungstenite::Message;

/// `(asset in upper case, second, price, arrival ns)`.
pub type OnTick = Arc<dyn Fn(&str, i64, f64, i64) + Send + Sync>;

#[derive(Default)]
pub struct RtdsStats {
    pub connected: AtomicBool,
    pub connects: AtomicU64,
    pub ticks: AtomicU64,
    pub errors: AtomicU64,
}

pub fn now_ns() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos() as i64).unwrap_or(0)
}

/// One RTDS frame's Chainlink tick, if it is one.
pub fn parse(text: &str) -> Option<(String, i64, f64)> {
    let v: Value = serde_json::from_str(text).ok()?;
    if v.get("topic").and_then(Value::as_str) != Some("crypto_prices_chainlink") {
        return None;
    }
    let p = v.get("payload")?;
    let sym = p.get("symbol")?.as_str()?;
    let asset = sym.split('/').next()?.to_uppercase();
    let ts = p.get("timestamp")?.as_i64()?;
    let px = p.get("value").and_then(|x| x.as_f64().or_else(|| x.as_str().and_then(|s| s.parse().ok())))?;
    Some((asset, ts.div_euclid(1_000), px))
}

pub fn spawn(url: String, on_tick: OnTick) -> Arc<RtdsStats> {
    let stats = Arc::new(RtdsStats::default());
    let st = stats.clone();
    tokio::spawn(async move {
        let mut backoff = Duration::from_secs(1);
        loop {
            match session(&url, &on_tick, &st).await {
                Ok(()) => backoff = Duration::from_secs(1),
                Err(e) => {
                    st.errors.fetch_add(1, Ordering::Relaxed);
                    tracing::warn!(error = %e, "rtds_error");
                }
            }
            st.connected.store(false, Ordering::Relaxed);
            tokio::time::sleep(backoff).await;
            backoff = (backoff * 2).min(Duration::from_secs(30));
        }
    });
    stats
}

async fn session(url: &str, on_tick: &OnTick, st: &RtdsStats) -> Result<(), String> {
    let (mut ws, _) = tokio_tungstenite::connect_async(url).await.map_err(|e| format!("connect: {e}"))?;
    let sub = json!({"action": "subscribe",
                     "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}]});
    ws.send(Message::text(sub.to_string())).await.map_err(|e| format!("subscribe: {e}"))?;
    st.connected.store(true, Ordering::Relaxed);
    st.connects.fetch_add(1, Ordering::Relaxed);
    tracing::info!("rtds_connected");
    let mut ping = tokio::time::interval(Duration::from_secs(5));
    let mut last_data = tokio::time::Instant::now();
    loop {
        tokio::select! {
            _ = ping.tick() => {
                if last_data.elapsed() > Duration::from_secs(15) {
                    return Err("silent for 15 s".into());
                }
                ws.send(Message::Ping(Vec::new().into())).await.map_err(|e| format!("ping: {e}"))?;
            }
            msg = ws.next() => {
                let Some(msg) = msg else { return Err("closed".into()) };
                match msg.map_err(|e| format!("read: {e}"))? {
                    Message::Text(t) => {
                        if let Some((asset, sec, px)) = parse(&t) {
                            last_data = tokio::time::Instant::now();
                            st.ticks.fetch_add(1, Ordering::Relaxed);
                            on_tick(&asset, sec, px, now_ns());
                        }
                    }
                    Message::Close(c) => return Err(format!("close {c:?}")),
                    _ => {}
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_a_chainlink_update() {
        let t = r#"{"connection_id":"x","payload":{"full_accuracy_value":"60156927000000000000000","symbol":"btc/usd","timestamp":1795531022000,"value":60156.927},"timestamp":1795531023109,"topic":"crypto_prices_chainlink","type":"update"}"#;
        assert_eq!(parse(t), Some(("BTC".into(), 1_795_531_022, 60156.927)));
        assert_eq!(parse(r#"{"topic":"crypto_prices","payload":{}}"#), None);
        assert_eq!(parse(""), None);
    }
}
