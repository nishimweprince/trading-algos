//! The Helix kpc tape (`.hlxz`), read in a stream and never copied (PLAN 0.1).
//!
//! - [`container`]: the v1 block container and the HLXF frame stream;
//! - [`records`]: typed records (windows, resolutions, BBO, quotes, trades, books);
//! - [`series`]: the array path the studies use, and the Kalshi contract switch;
//! - studies: [`validate`], [`resolutions`] (and [`gamma`]), [`replay`],
//!   [`sweeps`], [`leadlag`], [`fairvalue`], [`inventory`].
//!
//! Clocks: `recv` is the recorder's monotonic receive time, `wall` its
//! CLOCK_REALTIME at receive (the timeline every array uses, as `t`), and the
//! venue's own stamp where there is one. `research/helix/README.md` has the
//! measurements.

use std::path::{Path, PathBuf};

pub mod container;
pub mod fairvalue;
pub mod gamma;
pub mod inventory;
pub mod leadlag;
pub mod pd;
pub mod records;
pub mod replay;
pub mod resolutions;
pub mod series;
pub mod sweeps;
pub mod validate;

pub const NS: i64 = 1_000_000_000;

/// Helix asset id → our asset, 15-minute markets (the even ids are 5-minute
/// ones). The 14 windowed slots (0..=13) are frozen; spot slots start at 14
/// (`UsdcSpot`). Ids are u16: the wide tags carry them as `asset_wide`.
pub const HELIX_15M: [(u16, &str); 7] =
    [(1, "BTC"), (3, "ETH"), (5, "SOL"), (7, "XRP"), (9, "BNB"), (11, "DOGE"), (13, "HYPE")];

/// Chainlink reference prices are keyed by the asset's *Spot slot.
pub const CHAINLINK_SPOT: [(&str, u16); 7] =
    [("BTC", 15), ("ETH", 16), ("SOL", 17), ("XRP", 18), ("BNB", 19), ("DOGE", 20), ("HYPE", 21)];

pub const STREAMS: [&str; 11] = [
    "Polymarket_Window",
    "Polymarket_CLOB_Yes_BBO",
    "Polymarket_CLOB_No_BBO",
    "Polymarket_CLOB_Trade",
    "Polymarket_CLOB_No_Trade",
    "PolymarketChainlink_BBO",
    "Kalshi_Window",
    "Kalshi_Yes_BBO",
    "Kalshi_No_BBO",
    "Kalshi_Yes_Trade",
    "Kalshi_No_Trade",
];

pub fn asset_name(aid: u16) -> Option<&'static str> {
    HELIX_15M.iter().find(|(a, _)| *a == aid).map(|(_, n)| *n)
}

pub fn asset_id(name: &str) -> Option<u16> {
    HELIX_15M.iter().find(|(_, n)| *n == name).map(|(a, _)| *a)
}

pub fn chainlink_id(name: &str) -> Option<u16> {
    CHAINLINK_SPOT.iter().find(|(n, _)| *n == name).map(|(_, a)| *a)
}

/// `root`, else `$HELIX_TAPE_DIR`, else `$KPC_DATA`, else `~/kpc/raw`.
pub fn tape_dir(root: Option<&Path>) -> PathBuf {
    if let Some(r) = root {
        return r.to_path_buf();
    }
    for var in ["HELIX_TAPE_DIR", "KPC_DATA"] {
        if let Ok(v) = std::env::var(var)
            && !v.is_empty()
        {
            return PathBuf::from(v);
        }
    }
    std::env::var("HOME").map(PathBuf::from).unwrap_or_default().join("kpc/raw")
}

pub fn path(root: &Path, stream: &str, day: &str) -> PathBuf {
    root.join(stream).join(format!("{day}.hlxz"))
}

/// The days a stream has, sorted.
pub fn days(root: &Path, stream: &str) -> Vec<String> {
    let mut out: Vec<String> = std::fs::read_dir(root.join(stream))
        .into_iter()
        .flatten()
        .flatten()
        .filter_map(|e| {
            let p = e.path();
            (p.extension().is_some_and(|x| x == "hlxz"))
                .then(|| p.file_stem().and_then(|s| s.to_str()).map(str::to_owned))
                .flatten()
        })
        .collect();
    out.sort();
    out
}

/// A `--days` spec over the days `stream` has: `A-B` (inclusive), a comma list, or all.
pub fn select_days(root: &Path, stream: &str, spec: Option<&str>) -> Vec<String> {
    let available = days(root, stream);
    match spec {
        None | Some("") => available,
        Some(s) if s.contains('-') => {
            let (lo, hi) = s.split_once('-').expect("contains -");
            available.into_iter().filter(|d| lo <= d.as_str() && d.as_str() <= hi).collect()
        }
        Some(s) => s.split(',').filter(|d| available.iter().any(|a| a == d)).map(str::to_owned).collect(),
    }
}
