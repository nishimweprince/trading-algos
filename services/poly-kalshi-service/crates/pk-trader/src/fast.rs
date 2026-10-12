//! The armed trigger: a Kalshi frame to a Polymarket order on one thread.
//!
//! The normal path takes a frame through the ingest queue, the engine thread,
//! a channel to the order path, a task and the HTTP client before the order's
//! bytes leave (~280 µs in epoch 11). Here, on the Kalshi feed's own thread
//! ([`crate::hot`]):
//!
//! 1. the frame updates this trigger's copy of the Kalshi book (the engine's
//!    `Book` and frame parser, so the same book);
//! 2. if the asset is **armed** ([`pk_engine::fast`]: the engine's Polymarket
//!    quotes and the rule's parameters, sent when its gap nears the threshold),
//!    [`pk_core::decide`] runs on the live Kalshi quote and the armed
//!    Polymarket quotes — the same rule, the same inputs the engine would see;
//! 3. on an entry whose order is ready (signed, with fresh L2 headers, as
//!    HTTP/1.1 bytes), the window is claimed and the bytes are written on a
//!    warm connection ([`pk_exec::fast::FastSender`]).
//!
//! The per-frame check allocates nothing; the fire path allocates only the
//! claim's key. The engine records the entry when it reaches the frame
//! ([`FastGate::fired`]); the reply goes through the order path's usual result
//! handling (`ExecMsg::FastResult`), so journal, ledger and wallet agree.
//!
//! Orders are prepared off the hot path when an arm arrives (sign once per
//! spec), and their L2 headers are re-signed every [`REFRESH`].

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::{Duration, Instant};

use parking_lot::{Mutex, RwLock};
use pk_core::decision::{Action, decide};
use pk_core::num::px_key;
use pk_core::{Book, BookSide, NS};
use pk_engine::parse::KalshiFrame;
use pk_engine::{Arm, ArmSide, ArmUpdate, Claim, FastFire, FastGate, OrderIntent};
use pk_exec::auth::unix_now_s;
use pk_exec::fast::post_order_request;
use pk_exec::live::entry_spec;
use pk_exec::{FastReply, FastSender, OrderSigner, OrderSpec};
use pk_stats::Hist;
use rust_decimal::Decimal;
use rust_decimal::prelude::FromPrimitive;
use serde_json::{Value, json};
use tokio::sync::mpsc;

use crate::exec_bridge::{ExecBridge, ExecMsg, FastResult};
use crate::now_ns;

/// Re-sign the prepared orders' L2 headers this often.
pub const REFRESH: Duration = Duration::from_secs(2);
/// Kalshi books kept (current and next windows of every asset, with room).
const MAX_BOOKS: usize = 64;
/// The startup check's tag: an L2-signed `POST /order` with an empty body,
/// which no venue can turn into an order. 400 means the headers were accepted;
/// 401/403 means the fast path's requests would be refused.
const SELF_CHECK_TAG: u64 = u64::MAX;

#[derive(Clone)]
struct Prepared {
    side: ArmSide,
    spec: OrderSpec,
    body: Arc<Vec<u8>>,
    request: Arc<Vec<u8>>,
    signed_at: u64,
}

struct Armed {
    arm: Arm,
    yes: Option<Prepared>,
    no: Option<Prepared>,
}

enum ClaimState {
    Engine,
    Fast { fire: FastFire, taken: bool },
}

struct InFlight {
    intent: OrderIntent,
    spec: OrderSpec,
    t_frame: Instant,
    frame_ns: i64,
}

pub struct FastPath {
    arms: RwLock<HashMap<String, Armed>>,
    books: Mutex<HashMap<String, (Book, i64)>>,
    claims: Mutex<HashMap<(String, i64), ClaimState>>,
    in_flight: Mutex<HashMap<u64, InFlight>>,
    building: Mutex<HashSet<(String, bool)>>,
    next_tag: AtomicU64,
    sender: Arc<FastSender>,
    bridge: Arc<ExecBridge>,
    exec_tx: mpsc::UnboundedSender<ExecMsg>,
    rt: tokio::runtime::Handle,
    halted: AtomicBool,
    frames: AtomicU64,
    fires: AtomicU64,
    fire_failed: AtomicU64,
    not_ready: AtomicU64,
    frame_to_wire_ns: Mutex<Hist>,
    check_ns: Mutex<Hist>,
    last_error: Mutex<Option<String>>,
    self_check: Mutex<Option<Value>>,
}

