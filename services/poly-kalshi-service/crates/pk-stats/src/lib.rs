//! Statistics for poly-kalshi.
//!
//! [`Hist`] is an allocation-free log-linear histogram for the hot path: values
//! 0..7 exact, then 8 sub-buckets per power of two (≤ 12.5% relative error),
//! over the whole `u64` range. The caller picks the unit (ns, µs, ms).
//! [`Sample`] holds the moments research needs (mean, standard error, t).

use serde::Serialize;

const N_BUCKETS: usize = 8 + 61 * 8;

#[derive(Clone)]
pub struct Hist {
    counts: [u64; N_BUCKETS],
    n: u64,
    sum: u64,
    max: u64,
}

impl Default for Hist {
    fn default() -> Self {
        Self { counts: [0; N_BUCKETS], n: 0, sum: 0, max: 0 }
    }
}

impl std::fmt::Debug for Hist {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Hist").field("summary", &self.summary()).finish()
    }
}

fn index(v: u64) -> usize {
    if v < 8 {
        return v as usize;
    }
    let e = 63 - v.leading_zeros() as usize; // ≥ 3
    let m = ((v >> (e - 3)) & 7) as usize;
    (e - 2) * 8 + m
}

/// The smallest value in bucket `i`, and the bucket's width.
fn lower(i: usize) -> (u64, u64) {
    if i < 8 {
        return (i as u64, 1);
    }
    let e = i / 8 + 2;
    let m = (i % 8) as u64;
    ((8 + m) << (e - 3), 1u64 << (e - 3))
}

impl Hist {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn record(&mut self, v: u64) {
        self.counts[index(v)] += 1;
        self.n += 1;
        self.sum = self.sum.saturating_add(v);
        self.max = self.max.max(v);
    }

    pub fn len(&self) -> u64 {
        self.n
    }

    pub fn is_empty(&self) -> bool {
        self.n == 0
    }

    pub fn max(&self) -> u64 {
        self.max
    }

    /// An estimate of the q-quantile: the midpoint of the bucket that holds it,
    /// capped at the maximum.
    pub fn quantile(&self, q: f64) -> u64 {
        if self.n == 0 {
            return 0;
        }
        if q >= 1.0 {
            return self.max;
        }
        let rank = (q.max(0.0) * (self.n - 1) as f64) as u64 + 1;
        let mut cum = 0;
        for (i, &c) in self.counts.iter().enumerate() {
            cum += c;
            if cum >= rank {
                let (lo, w) = lower(i);
                return (lo + w / 2).min(self.max);
            }
        }
        self.max
    }

    pub fn merge(&mut self, other: &Hist) {
        for (a, b) in self.counts.iter_mut().zip(other.counts.iter()) {
            *a += b;
        }
        self.n += other.n;
        self.sum = self.sum.saturating_add(other.sum);
        self.max = self.max.max(other.max);
    }

    pub fn reset(&mut self) {
        *self = Self::default();
    }

    pub fn summary(&self) -> Summary {
        Summary {
            n: self.n,
            mean: if self.n == 0 { 0.0 } else { self.sum as f64 / self.n as f64 },
            p50: self.quantile(0.5),
            p90: self.quantile(0.9),
            p99: self.quantile(0.99),
            max: self.max,
        }
    }

    /// The summary with every value divided by `div`, e.g. ns → ms with 1e6.
    pub fn summary_scaled(&self, div: f64) -> SummaryF {
        let s = self.summary();
        let f = |v: u64| round3(v as f64 / div);
        SummaryF {
            n: s.n,
            mean: round3(s.mean / div),
            p50: f(s.p50),
            p90: f(s.p90),
            p99: f(s.p99),
            max: f(s.max),
        }
    }
}

fn round3(v: f64) -> f64 {
    (v * 1000.0).round() / 1000.0
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Serialize)]
pub struct Summary {
    pub n: u64,
    pub mean: f64,
    pub p50: u64,
    pub p90: u64,
    pub p99: u64,
    pub max: u64,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Serialize)]
pub struct SummaryF {
    pub n: u64,
    pub mean: f64,
    pub p50: f64,
    pub p90: f64,
    pub p99: f64,
    pub max: f64,
}

/// Running moments (Welford) for a mean, its standard error and t against zero.
#[derive(Clone, Copy, Debug, Default)]
pub struct Sample {
    n: u64,
    mean: f64,
    m2: f64,
}

