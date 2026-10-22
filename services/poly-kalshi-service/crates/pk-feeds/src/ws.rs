//! The WebSocket feeds.
//!
//! Each (re)connect first emits a `reconnect` control line, which makes the
//! engine drop that venue's books (or, for a shard, only its tokens' books)
//! until fresh snapshots arrive.
//!
//! - **Kalshi** (`k`): authenticated upgrade (RSA-PSS over `ts + "GET" + path`),
//!   channel `orderbook_delta`. Tickers rotate each window and are added and
//!   removed on the open socket with `update_subscription`. `seq` must be
//!   contiguous per `sid` (every frame with a seq advances it, acknowledgements
//!   too); on a gap the feed writes a `resync` control line and reconnects.
//! - **Polymarket** (`p`): the public market channel with
//!   `custom_feature_enabled`, a text `PING` every 10 s. One socket per shard,
//!   each with its own reconnect; it stays disconnected while nothing is wanted
//!   (an empty subscription is closed after ~10 s with 1008). A close with 1013
//!   ("slow consumer") is counted: draining the socket fast is the fix, which
//!   is why a feed task does nothing but read and hand frames on.
//! - **PolyBolt** (`b`): reference prices (Pyth and the Chainlink 60 s TWAP the
//!   markets settle on), authenticated with CLOB API credentials, recorded
//!   only. Its connects are written as `b` lines, never `c`, which the engine
//!   would read as a book reset.
//! - **Binance** (`n` spot, `f` perp), **Kraken** (`r`, v2 `ticker` with
//!   `event_trigger: bbo`), **Bitstamp** (`t`, `order_book_<pair>`, the top
//!   100 levels about every 100 ms) and **Coinbase** (`o`, the Exchange
//!   `ticker`: best bid and ask on each trade): public best bid/ask references, recorded
//!   and timed by the observer, never traded. Their connects are written under
//!   their own venue too. Bitstamp asks for a reconnect before maintenance
//!   (`bts:request_reconnect`).

use std::collections::{BTreeSet, HashMap};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use parking_lot::Mutex;
use serde_json::{Value, json};
use tokio::sync::Notify;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;

use crate::kalshi_sign::KalshiSigner;

/// Where frames go: `emit(venue, text)`.
pub type Emit = Arc<dyn Fn(char, String) + Send + Sync>;

#[derive(Clone)]
pub enum FeedKind {
    Kalshi { signer: Arc<KalshiSigner> },
    /// `silence`: reconnect after this long with no data (a socket that stays
    /// open while the venue stops sending, 10-22 05:04Z for three minutes).
    Poly { shard: Option<usize>, silence: Option<Duration> },
    PolyBolt { credentials: (String, String, String) },
    /// Binance best bid/ask streams (`<symbol>@bookTicker`), spot or USDⓈ-M perps.
    Binance { futures: bool },
    /// Kraken v2 `ticker` on best bid/offer changes; ids are symbols (`BTC/USD`).
    Kraken,
    /// Bitstamp `order_book_<pair>`; ids are channel names.
    Bitstamp,
    /// Coinbase Exchange `ticker`; ids are products (`BTC-USD`).
    Coinbase,
}

impl FeedKind {
    fn venue(&self) -> char {
        match self {
            FeedKind::Kalshi { .. } => 'k',
            FeedKind::Poly { .. } => 'p',
            FeedKind::PolyBolt { .. } => 'b',
            FeedKind::Binance { futures: false } => 'n',
            FeedKind::Binance { futures: true } => 'f',
            FeedKind::Kraken => 'r',
            FeedKind::Bitstamp => 't',
            FeedKind::Coinbase => 'o',
        }
    }

    /// Public references: recorded, timed and never read by the engine.
    fn reference(&self) -> bool {
        matches!(self, FeedKind::Binance { .. } | FeedKind::Kraken | FeedKind::Bitstamp | FeedKind::Coinbase)
    }
}

#[derive(Default)]
struct Shared {
    wanted: Mutex<BTreeSet<String>>,
    subscribed: Mutex<BTreeSet<String>>,
    changed: Notify,
    recycle: Notify,
    connected: AtomicBool,
    connects: AtomicU64,
    messages: AtomicU64,
    errors: AtomicU64,
    slow_consumer: AtomicU64,
    seq_gaps: AtomicU64,
    removed_since_connect: AtomicU64,
    auth_failures: AtomicU64,
    authed: AtomicBool,
    last_error: Mutex<Option<String>>,
    sid: Mutex<Option<i64>>,
}