impl FastPath {
    pub fn new(
        bridge: Arc<ExecBridge>,
        sender_conns: usize,
        exec_tx: mpsc::UnboundedSender<ExecMsg>,
        rt: tokio::runtime::Handle,
    ) -> anyhow::Result<Arc<Self>> {
        let clob_url = bridge.clob().http.base_url();
        let slot: Arc<std::sync::OnceLock<std::sync::Weak<FastPath>>> = Arc::default();
        let on_reply = {
            let slot = slot.clone();
            Arc::new(move |r: FastReply| {
                if let Some(fp) = slot.get().and_then(std::sync::Weak::upgrade) {
                    fp.on_reply(r);
                }
            })
        };
        let sender = FastSender::start(&clob_url, sender_conns, on_reply)?;
        let fp = Arc::new(Self {
            arms: RwLock::new(HashMap::new()),
            books: Mutex::new(HashMap::with_capacity(MAX_BOOKS)),
            claims: Mutex::new(HashMap::new()),
            in_flight: Mutex::new(HashMap::new()),
            building: Mutex::new(HashSet::new()),
            next_tag: AtomicU64::new(1),
            sender,
            bridge,
            exec_tx,
            rt,
            halted: AtomicBool::new(false),
            frames: AtomicU64::new(0),
            fires: AtomicU64::new(0),
            fire_failed: AtomicU64::new(0),
            not_ready: AtomicU64::new(0),
            frame_to_wire_ns: Mutex::new(Hist::new()),
            check_ns: Mutex::new(Hist::new()),
            last_error: Mutex::new(None),
            self_check: Mutex::new(None),
        });
        let _ = slot.set(Arc::downgrade(&fp));
        let check = fp.clone();
        std::thread::Builder::new().name("pk-fast-check".into()).spawn(move || check.run_self_check())?;
        Ok(fp)
    }

