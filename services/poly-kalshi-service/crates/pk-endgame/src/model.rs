//! The settlement number and its fair value.
//!
//! Polymarket's 15-minute Up/Down markets resolve Up when the mean of the
//! Chainlink prices stamped in the 60 s before the close is at least the mean
//! of those stamped in the 60 s before the open (RESULTS.md: reproduces 99.7%
//! of 24,321 windows). RTDS sends one Chainlink tick per second per asset,
//! stamped on whole seconds, so each average is over integer seconds
//! `[t − 60, t − 1]`.
//!
//! Fair value is PLAN 4.3's driftless log walk, but used where 4.3 never
//! looked (its scan stopped 60 s before the close): with part of the closing
//! average already printed, only the seconds still to come are uncertain.
//!
//! - Inside the closing minute, with `n` seconds still to print after the
//!   newest tick at price `s`, the average is `(sum + n·s) / (seen + n)`.
//!   Its log-sd is `w · σ · sqrt((n+1)(2n+1)/(6n))`, `w = n / (seen + n)`:
//!   the mean of a random walk's next `n` one-second steps.
//! - Before it, with `d` seconds until the first averaged second, the average
//!   is centred on `s` with variance `σ² (d + (N−1)(2N−1)/(6N))`, `N = 60`.
//!
//! σ is the sd of one-second log returns over a trailing window. A floor on
//! the sd covers our average against Polymarket's own (ties, a missed tick).

use std::collections::VecDeque;

pub const AVG_N: i64 = 60;
/// Seconds of ticks kept per asset: σ's window plus both averages, with room.
const KEEP_S: i64 = 1_800;

/// Abramowitz and Stegun 7.1.26 (|error| < 1.5e-7), as `pk helix fairvalue` has it.
pub fn ndtr(x: f64) -> f64 {
    let z = x.abs() / std::f64::consts::SQRT_2;
    let t = 1.0 / (1.0 + 0.3275911 * z);
    let poly =
        t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))));
    let erf = 1.0 - poly * (-z * z).exp();
    let sign = if x > 0.0 {
        1.0
    } else if x < 0.0 {
        -1.0
    } else {
        0.0
    };
    0.5 * (1.0 + sign * erf)
}

/// One asset's Chainlink seconds: `(second, price)` in time order, one per second.
#[derive(Clone, Debug, Default)]
pub struct Series {
    ticks: VecDeque<(i64, f64)>,
    /// Wall-clock arrival of the newest tick (ns).
    pub last_arrival_ns: i64,
}

impl Series {
    /// Add a tick; a second already held is overwritten (the later report wins).
    pub fn push(&mut self, sec: i64, px: f64, arrival_ns: i64) {
        if !(px.is_finite() && px > 0.0) {
            return;
        }
        match self.ticks.back() {
            Some(&(last, _)) if sec > last => self.ticks.push_back((sec, px)),
            Some(_) => match self.ticks.binary_search_by_key(&sec, |t| t.0) {
                Ok(i) => self.ticks[i].1 = px,
                Err(i) => self.ticks.insert(i, (sec, px)),
            },
            None => self.ticks.push_back((sec, px)),
        }
        self.last_arrival_ns = self.last_arrival_ns.max(arrival_ns);
        let newest = self.ticks.back().map(|t| t.0).unwrap_or(sec);
        while self.ticks.front().is_some_and(|t| t.0 < newest - KEEP_S) {
            self.ticks.pop_front();
        }
    }

    pub fn last(&self) -> Option<(i64, f64)> {
        self.ticks.back().copied()
    }

    /// `(count, sum)` of the seconds in `[from, to]`.
    pub fn sum_between(&self, from: i64, to: i64) -> (usize, f64) {
        let start = self.ticks.partition_point(|t| t.0 < from);
        self.ticks.range(start..).take_while(|t| t.0 <= to).fold((0, 0.0), |(n, s), t| (n + 1, s + t.1))
    }

    /// The 60 s average that a boundary at `t` (an open or a close) settles on:
    /// `(mean, seconds seen)`.
    pub fn boundary_avg(&self, t: i64) -> Option<(f64, usize)> {
        let (n, s) = self.sum_between(t - AVG_N, t - 1);
        (n > 0).then(|| (s / n as f64, n))
    }

