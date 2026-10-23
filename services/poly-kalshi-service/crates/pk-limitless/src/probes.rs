//! Latency probes for diagnosis: where the time to Limitless goes.
//!
//! - **warm**: `GET /markets/<slug>/orderbook` on the order path's warm HTTP/2
//!   connection, which is what an order rides on. That route is not cached
//!   (`cf-cache-status: BYPASS`), so it times Cloudflare and the origin; a
//!   response served from cache is counted and discarded, never timed.
//! - **cold**: every 12th round, a TCP connect to the API host and a full
//!   request on a fresh client (DNS, TCP, TLS, request): the cost the warm
//!   connection saves.
//! - The Cloudflare colo (`cf-ray`) shows where the edge answers from.
//!
//! Each round is a `lim_probe` line on the tape-side journal only every minute
//! (the summary), so the journal does not grow with the probe rate.

use std::sync::Arc;
use std::time::{Duration, Instant};

use parking_lot::Mutex;
use pk_core::num::{round_dp, round_opt};
use pk_exec::WarmHttp;
use pk_stats::Rolling;
use reqwest::Method;
use serde_json::{Value, json};

#[derive(Default)]
struct Stats {
    n: u64,
    warm_ms: Option<Rolling>,
    cold_ms: Option<Rolling>,
    tcp_ms: Option<Rolling>,
    colo: Option<String>,
    cache: Option<String>,
    cached_rejected: u64,
    errors: u64,
    last_error: Option<String>,
}

pub struct NetProbe {
    pub warm: Arc<WarmHttp>,
    host: String,
    stats: Mutex<Stats>,
}

fn rolling(r: &mut Option<Rolling>, n: usize) -> &mut Rolling {
    r.get_or_insert_with(|| Rolling::new(n))
}

impl NetProbe {
    pub fn new(warm: Arc<WarmHttp>, api_url: &str) -> Self {
        let host = url::Url::parse(api_url).ok().and_then(|u| u.host_str().map(str::to_owned)).unwrap_or_default();
        Self { warm, host, stats: Mutex::new(Stats::default()) }
    }

    /// The warm round trip's median, for paper fills.
    pub fn warm_p50(&self) -> Option<f64> {
        self.stats.lock().warm_ms.as_ref().and_then(|r| r.quantile(0.5))
    }

    pub async fn round(&self, slug: &str, cold: bool) {
        let path = format!("/markets/{slug}/orderbook");
        let t0 = Instant::now();
        let r = self.warm.send(Method::GET, &path, &[], None).await;
        let elapsed = t0.elapsed().as_secs_f64() * 1e3;
        match r {
            Ok(resp) => {
                let h = |k: &str| resp.headers().get(k).and_then(|v| v.to_str().ok()).map(str::to_owned);
                let cache = h("cf-cache-status");
                let colo = h("cf-ray").and_then(|r| r.rsplit('-').next().map(str::to_owned));
                let ok = resp.status().is_success();
                let _ = resp.bytes().await;
                let mut s = self.stats.lock();
                s.colo = colo.or(s.colo.take());
                s.cache.clone_from(&cache);
                if cache.as_deref() == Some("HIT") {
                    s.cached_rejected += 1;
                } else if ok {
                    s.n += 1;
                    rolling(&mut s.warm_ms, 720).push(elapsed);
                } else {
                    s.errors += 1;
                }
            }
            Err(e) => {
                let mut s = self.stats.lock();
                s.errors += 1;
                s.last_error = Some(e.to_string().chars().take(200).collect());
            }
        }
        if cold {
            self.cold(&path).await;
        }
    }

    async fn cold(&self, path: &str) {
        let t0 = Instant::now();
        if let Ok(Ok(_)) =
            tokio::time::timeout(Duration::from_secs(5), tokio::net::TcpStream::connect((self.host.as_str(), 443))).await
        {
            rolling(&mut self.stats.lock().tcp_ms, 120).push(t0.elapsed().as_secs_f64() * 1e3);
        }
        let Ok(client) = reqwest::Client::builder().timeout(Duration::from_secs(5)).build() else { return };
        let t0 = Instant::now();
        if client.get(format!("https://{}{path}", self.host)).send().await.is_ok_and(|r| r.status().is_success()) {
            rolling(&mut self.stats.lock().cold_ms, 120).push(t0.elapsed().as_secs_f64() * 1e3);
        }
    }