impl Sample {
    pub fn push(&mut self, x: f64) {
        self.n += 1;
        let d = x - self.mean;
        self.mean += d / self.n as f64;
        self.m2 += d * (x - self.mean);
    }

    pub fn n(&self) -> u64 {
        self.n
    }

    pub fn mean(&self) -> Option<f64> {
        (self.n > 0).then_some(self.mean)
    }

    /// Sample standard deviation (n − 1).
    pub fn sd(&self) -> Option<f64> {
        (self.n > 1).then(|| (self.m2 / (self.n - 1) as f64).sqrt())
    }

    pub fn se(&self) -> Option<f64> {
        self.sd().map(|sd| sd / (self.n as f64).sqrt())
    }

    pub fn t(&self) -> Option<f64> {
        match (self.mean(), self.se()) {
            (Some(m), Some(se)) if se > 0.0 => Some(m / se),
            _ => None,
        }
    }
}

impl FromIterator<f64> for Sample {
    fn from_iter<I: IntoIterator<Item = f64>>(iter: I) -> Self {
        let mut s = Sample::default();
        for x in iter {
            s.push(x);
        }
        s
    }
}

/// The exact q-quantile of a sorted slice, by linear interpolation (numpy's default).
pub fn quantile_sorted(sorted: &[f64], q: f64) -> Option<f64> {
    if sorted.is_empty() {
        return None;
    }
    let pos = q.clamp(0.0, 1.0) * (sorted.len() - 1) as f64;
    let lo = pos.floor() as usize;
    let hi = pos.ceil() as usize;
    Some(sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo as f64))
}

/// numpy's `pairwise_sum` for float64 (what `ndarray.sum` and pandas'
/// `Series.mean` use): 8 interleaved accumulators per 128-element block,
/// blocks split in halves. Bit-for-bit with numpy, so results that round on
/// a tie land where pandas' do.
pub fn np_sum(a: &[f64]) -> f64 {
    let n = a.len();
    if n < 8 {
        let mut res = 0.0;
        for x in a {
            res += x;
        }
        res
    } else if n <= 128 {
        let mut r = [a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]];
        let mut i = 8;
        while i < n - (n % 8) {
            for j in 0..8 {
                r[j] += a[i + j];
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        while i < n {
            res += a[i];
            i += 1;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        np_sum(&a[..n2]) + np_sum(&a[n2..])
    }
}

/// `numpy.mean` / pandas `Series.mean` on float64.
pub fn np_mean(a: &[f64]) -> Option<f64> {
    (!a.is_empty()).then(|| np_sum(a) / a.len() as f64)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn buckets_round_trip_within_an_eighth() {
        for v in [0u64, 1, 7, 8, 9, 15, 16, 100, 1_000, 123_456, 1 << 40, u64::MAX / 3] {
            let (lo, w) = lower(index(v));
            assert!(lo <= v && v < lo + w, "{v} not in [{lo}, {})", lo + w);
            assert!(w as f64 <= (lo as f64 / 8.0).max(1.0));
        }
    }

    #[test]
    fn quantiles() {
        let mut h = Hist::new();
        for v in 1..=1000 {
            h.record(v);
        }
        let s = h.summary();
        assert_eq!(s.n, 1000);
        assert!((s.p50 as f64 - 500.0).abs() / 500.0 < 0.07, "{s:?}");
        assert!((s.p99 as f64 - 990.0).abs() / 990.0 < 0.07, "{s:?}");
        assert_eq!(s.max, 1000);
        assert_eq!(Hist::new().quantile(0.5), 0);
    }

    #[test]
    fn merge_adds() {
        let mut a = Hist::new();
        let mut b = Hist::new();
        a.record(5);
        b.record(50);
        a.merge(&b);
        assert_eq!(a.len(), 2);
        assert_eq!(a.max(), 50);
    }

    #[test]
    fn sample_moments() {
        let s: Sample = [1.0, 2.0, 3.0, 4.0].into_iter().collect();
        assert_eq!(s.mean(), Some(2.5));
        assert!((s.sd().unwrap() - 1.2909944).abs() < 1e-6);
        assert!((s.t().unwrap() - 3.8729833).abs() < 1e-6);
    }

    #[test]
    fn interpolated_quantile() {
        let v = [1.0, 2.0, 3.0, 4.0];
        assert_eq!(quantile_sorted(&v, 0.5), Some(2.5));
        assert_eq!(quantile_sorted(&v, 1.0), Some(4.0));
        assert_eq!(quantile_sorted(&[], 0.5), None);
    }
}
