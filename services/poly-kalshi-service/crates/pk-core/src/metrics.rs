//! Rolling metrics: the last `WINDOW` samples per name, percentiles computed
//! only when status is read.
//!
//! Names (unit in the suffix): `feed_ms_p` / `feed_ms_k` (receive minus venue
//! stamp, includes clock skew), `age_ms_kalshi` / `age_ms_poly` (book age at a
//! window-consuming decision), `resolution_s_poly` / `resolution_s_kalshi`.

use std::collections::{BTreeMap, VecDeque};

use serde::Serialize;

use crate::num::round_dp;

pub const WINDOW: usize = 4096;

#[derive(Debug, Default)]
struct Series {
    samples: VecDeque<f64>,
    count: u64,
}

#[derive(Debug)]
pub struct Metrics {
    window: usize,
    series: BTreeMap<&'static str, Series>,
}

impl Default for Metrics {
    fn default() -> Self {
        Self::new(WINDOW)
    }
}

#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct MetricSnapshot {
    pub n: u64,
    pub window: usize,
    pub p50: f64,
    pub p90: f64,
    pub p99: f64,
    pub max: f64,
}

impl Metrics {
    pub fn new(window: usize) -> Self {
        Self { window, series: BTreeMap::new() }
    }

    pub fn add(&mut self, name: &'static str, value: f64) {
        let s = self.series.entry(name).or_default();
        if s.samples.len() == self.window {
            s.samples.pop_front();
        }
        s.samples.push_back(value);
        s.count += 1;
    }

    pub fn snapshot(&self) -> BTreeMap<&'static str, MetricSnapshot> {
        let mut out = BTreeMap::new();
        for (name, s) in &self.series {
            if s.samples.is_empty() {
                continue;
            }
            let mut v: Vec<f64> = s.samples.iter().copied().collect();
            v.sort_by(f64::total_cmp);
            let pct = |q: f64| v[((q * v.len() as f64) as usize).min(v.len() - 1)];
            out.insert(
                *name,
                MetricSnapshot {
                    n: s.count,
                    window: v.len(),
                    p50: round_dp(pct(0.5), 3),
                    p90: round_dp(pct(0.9), 3),
                    p99: round_dp(pct(0.99), 3),
                    max: round_dp(v[v.len() - 1], 3),
                },
            );
        }
        out
    }
}
