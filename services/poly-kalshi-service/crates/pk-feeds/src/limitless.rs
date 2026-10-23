//! Limitless (`l`): the public `/markets` namespace over Socket.IO v4, WebSocket only.
//!
//! The framing is small enough to speak by hand (no client library):
//! - Engine.IO `0{..}` opens the socket; the client sends `40/markets,` and
//!   waits for the namespace's `40/markets,{..}`.
//! - The server pings (`2`) and the client answers `3`. The client never pings.
//! - Events are `42/markets,["name",payload]`. `subscribe_market_prices
//!   {marketSlugs}` replaces the whole subscription; the server acknowledges it
//!   with `system` and then sends one full `orderbookUpdate` snapshot per slug.
//!
//! Every `orderbookUpdate` is the whole book (coalesced), so a frame needs no
//! history. Each event is emitted as one compact line `{"ev":name,"d":payload}`;
//! connects as `pk_reconnect` lines under `l`. The load balancer drops every
//! connection after 24 h without a close frame, so the session recycles itself
//! before that.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::{Duration, Instant};

use futures_util::{SinkExt, StreamExt};
use parking_lot::Mutex;
use serde_json::{Value, json};
use tokio::sync::Notify;
use tokio_tungstenite::tungstenite::Message;

use crate::ws::Emit;

pub const NAMESPACE: &str = "/markets";
/// Recycle well before the load balancer's 24 h cut.
const MAX_SESSION: Duration = Duration::from_secs(23 * 3600);
/// No frame at all (pings included) for this long: the socket is dead.
const SILENCE: Duration = Duration::from_secs(70);

#[derive(Default)]
struct Shared {
    wanted: Mutex<Vec<String>>,
    changed: Notify,
    connected: AtomicBool,
    connects: AtomicU64,
    messages: AtomicU64,
    books: AtomicU64,
    pings: AtomicU64,
    errors: AtomicU64,
    exceptions: AtomicU64,
    /// Subscribe emitted → `system` acknowledgement, µs (the last one).
    sub_ack_us: AtomicU64,
    /// Engine.IO ping interval the server announced, ms.
    ping_interval_ms: AtomicU64,
    last_error: Mutex<Option<String>>,
}

/// The runtime's handle on the socket.
#[derive(Clone)]
pub struct LimitlessFeed {
    shared: Arc<Shared>,
}

impl LimitlessFeed {
    /// Make the subscription exactly `slugs` (sent at once, and on every reconnect).
    pub fn set_wanted(&self, mut slugs: Vec<String>) {
        slugs.sort();
        slugs.dedup();
        let mut w = self.shared.wanted.lock();
        if *w != slugs {
            *w = slugs;
            drop(w);
            self.shared.changed.notify_one();
        }
    }

    pub fn connected(&self) -> bool {
        self.shared.connected.load(Ordering::Relaxed)
    }

    pub fn stats(&self) -> Value {
        let s = &self.shared;
        let ack = s.sub_ack_us.load(Ordering::Relaxed);
        json!({
            "connected": self.connected(),
            "connects": s.connects.load(Ordering::Relaxed),
            "messages": s.messages.load(Ordering::Relaxed),
            "books": s.books.load(Ordering::Relaxed),
            "pings": s.pings.load(Ordering::Relaxed),
            "errors": s.errors.load(Ordering::Relaxed),
            "exceptions": s.exceptions.load(Ordering::Relaxed),
            "subscribed": s.wanted.lock().len(),
            "sub_ack_ms": if ack > 0 { Some(ack as f64 / 1e3) } else { None },
            "ping_interval_ms": s.ping_interval_ms.load(Ordering::Relaxed),
            "last_error": s.last_error.lock().clone(),
        })
    }
}

