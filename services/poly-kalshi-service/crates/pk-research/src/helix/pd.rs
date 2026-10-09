//! pandas' arithmetic, where the studies' numbers depend on it: `Series.mean`
//! and `.std` sum pairwise (numpy), `groupby(...).mean()` and `.sum()` sum
//! with Kahan compensation, quantiles interpolate as numpy does, and `pd.cut`
//! bins are right-closed with the first one including its left edge.

use std::collections::BTreeMap;

pub use pk_stats::{np_mean, np_sum};

/// `Series.var(ddof)`: pandas' two-pass variance over numpy sums.
pub fn np_var(v: &[f64], ddof: usize) -> Option<f64> {
    if v.len() <= ddof {
        return None;
    }
    let avg = np_sum(v) / v.len() as f64;
    let sqr: Vec<f64> = v.iter().map(|x| (avg - x) * (avg - x)).collect();
    Some(np_sum(&sqr) / (v.len() - ddof) as f64)
}

pub fn np_std(v: &[f64]) -> Option<f64> {
    np_var(v, 1).map(f64::sqrt)
}

/// `groupby(...).sum()` for one group: Kahan-compensated, in row order.
pub fn kahan_sum(v: &[f64]) -> f64 {
    let (mut sum, mut comp) = (0.0f64, 0.0f64);
    for &x in v {
        let y = x - comp;
        let t = sum + y;
        comp = t - sum - y;
        sum = t;
    }
    sum
}

/// `groupby(...).mean()` for one group.
pub fn group_mean(v: &[f64]) -> f64 {
    kahan_sum(v) / v.len() as f64
}

/// Values grouped by key, keys sorted (pandas' default), rows in order.
pub fn group_by<K: Ord, T: Copy>(rows: impl IntoIterator<Item = (K, T)>) -> BTreeMap<K, Vec<T>> {
    let mut out: BTreeMap<K, Vec<T>> = BTreeMap::new();
    for (k, v) in rows {
        out.entry(k).or_default().push(v);
    }
    out
}

/// `np.median` (NaN-free input), None when empty.
pub fn median(v: &[f64]) -> Option<f64> {
    pk_results::stats::median(v)
}

/// `Series.quantile(q)`: numpy's linear method, with its two-sided lerp.
pub fn quantile(v: &[f64], q: f64) -> Option<f64> {
    if v.is_empty() {
        return None;
    }
    let mut s = v.to_vec();
    s.sort_by(f64::total_cmp);
    let pos = q * (s.len() - 1) as f64;
    let lo = pos.floor();
    let t = pos - lo;
    let lo = lo as usize;
    let hi = (lo + 1).min(s.len() - 1);
    let (a, b) = (s[lo], s[hi]);
    let d = b - a;
    Some(if t >= 0.5 { b - d * (1.0 - t) } else { a + d * t })
}

/// `pd.cut(x, edges, include_lowest=True)`: the bin's index, or None outside.
pub fn cut(x: f64, edges: &[f64]) -> Option<usize> {
    if x.is_nan() {
        return None;
    }
    if x == edges[0] {
        return Some(0);
    }
    (0..edges.len() - 1).find(|&i| edges[i] < x && x <= edges[i + 1])
}

pub const SPREAD_EDGES: [f64; 6] = [0.0, 0.015, 0.025, 0.05, 0.1, 1.0];
pub const SPREAD_LABELS: [&str; 5] =
    ["(-0.001, 0.015]", "(0.015, 0.025]", "(0.025, 0.05]", "(0.05, 0.1]", "(0.1, 1.0]"];

/// `np.linspace(0, 1, 11)` and pandas' labels for it.
pub fn decile_edges() -> Vec<f64> {
    (0..11).map(|i| i as f64 * (1.0 / 10.0)).map(|x| if x > 1.0 { 1.0 } else { x }).collect()
}

pub const DECILE_LABELS: [&str; 10] = [
    "(-0.001, 0.1]",
    "(0.1, 0.2]",
    "(0.2, 0.3]",
    "(0.3, 0.4]",
    "(0.4, 0.5]",
    "(0.5, 0.6]",
    "(0.6, 0.7]",
    "(0.7, 0.8]",
    "(0.8, 0.9]",
    "(0.9, 1.0]",
];

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn linspace_matches_numpy() {
        // np.linspace(0, 1, 11): start + i * step, the last forced to stop.
        let e = decile_edges();
        assert_eq!(e[3], 0.30000000000000004);
        assert_eq!(e[10], 1.0);
        assert_eq!(cut(0.0, &e), Some(0));
        assert_eq!(cut(0.1, &e), Some(0));
        assert_eq!(cut(0.30000000000000003, &e), Some(2));
        assert_eq!(cut(1.0, &SPREAD_EDGES), Some(4));
        assert_eq!(cut(1.0000001, &SPREAD_EDGES), None);
    }

    #[test]
    fn numpy_quantile() {
        assert_eq!(quantile(&[1.0, 2.0, 3.0, 4.0], 0.5), Some(2.5));
        assert_eq!(quantile(&[1.0, 2.0, 3.0, 4.0], 0.1), Some(1.3));
    }
}
