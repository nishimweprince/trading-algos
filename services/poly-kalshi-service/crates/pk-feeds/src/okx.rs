//! OKX (`o`): the public v5 WebSocket, for event contracts and their index.
//!
//! - Per event instrument: `books` (a snapshot, then updates with absolute
//!   sizes, `seqId` / `prevSeqId`) and `trades`. Per underlying (`BTC-USDT`):
//!   `index-tickers`, the index the contracts settle on.
//! - OKX closes a socket that has sent nothing for 30 s, so the client sends a
//!   text `ping` every 20 s and the server answers `pong`.
//! - A `books` update whose `prevSeqId` is not the last `seqId` seen is a gap:
//!   the instrument is unsubscribed and subscribed again for a fresh snapshot,
//!   and a `pk_resync` line tells the engine to drop that book until it comes.
//!
//! Every frame is emitted untouched under `o`; connects are `pk_reconnect`
//! lines. Subscriptions are diffed: only what changed is (un)subscribed.

use std::collections::{BTreeSet, HashMap};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::{Duration, Instant};

use futures_util::{SinkExt, StreamExt};
use parking_lot::Mutex;
use serde_json::{Value, json};
use tokio::sync::Notify;
use tokio_tungstenite::tungstenite::Message;

use crate::ws::Emit;

const PING_EVERY: Duration = Duration::from_secs(20);
/// No frame at all (pongs included) for this long: the socket is dead.
const SILENCE: Duration = Duration::from_secs(45);
/// Recycle once a day, as the other feeds do.
const MAX_SESSION: Duration = Duration::from_secs(23 * 3600);

#[derive(Default)]
struct Shared {
    instruments: Mutex<BTreeSet<String>>,
    indexes: Mutex<BTreeSet<String>>,
    changed: Notify,
    connected: AtomicBool,
    connects: AtomicU64,
    messages: AtomicU64,
    books: AtomicU64,
    pongs: AtomicU64,
    seq_gaps: AtomicU64,
    errors: AtomicU64,
    venue_errors: AtomicU64,
    /// Subscribe sent → the first `subscribe` event back, µs (the last one).
    sub_ack_us: AtomicU64,
    last_error: Mutex<Option<String>>,
}

/// The runtime's handle on the socket.
#[derive(Clone)]
pub struct OkxFeed {
    shared: Arc<Shared>,
}

impl OkxFeed {
    /// Make the subscription exactly these event instruments and index ids.
    pub fn set_wanted(&self, instruments: BTreeSet<String>, indexes: BTreeSet<String>) {
        let mut i = self.shared.instruments.lock();
        let mut x = self.shared.indexes.lock();
        if *i != instruments || *x != indexes {
            *i = instruments;
            *x = indexes;
            drop((i, x));
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
            "pongs": s.pongs.load(Ordering::Relaxed),
            "seq_gaps": s.seq_gaps.load(Ordering::Relaxed),
            "errors": s.errors.load(Ordering::Relaxed),
            "venue_errors": s.venue_errors.load(Ordering::Relaxed),
            "subscribed": s.instruments.lock().len() + s.indexes.lock().len(),
            "sub_ack_ms": if ack > 0 { Some(ack as f64 / 1e3) } else { None },
            "last_error": s.last_error.lock().clone(),
        })
    }
}

