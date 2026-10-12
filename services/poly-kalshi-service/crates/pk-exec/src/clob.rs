//! The timed order path to the Polymarket CLOB.
//!
//! Every POST and DELETE is stamped on the monotonic clock:
//!
//! ```text
//! t_decision ─ sign (0 if pre-signed) ─ t_write ─ network + match ─ t_headers ─ body ─ t_ack
//! ```
//!
//! and written as an [`OrderTiming`] record. [`OrderStats`] keeps the histograms
//! the status page shows: decision→ack, write→ack (the round trip the venue
//! controls), sign time, cancel round trip.

use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use parking_lot::Mutex;
use pk_stats::{Hist, SummaryF};
use reqwest::{Method, StatusCode};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::auth::unix_now_s;
use crate::presign::{PresignCache, PresignStats};
use crate::signing::{OrderSigner, OrderSpec};
use crate::transport::{WarmHttp, WarmStats};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Trigger {
    /// An engine decision (the measurement that matters).
    Decision,
    /// The probe's background cadence.
    Cadence,
    /// Clean-up: cancel after a post, or the kill switch.
    Cleanup,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Kind {
    Post,
    Cancel,
}

/// One request on the order path: the journal's `order_timing` record.
#[derive(Clone, Debug, Serialize)]
pub struct OrderTiming {
    #[serde(rename = "type")]
    pub record_type: &'static str,
    pub kind: Kind,
    pub trigger: Trigger,
    pub t_ns: u64,
    pub token: Option<String>,
    pub price: Option<Decimal>,
    pub size: Option<Decimal>,
    pub presigned: bool,
    pub sign_us: u64,
    pub decision_to_write_us: u64,
    pub write_to_headers_us: u64,
    pub headers_to_ack_us: u64,
    pub decision_to_ack_us: u64,
    pub http: String,
    pub status: u16,
    pub ok: bool,
    pub order_id: Option<String>,
    pub venue_status: Option<String>,
    pub error: Option<String>,
    pub making: Option<String>,
    pub taking: Option<String>,
    /// The fast path: the triggering Kalshi frame's arrival → the order's bytes
    /// written to the socket, on this host's clock.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub frame_to_wire_us: Option<u64>,
    /// `"fast"` when the armed trigger sent it, else absent.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub path: Option<&'static str>,
}

/// What the fast path's own connection got back for a POST /order.
#[derive(Clone, Debug)]
pub struct RawReply {
    pub status: u16,
    pub body: Vec<u8>,
    pub error: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PostAck {
    #[serde(default)]
    pub success: bool,
    #[serde(default)]
    pub error_msg: Option<String>,
    #[serde(rename = "orderID", default)]
    pub order_id: String,
    #[serde(default)]
    pub status: Option<String>,
    #[serde(default)]
    pub making_amount: Option<String>,
    #[serde(default)]
    pub taking_amount: Option<String>,
}

#[derive(Debug, Default, Deserialize)]
pub struct CancelAck {
    #[serde(default)]
    pub canceled: Vec<String>,
    #[serde(default, alias = "notCanceled")]
    pub not_canceled: serde_json::Map<String, serde_json::Value>,
}

#[derive(Debug, Deserialize)]
struct Level {
    price: Decimal,
    size: Decimal,
}

#[derive(Debug, Deserialize)]
struct BookResp {
    #[serde(default)]
    bids: Vec<Level>,
    #[serde(default)]
    asks: Vec<Level>,
    tick_size: Decimal,
    min_order_size: Decimal,
}

/// The touch of one token's book, from the REST snapshot.
#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
pub struct Top {
    pub best_bid: Option<Decimal>,
    pub best_ask: Option<Decimal>,
    pub tick: Decimal,
    pub min_size: Decimal,
}

#[derive(Debug, Deserialize)]
pub struct OrderState {
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub size_matched: Decimal,
    #[serde(default)]
    pub price: Option<Decimal>,
}

/// Collateral and outcome tokens both carry 6 decimals on chain; the
/// balance endpoint answers in those base units.
pub const BASE_UNITS: Decimal = Decimal::from_parts(1_000_000, 0, 0, false, 0);

#[derive(Debug, Deserialize)]
struct BalanceResp {
    balance: Decimal,
}

#[derive(Default)]
pub struct OrderStats {
    pub decision_to_ack_us: Hist,
    pub write_to_ack_us: Hist,
    pub sign_us: Hist,
    pub cancel_rtt_us: Hist,
    pub posts: u64,
    pub post_errors: u64,
    pub cancels: u64,
    pub cancel_errors: u64,
}

#[derive(Debug, Serialize)]
pub struct ExecStats {
    pub decision_to_ack_ms: SummaryF,
    pub write_to_ack_ms: SummaryF,
    pub sign_ms: SummaryF,
    pub cancel_rtt_ms: SummaryF,
    pub posts: u64,
    pub post_errors: u64,
    pub cancels: u64,
    pub cancel_errors: u64,
    pub presign: PresignStats,
    pub connection: WarmStats,
}

pub struct ClobExec<S: OrderSigner> {
    pub http: Arc<WarmHttp>,
    pub signer: Arc<S>,
    pub presign: PresignCache,
    stats: Mutex<OrderStats>,
}

pub fn wall_ns() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos() as u64).unwrap_or_default()
}

