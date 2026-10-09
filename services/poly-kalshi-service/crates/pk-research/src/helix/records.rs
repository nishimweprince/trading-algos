//! Typed Helix records: `recv3`'s dataclasses and `reader`'s typed iterators.
//!
//! Every reader takes the record's canonical (ns) tag and also reads its W twin
//! and its legacy (µs) tag, scaling legacy times by 1000. Layouts, from Helix's
//! payload headers (README):
//!
//! | record | size | layout |
//! |---|---|---|
//! | `PolyWindow` | 256 | `<QQ 64s 80s 80s d B 7x>` |
//! | resolution | 96 | `<QQ 64s d B B H 4x>` |
//! | `PolyClobBbo` | 224 | `<QQQ qqqq 80s 80s B 7x>`, prices and sizes ×1e8 |
//! | `QuoteV3` | 64 | `<dddd qq Q BBBB 4x>` |
//! | `TradeV3` | 72 | `<16s c 7x dd Q qq B B 6x>` |
//! | `PolyClobTrade` | 208 | `<80s 80s c 7x d d q q Q>` |
//! | `HubOrderbook` | 688 | 20 bids, 20 asks (price, size), then n, seq, ts, source, asset |

use std::collections::HashMap;
use std::path::Path;

use super::container::{dual, records};
use super::{HELIX_15M, path};

pub const TAG_QUOTE_NS: u8 = 22;
pub const TAG_BOOK_NS: u8 = 23;
pub const TAG_TRADE_NS: u8 = 24;
pub const TAG_POLY_TRADE_NS: u8 = 25;
pub const TAG_POLY_BBO_NS: u8 = 27;
pub const TAG_POLY_WINDOW_NS: u8 = 32;
const E8: f64 = 1e8;

fn f64_at(d: &[u8], o: usize) -> f64 {
    f64::from_le_bytes(d[o..o + 8].try_into().expect("8 bytes"))
}

fn i64_at(d: &[u8], o: usize) -> i64 {
    i64::from_le_bytes(d[o..o + 8].try_into().expect("8 bytes"))
}

fn u64_at(d: &[u8], o: usize) -> u64 {
    u64::from_le_bytes(d[o..o + 8].try_into().expect("8 bytes"))
}

/// A NUL-terminated ASCII field (`split(b"\0", 1)[0].decode("ascii", "replace")`).
fn cstr(d: &[u8]) -> String {
    let end = d.iter().position(|b| *b == 0).unwrap_or(d.len());
    d[..end].iter().map(|&b| if b.is_ascii() { b as char } else { '\u{fffd}' }).collect()
}

#[derive(Clone, Debug, PartialEq)]
pub struct PolyWindow {
    pub ts_recorded_ns: i64,
    pub open_unix: i64,
    pub condition_id: String,
    pub token_yes: String,
    pub token_no: String,
    pub strike: f64,
    pub asset: u8,
}

#[derive(Clone, Debug, PartialEq)]
pub struct PolyResolution {
    pub ts_recorded_ns: i64,
    pub open_unix: i64,
    pub condition_id: String,
    pub close_px: f64,
    pub asset: u16,
}