/// The runtime's handle on one socket.
#[derive(Clone)]
pub struct FeedHandle {
    kind_name: &'static str,
    shared: Arc<Shared>,
}

impl FeedHandle {
    /// Make the live subscription `ids`. Kept across reconnects.
    pub fn set_wanted(&self, ids: BTreeSet<String>) {
        let mut w = self.shared.wanted.lock();
        if *w != ids {
            *w = ids;
            drop(w);
            self.shared.changed.notify_one();
        }
    }

    /// Reconnect: the new socket subscribes `wanted` only.
    pub fn recycle(&self) {
        self.shared.recycle.notify_one();
    }

    pub fn connected(&self) -> bool {
        self.shared.connected.load(Ordering::Relaxed)
    }

    pub fn last_error(&self) -> Option<String> {
        self.shared.last_error.lock().clone()
    }

    pub fn removed_since_connect(&self) -> u64 {
        self.shared.removed_since_connect.load(Ordering::Relaxed)
    }

    pub fn stats(&self) -> Value {
        let s = &self.shared;
        let mut v = json!({
            "connected": self.connected(),
            "connects": s.connects.load(Ordering::Relaxed),
            "messages": s.messages.load(Ordering::Relaxed),
            "errors": s.errors.load(Ordering::Relaxed),
            "last_error": self.last_error(),
            "subscribed": s.subscribed.lock().len(),
            "wanted": s.wanted.lock().len(),
            "slow_consumer": s.slow_consumer.load(Ordering::Relaxed),
        });
        let o = v.as_object_mut().expect("object");
        match self.kind_name {
            "kalshi" => {
                o.insert("sid".into(), (*s.sid.lock()).into());
                o.insert("seq_gaps".into(), s.seq_gaps.load(Ordering::Relaxed).into());
            }
            "poly" => {
                o.insert("removed_since_connect".into(), self.removed_since_connect().into());
            }
            "binance_spot" | "binance_perp" | "kraken" | "bitstamp" | "coinbase" => {}
            _ => {
                o.insert("authed".into(), s.authed.load(Ordering::Relaxed).into());
                o.insert("auth_failures".into(), s.auth_failures.load(Ordering::Relaxed).into());
            }
        }
        v
    }
}

/// Start a feed task. It runs until the runtime shuts down.
pub fn spawn_feed(url: String, kind: FeedKind, emit: Emit) -> FeedHandle {
    let shared = Arc::new(Shared::default());
    let kind_name = match &kind {
        FeedKind::Kalshi { .. } => "kalshi",
        FeedKind::Poly { .. } => "poly",
        FeedKind::PolyBolt { .. } => "polybolt",
        FeedKind::Binance { futures: false } => "binance_spot",
        FeedKind::Binance { futures: true } => "binance_perp",
        FeedKind::Kraken => "kraken",
        FeedKind::Bitstamp => "bitstamp",
        FeedKind::Coinbase => "coinbase",
    };
    let handle = FeedHandle { kind_name, shared: shared.clone() };
    tokio::spawn(run(url, kind, emit, shared));
    handle
}

fn set_error(shared: &Shared, e: String) {
    shared.errors.fetch_add(1, Ordering::Relaxed);
    if e.contains("1013") {
        shared.slow_consumer.fetch_add(1, Ordering::Relaxed);
    }
    *shared.last_error.lock() = Some(e.chars().take(200).collect());
}

async fn run(url: String, kind: FeedKind, emit: Emit, shared: Arc<Shared>) {
    let venue = kind.venue();
    let mut backoff = Duration::from_secs(1);
    loop {
        if matches!(kind, FeedKind::Poly { .. }) && shared.wanted.lock().is_empty() {
            tokio::select! {
                _ = shared.changed.notified() => {}
                _ = tokio::time::sleep(Duration::from_millis(500)) => {}
            }
            continue;
        }
        let connects = shared.connects.load(Ordering::Relaxed);
        if let Err(e) = session(&url, &kind, &emit, &shared).await {
            tracing::warn!(venue = %venue, error = %e, "feed_error");
            set_error(&shared, e);
        }
        shared.connected.store(false, Ordering::Relaxed);
        if shared.connects.load(Ordering::Relaxed) != connects {
            backoff = Duration::from_secs(1); // it connected: start the backoff over
        }
        tokio::time::sleep(backoff).await;
        backoff = (backoff * 2).min(Duration::from_secs(30));
    }
}