fn us(d: Duration) -> u64 {
    d.as_micros() as u64
}

fn version(v: reqwest::Version) -> String {
    format!("{v:?}")
}

impl<S: OrderSigner> ClobExec<S> {
    pub fn new(http: Arc<WarmHttp>, signer: Arc<S>, presign: PresignCache) -> Self {
        Self { http, signer, presign, stats: Mutex::new(OrderStats::default()) }
    }

    async fn authed(
        &self,
        method: Method,
        path: &str,
        body: Option<Vec<u8>>,
    ) -> reqwest::Result<reqwest::Response> {
        let bytes = body.as_deref().unwrap_or_default();
        let headers = self.signer.creds().headers(unix_now_s(), method.as_str(), path, bytes);
        self.http.send(method, path, &headers, body).await
    }

    /// Post one order: pre-signed if the cache has this exact spec, else signed
    /// now (and counted as a miss). `t_decision` starts the clock.
    pub async fn post(
        &self,
        spec: &OrderSpec,
        t_decision: Instant,
        trigger: Trigger,
    ) -> (Option<PostAck>, OrderTiming) {
        let t_ns = wall_ns();
        let (body, presigned, sign_time) = match self.presign.take(spec) {
            Some(signed) => (signed.body, true, Duration::ZERO),
            None => match self.signer.sign(spec).await {
                Ok(signed) => (signed.body, false, signed.sign_time),
                Err(e) => {
                    let mut timing =
                        self.timing(Kind::Post, trigger, t_ns, Some(spec), false, sign_time_zero());
                    timing.error = Some(format!("sign: {e:#}"));
                    timing.decision_to_ack_us = us(t_decision.elapsed());
                    self.stats.lock().post_errors += 1;
                    return (None, timing);
                }
            },
        };
        let t_write = Instant::now();
        let result = self.authed(Method::POST, "/order", Some(body)).await;
        let mut timing = self.timing(Kind::Post, trigger, t_ns, Some(spec), presigned, sign_time);
        timing.decision_to_write_us = us(t_write - t_decision);
        let ack = self.finish(&mut timing, result, t_decision, t_write).await;
        let ack = apply_ack(&mut timing, ack.as_deref());
        self.count_post(&timing);
        (ack, timing)
    }

    /// A POST /order the fast path sent on its own connection: the same
    /// `order_timing` record, acknowledgement and statistics as [`Self::post`].
    /// Times are µs: frame → bytes written, frame → acknowledgement, written →
    /// acknowledgement.
    pub fn raw_post(
        &self,
        spec: &OrderSpec,
        t_ns: u64,
        reply: &RawReply,
        (frame_to_wire_us, frame_to_ack_us, write_to_ack_us): (u64, u64, u64),
    ) -> (Option<PostAck>, OrderTiming) {
        let mut timing = self.timing(Kind::Post, Trigger::Decision, t_ns, Some(spec), true, Duration::ZERO);
        timing.path = Some("fast");
        timing.http = "HTTP/1.1".into();
        timing.frame_to_wire_us = Some(frame_to_wire_us);
        timing.decision_to_write_us = frame_to_wire_us;
        timing.write_to_headers_us = write_to_ack_us;
        timing.decision_to_ack_us = frame_to_ack_us;
        timing.status = reply.status;
        let body = match &reply.error {
            Some(e) => {
                timing.error = Some(format!("send: {e}"));
                None
            }
            None => {
                timing.ok = reply.status == 200;
                if !timing.ok {
                    let n = reply.body.len().min(300);
                    timing.error = Some(String::from_utf8_lossy(&reply.body[..n]).into_owned());
                }
                Some(reply.body.as_slice())
            }
        };
        let ack = apply_ack(&mut timing, body);
        self.count_post(&timing);
        (ack, timing)
    }