/// Start the feed task. It runs until the runtime shuts down.
pub fn spawn_limitless(url: String, emit: Emit) -> LimitlessFeed {
    let shared = Arc::new(Shared::default());
    let feed = LimitlessFeed { shared: shared.clone() };
    tokio::spawn(async move {
        let mut backoff = Duration::from_secs(1);
        loop {
            let connects = shared.connects.load(Ordering::Relaxed);
            if let Err(e) = session(&url, &emit, &shared).await {
                tracing::warn!(venue = "l", error = %e, "feed_error");
                shared.errors.fetch_add(1, Ordering::Relaxed);
                *shared.last_error.lock() = Some(e.chars().take(200).collect());
            }
            shared.connected.store(false, Ordering::Relaxed);
            if shared.connects.load(Ordering::Relaxed) != connects {
                backoff = Duration::from_secs(1);
            }
            tokio::time::sleep(backoff).await;
            backoff = (backoff * 2).min(Duration::from_secs(30));
        }
    });
    feed
}

/// One Socket.IO packet on the `/markets` namespace, as the client reads it.
#[derive(Debug, PartialEq)]
pub enum Packet {
    /// Engine.IO open, with the server's ping interval (ms).
    Open(Option<u64>),
    Ping,
    /// The namespace accepted the connection.
    Connected,
    /// The namespace refused or closed it.
    Refused(String),
    Event(String, Value),
    Other,
}

pub fn parse_packet(text: &str) -> Packet {
    if let Some(rest) = text.strip_prefix('0')
        && rest.starts_with('{')
    {
        let interval = serde_json::from_str::<Value>(rest).ok().and_then(|v| v["pingInterval"].as_u64());
        return Packet::Open(interval);
    }
    if text == "2" {
        return Packet::Ping;
    }
    let ns = format!("{NAMESPACE},");
    let Some(kind) = text.get(..2) else { return Packet::Other };
    let Some(body) = text.get(2..).and_then(|r| r.strip_prefix(ns.as_str()).or(Some(r))) else {
        return Packet::Other;
    };
    match kind {
        "40" => Packet::Connected,
        "41" => Packet::Refused("disconnected".into()),
        "44" => Packet::Refused(body.chars().take(200).collect()),
        "42" => {
            // An ack id may precede the array: `42/markets,12["ev",..]`.
            let array = body.trim_start_matches(|c: char| c.is_ascii_digit());
            match serde_json::from_str::<Value>(array) {
                Ok(Value::Array(mut a)) if !a.is_empty() => {
                    let name = a[0].as_str().unwrap_or_default().to_owned();
                    let payload = if a.len() > 1 { a.swap_remove(1) } else { Value::Null };
                    Packet::Event(name, payload)
                }
                _ => Packet::Other,
            }
        }
        _ => Packet::Other,
    }
}

/// `42/markets,["name",payload]`.
pub fn event_frame(name: &str, payload: &Value) -> String {
    format!("42{NAMESPACE},{}", json!([name, payload]))
}