struct Session<'a> {
    kind: &'a FeedKind,
    emit: &'a Emit,
    shared: &'a Shared,
    // Kalshi
    sid: Option<i64>,
    subscribe_in_flight: bool,
    pending_add: BTreeSet<String>,
    seq: HashMap<i64, i64>,
    next_id: u64,
    // Polymarket
    initial_sent: bool,
    // PolyBolt
    authed: bool,
}

type Ws = tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

async fn session(url: &str, kind: &FeedKind, emit: &Emit, shared: &Shared) -> Result<(), String> {
    let venue = kind.venue();
    let mut request = url.into_client_request().map_err(|e| format!("request: {e}"))?;
    if let FeedKind::Kalshi { signer } = kind {
        let path =
            url::Url::parse(url).map(|u| u.path().to_owned()).unwrap_or_else(|_| "/trade-api/ws/v2".into());
        for (k, v) in signer.headers("GET", &path, None) {
            request.headers_mut().insert(k, v.parse().map_err(|e| format!("header: {e}"))?);
        }
    }
    let mut config = WebSocketConfig::default();
    config.max_message_size = Some(8 << 20);
    config.max_frame_size = Some(8 << 20);
    let (mut ws, _) = tokio_tungstenite::connect_async_with_config(request, Some(config), true)
        .await
        .map_err(|e| format!("connect: {e}"))?;
    let n = shared.connects.fetch_add(1, Ordering::Relaxed) + 1;
    shared.connected.store(true, Ordering::Relaxed);
    shared.subscribed.lock().clear();
    shared.removed_since_connect.store(0, Ordering::Relaxed);
    shared.authed.store(false, Ordering::Relaxed);
    *shared.sid.lock() = None;
    // The reconnect control line: the engine drops this venue's (or shard's) books.
    let mut ctl = json!({"type": "reconnect", "venue": venue.to_string(), "n": n});
    if let FeedKind::Poly { shard: Some(i), .. } = kind {
        ctl["shard"] = (*i).into();
        ctl["tokens"] = shared.wanted.lock().iter().cloned().collect::<Vec<_>>().into();
    }
    // Recorded-only venues: the engine must not read their reconnects.
    if kind.reference() || matches!(kind, FeedKind::PolyBolt { .. }) {
        ctl["type"] = "pk_reconnect".into();
        emit(venue, ctl.to_string());
    } else {
        emit('c', ctl.to_string());
    }
    tracing::info!(venue = %venue, n, "feed_connected");

    let mut s = Session {
        kind,
        emit,
        shared,
        sid: None,
        subscribe_in_flight: false,
        pending_add: BTreeSet::new(),
        seq: HashMap::new(),
        next_id: 0,
        initial_sent: false,
        authed: false,
    };
    if let FeedKind::PolyBolt { credentials: (key, secret, pass) } = kind {
        let auth = json!({"op": "auth", "rid": "auth", "auth": {"apiKey": key, "secret": secret, "passphrase": pass}});
        ws.send(Message::text(auth.to_string())).await.map_err(|e| format!("auth: {e}"))?;
    } else {
        s.sync(&mut ws).await?;
    }
    let ping_every = match kind {
        FeedKind::Poly { .. } => Duration::from_secs(10),
        _ => Duration::from_secs(20),
    };
    let mut ping = tokio::time::interval_at(tokio::time::Instant::now() + ping_every, ping_every);
    let mut watchdog = tokio::time::interval(Duration::from_secs(1));
    let mut last_data = tokio::time::Instant::now();
    loop {
        tokio::select! {
            biased;
            msg = ws.next() => {
                let Some(msg) = msg else { return Err("closed".into()) };
                let msg = msg.map_err(|e| format!("read: {e}"))?;
                let text = match msg {
                    Message::Text(t) => t.as_str().to_owned(),
                    Message::Binary(b) => String::from_utf8_lossy(&b).into_owned(),
                    Message::Close(frame) => {
                        let why = frame.map(|f| format!("{} {}", u16::from(f.code), f.reason)).unwrap_or_default();
                        return Err(format!("close: {why}"));
                    }
                    _ => continue,
                };
                shared.messages.fetch_add(1, Ordering::Relaxed);
                if text != "PONG" {
                    last_data = tokio::time::Instant::now();
                }
                if !s.on_message(&mut ws, text).await? {
                    return Ok(());
                }
            }
            _ = shared.changed.notified() => s.sync(&mut ws).await?,
            _ = shared.recycle.notified() => {
                let _ = ws.close(None).await;
                return Ok(());
            }
            _ = watchdog.tick() => {
                if let FeedKind::Poly { silence: Some(limit), .. } = kind
                    && last_data.elapsed() > *limit
                    && !shared.wanted.lock().is_empty()
                {
                    return Err(format!("silent {:.0}s", last_data.elapsed().as_secs_f64()));
                }
            }
            _ = ping.tick() => {
                let m = match kind {
                    FeedKind::Poly { .. } => Message::text("PING"),
                    FeedKind::Kraken => Message::text(r#"{"method":"ping"}"#),
                    FeedKind::Bitstamp => Message::text(r#"{"event":"bts:heartbeat"}"#),
                    _ => Message::Ping(Default::default()),
                };
                ws.send(m).await.map_err(|e| format!("ping: {e}"))?;
            }
        }
    }
}

impl Session<'_> {
    fn cmd_id(&mut self) -> u64 {
        self.next_id += 1;
        self.next_id
    }

    /// Bring the subscription to `wanted`.
    async fn sync(&mut self, ws: &mut Ws) -> Result<(), String> {
        let wanted = self.shared.wanted.lock().clone();
        let subscribed = self.shared.subscribed.lock().clone();
        let add: Vec<String> = wanted.difference(&subscribed).cloned().collect();
        let remove: Vec<String> = subscribed.difference(&wanted).cloned().collect();
        if !add.is_empty() {
            self.add(ws, add).await?;
        }
        if !remove.is_empty() {
            self.remove(ws, remove).await?;
        }
        Ok(())
    }

    async fn send(ws: &mut Ws, body: Value) -> Result<(), String> {
        ws.send(Message::text(body.to_string())).await.map_err(|e| format!("send: {e}"))
    }

    async fn add(&mut self, ws: &mut Ws, ids: Vec<String>) -> Result<(), String> {
        match self.kind {
            FeedKind::Kalshi { .. } => {
                if self.sid.is_none() && self.subscribe_in_flight {
                    // One sid only: add once the first subscribe is acknowledged.
                    self.pending_add.extend(ids);
                    return Ok(());
                }
                let body = match self.sid {
                    None => {
                        self.subscribe_in_flight = true;
                        json!({"id": self.cmd_id(), "cmd": "subscribe",
                               "params": {"channels": ["orderbook_delta"], "market_tickers": ids}})
                    }
                    Some(sid) => json!({"id": self.cmd_id(), "cmd": "update_subscription",
                               "params": {"sids": [sid], "market_tickers": ids, "action": "add_markets"}}),
                };
                Self::send(ws, body).await?;
            }
            FeedKind::Poly { .. } => {
                let mut body = if self.initial_sent {
                    json!({"assets_ids": ids, "operation": "subscribe"})
                } else {
                    self.initial_sent = true;
                    json!({"assets_ids": ids, "type": "market"})
                };
                body["custom_feature_enabled"] = true.into();
                Self::send(ws, body).await?;
            }
            FeedKind::PolyBolt { .. } => {
                if !self.authed {
                    return Ok(()); // subscribed once the auth is acknowledged
                }
                let subs: Vec<Value> = ids
                    .iter()
                    .flat_map(|s| {
                        [
                            json!({"channel": "price.crypto", "filter": {"symbol": s}}),
                            json!({"channel": "price.crypto.twap", "filter": {"symbol": s, "window_seconds": 60}}),
                        ]
                    })
                    .collect();
                Self::send(ws, json!({"op": "subscribe", "rid": "sub", "subscriptions": subs})).await?;
            }
            FeedKind::Binance { .. } => {
                let body = json!({"method": "SUBSCRIBE", "params": ids, "id": self.cmd_id()});
                Self::send(ws, body).await?;
            }
            FeedKind::Kraken => {
                let body = json!({"method": "subscribe", "req_id": self.cmd_id(),
                                  "params": {"channel": "ticker", "symbol": ids, "event_trigger": "bbo"}});
                Self::send(ws, body).await?;
            }
            FeedKind::Bitstamp => {
                for channel in &ids {
                    Self::send(ws, json!({"event": "bts:subscribe", "data": {"channel": channel}})).await?;
                }
            }
            FeedKind::Coinbase => {
                let body = json!({"type": "subscribe", "product_ids": ids, "channels": ["ticker"]});
                Self::send(ws, body).await?;
            }
        }
        self.shared.subscribed.lock().extend(ids);
        Ok(())
    }

    async fn remove(&mut self, ws: &mut Ws, ids: Vec<String>) -> Result<(), String> {
        match self.kind {
            FeedKind::Kalshi { .. } => {
                let Some(sid) = self.sid else { return Ok(()) };
                let body = json!({"id": self.cmd_id(), "cmd": "update_subscription",
                                  "params": {"sids": [sid], "market_tickers": ids, "action": "delete_markets"}});
                Self::send(ws, body).await?;
            }
            FeedKind::Poly { .. } => {
                Self::send(ws, json!({"assets_ids": ids, "operation": "unsubscribe"})).await?;
                self.shared.removed_since_connect.fetch_add(ids.len() as u64, Ordering::Relaxed);
            }
            FeedKind::PolyBolt { .. } => return Ok(()), // the symbol set is fixed
            FeedKind::Binance { .. } => {
                let body = json!({"method": "UNSUBSCRIBE", "params": ids, "id": self.cmd_id()});
                Self::send(ws, body).await?;
            }
            FeedKind::Kraken => {
                let body = json!({"method": "unsubscribe", "req_id": self.cmd_id(),
                                  "params": {"channel": "ticker", "symbol": ids, "event_trigger": "bbo"}});
                Self::send(ws, body).await?;
            }
            FeedKind::Bitstamp => {
                for channel in &ids {
                    Self::send(ws, json!({"event": "bts:unsubscribe", "data": {"channel": channel}})).await?;
                }
            }
            FeedKind::Coinbase => {
                let body = json!({"type": "unsubscribe", "product_ids": ids, "channels": ["ticker"]});
                Self::send(ws, body).await?;
            }
        }
        let mut sub = self.shared.subscribed.lock();
        for id in &ids {
            sub.remove(id);
        }
        Ok(())
    }

    /// Handle one frame; `Ok(false)` closes the socket (and reconnects).
    async fn on_message(&mut self, ws: &mut Ws, text: String) -> Result<bool, String> {
        match self.kind {
            FeedKind::Poly { .. } => {
                if text != "PONG" {
                    (self.emit)('p', text);
                }
                Ok(true)
            }
            FeedKind::Kalshi { .. } => self.kalshi(ws, text).await,
            FeedKind::PolyBolt { .. } => {
                if !self.authed {
                    let body: Value = serde_json::from_str(&text).unwrap_or(Value::Null);
                    let failed = body.get("ok") == Some(&Value::Bool(false))
                        || body.get("error").is_some_and(|e| !e.is_null())
                        || body.get("status").and_then(Value::as_str) == Some("error");
                    if failed {
                        self.shared.auth_failures.fetch_add(1, Ordering::Relaxed);
                        set_error(
                            self.shared,
                            format!("polybolt auth: {}", text.chars().take(150).collect::<String>()),
                        );
                        (self.emit)('b', text);
                        return Ok(false);
                    }
                    // The first frame that is not an error acknowledges the auth.
                    self.authed = true;
                    self.shared.authed.store(true, Ordering::Relaxed);
                    self.sync(ws).await?;
                }
                (self.emit)('b', text);
                Ok(true)
            }
            FeedKind::Binance { .. } => {
                // `{"result":null,"id":n}` acknowledges a (un)subscribe; an error has `code`.
                if text.starts_with("{\"result\"") {
                    return Ok(true);
                }
                if text.contains("\"code\"") && !text.contains("\"stream\"") {
                    let head: String = text.chars().take(150).collect();
                    set_error(self.shared, format!("binance: {head}"));
                }
                (self.emit)(self.kind.venue(), text);
                Ok(true)
            }
            FeedKind::Kraken => {
                // Every second a heartbeat; pongs and acks carry `method`.
                if text == r#"{"channel":"heartbeat"}"# || text.starts_with(r#"{"method":"pong""#) {
                    return Ok(true);
                }
                if text.starts_with(r#"{"method""#) {
                    if text.contains(r#""success":false"#) {
                        let head: String = text.chars().take(150).collect();
                        set_error(self.shared, format!("kraken: {head}"));
                    } else {
                        return Ok(true);
                    }
                }
                (self.emit)('r', text);
                Ok(true)
            }
            FeedKind::Bitstamp => {
                if text.starts_with(r#"{"data""#) {
                    (self.emit)('t', text);
                    return Ok(true);
                }
                let event =
                    serde_json::from_str::<Value>(&text).ok().and_then(|v| v["event"].as_str().map(str::to_owned));
                match event.as_deref() {
                    Some("bts:heartbeat" | "bts:subscription_succeeded" | "bts:unsubscription_succeeded") => {
                        Ok(true)
                    }
                    Some("bts:request_reconnect") => Ok(false),
                    _ => {
                        let head: String = text.chars().take(150).collect();
                        set_error(self.shared, format!("bitstamp: {head}"));
                        (self.emit)('t', text);
                        Ok(true)
                    }
                }
            }
            FeedKind::Coinbase => {
                if text.starts_with(r#"{"type":"subscriptions""#) {
                    return Ok(true);
                }
                if text.starts_with(r#"{"type":"error""#) {
                    let head: String = text.chars().take(150).collect();
                    set_error(self.shared, format!("coinbase: {head}"));
                }
                (self.emit)('o', text);
                Ok(true)
            }
        }
    }

    async fn kalshi(&mut self, ws: &mut Ws, text: String) -> Result<bool, String> {
        let Ok(body) = serde_json::from_str::<Value>(&text) else {
            (self.emit)('k', text);
            return Ok(true);
        };
        let kind = body.get("type").and_then(Value::as_str).unwrap_or_default();
        match kind {
            "subscribed" => {
                if let Some(sid) = body.get("msg").and_then(|m| m.get("sid")).and_then(Value::as_i64) {
                    self.sid = Some(sid);
                    *self.shared.sid.lock() = Some(sid);
                }
                self.subscribe_in_flight = false;
                if !self.pending_add.is_empty() {
                    let ids: Vec<String> = std::mem::take(&mut self.pending_add).into_iter().collect();
                    self.add(ws, ids).await?;
                }
            }
            "error" => {
                set_error(
                    self.shared,
                    format!("kalshi error: {}", body.get("msg").cloned().unwrap_or_default()),
                );
                (self.emit)('k', text);
                // A refused update leaves the subscription unknown: start over.
                return Ok(false);
            }
            _ => {}
        }
        // Every frame with a seq advances it, acknowledgements included.
        if let (Some(seq), Some(sid)) =
            (body.get("seq").and_then(Value::as_i64), body.get("sid").and_then(Value::as_i64))
        {
            if let Some(&last) = self.seq.get(&sid)
                && seq != last + 1
            {
                self.shared.seq_gaps.fetch_add(1, Ordering::Relaxed);
                let ctl = json!({"type": "resync", "venue": "k", "sid": sid, "expected": last + 1, "got": seq, "frame": kind});
                (self.emit)('c', ctl.to_string());
                tracing::warn!(expected = last + 1, got = seq, frame = kind, "kalshi_seq_gap");
                return Ok(false);
            }
            self.seq.insert(sid, seq);
        }
        (self.emit)('k', text);
        Ok(true)
    }
}

