//! Latency telemetry: rolling samples of the times that decide the race to a
//! stale Polymarket US ask, measured without an API key.
//!
//! | Series | What it times |
//! |---|---|
//! | `gw_rtt` | a Polymarket US gateway request, send → body parsed (the pooled, warm connection) |
//! | `gw_server` | the gateway's own `x-pm-server-latency` for that request |
//! | `gw_edge_origin` | `gw_rtt` − `gw_server`: our host → Cloudflare edge → origin and back |
//! | `k_lock_wait` | a Kalshi frame waiting for the engine lock |
//! | `k_process` | applying the frame and evaluating its window, under the lock |
//! | `timer_lag` | how late a 10 ms timer fires: scheduler and noisy-neighbour jitter |
//!
//! Each series keeps its last `CAP` samples in microseconds; quantiles are
//! read from that window.

use std::collections::VecDeque;

use parking_lot::Mutex;
use serde_json::{Value, json};

const CAP: usize = 8_192;

#[derive(Default)]
pub struct Series {
    v: Mutex<VecDeque<u32>>,
    n: std::sync::atomic::AtomicU64,
}

impl Series {
    pub fn add_us(&self, us: i64) {
        let us = us.clamp(0, u32::MAX as i64) as u32;
        let mut v = self.v.lock();
        if v.len() == CAP {
            v.pop_front();
        }
        v.push_back(us);
        self.n.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    }

    /// p50, p95, p99, max over the window (µs), and the samples ever seen.
    pub fn value(&self) -> Value {
        let mut s: Vec<u32> = self.v.lock().iter().copied().collect();
        let total = self.n.load(std::sync::atomic::Ordering::Relaxed);
        if s.is_empty() {
            return json!({"n": total, "window": 0});
        }
        s.sort_unstable();
        let q = |p: f64| s[((s.len() - 1) as f64 * p).round() as usize];
        json!({"n": total, "window": s.len(), "p50": q(0.5), "p95": q(0.95), "p99": q(0.99), "max": s[s.len() - 1]})
    }
}

#[derive(Default)]
pub struct Lat {
    pub gw_rtt: Series,
    pub gw_server: Series,
    pub gw_edge_origin: Series,
    pub k_lock_wait: Series,
    pub k_process: Series,
    pub timer_lag: Series,
}

impl Lat {
    pub fn value(&self) -> Value {
        json!({
            "gw_rtt_us": self.gw_rtt.value(), "gw_server_us": self.gw_server.value(),
            "gw_edge_origin_us": self.gw_edge_origin.value(), "k_lock_wait_us": self.k_lock_wait.value(),
            "k_process_us": self.k_process.value(), "timer_lag_us": self.timer_lag.value(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quantiles_over_a_rolling_window() {
        let s = Series::default();
        assert_eq!(s.value()["window"], 0);
        for us in 1..=100 {
            s.add_us(us);
        }
        let v = s.value();
        assert_eq!((v["p50"].as_u64(), v["p99"].as_u64(), v["max"].as_u64()), (Some(51), Some(99), Some(100)));
        for _ in 0..CAP {
            s.add_us(7);
        }
        let v = s.value();
        assert_eq!((v["window"].as_u64(), v["max"].as_u64(), v["n"].as_u64()), (Some(CAP as u64), Some(7), Some(100 + CAP as u64)));
        s.add_us(-5);
        assert_eq!(s.value()["p50"], 7);
    }
}