#[derive(Clone, Debug, PartialEq)]
pub struct PolyBbo {
    pub ts_received_ns: i64,
    pub ts_wall_ns: i64,
    pub ts_publisher_ns: i64,
    pub bid_px: f64,
    pub bid_sz: f64,
    pub ask_px: f64,
    pub ask_sz: f64,
    pub condition_id: String,
    pub token_id: String,
    pub outcome: u8,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Quote {
    pub bid: f64,
    pub ask: f64,
    pub bid_size: f64,
    pub ask_size: f64,
    pub ts_received_ns: i64,
    pub ts_wall_ns: i64,
    pub seq: u64,
    pub source: u8,
    pub asset_id: u8,
    pub changed: u8,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Trade {
    pub side: char,
    pub price: f64,
    pub size: f64,
    pub trade_id: u64,
    pub ts_received_ns: i64,
    pub ts_wall_ns: i64,
    pub asset_id: u8,
}

#[derive(Clone, Debug, PartialEq)]
pub struct PolyTrade {
    pub condition_id: String,
    pub asset_id: String,
    pub side: String,
    pub price: f64,
    pub size: f64,
    pub ts_publisher_ns: i64,
    pub ts_received_ns: i64,
    pub seq: u64,
}

/// A book snapshot's touch: what the studies read of `recv3.iter_book`.
#[derive(Clone, Debug, PartialEq)]
pub struct BookTop {
    pub ts_received_ns: i64,
    pub asset_id: i32,
    pub best_bid: f64,
    pub best_ask: Option<f64>,
}

pub fn decode_window(r: &[u8], scale: i64) -> PolyWindow {
    PolyWindow {
        ts_recorded_ns: i64_at(r, 0) * scale,
        open_unix: i64_at(r, 8),
        condition_id: cstr(&r[16..80]),
        token_yes: cstr(&r[80..160]),
        token_no: cstr(&r[160..240]),
        strike: f64_at(r, 240),
        asset: r[248],
    }
}

pub fn decode_poly_bbo(r: &[u8], scale: i64) -> PolyBbo {
    PolyBbo {
        ts_received_ns: i64_at(r, 0) * scale,
        ts_wall_ns: i64_at(r, 8) * scale,
        ts_publisher_ns: i64_at(r, 16) * scale,
        bid_px: i64_at(r, 24) as f64 / E8,
        bid_sz: i64_at(r, 32) as f64 / E8,
        ask_px: i64_at(r, 40) as f64 / E8,
        ask_sz: i64_at(r, 48) as f64 / E8,
        condition_id: cstr(&r[56..136]),
        token_id: cstr(&r[136..216]),
        outcome: r[216],
    }
}

pub fn decode_quote(r: &[u8], scale: i64) -> Quote {
    Quote {
        bid: f64_at(r, 0),
        ask: f64_at(r, 8),
        bid_size: f64_at(r, 16),
        ask_size: f64_at(r, 24),
        ts_received_ns: i64_at(r, 32) * scale,
        ts_wall_ns: i64_at(r, 40) * scale,
        seq: u64_at(r, 48),
        source: r[56],
        asset_id: r[58],
        changed: r[59],
    }
}

/// `reader.iter_windows`: the 15-minute windows, once each (a restart re-emits
/// them); the same window recorded with two token pairs is an error.
pub fn iter_windows(day: &str, root: &Path) -> anyhow::Result<Vec<PolyWindow>> {
    let mut seen: HashMap<(u8, i64), (String, String)> = HashMap::new();
    let mut out = Vec::new();
    for (r, scale) in dual(&path(root, "Polymarket_Window", day), TAG_POLY_WINDOW_NS, 256)? {
        let w = decode_window(&r, scale);
        if !HELIX_15M.iter().any(|(a, _)| *a == w.asset) {
            continue;
        }
        let ids = (w.token_yes.clone(), w.token_no.clone());
        match seen.get(&(w.asset, w.open_unix)) {
            Some(prev) if *prev != ids => {
                anyhow::bail!("{day}: window ({}, {}) recorded with two token pairs", w.asset, w.open_unix)
            }
            Some(_) => continue,
            None => {
                seen.insert((w.asset, w.open_unix), ids);
                out.push(w);
            }
        }
    }
    Ok(out)
}

/// `reader.iter_resolutions`: tags 33 (ns), 47 (wide) and 8 (µs), in that order.
pub fn iter_resolutions(day: &str, root: &Path) -> anyhow::Result<Vec<PolyResolution>> {
    let p = path(root, "Polymarket_Window", day);
    let mut out = Vec::new();
    for (tag, scale) in [(33u8, 1i64), (47, 1), (8, 1000)] {
        for r in records(&p, tag, 96)? {
            let (aid, wide) = (r[89] as u16, u16::from_le_bytes([r[90], r[91]]));
            out.push(PolyResolution {
                ts_recorded_ns: i64_at(&r, 0) * scale,
                open_unix: i64_at(&r, 8),
                condition_id: cstr(&r[16..80]),
                close_px: f64_at(&r, 80),
                asset: if wide != 0 { wide } else { aid },
            });
        }
    }
    Ok(out)
}

pub fn iter_poly_bbo(file: &Path) -> anyhow::Result<Vec<PolyBbo>> {
    Ok(dual(file, TAG_POLY_BBO_NS, 224)?.into_iter().map(|(r, s)| decode_poly_bbo(&r, s)).collect())
}

/// `recv3.iter_quotes`: SpotQuote records; `only_changed` drops the re-stamps.
pub fn iter_quotes(file: &Path, only_changed: bool) -> anyhow::Result<Vec<Quote>> {
    Ok(dual(file, TAG_QUOTE_NS, 64)?
        .into_iter()
        .map(|(r, s)| decode_quote(&r, s))
        .filter(|q| !only_changed || q.changed == 1)
        .collect())
}

/// `recv3.iter_trades`: trade prints with a `B`/`S` side.
pub fn iter_trades(file: &Path) -> anyhow::Result<Vec<Trade>> {
    Ok(dual(file, TAG_TRADE_NS, 72)?
        .into_iter()
        .filter_map(|(r, scale)| {
            let side = r[16] as char;
            matches!(side, 'B' | 'S').then(|| Trade {
                side,
                price: f64_at(&r, 24),
                size: f64_at(&r, 32),
                trade_id: u64_at(&r, 40),
                ts_received_ns: i64_at(&r, 48) * scale,
                ts_wall_ns: i64_at(&r, 56) * scale,
                asset_id: r[65],
            })
        })
        .collect())
}

pub fn iter_poly_trades(file: &Path) -> anyhow::Result<Vec<PolyTrade>> {
    Ok(dual(file, TAG_POLY_TRADE_NS, 208)?
        .into_iter()
        .map(|(r, scale)| PolyTrade {
            condition_id: cstr(&r[0..80]),
            asset_id: cstr(&r[80..160]),
            side: (r[160] as char).to_string(),
            price: f64_at(&r, 168),
            size: f64_at(&r, 176),
            ts_publisher_ns: i64_at(&r, 184) * scale,
            ts_received_ns: i64_at(&r, 192) * scale,
            seq: u64_at(&r, 200),
        })
        .collect())
}

/// Lighter's price bands for pre-V3 book records with no asset byte.
fn book_asset_of(top_bid: f64) -> i32 {
    const BANDS: [(f64, f64, i32); 6] = [
        (20000.0, 200000.0, 15),
        (1000.0, 4000.0, 16),
        (300.0, 1000.0, 19),
        (40.0, 200.0, 17),
        (0.5, 5.0, 18),
        (0.001, 0.5, 20),
    ];
    BANDS.iter().find(|(lo, hi, _)| *lo <= top_bid && top_bid < *hi).map(|b| b.2).unwrap_or(-1)
}

/// `recv3.iter_book`, reduced to the touch: snapshots with at least one bid.
pub fn iter_book(file: &Path) -> anyhow::Result<Vec<BookTop>> {
    let level = |r: &[u8], k: usize| {
        let (p, s) = (f64_at(r, 16 * k), f64_at(r, 16 * k + 8));
        (p > 0.0 && s > 0.0).then_some(p)
    };
    Ok(dual(file, TAG_BOOK_NS, 688)?
        .into_iter()
        .filter_map(|(r, scale)| {
            let best_bid = (0..20).find_map(|k| level(&r, k))?;
            let best_ask = (20..40).find_map(|k| level(&r, k));
            let aid = if r[673] != 0 { r[673] as i32 } else { book_asset_of(best_bid) };
            Some(BookTop { ts_received_ns: i64_at(&r, 664) * scale, asset_id: aid, best_bid, best_ask })
        })
        .collect())
}