/// `n` Polymarket sockets with assets spread round-robin across them, so each
/// asset's tokens stay on one shard and a reconnect drops only its books.
pub struct PolyShards {
    pub shards: Vec<FeedHandle>,
    shard_of: HashMap<String, usize>,
}

impl PolyShards {
    pub fn spawn(url: &str, emit: Emit, assets: &[String], n: usize, silence: Option<Duration>) -> Self {
        let n = if n == 0 { assets.len() } else { n }.clamp(1, assets.len().max(1));
        let shards = (0..n)
            .map(|i| spawn_feed(url.to_owned(), FeedKind::Poly { shard: Some(i), silence }, emit.clone()))
            .collect();
        let shard_of = assets.iter().enumerate().map(|(i, a)| (a.clone(), i % n)).collect();
        Self { shards, shard_of }
    }

    pub fn set_wanted_groups(&self, groups: &HashMap<String, BTreeSet<String>>) {
        let mut wanted = vec![BTreeSet::new(); self.shards.len()];
        for (asset, ids) in groups {
            let i = self.shard_of.get(asset).copied().unwrap_or(0);
            wanted[i].extend(ids.iter().cloned());
        }
        for (s, w) in self.shards.iter().zip(wanted) {
            s.set_wanted(w);
        }
    }

    pub fn removed_since_connect(&self) -> u64 {
        self.shards.iter().map(FeedHandle::removed_since_connect).max().unwrap_or(0)
    }

