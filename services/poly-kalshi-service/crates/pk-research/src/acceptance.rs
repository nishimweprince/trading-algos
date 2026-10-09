//! PLAN phase 2 acceptance: the live service's own metrics, polled for 24 h.
//!
//! ```text
//! pk acceptance --hours 24 [--every 300]
//! ```
//!
//! Every `--every` seconds it reads `/v1/status` and keeps the rolling metrics
//! (`engine.latency`; the Python tool read a top-level `latency` that does not
//! exist, so its checks were always empty). At the end: the verdict per
//! criterion over the worst poll:
//!
//! - Polymarket `slow_consumer` closes ≤ 1 over the run;
//! - `loop_lag_ms` max < 250 (the trader's async runtime);
//! - `queue_us` p99 < 250 000 (lines waiting for the engine thread);
//! - `age_ms_poly` p99 < 150;
//! - `engine_us` p99 < 300.

use serde_json::{Value, json};

pub const CRITERIA: [(&str, &str, f64); 4] = [
    ("loop_lag_ms", "max", 250.0),
    ("queue_us", "p99", 250_000.0),
    ("age_ms_poly", "p99", 150.0),
    ("engine_us", "p99", 300.0),
];

pub fn poll_of(status: &Value) -> Value {
    json!({
        "latency": status.pointer("/engine/latency").cloned().unwrap_or(json!({})),
        "slow_consumer": status.pointer("/feeds/poly/slow_consumer"),
    })
}

pub fn verdict(polls: &[Value], failures: u64) -> Value {
    let mut worst = serde_json::Map::new();
    let mut checks = serde_json::Map::new();
    for (name, stat, limit) in CRITERIA {
        let w = polls.iter().filter_map(|p| p["latency"][name][stat].as_f64()).reduce(f64::max);
        worst.insert(name.into(), w.into());
        checks.insert(name.into(), w.map(|w| w < limit).into());
    }
    let slow: Vec<f64> = polls.iter().filter_map(|p| p["slow_consumer"].as_f64()).collect();
    let delta = (slow.len() > 1).then(|| slow[slow.len() - 1] - slow[0]);
    checks.insert("slow_consumer".into(), delta.map(|d| d <= 1.0).into());
    checks.insert("no_failed_polls".into(), (failures == 0).into());
    let pass = checks.values().all(|v| v == &Value::Bool(true));
    json!({"worst": worst, "slow_consumer_closes": delta, "failed_polls": failures, "polls": polls.len(),
           "checks": checks, "pass": pass})
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn worst_poll_decides() {
        let p = |lag: f64, slow: u64| {
            json!({"latency": {"loop_lag_ms": {"max": lag}, "queue_us": {"p99": 10.0}, "age_ms_poly": {"p99": 20.0},
                               "engine_us": {"p99": 5.0}}, "slow_consumer": slow})
        };
        let v = verdict(&[p(10.0, 3), p(20.0, 4)], 0);
        assert_eq!(v["pass"], true);
        let v = verdict(&[p(10.0, 3), p(300.0, 6)], 0);
        assert_eq!(
            (v["checks"]["loop_lag_ms"].clone(), v["checks"]["slow_consumer"].clone()),
            (json!(false), json!(false))
        );
        assert_eq!(verdict(&[p(1.0, 1), p(1.0, 1)], 1)["pass"], false);
    }
}