async fn session(url: &str, emit: &Emit, shared: &Shared) -> Result<(), String> {
    let (mut ws, _) = tokio_tungstenite::connect_async(url).await.map_err(|e| format!("connect: {e}"))?;
    let started = Instant::now();
    let mut joined = false;
    let mut sub_sent: Option<Instant> = None;
    let mut last_frame = Instant::now();
    let mut watchdog = tokio::time::interval(Duration::from_secs(1));
    loop {
        tokio::select! {
            biased;
            msg = ws.next() => {
                let Some(msg) = msg else { return Err("closed".into()) };
                let text = match msg.map_err(|e| format!("read: {e}"))? {
                    Message::Text(t) => t.as_str().to_owned(),
                    Message::Close(f) => {
                        return Err(format!("close: {}", f.map(|f| format!("{} {}", u16::from(f.code), f.reason)).unwrap_or_default()));
                    }
                    _ => continue,
                };
                last_frame = Instant::now();
                shared.messages.fetch_add(1, Ordering::Relaxed);
                match parse_packet(&text) {
                    Packet::Open(interval) => {
                        if let Some(i) = interval {
                            shared.ping_interval_ms.store(i, Ordering::Relaxed);
                        }
                        ws.send(Message::text(format!("40{NAMESPACE},"))).await.map_err(|e| format!("send: {e}"))?;
                    }
                    Packet::Ping => {
                        shared.pings.fetch_add(1, Ordering::Relaxed);
                        ws.send(Message::text("3")).await.map_err(|e| format!("pong: {e}"))?;
                    }
                    Packet::Connected => {
                        joined = true;
                        let n = shared.connects.fetch_add(1, Ordering::Relaxed) + 1;
                        shared.connected.store(true, Ordering::Relaxed);
                        emit('l', json!({"type": "pk_reconnect", "venue": "l", "n": n}).to_string());
                        tracing::info!(venue = "l", n, "feed_connected");
                        sub_sent = subscribe(&mut ws, shared).await?;
                    }
                    Packet::Refused(why) => return Err(format!("namespace: {why}")),
                    Packet::Event(name, payload) => {
                        match name.as_str() {
                            "orderbookUpdate" => {
                                shared.books.fetch_add(1, Ordering::Relaxed);
                            }
                            "system" => {
                                if let Some(t0) = sub_sent.take() {
                                    shared.sub_ack_us.store(t0.elapsed().as_micros() as u64, Ordering::Relaxed);
                                }
                            }
                            "exception" | "error" => {
                                shared.exceptions.fetch_add(1, Ordering::Relaxed);
                                *shared.last_error.lock() = Some(payload.to_string().chars().take(200).collect());
                            }
                            _ => {}
                        }
                        emit('l', json!({"ev": name, "d": payload}).to_string());
                    }
                    Packet::Other => {}
                }
            }
            _ = shared.changed.notified() => {
                if joined {
                    sub_sent = subscribe(&mut ws, shared).await?;
                }
            }
            _ = watchdog.tick() => {
                if last_frame.elapsed() > SILENCE {
                    return Err(format!("silent {:.0}s", last_frame.elapsed().as_secs_f64()));
                }
                if started.elapsed() > MAX_SESSION {
                    let _ = ws.close(None).await;
                    return Ok(());
                }
            }
        }
    }
}

type Ws = tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

async fn subscribe(ws: &mut Ws, shared: &Shared) -> Result<Option<Instant>, String> {
    let slugs = shared.wanted.lock().clone();
    if slugs.is_empty() {
        return Ok(None);
    }
    let frame = event_frame("subscribe_market_prices", &json!({"marketSlugs": slugs}));
    ws.send(Message::text(frame)).await.map_err(|e| format!("subscribe: {e}"))?;
    Ok(Some(Instant::now()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn packets() {
        assert_eq!(parse_packet(r#"0{"sid":"a","pingInterval":25000,"pingTimeout":20000}"#), Packet::Open(Some(25000)));
        assert_eq!(parse_packet("2"), Packet::Ping);
        assert_eq!(parse_packet(r#"40/markets,{"sid":"x"}"#), Packet::Connected);
        assert!(matches!(parse_packet(r#"44/markets,{"message":"nope"}"#), Packet::Refused(_)));
        let Packet::Event(name, d) = parse_packet(r#"42/markets,["orderbookUpdate",{"marketSlug":"s","version":3}]"#) else {
            panic!("event")
        };
        assert_eq!((name.as_str(), d["version"].as_i64()), ("orderbookUpdate", Some(3)));
        let Packet::Event(name, _) = parse_packet(r#"42/markets,7["system",{"message":"ok"}]"#) else { panic!("ack id") };
        assert_eq!(name, "system");
        assert_eq!(parse_packet("3"), Packet::Other);
        assert_eq!(
            event_frame("subscribe_market_prices", &json!({"marketSlugs": ["a"]})),
            r#"42/markets,["subscribe_market_prices",{"marketSlugs":["a"]}]"#
        );
    }
}