    pub fn recycle(&self, min_removed: u64) {
        for s in &self.shards {
            if s.removed_since_connect() >= min_removed {
                s.recycle();
            }
        }
    }

    pub fn connected(&self) -> bool {
        self.shards.iter().all(FeedHandle::connected)
    }

    pub fn last_error(&self) -> Option<String> {
        self.shards.iter().find_map(FeedHandle::last_error)
    }

    pub fn stats(&self) -> Value {
        let per: Vec<Value> = self.shards.iter().map(FeedHandle::stats).collect();
        let sum = |k: &str| per.iter().filter_map(|p| p.get(k).and_then(Value::as_u64)).sum::<u64>();
        json!({
            "connected": self.connected(),
            "connects": sum("connects"),
            "messages": sum("messages"),
            "errors": sum("errors"),
            "subscribed": sum("subscribed"),
            "wanted": sum("wanted"),
            "slow_consumer": sum("slow_consumer"),
            "last_error": self.last_error(),
            "removed_since_connect": self.removed_since_connect(),
            "sockets": self.shards.len(),
            "shards": per,
        })
    }
}

/// The Binance `bookTicker` stream for an asset; HYPE has no Binance spot market.
pub fn binance_stream(asset: &str, futures: bool) -> Option<String> {
    (futures || asset != "HYPE").then(|| format!("{}usdt@bookTicker", asset.to_lowercase()))
}

