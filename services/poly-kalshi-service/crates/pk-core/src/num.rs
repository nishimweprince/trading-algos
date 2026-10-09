//! Numbers as the venues send them and as the journal writes them.
//!
//! - Prices are book keys in ticks of 0.0001 ([`px_key`]): that covers Kalshi's
//!   deci-cent and Polymarket's cent ticks, and an integer key has no float
//!   ghosts.
//! - [`round_dp`] rounds exactly as Python's `round(x, n)` does (ties to even, on
//!   the exact binary value), so journal numbers match the Python engine's.
//! - [`Num`] reads a JSON string or number as `f64`, as the venues mix both.

use std::io::Write as _;

use serde::{Deserialize, Deserializer};

/// Ticks per unit price.
pub const PX_SCALE: f64 = 10_000.0;

/// A price's book key: `round(price, 4)` in ticks of 0.0001.
#[inline]
pub fn px_key(price: f64) -> i32 {
    (price * PX_SCALE).round() as i32
}

/// The price of a book key (the nearest double to `key / 10^4`).
#[inline]
pub fn key_px(key: i32) -> f64 {
    key as f64 / PX_SCALE
}

/// Sizes are kept to 6 dp, finer than either venue: summed fractional Kalshi
/// deltas otherwise leave ~1e-14 ghost levels that cross the book.
#[inline]
pub fn round_size(size: f64) -> f64 {
    (size * 1e6).round() / 1e6
}

/// Python's `round(x, n)`: correctly rounded, ties to even.
pub fn round_dp(x: f64, n: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    let mut buf = [0u8; 64];
    let mut cur = std::io::Cursor::new(&mut buf[..]);
    if write!(cur, "{x:.n$}").is_err() {
        // More than 64 characters: a magnitude no price or size reaches.
        return x;
    }
    let len = cur.position() as usize;
    std::str::from_utf8(&buf[..len]).ok().and_then(|s| s.parse().ok()).unwrap_or(x)
}

pub fn round_opt(x: Option<f64>, n: usize) -> Option<f64> {
    x.map(|v| round_dp(v, n))
}

/// A JSON number or numeric string as `f64`.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Num(pub f64);

impl<'de> Deserialize<'de> for Num {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct V;
        impl serde::de::Visitor<'_> for V {
            type Value = Num;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("a number or a numeric string")
            }
            fn visit_f64<E>(self, v: f64) -> Result<Num, E> {
                Ok(Num(v))
            }
            fn visit_i64<E>(self, v: i64) -> Result<Num, E> {
                Ok(Num(v as f64))
            }
            fn visit_u64<E>(self, v: u64) -> Result<Num, E> {
                Ok(Num(v as f64))
            }
            fn visit_str<E: serde::de::Error>(self, v: &str) -> Result<Num, E> {
                v.trim().parse().map(Num).map_err(E::custom)
            }
        }
        d.deserialize_any(V)
    }
}

/// A venue timestamp in ms: a number, or a string of digits. Anything else
/// (or ≤ 0) is no timestamp.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct VenueMs(pub Option<f64>);

impl<'de> Deserialize<'de> for VenueMs {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> serde::de::Visitor<'de> for V {
            type Value = VenueMs;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("a timestamp")
            }
            fn visit_f64<E>(self, v: f64) -> Result<VenueMs, E> {
                Ok(VenueMs((v > 0.0).then_some(v)))
            }
            fn visit_i64<E>(self, v: i64) -> Result<VenueMs, E> {
                Ok(VenueMs((v > 0).then_some(v as f64)))
            }
            fn visit_u64<E>(self, v: u64) -> Result<VenueMs, E> {
                Ok(VenueMs((v > 0).then_some(v as f64)))
            }
            fn visit_str<E>(self, v: &str) -> Result<VenueMs, E> {
                if v.is_empty() || !v.bytes().all(|b| b.is_ascii_digit()) {
                    return Ok(VenueMs(None));
                }
                Ok(VenueMs(v.parse::<u64>().ok().filter(|&ms| ms > 0).map(|ms| ms as f64)))
            }
            fn visit_bool<E>(self, _: bool) -> Result<VenueMs, E> {
                Ok(VenueMs(None))
            }
            fn visit_unit<E>(self) -> Result<VenueMs, E> {
                Ok(VenueMs(None))
            }
            fn visit_none<E>(self) -> Result<VenueMs, E> {
                Ok(VenueMs(None))
            }
            fn visit_map<A: serde::de::MapAccess<'de>>(self, mut a: A) -> Result<VenueMs, A::Error> {
                while a.next_entry::<serde::de::IgnoredAny, serde::de::IgnoredAny>()?.is_some() {}
                Ok(VenueMs(None))
            }
            fn visit_seq<A: serde::de::SeqAccess<'de>>(self, mut a: A) -> Result<VenueMs, A::Error> {
                while a.next_element::<serde::de::IgnoredAny>()?.is_some() {}
                Ok(VenueMs(None))
            }
        }
        d.deserialize_any(V)
    }
}

/// Receive time minus the venue's own stamp (ms): `recv_ns / 1e6 - venue_ms`.
#[inline]
pub fn lag_ms(venue_ms: Option<f64>, recv_ns: i64) -> Option<f64> {
    venue_ms.map(|ms| recv_ns as f64 / 1e6 - ms)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_rounding() {
        assert_eq!(round_dp(0.125, 2), 0.12);
        assert_eq!(round_dp(0.375, 2), 0.38);
        assert_eq!(round_dp(2.675, 2), 2.67);
        assert_eq!(round_dp(2.5, 0), 2.0);
        assert_eq!(round_dp(0.15005, 4), 0.15);
        assert_eq!(round_dp(-1234.56789, 2), -1234.57);
    }

    #[test]
    fn keys() {
        for s in ["0.45", "0.455", "0.4550", "0.0001", "0.9999", "0.01"] {
            let p: f64 = s.parse().unwrap();
            assert_eq!(key_px(px_key(p)), round_dp(p, 4), "{s}");
        }
        assert_eq!(px_key(1.0 - 0.455), 5450);
    }

    #[test]
    fn nums() {
        let v: Vec<Num> = serde_json::from_str(r#"["0.5", 0.25, 3, "-54.37"]"#).unwrap();
        assert_eq!(v, vec![Num(0.5), Num(0.25), Num(3.0), Num(-54.37)]);
        let t: Vec<VenueMs> = serde_json::from_str(r#"["1700", 1700, "x", 0, null, "-5"]"#).unwrap();
        assert_eq!(
            t,
            vec![
                VenueMs(Some(1700.0)),
                VenueMs(Some(1700.0)),
                VenueMs(None),
                VenueMs(None),
                VenueMs(None),
                VenueMs(None)
            ]
        );
    }
}