    /// σ of one-second log returns over `[upto − window, upto]`, each return
    /// scaled by `1/sqrt(dt)` for gaps of up to 5 s (longer gaps are skipped).
    pub fn sigma(&self, upto: i64, window: i64, min_n: usize) -> Option<f64> {
        let start = self.ticks.partition_point(|t| t.0 < upto - window);
        let mut prev: Option<(i64, f64)> = None;
        let (mut n, mut mean, mut m2) = (0usize, 0.0f64, 0.0f64);
        for &(sec, px) in self.ticks.range(start..).take_while(|t| t.0 <= upto) {
            if let Some((ps, pp)) = prev {
                let dt = sec - ps;
                if (1..=5).contains(&dt) {
                    let r = (px / pp).ln() / (dt as f64).sqrt();
                    n += 1;
                    let d = r - mean;
                    mean += d / n as f64;
                    m2 += d * (r - mean);
                }
            }
            prev = Some((sec, px));
        }
        (n >= min_n.max(2)).then(|| (m2 / (n - 1) as f64).sqrt())
    }
}

/// The model's view at one instant.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Fair {
    pub p_up: f64,
    /// The expected closing average.
    pub mean: f64,
    /// Its log-sd (after the floor).
    pub sd: f64,
    /// Seconds of the closing average already printed.
    pub seen: usize,
    /// Seconds still to print.
    pub to_come: i64,
}

/// P(closing average ≥ `k`) for a window closing at `close`, from the series
/// as it stands. `None` without a tick or a σ.
pub fn fair(series: &Series, k: f64, close: i64, sigma: f64, sd_floor: f64) -> Option<Fair> {
    fair_from(series, k, close, sigma, sd_floor, None)
}