/// Start the feed task. It runs until the runtime shuts down.
pub fn spawn_okx(url: String, emit: Emit) -> OkxFeed {
    let shared = Arc::new(Shared::default());
    let feed = OkxFeed { shared: shared.clone() };
    tokio::spawn(async move {
        let mut backoff = Duration::from_secs(1);
        loop {
            let connects = shared.connects.load(Ordering::Relaxed);
            if let Err(e) = session(&url, &emit, &shared).await {
                tracing::warn!(venue = "o", error = %e, "feed_error");
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

/// The channel args for a set of instruments and indexes.
pub fn args(instruments: &BTreeSet<String>, indexes: &BTreeSet<String>) -> Vec<Value> {
    let mut a = Vec::new();
    for i in instruments {
        a.push(json!({"channel": "books", "instId": i}));
        a.push(json!({"channel": "trades", "instId": i}));
    }
    for x in indexes {
        a.push(json!({"channel": "index-tickers", "instId": x}));
    }
    a
}

/// One `books` frame's sequence facts: instrument, action, seqId, prevSeqId.
#[derive(Debug, PartialEq)]
pub struct BookSeq {
    pub inst: String,
    pub snapshot: bool,
    pub seq: i64,
    pub prev: i64,
}

pub fn book_seq(v: &Value) -> Option<BookSeq> {
    if v.pointer("/arg/channel").and_then(Value::as_str) != Some("books") {
        return None;
    }
    let d = v.pointer("/data/0")?;
    Some(BookSeq {
        inst: v.pointer("/arg/instId")?.as_str()?.to_owned(),
        snapshot: v.get("action").and_then(Value::as_str) == Some("snapshot"),
        seq: d.get("seqId")?.as_i64()?,
        prev: d.get("prevSeqId").and_then(Value::as_i64).unwrap_or(-1),
    })
}

/// Whether an update continues the last sequence number seen (`None`: no snapshot yet).
/// OKX repeats `seqId` when nothing changed, with `prevSeqId` equal to it.
pub fn continues(last: Option<i64>, b: &BookSeq) -> bool {
    b.snapshot || last.is_some_and(|l| b.prev == l)
}

type Ws = tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

async fn send_op(ws: &mut Ws, op: &str, args: Vec<Value>) -> Result<(), String> {
    // OKX caps one request at 64 KB; 100 args stays far under it.
    for chunk in args.chunks(100) {
        ws.send(Message::text(json!({"op": op, "args": chunk}).to_string())).await.map_err(|e| format!("{op}: {e}"))?;
    }
    Ok(())
}

async fn session(url: &str, emit: &Emit, shared: &Shared) -> Result<(), String> {
    let (mut ws, _) = tokio_tungstenite::connect_async(url).await.map_err(|e| format!("connect: {e}"))?;
    let n = shared.connects.fetch_add(1, Ordering::Relaxed) + 1;
    shared.connected.store(true, Ordering::Relaxed);
    emit('o', json!({"type": "pk_reconnect", "venue": "o", "n": n}).to_string());
    tracing::info!(venue = "o", n, "feed_connected");
    let started = Instant::now();
    let mut last_frame = Instant::now();
    let mut ping = tokio::time::interval(PING_EVERY);
    ping.tick().await;
    let mut have: (BTreeSet<String>, BTreeSet<String>) = (BTreeSet::new(), BTreeSet::new());
    let mut seqs: HashMap<String, i64> = HashMap::new();
    let mut sub_sent: Option<Instant> = None;
    shared.changed.notify_one();
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
                if text == "pong" {
                    shared.pongs.fetch_add(1, Ordering::Relaxed);
                    continue;
                }
                let Ok(v) = serde_json::from_str::<Value>(&text) else { continue };
                match v.get("event").and_then(Value::as_str) {
                    Some("subscribe") => {
                        if let Some(t0) = sub_sent.take() {
                            shared.sub_ack_us.store(t0.elapsed().as_micros() as u64, Ordering::Relaxed);
                        }
                    }
                    Some("error") => {
                        shared.venue_errors.fetch_add(1, Ordering::Relaxed);
                        *shared.last_error.lock() = Some(text.chars().take(200).collect());
                    }
                    _ => {}
                }
                let resync = book_seq(&v).and_then(|b| {
                    shared.books.fetch_add(1, Ordering::Relaxed);
                    let last = seqs.get(&b.inst).copied();
                    if continues(last, &b) {
                        seqs.insert(b.inst, b.seq);
                        None
                    } else {
                        seqs.remove(&b.inst);
                        Some(b.inst)
                    }
                });
                emit('o', text);
                if let Some(inst) = resync {
                    shared.seq_gaps.fetch_add(1, Ordering::Relaxed);
                    emit('o', json!({"type": "pk_resync", "venue": "o", "instId": inst}).to_string());
                    let a = vec![json!({"channel": "books", "instId": inst})];
                    send_op(&mut ws, "unsubscribe", a.clone()).await?;
                    send_op(&mut ws, "subscribe", a).await?;
                }
            }
            _ = shared.changed.notified() => {
                let want = (shared.instruments.lock().clone(), shared.indexes.lock().clone());
                let gone = args(&have.0.difference(&want.0).cloned().collect(), &have.1.difference(&want.1).cloned().collect());
                let new = args(&want.0.difference(&have.0).cloned().collect(), &want.1.difference(&have.1).cloned().collect());
                if !gone.is_empty() {
                    send_op(&mut ws, "unsubscribe", gone).await?;
                }
                if !new.is_empty() {
                    send_op(&mut ws, "subscribe", new).await?;
                    sub_sent = Some(Instant::now());
                }
                seqs.retain(|k, _| want.0.contains(k));
                have = want;
            }
            _ = ping.tick() => {
                if last_frame.elapsed() > SILENCE {
                    return Err(format!("silent {:.0}s", last_frame.elapsed().as_secs_f64()));
                }
                if started.elapsed() > MAX_SESSION {
                    let _ = ws.close(None).await;
                    return Ok(());
                }
                ws.send(Message::text("ping")).await.map_err(|e| format!("ping: {e}"))?;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sequence() {
        let snap: Value = serde_json::from_str(
            r#"{"arg":{"channel":"books","instId":"BTC-UPDOWN-15MIN-261023-2230-2245"},"action":"snapshot","data":[{"asks":[["0.96","0.1","0","1"]],"bids":[],"ts":"1","checksum":0,"seqId":23584,"prevSeqId":-1}]}"#,
        )
        .unwrap();
        let b = book_seq(&snap).unwrap();
        assert!(b.snapshot && b.seq == 23584 && continues(None, &b));
        let upd: Value = serde_json::from_str(
            r#"{"arg":{"channel":"books","instId":"X"},"action":"update","data":[{"asks":[],"bids":[],"ts":"2","seqId":23585,"prevSeqId":23584}]}"#,
        )
        .unwrap();
        let u = book_seq(&upd).unwrap();
        assert!(continues(Some(23584), &u));
        assert!(!continues(Some(23580), &u), "a gap");
        assert!(!continues(None, &u), "an update before any snapshot");
        let other: Value = serde_json::from_str(r#"{"arg":{"channel":"trades","instId":"X"},"data":[{}]}"#).unwrap();
        assert_eq!(book_seq(&other), None);
    }

    #[test]
    fn channel_args() {
        let a = args(&BTreeSet::from(["E1".to_owned()]), &BTreeSet::from(["BTC-USDT".to_owned()]));
        assert_eq!(a.len(), 3);
        assert_eq!(a[2], json!({"channel": "index-tickers", "instId": "BTC-USDT"}));
    }
}