    pub fn stats(&self) -> Value {
        let s = self.stats.lock();
        let q = |r: &Option<Rolling>, p: f64| round_opt(r.as_ref().and_then(|r| r.quantile(p)), 1);
        json!({
            "host": self.host,
            "n": s.n,
            "warm_ms": {"p10": q(&s.warm_ms, 0.1), "p50": q(&s.warm_ms, 0.5), "p90": q(&s.warm_ms, 0.9), "p99": q(&s.warm_ms, 0.99)},
            "cold_ms": {"p50": q(&s.cold_ms, 0.5), "p90": q(&s.cold_ms, 0.9)},
            "tcp_connect_ms": {"p50": q(&s.tcp_ms, 0.5), "p90": q(&s.tcp_ms, 0.9)},
            "colo": s.colo,
            "cache": s.cache,
            "cached_rejected": s.cached_rejected,
            "errors": s.errors,
            "last_error": s.last_error,
            "connection": serde_json::to_value(self.warm.stats()).unwrap_or(Value::Null),
        })
    }

    /// The minute's summary line for the journal.
    pub fn summary(&self) -> Value {
        let s = self.stats.lock();
        let q = |r: &Option<Rolling>, p: f64| round_opt(r.as_ref().and_then(|r| r.quantile(p)), 1);
        json!({
            "n": s.n, "colo": s.colo, "errors": s.errors,
            "warm_ms_p50": q(&s.warm_ms, 0.5), "warm_ms_p90": q(&s.warm_ms, 0.9),
            "cold_ms_p50": q(&s.cold_ms, 0.5), "tcp_connect_ms_p50": q(&s.tcp_ms, 0.5),
        })
    }
}

/// Order-path probe results (post-only far below the bid, then cancel).
#[derive(Default)]
pub struct OrderProbeStats {
    pub posted: u64,
    pub rejected: u64,
    pub decision_to_ack: Option<Rolling>,
    pub cancel: Option<Rolling>,
    pub last_error: Option<String>,
}

impl OrderProbeStats {
    pub fn push(&mut self, ack_ms: f64, cancel_ms: Option<f64>) {
        self.posted += 1;
        rolling(&mut self.decision_to_ack, 500).push(ack_ms);
        if let Some(c) = cancel_ms {
            rolling(&mut self.cancel, 500).push(c);
        }
    }

    pub fn json(&self) -> Value {
        let q = |r: &Option<Rolling>, p: f64| round_opt(r.as_ref().and_then(|r| r.quantile(p)), 1);
        json!({
            "posted": self.posted, "rejected": self.rejected,
            "decision_to_ack_ms": {"p50": q(&self.decision_to_ack, 0.5), "p90": q(&self.decision_to_ack, 0.9)},
            "cancel_ms": {"p50": q(&self.cancel, 0.5), "p90": q(&self.cancel, 0.9)},
            "last_error": self.last_error,
        })
    }
}

/// Live taker orders: ack, and ack → final (the venue's hold plus matching).
#[derive(Default)]
pub struct TakerStats {
    pub orders: u64,
    pub filled: u64,
    pub ack: Option<Rolling>,
    pub held: Option<Rolling>,
}

impl TakerStats {
    pub fn push(&mut self, ack_ms: f64, held_ms: Option<f64>, filled: bool) {
        self.orders += 1;
        if filled {
            self.filled += 1;
        }
        rolling(&mut self.ack, 500).push(ack_ms);
        if let Some(h) = held_ms {
            rolling(&mut self.held, 500).push(h);
        }
    }

    pub fn json(&self) -> Value {
        let q = |r: &Option<Rolling>, p: f64| round_opt(r.as_ref().and_then(|r| r.quantile(p)), 1);
        json!({
            "orders": self.orders, "filled": self.filled,
            "fill_rate": (self.orders > 0).then(|| round_dp(self.filled as f64 / self.orders as f64, 3)),
            "decision_to_ack_ms": {"p50": q(&self.ack, 0.5), "p90": q(&self.ack, 0.9)},
            "ack_to_final_ms": {"p50": q(&self.held, 0.5), "p90": q(&self.held, 0.9)},
        })
    }
}