    /// Once a connection is up: is a signed request on it accepted?
    fn run_self_check(&self) {
        let start = Instant::now();
        while self.sender.stats().up == 0 {
            if start.elapsed() > Duration::from_secs(30) {
                *self.self_check.lock() = Some(json!({"ok": false, "error": "no connection in 30 s"}));
                return;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        let creds = self.bridge.clob().signer.creds();
        let request = post_order_request(self.sender.host(), creds, b"{}", unix_now_s());
        if let Err(e) = self.sender.send(SELF_CHECK_TAG, &request) {
            *self.self_check.lock() = Some(json!({"ok": false, "error": e.to_string()}));
        }
    }

    // --- the hot path (the Kalshi feed's thread) -------------------------------------

    /// One Kalshi frame, before it is queued for the engine.
    pub fn on_kalshi(&self, text: &str) {
        let t_frame = Instant::now();
        let frame_ns = now_ns();
        self.frames.fetch_add(1, Ordering::Relaxed);
        let Ok(frame) = serde_json::from_str::<KalshiFrame>(text) else { return };
        let snapshot = match frame.kind.as_deref() {
            Some("orderbook_snapshot") => true,
            Some("orderbook_delta") => false,
            _ => return,
        };
        let Some(msg) = frame.msg else { return };
        let Some(ticker) = msg.market_ticker.as_ref().map(|t| t.as_str()) else { return };
        let quote = {
            let mut books = self.books.lock();
            if !books.contains_key(ticker) {
                if !snapshot {
                    return;
                }
                if books.len() >= MAX_BOOKS {
                    let cutoff = frame_ns - 3600 * NS;
                    books.retain(|_, (_, seen)| *seen >= cutoff);
                }
                books.insert(ticker.to_owned(), (Book::default(), frame_ns));
            }
            let Some((book, seen)) = books.get_mut(ticker) else { return };
            *seen = frame_ns;
            if snapshot {
                let yes = msg.levels(true).into_iter().map(|(p, s)| (px_key(p), s));
                let no = msg.levels(false).into_iter().map(|(p, s)| (px_key(1.0 - p), s));
                book.snapshot_keys(yes, no, frame_ns);
            } else {
                if book.snapshot_ns.is_none() {
                    return;
                }
                let Some((price, delta)) = msg.delta() else { return };
                if msg.side.as_deref() == Some("yes") {
                    book.add_level(BookSide::Bid, px_key(price), delta, frame_ns);
                } else {
                    book.add_level(BookSide::Ask, px_key(1.0 - price), delta, frame_ns);
                }
            }
            let arms = self.arms.read();
            let Some(a) = arms.get(ticker) else { return };
            book.quote(Some(a.arm.open_unix * NS))
        };
        if self.halted.load(Ordering::Acquire) {
            return;
        }
        let fire = {
            let arms = self.arms.read();
            let Some(a) = arms.get(ticker) else { return };
            let sec_into = frame_ns as f64 / 1e9 - a.arm.open_unix as f64;
            let arm = &a.arm;
            let d = decide(&quote, &arm.poly_yes, &arm.poly_no, &arm.params, sec_into, false, None, None);
            self.check_ns.lock().record(t_frame.elapsed().as_nanos() as u64);
            if d.action != Action::Enter {
                return;
            }
            let yes = d.buy_yes == Some(true);
            let Some(p) = (if yes { a.yes.as_ref() } else { a.no.as_ref() }) else {
                self.not_ready.fetch_add(1, Ordering::Relaxed);
                return;
            };
            if Some(p.side.entry_px) != d.entry_px {
                self.not_ready.fetch_add(1, Ordering::Relaxed);
                return;
            }
            let fire = FastFire {
                asset: a.arm.asset.clone(),
                open_unix: a.arm.open_unix,
                buy_yes: yes,
                entry_px: p.side.entry_px,
                limit_px: p.side.limit_px,
                contracts: p.side.contracts,
                gap: d.gap.unwrap_or(0.0),
                t_ns: frame_ns,
            };
            (fire, p.clone(), a.arm.clone())
        };
        let (fire, prepared, arm) = fire;
        let key = (fire.asset.clone(), fire.open_unix);
        {
            let mut claims = self.claims.lock();
            if claims.contains_key(&key) {
                return;
            }
            claims.insert(key.clone(), ClaimState::Fast { fire: fire.clone(), taken: false });
        }
        let tag = self.next_tag.fetch_add(1, Ordering::Relaxed);
        match self.sender.send(tag, &prepared.request) {
            Ok(t_written) => {
                self.frame_to_wire_ns.lock().record(t_written.duration_since(t_frame).as_nanos() as u64);
                self.fires.fetch_add(1, Ordering::Relaxed);
                self.arms.write().remove(ticker);
                let intent = intent_of(&arm, &fire, &prepared);
                let flight = InFlight { intent, spec: prepared.spec, t_frame, frame_ns };
                self.in_flight.lock().insert(tag, flight);
            }
            Err(e) => {
                // Not sent: release the window to the engine's own path.
                self.claims.lock().remove(&key);
                self.fire_failed.fetch_add(1, Ordering::Relaxed);
                *self.last_error.lock() = Some(format!("send: {e}"));
            }
        }
    }

    /// A Kalshi reconnect or resync: the books are gone until the next snapshot.
    pub fn on_control(&self, text: &str) {
        let kalshi = text.contains(r#""venue":"k""#);
        if kalshi && (text.contains(r#""reconnect""#) || text.contains(r#""resync""#)) {
            self.books.lock().clear();
        }
    }

    fn on_reply(&self, reply: FastReply) {
        if reply.tag == SELF_CHECK_TAG {
            let body = String::from_utf8_lossy(&reply.body[..reply.body.len().min(200)]).into_owned();
            let ok = reply.error.is_none() && !matches!(reply.status, 0 | 401 | 403);
            let rtt_us = reply.t_reply.saturating_duration_since(reply.t_written).as_micros() as u64;
            tracing::info!(status = reply.status, ok, rtt_us, %body, "fast_path_self_check");
            *self.self_check.lock() = Some(json!({
                "ok": ok, "status": reply.status, "body": body, "rtt_us": rtt_us, "error": reply.error,
            }));
            return;
        }
        let Some(f) = self.in_flight.lock().remove(&reply.tag) else { return };
        let r =
            FastResult { intent: f.intent, spec: f.spec, reply, t_frame: f.t_frame, frame_ns: f.frame_ns };
        let _ = self.exec_tx.send(ExecMsg::FastResult(Box::new(r)));
    }

    // --- arming (the engine thread) and preparation (the runtime) ------------------

    /// The engine's arm and disarm updates.
    pub fn update(self: &Arc<Self>, updates: Vec<ArmUpdate>) {
        for u in updates {
            match u {
                ArmUpdate::Disarm { kalshi_ticker } => {
                    self.arms.write().remove(&kalshi_ticker);
                }
                ArmUpdate::Arm(arm) => {
                    let ticker = arm.kalshi_ticker.clone();
                    let mut todo = Vec::new();
                    {
                        let mut arms = self.arms.write();
                        let fresh = || Armed { arm: arm.clone(), yes: None, no: None };
                        let entry = arms.entry(ticker.clone()).or_insert_with(fresh);
                        for (yes, want) in [(true, &arm.yes), (false, &arm.no)] {
                            let slot = if yes { &mut entry.yes } else { &mut entry.no };
                            if slot.as_ref().map(|p| &p.side) != want.as_ref() {
                                *slot = None;
                                if let Some(side) = want {
                                    todo.push((yes, side.clone()));
                                }
                            }
                        }
                        entry.arm = arm.clone();
                    }
                    for (yes, side) in todo {
                        let q = if yes { arm.poly_yes } else { arm.poly_no };
                        if !self.building.lock().insert((ticker.clone(), yes)) {
                            continue;
                        }
                        let this = self.clone();
                        let ticker = ticker.clone();
                        self.rt.spawn(async move {
                            if let Err(e) = this.prepare(&ticker, yes, &side, q.bid, q.ask).await {
                                *this.last_error.lock() = Some(format!("prepare: {e:#}"));
                            }
                            this.building.lock().remove(&(ticker, yes));
                        });
                    }
                }
            }
        }
    }

    /// Sign the side's order and build its request; install it if still wanted.
    async fn prepare(
        &self,
        ticker: &str,
        yes: bool,
        side: &ArmSide,
        bid: Option<f64>,
        ask: Option<f64>,
    ) -> anyhow::Result<()> {
        let limits = self.bridge.live_limits().ok_or_else(|| anyhow::anyhow!("not live"))?;
        let top = self.bridge.top_for(&side.token, bid, ask).await;
        let top = top.ok_or_else(|| anyhow::anyhow!("no book meta"))?;
        let limit = Decimal::from_f64(side.limit_px).unwrap_or_default().round_dp(4);
        let spec = entry_spec(&side.token, limit, Decimal::from(side.contracts), &top, &limits)?;
        let signed = self.bridge.clob().signer.sign(&spec).await?;
        let ts = unix_now_s();
        let creds = self.bridge.clob().signer.creds();
        let request = post_order_request(self.sender.host(), creds, &signed.body, ts);
        let prepared = Prepared {
            side: side.clone(),
            spec,
            body: Arc::new(signed.body),
            request: Arc::new(request),
            signed_at: ts,
        };
        let mut arms = self.arms.write();
        if let Some(entry) = arms.get_mut(ticker) {
            let want = if yes { &entry.arm.yes } else { &entry.arm.no };
            if want.as_ref() == Some(side) {
                *(if yes { &mut entry.yes } else { &mut entry.no }) = Some(prepared);
            }
        }
        Ok(())
    }

    /// Re-sign the prepared orders' L2 headers (the venue checks their age).
    pub async fn refresh_loop(self: Arc<Self>) {
        let mut every = tokio::time::interval(REFRESH);
        every.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            every.tick().await;
            let ts = unix_now_s();
            let stale: Vec<(String, bool, Arc<Vec<u8>>)> = {
                let arms = self.arms.read();
                arms.iter()
                    .flat_map(|(t, a)| {
                        [(true, &a.yes), (false, &a.no)].into_iter().filter_map(move |(yes, p)| {
                            let p = p.as_ref().filter(|p| p.signed_at + 1 < ts)?;
                            Some((t.clone(), yes, p.body.clone()))
                        })
                    })
                    .collect()
            };
            if stale.is_empty() {
                continue;
            }
            let creds = self.bridge.clob().signer.creds();
            let fresh: Vec<(String, bool, Arc<Vec<u8>>, Arc<Vec<u8>>)> = stale
                .into_iter()
                .map(|(t, yes, body)| {
                    let req = Arc::new(post_order_request(self.sender.host(), creds, &body, ts));
                    (t, yes, body, req)
                })
                .collect();
            let mut arms = self.arms.write();
            for (t, yes, body, req) in fresh {
                let Some(entry) = arms.get_mut(&t) else { continue };
                let slot = if yes { &mut entry.yes } else { &mut entry.no };
                if let Some(p) = slot.as_mut().filter(|p| Arc::ptr_eq(&p.body, &body)) {
                    p.request = req;
                    p.signed_at = ts;
                }
            }
        }
    }

    /// Mirror the engine's halt: halted, nothing fires and every arm is dropped
    /// (the engine re-arms once it is resumed).
    pub fn set_halted(&self, halted: bool) {
        let was = self.halted.swap(halted, Ordering::AcqRel);
        if halted && !was {
            self.arms.write().clear();
        }
    }

    pub fn stats(&self) -> Value {
        let ms = |h: &Hist| {
            json!({
                "n": h.len(),
                "p50_us": (h.len() > 0).then(|| h.quantile(0.5) as f64 / 1e3),
                "p99_us": (h.len() > 0).then(|| h.quantile(0.99) as f64 / 1e3),
                "max_us": (h.len() > 0).then(|| h.max() as f64 / 1e3),
            })
        };
        let arms = self.arms.read();
        json!({
            "enabled": true,
            "halted": self.halted.load(Ordering::Relaxed),
            "armed": arms.len(),
            "ready_orders": arms.values().map(|a| a.yes.iter().count() + a.no.iter().count()).sum::<usize>(),
            "frames": self.frames.load(Ordering::Relaxed),
            "fires": self.fires.load(Ordering::Relaxed),
            "fire_failed": self.fire_failed.load(Ordering::Relaxed),
            "not_ready": self.not_ready.load(Ordering::Relaxed),
            "frame_to_wire": ms(&self.frame_to_wire_ns.lock()),
            "check": ms(&self.check_ns.lock()),
            "sender": serde_json::to_value(self.sender.stats()).unwrap_or(Value::Null),
            "self_check": self.self_check.lock().clone(),
            "last_error": self.last_error.lock().clone(),
        })
    }
}

fn intent_of(arm: &Arm, fire: &FastFire, p: &Prepared) -> OrderIntent {
    let q = if fire.buy_yes { arm.poly_yes } else { arm.poly_no };
    OrderIntent {
        asset: arm.asset.clone(),
        open_unix: arm.open_unix,
        token: p.side.token.clone(),
        side: if fire.buy_yes { "yes" } else { "no" },
        entry_px: p.side.entry_px,
        limit_px: p.side.limit_px,
        contracts: p.side.contracts,
        decision_ns: fire.t_ns,
        best_bid: q.bid,
        best_ask: q.ask,
        condition_id: arm.condition_id.clone(),
        fee_rate: arm.fee_rate,
        fee_exponent: arm.fee_exponent,
        reserved: p.side.reserved,
        live: true,
    }
}

impl FastGate for FastPath {
    fn fired(&self, asset: &str, open_unix: i64) -> Option<FastFire> {
        let mut claims = self.claims.lock();
        match claims.get_mut(&(asset.to_owned(), open_unix)) {
            Some(ClaimState::Fast { fire, taken }) if !*taken => {
                *taken = true;
                Some(fire.clone())
            }
            _ => None,
        }
    }

    fn claim(&self, asset: &str, open_unix: i64) -> Claim {
        let mut claims = self.claims.lock();
        let key = (asset.to_owned(), open_unix);
        if let Some(ClaimState::Fast { fire, taken }) = claims.get_mut(&key) {
            *taken = true;
            return Claim::Fast(fire.clone());
        }
        if claims.len() > 512 {
            let cutoff = now_ns() / NS - 6 * 3600;
            claims.retain(|(_, open), _| *open >= cutoff);
        }
        claims.insert(key, ClaimState::Engine);
        Claim::Engine
    }
}