/// Kraken v2 symbols (v2 names DOGE as DOGE; REST calls it XDG).
pub const KRAKEN_SYMBOLS: [(&str, &str); 8] = [
    ("BTC", "BTC/USD"),
    ("ETH", "ETH/USD"),
    ("SOL", "SOL/USD"),
    ("XRP", "XRP/USD"),
    ("BNB", "BNB/USD"),
    ("DOGE", "DOGE/USD"),
    ("HYPE", "HYPE/USD"),
    ("ZEC", "ZEC/USD"),
];

pub fn kraken_symbol(asset: &str) -> Option<&'static str> {
    KRAKEN_SYMBOLS.iter().find(|(a, _)| *a == asset).map(|(_, s)| *s)
}

/// The Bitstamp order-book channel for an asset (USD pairs).
pub fn bitstamp_channel(asset: &str) -> Option<String> {
    const LISTED: [&str; 8] = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "HYPE", "ZEC"];
    LISTED.contains(&asset).then(|| format!("order_book_{}usd", asset.to_lowercase()))
}

/// The Coinbase Exchange product for an asset (USD pairs).
pub fn coinbase_product(asset: &str) -> Option<String> {
    const LISTED: [&str; 8] = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "HYPE", "ZEC"];
    LISTED.contains(&asset).then(|| format!("{asset}-USD"))
}

pub const POLYBOLT_SYMBOLS: [(&str, &str); 8] = [
    ("BTC", "btcusd"),
    ("ETH", "ethusd"),
    ("SOL", "solusd"),
    ("XRP", "xrpusd"),
    ("BNB", "bnbusd"),
    ("DOGE", "dogeusd"),
    ("HYPE", "hypeusd"),
    ("ZEC", "zecusd"),
];
