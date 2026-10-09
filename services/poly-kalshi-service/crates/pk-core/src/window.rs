//! A window's identity: the three book ids that belong to one asset's
//! 15-minute window, and its fee schedule.
//!
//! - Polymarket's slug is `<asset>-updown-15m-<open_unix>`, `clobTokenIds = [Up, Down]`.
//! - Kalshi's series is `KX<ASSET>15M`; the market is the one with `close_time = open + 900`.
//!
//! Books are keyed by these ids, never by asset, so an old window's terminal
//! book can never be read as the new window's (kpc trap 3).

use serde::{Deserialize, Serialize};

use crate::decision::WINDOW_S;
use crate::fees::{DEFAULT_EXPONENT, DEFAULT_RATE};

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Window {
    pub asset: String,
    pub open_unix: i64,
    pub kalshi_ticker: String,
    pub yes_token: String,
    pub no_token: String,
    pub condition_id: String,
    #[serde(default = "default_rate")]
    pub fee_rate: f64,
    #[serde(default = "default_exponent")]
    pub fee_exponent: f64,
}

fn default_rate() -> f64 {
    DEFAULT_RATE
}

fn default_exponent() -> f64 {
    DEFAULT_EXPONENT
}

impl Window {
    pub fn close_unix(&self) -> i64 {
        self.open_unix + WINDOW_S
    }

    /// `[kalshi_ticker, yes_token, no_token]`.
    pub fn ids(&self) -> [&str; 3] {
        [&self.kalshi_ticker, &self.yes_token, &self.no_token]
    }

    /// The control line that carries this identity on the tape.
    pub fn control(&self) -> serde_json::Value {
        let mut v = serde_json::to_value(self).expect("window serialises");
        v.as_object_mut().expect("object").insert("type".into(), "window".into());
        v
    }
}

pub fn window_open(unix_s: i64) -> i64 {
    unix_s.div_euclid(WINDOW_S) * WINDOW_S
}

pub fn poly_slug(asset: &str, open_unix: i64) -> String {
    format!("{}-updown-15m-{open_unix}", asset.to_lowercase())
}

/// `recv_ns / 1e9` as Python's true division gives it (to within an ulp).
#[inline]
pub fn secs_f64(t_ns: i64) -> f64 {
    let q = t_ns.div_euclid(1_000_000_000);
    let r = t_ns.rem_euclid(1_000_000_000);
    q as f64 + r as f64 / 1e9
}