    fn count_post(&self, timing: &OrderTiming) {
        let mut stats = self.stats.lock();
        stats.posts += 1;
        if timing.ok {
            stats.decision_to_ack_us.record(timing.decision_to_ack_us);
            stats.write_to_ack_us.record(timing.write_to_headers_us + timing.headers_to_ack_us);
            stats.sign_us.record(timing.sign_us);
        } else {
            stats.post_errors += 1;
        }
    }

    pub async fn cancel(&self, order_id: &str, trigger: Trigger) -> (Option<CancelAck>, OrderTiming) {
        let t_ns = wall_ns();
        let t0 = Instant::now();
        let body = serde_json::to_vec(&serde_json::json!({ "orderID": order_id })).expect("json");
        let result = self.authed(Method::DELETE, "/order", Some(body)).await;
        let mut timing = self.timing(Kind::Cancel, trigger, t_ns, None, false, Duration::ZERO);
        timing.order_id = Some(order_id.to_owned());
        let bytes = self.finish(&mut timing, result, t0, t0).await;
        let ack = bytes.and_then(|b| serde_json::from_slice::<CancelAck>(&b).ok());
        let cancelled = ack.as_ref().is_some_and(|a| a.canceled.iter().any(|c| c == order_id));
        timing.ok = timing.ok && cancelled;
        let mut stats = self.stats.lock();
        stats.cancels += 1;
        if timing.ok {
            stats.cancel_rtt_us.record(timing.decision_to_ack_us);
        } else {
            stats.cancel_errors += 1;
        }
        (ack, timing)
    }

    /// A GET whose query string is not signed (the venue signs the path only).
    async fn authed_get(&self, path: &str, query: &str) -> anyhow::Result<reqwest::Response> {
        let headers = self.signer.creds().headers(unix_now_s(), "GET", path, b"");
        let full = if query.is_empty() { path.to_owned() } else { format!("{path}?{query}") };
        Ok(self.http.send(Method::GET, &full, &headers, None).await?.error_for_status()?)
    }

    /// The wallet's pUSD (collateral) the CLOB will let us spend, in USD.
    pub async fn balance(&self, signature_type: u8) -> anyhow::Result<Decimal> {
        let q = format!("asset_type=COLLATERAL&signature_type={signature_type}");
        let b: BalanceResp = self.authed_get("/balance-allowance", &q).await?.json().await?;
        Ok(b.balance / BASE_UNITS)
    }

    /// Shares of one outcome token held.
    pub async fn token_balance(&self, token: &str, signature_type: u8) -> anyhow::Result<Decimal> {
        let q = format!("asset_type=CONDITIONAL&token_id={token}&signature_type={signature_type}");
        let b: BalanceResp = self.authed_get("/balance-allowance", &q).await?.json().await?;
        Ok(b.balance / BASE_UNITS)
    }

    /// Orders resting on the account (a page; the reconciler only needs to
    /// know whether there are any).
    pub async fn open_orders(&self) -> anyhow::Result<Vec<serde_json::Value>> {
        let v: serde_json::Value = self.authed_get("/data/orders", "").await?.json().await?;
        Ok(match v {
            serde_json::Value::Array(a) => a,
            serde_json::Value::Object(mut o) => match o.remove("data") {
                Some(serde_json::Value::Array(a)) => a,
                _ => Vec::new(),
            },
            _ => Vec::new(),
        })
    }

    /// The kill switch: cancel every open order on the account.
    pub async fn cancel_all(&self) -> anyhow::Result<CancelAck> {
        let resp = self.authed(Method::DELETE, "/cancel-all", None).await?.error_for_status()?;
        Ok(resp.json().await?)
    }

    pub async fn order_state(&self, order_id: &str) -> anyhow::Result<OrderState> {
        let path = format!("/data/order/{order_id}");
        Ok(self.authed_get(&path, "").await?.json().await?)
    }