/// [`fair`], with `s_now` (when given) as the price the seconds still to come
/// start from, in place of the newest Chainlink tick: Chainlink trails spot by
/// about 2 s, so a spot-adjusted price is what those seconds will print near.
pub fn fair_from(series: &Series, k: f64, close: i64, sigma: f64, sd_floor: f64, s_now: Option<f64>) -> Option<Fair> {
    let (last_sec, s_tick) = series.last()?;
    let s = s_now.filter(|x| x.is_finite() && *x > 0.0).unwrap_or(s_tick);
    if !(k > 0.0 && sigma.is_finite() && sigma >= 0.0) || last_sec >= close {
        return None;
    }
    let avg_start = close - AVG_N;
    let (mean, var, seen, to_come) = if last_sec >= avg_start {
        let (seen, sum) = series.sum_between(avg_start, last_sec);
        let n = (close - 1 - last_sec).max(0);
        let total = seen as f64 + n as f64;
        if total <= 0.0 {
            return None;
        }
        let mean = (sum + n as f64 * s) / total;
        let var = if n == 0 {
            0.0
        } else {
            let nf = n as f64;
            let w = nf / total;
            w * w * sigma * sigma * (nf + 1.0) * (2.0 * nf + 1.0) / (6.0 * nf)
        };
        (mean, var, seen, n)
    } else {
        let d = (avg_start - last_sec) as f64;
        let nf = AVG_N as f64;
        let var = sigma * sigma * (d + (nf - 1.0) * (2.0 * nf - 1.0) / (6.0 * nf));
        (s, var, 0, AVG_N)
    };
    let sd = var.sqrt().max(sd_floor);
    let p_up = ndtr((mean.ln() - k.ln()) / sd);
    Some(Fair { p_up, mean, sd, seen, to_come })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn flat(from: i64, to: i64, px: f64) -> Series {
        let mut s = Series::default();
        for t in from..=to {
            s.push(t, px, 0);
        }
        s
    }

    #[test]
    fn ndtr_matches_known_values() {
        assert!((ndtr(0.0) - 0.5).abs() < 1e-12);
        assert!((ndtr(1.0) - 0.841_344_75).abs() < 2e-7);
        assert!((ndtr(-1.96) - 0.024_997_9).abs() < 2e-7);
        assert!(ndtr(10.0) > 0.999_999);
    }

    #[test]
    fn push_orders_and_overwrites() {
        let mut s = Series::default();
        s.push(10, 1.0, 0);
        s.push(12, 3.0, 0);
        s.push(11, 2.0, 0);
        s.push(11, 2.5, 0);
        assert_eq!(s.sum_between(10, 12), (3, 6.5));
        assert_eq!(s.last(), Some((12, 3.0)));
    }

    #[test]
    fn boundary_average_uses_the_sixty_seconds_before() {
        let mut s = flat(0, 59, 100.0);
        s.push(60, 1_000.0, 0); // at the boundary itself: not in it
        let (m, n) = s.boundary_avg(60).unwrap();
        assert_eq!(n, 60);
        assert!((m - 100.0).abs() < 1e-9);
    }

    #[test]
    fn sigma_of_a_known_walk() {
        let mut s = Series::default();
        let mut px = 100.0;
        for t in 0..400 {
            px *= if t % 2 == 0 { 1.001 } else { 1.0 / 1.001 };
            s.push(t, px, 0);
        }
        let sig = s.sigma(399, 300, 100).unwrap();
        assert!((sig - 0.001).abs() < 2e-5, "{sig}");
        assert!(s.sigma(399, 50, 100).is_none());
    }

    #[test]
    fn at_the_strike_with_nothing_seen_is_a_coin_flip() {
        let close = 1_000;
        let s = flat(0, close - 90, 100.0);
        let f = fair(&s, 100.0, close, 0.0005, 1e-6).unwrap();
        assert!((f.p_up - 0.5).abs() < 1e-9);
        assert_eq!(f.seen, 0);
    }

    #[test]
    fn a_printed_lead_becomes_near_certain() {
        let close = 1_000;
        // 50 of the 60 closing seconds printed 0.2% above the strike.
        let mut s = flat(0, close - 61, 100.0);
        for t in close - 60..close - 10 {
            s.push(t, 100.2, 0);
        }
        let f = fair(&s, 100.0, close, 0.0005, 1e-6).unwrap();
        assert_eq!((f.seen, f.to_come), (50, 10));
        assert!(f.p_up > 0.999, "{f:?}");
        // The same lead with the whole minute still to come is far less sure.
        let early = flat(0, close - 80, 100.2);
        let g = fair(&early, 100.0, close, 0.0005, 1e-6).unwrap();
        assert!(g.p_up < f.p_up && g.p_up > 0.5, "{g:?}");
    }

    #[test]
    fn everything_printed_is_decided_up_to_the_floor() {
        let close = 1_000;
        let s = flat(0, close - 1, 100.001);
        let f = fair(&s, 100.0, close, 0.0005, 0.00005).unwrap();
        assert_eq!(f.to_come, 0);
        assert!((f.sd - 0.00005).abs() < 1e-12);
        assert!(f.p_up > 0.5 && f.p_up < 0.7, "{f:?}"); // 1 bp up is 0.2 sd: not certain
    }

    #[test]
    fn a_spot_price_moves_the_seconds_to_come() {
        let close = 1_000;
        let mut s = flat(0, close - 61, 100.0);
        for t in close - 60..close - 30 {
            s.push(t, 100.0, 0);
        }
        let base = fair(&s, 100.0, close, 0.0005, 1e-6).unwrap();
        assert!((base.p_up - 0.5).abs() < 1e-9);
        let up = fair_from(&s, 100.0, close, 0.0005, 1e-6, Some(100.05)).unwrap();
        assert!(up.p_up > 0.6, "{up:?}");
        assert_eq!(fair_from(&s, 100.0, close, 0.0005, 1e-6, Some(f64::NAN)).unwrap().p_up, base.p_up);
    }

    #[test]
    fn down_is_symmetric() {
        let close = 1_000;
        let mut s = flat(0, close - 61, 100.0);
        for t in close - 60..close - 20 {
            s.push(t, 99.9, 0);
        }
        // 40 s printed 10 bp under the strike, 20 s to come at σ = 5 bp/s: z ≈ −2.2.
        let f = fair(&s, 100.0, close, 0.0005, 1e-6).unwrap();
        assert!(f.p_up > 0.005 && f.p_up < 0.02, "{f:?}");
        let g = fair(&s, 100.0, close, 0.0002, 1e-6).unwrap();
        assert!(g.p_up < 1e-4, "{g:?}");
    }
}
