//! The paper venue's latency, measured instead of assumed.
//!
//! Every successful post on the real order path (a probe, or a live entry)
//! gives one decision → acknowledgement round trip. The estimate is the p50 of
//! the last `window` of them once there are `min_samples`; until then it is the
//! seed (`PK_PAPER_LATENCY_MS`). It lives in memory only: the trader publishes
//! each change as a `latency` control line, so the tape (and any replay of it)
//! carries the value the paper venue used.

use pk_stats::Rolling;
use serde::Serialize;

/// A change smaller than this is not worth a control line.
pub const PUBLISH_STEP_MS: f64 = 1.0;

#[derive(Clone, Debug)]
pub struct LatencyEstimator {
    seed_ms: f64,
    min_samples: usize,
    samples: Rolling,
    published: Option<f64>,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct LatencyNow {
    pub ms: f64,
    pub n: usize,
    /// "seed" until enough round trips exist, then "measured".
    pub source: &'static str,
}

impl LatencyEstimator {
    pub fn new(seed_ms: f64, window: usize, min_samples: usize) -> Self {
        Self { seed_ms, min_samples: min_samples.max(1), samples: Rolling::new(window), published: None }
    }

    /// Add one round trip (µs). Returns the estimate when it should be
    /// published: the first time there are enough samples, then whenever the
    /// p50 moves by at least [`PUBLISH_STEP_MS`].
    pub fn record(&mut self, decision_to_ack_us: u64) -> Option<LatencyNow> {
        self.samples.push(decision_to_ack_us as f64 / 1e3);
        let now = self.current();
        if now.source != "measured" {
            return None;
        }
        let moved = self.published.is_none_or(|p| (now.ms - p).abs() >= PUBLISH_STEP_MS);
        moved.then(|| {
            self.published = Some(now.ms);
            now
        })
    }

    pub fn current(&self) -> LatencyNow {
        let n = self.samples.len();
        match self.samples.quantile(0.5).filter(|_| n >= self.min_samples) {
            Some(p50) => LatencyNow { ms: (p50 * 10.0).round() / 10.0, n, source: "measured" },
            None => LatencyNow { ms: self.seed_ms, n, source: "seed" },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn seed_until_enough_then_the_rolling_p50() {
        let mut e = LatencyEstimator::new(150.0, 5, 3);
        assert_eq!(e.record(40_000), None);
        assert_eq!(e.record(60_000), None);
        assert_eq!(e.current().source, "seed");
        assert_eq!(e.current().ms, 150.0);
        let first = e.record(50_000).unwrap();
        assert_eq!((first.ms, first.n, first.source), (50.0, 3, "measured"));
        // 40, 60, 50, 50.4: the p50 moves by 0.2 ms, not published.
        assert_eq!(e.record(50_400), None);
        // The old samples roll out; the slow ones now set the median.
        for _ in 0..3 {
            e.record(90_000);
        }
        assert_eq!(e.current().ms, 90.0);
    }
}