    /// The touch of one token from the REST book (off the hot path).
    pub async fn top(&self, token: &str) -> anyhow::Result<Top> {
        let resp = self
            .http
            .send(Method::GET, &format!("/book?token_id={token}"), &[], None)
            .await?
            .error_for_status()?;
        let book: BookResp = resp.json().await?;
        Ok(Top {
            best_bid: book.bids.iter().filter(|l| l.size > Decimal::ZERO).map(|l| l.price).max(),
            best_ask: book.asks.iter().filter(|l| l.size > Decimal::ZERO).map(|l| l.price).min(),
            tick: book.tick_size,
            min_size: book.min_order_size,
        })
    }

    fn timing(
        &self,
        kind: Kind,
        trigger: Trigger,
        t_ns: u64,
        spec: Option<&OrderSpec>,
        presigned: bool,
        sign_time: Duration,
    ) -> OrderTiming {
        OrderTiming {
            record_type: "order_timing",
            kind,
            trigger,
            t_ns,
            token: spec.map(|s| s.token.clone()),
            price: spec.map(|s| s.price),
            size: spec.map(|s| s.size),
            presigned,
            sign_us: us(sign_time),
            decision_to_write_us: 0,
            write_to_headers_us: 0,
            headers_to_ack_us: 0,
            decision_to_ack_us: 0,
            http: String::new(),
            status: 0,
            ok: false,
            order_id: None,
            venue_status: None,
            error: None,
            making: None,
            taking: None,
            frame_to_wire_us: None,
            path: None,
        }
    }

    async fn finish(
        &self,
        timing: &mut OrderTiming,
        result: reqwest::Result<reqwest::Response>,
        t_decision: Instant,
        t_write: Instant,
    ) -> Option<Vec<u8>> {
        match result {
            Err(e) => {
                timing.error = Some(format!("send: {e}"));
                timing.decision_to_ack_us = us(t_decision.elapsed());
                None
            }
            Ok(resp) => {
                let t_headers = Instant::now();
                timing.http = version(resp.version());
                let status = resp.status();
                timing.status = status.as_u16();
                let body = resp.bytes().await;
                let t_ack = Instant::now();
                timing.write_to_headers_us = us(t_headers - t_write);
                timing.headers_to_ack_us = us(t_ack - t_headers);
                timing.decision_to_ack_us = us(t_ack - t_decision);
                match body {
                    Ok(b) => {
                        timing.ok = status == StatusCode::OK;
                        if !timing.ok {
                            timing.error = Some(String::from_utf8_lossy(&b[..b.len().min(300)]).into_owned());
                        }
                        Some(b.to_vec())
                    }
                    Err(e) => {
                        timing.error = Some(format!("body: {e}"));
                        None
                    }
                }
            }
        }
    }

    pub fn stats(&self) -> ExecStats {
        let s = self.stats.lock();
        ExecStats {
            decision_to_ack_ms: s.decision_to_ack_us.summary_scaled(1e3),
            write_to_ack_ms: s.write_to_ack_us.summary_scaled(1e3),
            sign_ms: s.sign_us.summary_scaled(1e3),
            cancel_rtt_ms: s.cancel_rtt_us.summary_scaled(1e3),
            posts: s.posts,
            post_errors: s.post_errors,
            cancels: s.cancels,
            cancel_errors: s.cancel_errors,
            presign: self.presign.stats(),
            connection: self.http.stats(),
        }
    }
}

/// Read a POST /order acknowledgement into its timing record.
fn apply_ack(timing: &mut OrderTiming, body: Option<&[u8]>) -> Option<PostAck> {
    let ack = body.and_then(|b| match serde_json::from_slice::<PostAck>(b) {
        Ok(a) => Some(a),
        Err(e) => {
            timing.error.get_or_insert_with(|| format!("ack: {e}"));
            None
        }
    });
    if let Some(a) = &ack {
        timing.ok = timing.ok && a.success;
        timing.order_id = (!a.order_id.is_empty()).then(|| a.order_id.clone());
        timing.venue_status = a.status.clone();
        timing.making = a.making_amount.clone();
        timing.taking = a.taking_amount.clone();
        if let Some(msg) = a.error_msg.as_ref().filter(|m| !m.is_empty()) {
            timing.error.get_or_insert_with(|| msg.clone());
        }
    }
    ack
}

const fn sign_time_zero() -> Duration {
    Duration::ZERO
}
