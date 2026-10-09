//! Typed Helix records: `recv3`'s dataclasses and `reader`'s typed iterators.
//!
//! Every reader takes the record's canonical (ns) tag and also reads its W twin
//! and its legacy (µs) tag, scaling legacy times by 1000. Layouts, from Helix's
//! payload headers (README); the size is the smallest a reader accepts, since
//! layouts only grow at the end and each block states its own:
//!
//! | record | size | layout |
//! |---|---|---|
//! | `PolyWindow` | 256 | `<QQ 64s 80s 80s d B x H 4x>` |
//! | resolution | 96 | `<QQ 64s d B B H 4x>` |
//! | `PolyClobBbo` | 224 | `<QQQ qqqq 80s 80s B 7x>`, prices and sizes ×1e8 |
//! | `QuoteV3` | 64 | `<dddd qq Q BBBB H 2x>` |
//! | `TradeV3` | 72 | `<16s c 7x dd Q qq B B H 4x>` |
//! | `PolyClobTrade` | 208 | `<80s 80s c 7x d d q q Q>` |
//! | `KalshiWindow` | 80 | `<QQ 48s d B x H 4x>` |
//! | `HubOrderbook` | 688 | 20 bids, 20 asks (price, size), then n, seq, ts, source, asset |
//!
//! Asset ids: under a wide tag (43+) the u16 `asset_wide` is authoritative,
//! under the older tags the u8 (see [`asset_of`]). Condition ids are stored
//! normalized ([`norm_condition_id`]): the window and resolution records carry
//! bare hex, the BBO and trade records `0x`-prefixed hex.

use std::collections::HashMap;
use std::path::Path;

use super::container::{dual, is_wide, records};
use super::{HELIX_15M, path};

pub const TAG_QUOTE_NS: u8 = 22;
pub const TAG_BOOK_NS: u8 = 23;
pub const TAG_TRADE_NS: u8 = 24;
pub const TAG_POLY_TRADE_NS: u8 = 25;
pub const TAG_POLY_BBO_NS: u8 = 27;
pub const TAG_POLY_WINDOW_NS: u8 = 32;
pub const TAG_POLY_RESOLUTION_NS: u8 = 33;
pub const TAG_KALSHI_WINDOW_NS: u8 = 34;
const E8: f64 = 1e8;

/// `HubSpotQuote.source` / `HubTradePrint.source` venue ids.
pub mod venue {
    /// Order routing only: **no feed ever writes it**, so a BBO read at venue 8
    /// returns nothing. Read 25/26 for Polymarket quotes.
    pub const POLYMARKET_CLOB: u8 = 8;
    pub const POLYMARKET_CLOB_YES: u8 = 25;
    pub const POLYMARKET_CLOB_NO: u8 = 26;
    pub const KALSHI_BINARY_YES: u8 = 31;
    pub const KALSHI_BINARY_NO: u8 = 32;
}

/// `FileHeader.source_id` per kpc stream (`PolymarketChainlink_BBO` has none listed).
pub const SOURCE_IDS: [(&str, u16); 10] = [
    ("Kalshi_Yes_BBO", 0x1700),
    ("Kalshi_No_BBO", 0x1701),
    ("Kalshi_Yes_Trade", 0x1702),
    ("Kalshi_No_Trade", 0x1703),
    ("Kalshi_Window", 0x1706),
    ("Polymarket_CLOB_Trade", 0x0C20),
    ("Polymarket_CLOB_No_Trade", 0x0C21),
    ("Polymarket_CLOB_Yes_BBO", 0x0C24),
    ("Polymarket_CLOB_No_BBO", 0x0C25),
    ("Polymarket_Window", 0x0C10),
];

/// The asset id of a record written under `tag`: the u16 `asset_wide` under a
/// wide tag (43+), else the u8 `asset_id`. A wide record whose `asset_wide` is
/// still zero padding while the u8 is set falls back to the u8.
pub fn asset_of(tag: u8, id: u8, wide: u16) -> u16 {
    if is_wide(tag) && !(wide == 0 && id != 0) { wide } else { id as u16 }
}

fn u16_at(d: &[u8], o: usize) -> u16 {
    u16::from_le_bytes([d[o], d[o + 1]])
}

/// A condition id as comparable text: no `0x`, lowercase. Window and
/// resolution records carry bare hex, BBO and trade records `0x` hex; a join on
/// the raw strings returns nothing.
pub fn norm_condition_id(id: &str) -> String {
    let id = id.trim();
    id.strip_prefix("0x").or_else(|| id.strip_prefix("0X")).unwrap_or(id).to_ascii_lowercase()
}

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

/// `PolyWindowPayload` (tags 32/46, legacy 7).
#[derive(Clone, Debug, PartialEq)]
pub struct PolyWindow {
    pub ts_recorded_ns: i64,
    /// Seconds.
    pub open_unix: i64,
    /// Normalized ([`norm_condition_id`]).
    pub condition_id: String,
    /// `clobTokenIds[0]`, decimal u256.
    pub token_yes: String,
    /// `clobTokenIds[1]`.
    pub token_no: String,
    /// NaN if unfetched.
    pub strike: f64,
    pub asset: u16,
}

/// `PolymarketResolutionPayload` (tags 33/47, legacy 8).
#[derive(Clone, Debug, PartialEq)]
pub struct PolyResolution {
    pub ts_recorded_ns: i64,
    pub open_unix: i64,
    /// Normalized ([`norm_condition_id`]).
    pub condition_id: String,
    /// `outcome_pmps`: the close-boundary price (the next window's strike); NaN if unfetched.
    pub close_px: f64,
    /// `outcome_ws`: 0 YES won, 1 NO won, `None` for 255 (not received, which
    /// is frequent). **Never settle on it**: settle on Gamma's outcome, or on
    /// the strike path (`strike(n) > strike(n−1)`).
    pub outcome_ws: Option<u8>,
    pub asset: u16,
}

/// `KalshiWindowPayload` (tags 34/48, legacy 18): one record per (asset,
/// ticker), written when the ticker first appears or rotates.
#[derive(Clone, Debug, PartialEq)]
pub struct KalshiWindow {
    pub ts_recorded_ns: i64,
    /// Seconds, 0 if unparsable. On the kpc tape it is the ticker's ET close
    /// read as UTC (README), so join on `ts_recorded_ns` instead.
    pub open_unix: i64,
    pub ticker: String,
    /// NaN if unparsable.
    pub strike: f64,
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
    /// Per (venue, asset); advances only on a real book change.
    pub seq: u64,
    /// The venue ([`venue`]).
    pub source: u8,
    /// [`asset_of`] the record's tag: the Helix slot (`Btc15m` = 1, …). On
    /// Kalshi every concurrent strike contract of a family shares it; join
    /// [`KalshiWindow`] on (asset, time) for the ticker.
    pub asset_id: u16,
    /// 1 a real book change, 0 a stale re-stamp (filter these out).
    pub changed: u8,
}

/// `HubTradePrint` (tags 24/45, legacy 3).
#[derive(Clone, Debug, PartialEq)]
pub struct Trade {
    pub symbol: String,
    /// `B` or `S`. **On Kalshi this is the YES/NO leg, not the aggressor**:
    /// classify the aggressor against the prevailing BBO with [`aggressor`].
    pub side: char,
    pub price: f64,
    pub size: f64,
    /// FNV-1a of the venue's id when it is not numeric.
    pub trade_id: u64,
    pub ts_received_ns: i64,
    pub ts_wall_ns: i64,
    pub source: u8,
    pub asset_id: u16,
}

/// `PolyClobTradePayload` (tag 25, legacy 12).
#[derive(Clone, Debug, PartialEq)]
pub struct PolyTrade {
    /// Normalized ([`norm_condition_id`]).
    pub condition_id: String,
    /// Named `asset_id` on the tape but it is the **token id** (decimal u256),
    /// not a Helix asset: join it like a token ([`PolyTrade::token_id`]).
    pub asset_id: String,
    pub side: String,
    pub price: f64,
    pub size: f64,
    pub ts_publisher_ns: i64,
    pub ts_received_ns: i64,
    pub seq: u64,
}

impl PolyTrade {
    pub fn token_id(&self) -> &str {
        &self.asset_id
    }
}

/// The quote rule: a trade at or above the ask was bought by the taker
/// (`B`), at or below the bid sold (`S`), and inside the spread it goes to the
/// nearer side. `None` at the exact midpoint or without a two-sided book.
pub fn aggressor(price: f64, bid: f64, ask: f64) -> Option<char> {
    if !(price.is_finite() && bid.is_finite() && ask.is_finite()) || bid <= 0.0 || ask <= 0.0 || ask < bid {
        return None;
    }
    if price >= ask {
        return Some('B');
    }
    if price <= bid {
        return Some('S');
    }
    let mid = (bid + ask) / 2.0;
    if price > mid {
        Some('B')
    } else if price < mid {
        Some('S')
    } else {
        None
    }
}

/// A book snapshot's touch: what the studies read of `recv3.iter_book`.
#[derive(Clone, Debug, PartialEq)]
pub struct BookTop {
    pub ts_received_ns: i64,
    pub asset_id: i32,
    pub best_bid: f64,
    pub best_ask: Option<f64>,
}

pub fn decode_window(r: &[u8], scale: i64, tag: u8) -> PolyWindow {
    PolyWindow {
        ts_recorded_ns: i64_at(r, 0) * scale,
        open_unix: i64_at(r, 8),
        condition_id: norm_condition_id(&cstr(&r[16..80])),
        token_yes: cstr(&r[80..160]),
        token_no: cstr(&r[160..240]),
        strike: f64_at(r, 240),
        asset: asset_of(tag, r[248], u16_at(r, 250)),
    }
}

pub fn decode_resolution(r: &[u8], scale: i64, tag: u8) -> PolyResolution {
    PolyResolution {
        ts_recorded_ns: i64_at(r, 0) * scale,
        open_unix: i64_at(r, 8),
        condition_id: norm_condition_id(&cstr(&r[16..80])),
        close_px: f64_at(r, 80),
        outcome_ws: (r[88] != 0xFF).then_some(r[88]),
        asset: asset_of(tag, r[89], u16_at(r, 90)),
    }
}

pub fn decode_kalshi_window(r: &[u8], scale: i64, tag: u8) -> KalshiWindow {
    KalshiWindow {
        ts_recorded_ns: i64_at(r, 0) * scale,
        open_unix: i64_at(r, 8),
        ticker: cstr(&r[16..64]),
        strike: f64_at(r, 64),
        asset: asset_of(tag, r[72], u16_at(r, 74)),
    }
}

pub fn decode_trade(r: &[u8], scale: i64, tag: u8) -> Trade {
    Trade {
        symbol: cstr(&r[0..16]),
        side: r[16] as char,
        price: f64_at(r, 24),
        size: f64_at(r, 32),
        trade_id: u64_at(r, 40),
        ts_received_ns: i64_at(r, 48) * scale,
        ts_wall_ns: i64_at(r, 56) * scale,
        source: r[64],
        asset_id: asset_of(tag, r[65], u16_at(r, 66)),
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
        condition_id: norm_condition_id(&cstr(&r[56..136])),
        token_id: cstr(&r[136..216]),
        outcome: r[216],
    }
}

pub fn decode_quote(r: &[u8], scale: i64, tag: u8) -> Quote {
    Quote {
        bid: f64_at(r, 0),
        ask: f64_at(r, 8),
        bid_size: f64_at(r, 16),
        ask_size: f64_at(r, 24),
        ts_received_ns: i64_at(r, 32) * scale,
        ts_wall_ns: i64_at(r, 40) * scale,
        seq: u64_at(r, 48),
        source: r[56],
        asset_id: asset_of(tag, r[58], u16_at(r, 60)),
        changed: r[59],
    }
}

/// Is this a 15-minute window: a `*15m` slot and an open on the 900 s grid
/// (the Window stream carries the 5-minute windows too)?
pub fn is_15m(asset: u16, open_unix: i64) -> bool {
    HELIX_15M.iter().any(|(a, _)| *a == asset) && open_unix.rem_euclid(900) == 0
}

/// `reader.iter_windows`: the 15-minute windows, once each (a restart re-emits
/// them); the same window recorded with two token pairs is an error.
pub fn iter_windows(day: &str, root: &Path) -> anyhow::Result<Vec<PolyWindow>> {
    let mut seen: HashMap<(u16, i64), (String, String)> = HashMap::new();
    let mut out = Vec::new();
    for (r, scale, tag) in dual(&path(root, "Polymarket_Window", day), TAG_POLY_WINDOW_NS, 256)? {
        let w = decode_window(&r, scale, tag);
        if !is_15m(w.asset, w.open_unix) {
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
    for (tag, scale) in [(TAG_POLY_RESOLUTION_NS, 1i64), (47, 1), (8, 1000)] {
        for r in records(&p, tag, 96)? {
            out.push(decode_resolution(&r, scale, tag));
        }
    }
    Ok(out)
}

/// The Kalshi ticker records of a day (tags 48, 34 and legacy 18), in file
/// order. Empty when the day has no `Kalshi_Window` file.
pub fn iter_kalshi_windows(day: &str, root: &Path) -> anyhow::Result<Vec<KalshiWindow>> {
    let p = path(root, "Kalshi_Window", day);
    if !p.exists() {
        return Ok(Vec::new());
    }
    Ok(dual(&p, TAG_KALSHI_WINDOW_NS, 80)?
        .into_iter()
        .map(|(r, scale, tag)| decode_kalshi_window(&r, scale, tag))
        .collect())
}

/// The Kalshi ticker in force for `asset` at `ts_ns` (the records' own clock):
/// the latest record for the asset written at or before it. The quote stream
/// is keyed by asset only, so this is how a quote is tied to one contract.
pub fn kalshi_ticker_at(windows: &[KalshiWindow], asset: u16, ts_ns: i64) -> Option<&KalshiWindow> {
    windows.iter().filter(|w| w.asset == asset && w.ts_recorded_ns <= ts_ns).max_by_key(|w| w.ts_recorded_ns)
}

/// Where a Polymarket token belongs: its window's asset, open and outcome
/// (0 YES, 1 NO). The BBO and trade records carry no asset, only the token.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TokenWindow {
    pub asset: u16,
    pub open_unix: i64,
    pub outcome: u8,
}

/// `token_id → (asset, open_unix, outcome)` over `windows`: the join from a
/// Polymarket BBO or trade record to its window.
pub fn token_index(windows: &[PolyWindow]) -> HashMap<String, TokenWindow> {
    let mut out = HashMap::with_capacity(windows.len() * 2);
    for w in windows {
        for (token, outcome) in [(&w.token_yes, 0u8), (&w.token_no, 1)] {
            out.insert(token.clone(), TokenWindow { asset: w.asset, open_unix: w.open_unix, outcome });
        }
    }
    out
}

pub fn iter_poly_bbo(file: &Path) -> anyhow::Result<Vec<PolyBbo>> {
    Ok(dual(file, TAG_POLY_BBO_NS, 224)?.into_iter().map(|(r, s, _)| decode_poly_bbo(&r, s)).collect())
}

/// `recv3.iter_quotes`: SpotQuote records; `only_changed` drops the re-stamps.
pub fn iter_quotes(file: &Path, only_changed: bool) -> anyhow::Result<Vec<Quote>> {
    Ok(dual(file, TAG_QUOTE_NS, 64)?
        .into_iter()
        .map(|(r, s, tag)| decode_quote(&r, s, tag))
        .filter(|q| !only_changed || q.changed == 1)
        .collect())
}

/// `recv3.iter_trades`: trade prints with a `B`/`S` side (on Kalshi, the leg).
pub fn iter_trades(file: &Path) -> anyhow::Result<Vec<Trade>> {
    Ok(dual(file, TAG_TRADE_NS, 72)?
        .into_iter()
        .filter(|(r, _, _)| matches!(r[16], b'B' | b'S'))
        .map(|(r, scale, tag)| decode_trade(&r, scale, tag))
        .collect())
}

pub fn iter_poly_trades(file: &Path) -> anyhow::Result<Vec<PolyTrade>> {
    Ok(dual(file, TAG_POLY_TRADE_NS, 208)?
        .into_iter()
        .map(|(r, scale, _)| PolyTrade {
            condition_id: norm_condition_id(&cstr(&r[0..80])),
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
        .filter_map(|(r, scale, _)| {
            let best_bid = (0..20).find_map(|k| level(&r, k))?;
            let best_ask = (20..40).find_map(|k| level(&r, k));
            let aid = if r[673] != 0 { r[673] as i32 } else { book_asset_of(best_bid) };
            Some(BookTop { ts_received_ns: i64_at(&r, 664) * scale, asset_id: aid, best_bid, best_ask })
        })
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::helix::container::{BLK_MAGIC, FILE_MAGIC, blocks, tags};
    use crate::helix::series::{Extra, kalshi_quote_series, poly_bbo_series};

    /// A `.hlxz` day file: a FileHeader, then one block per `(tag, rsz, records)`.
    fn hlxz(blocks: &[(u8, usize, Vec<Vec<u8>>)]) -> Vec<u8> {
        let mut f = Vec::new();
        f.extend(FILE_MAGIC.to_le_bytes());
        f.extend(1u16.to_le_bytes());
        f.extend(32u16.to_le_bytes());
        f.extend([0u8; 24]);
        for (tag, rsz, recs) in blocks {
            let raw: Vec<u8> = recs
                .iter()
                .flat_map(|r| {
                    let mut r = r.clone();
                    r.resize(*rsz, 0);
                    r
                })
                .collect();
            let comp = zstd::bulk::compress(&raw, 3).unwrap();
            f.extend(BLK_MAGIC.to_le_bytes());
            f.extend([*tag, 0]);
            f.extend((*rsz as u16).to_le_bytes());
            f.extend((recs.len() as u32).to_le_bytes());
            f.extend((comp.len() as u32).to_le_bytes());
            f.extend((raw.len() as u32).to_le_bytes());
            f.extend([0u8; 20]);
            f.extend(comp);
        }
        f
    }

    fn scratch(name: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("pk-helix-records-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn put(root: &Path, stream: &str, day: &str, bytes: &[u8]) -> std::path::PathBuf {
        let p = path(root, stream, day);
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::write(&p, bytes).unwrap();
        p
    }

    fn cfield(s: &str, n: usize) -> Vec<u8> {
        let mut v = s.as_bytes().to_vec();
        v.resize(n, 0);
        v
    }

    fn quote(bid: f64, ask: f64, wall: i64, seq: u64, source: u8, aid: u8, wide: u16) -> Vec<u8> {
        let mut r = Vec::new();
        for x in [bid, ask, 10.0, 20.0] {
            r.extend(x.to_le_bytes());
        }
        r.extend((wall - 5).to_le_bytes());
        r.extend(wall.to_le_bytes());
        r.extend(seq.to_le_bytes());
        r.extend([source, 0, aid, 1]);
        r.extend(wide.to_le_bytes());
        r.extend([0, 0]);
        r
    }

    fn poly_bbo(wall: i64, bid: f64, ask: f64, cond: &str, token: &str) -> Vec<u8> {
        let mut r = Vec::new();
        r.extend((wall - 7).to_le_bytes());
        r.extend(wall.to_le_bytes());
        r.extend(0i64.to_le_bytes());
        for x in [bid, 100.0, ask, 200.0] {
            r.extend(((x * 1e8) as i64).to_le_bytes());
        }
        r.extend(cfield(cond, 80));
        r.extend(cfield(token, 80));
        r.push(0);
        r.extend([0u8; 7]);
        r
    }

    fn window(open: i64, cond: &str, yes: &str, no: &str, aid: u8, wide: u16) -> Vec<u8> {
        let mut r = Vec::new();
        r.extend(1i64.to_le_bytes());
        r.extend(open.to_le_bytes());
        r.extend(cfield(cond, 64));
        r.extend(cfield(yes, 80));
        r.extend(cfield(no, 80));
        r.extend(100.0f64.to_le_bytes());
        r.extend([aid, 0]);
        r.extend(wide.to_le_bytes());
        r.extend([0u8; 4]);
        r
    }

    #[test]
    fn grown_records_decode_like_the_frozen_prefix() {
        let root = scratch("grown");
        let day = "20261003";
        let rec = poly_bbo(1_000, 0.41, 0.42, "0xABC", "123");
        let p = put(
            &root,
            "Polymarket_CLOB_Yes_BBO",
            day,
            &hlxz(&[(27, 224, vec![rec.clone()]), (27, 232, vec![rec])]),
        );
        let typed = iter_poly_bbo(&p).unwrap();
        assert_eq!(typed.len(), 2);
        assert_eq!(typed[0], typed[1]);
        assert_eq!(typed[0].condition_id, "abc");
        let raws: Vec<_> = blocks(&p, &tags(TAG_POLY_BBO_NS), 224).unwrap().collect();
        assert_eq!(raws.iter().map(|r| r.rsz).collect::<Vec<_>>(), [224, 232]);
        let s = poly_bbo_series(&root, "Polymarket_CLOB_Yes_BBO", day, &["123".into()], Extra::default())
            .unwrap();
        assert_eq!(s.raw_rows, 2);
        assert_eq!(s.bid[0], 4100);
    }

    #[test]
    fn a_tag_whose_blocks_are_all_too_narrow_is_an_error() {
        let root = scratch("narrow");
        let p = put(&root, "Polymarket_CLOB_Yes_BBO", "20261003", &hlxz(&[(27, 56, vec![vec![0; 56]])]));
        assert!(iter_poly_bbo(&p).is_err());
        assert!(blocks(&p, &tags(TAG_POLY_BBO_NS), 224).is_err());
        // A stale narrow block beside good ones is skipped.
        let rec = poly_bbo(1, 0.5, 0.51, "0x1", "9");
        let p = put(
            &root,
            "Polymarket_CLOB_No_BBO",
            "20261003",
            &hlxz(&[(27, 56, vec![vec![0; 56]]), (27, 224, vec![rec])]),
        );
        assert_eq!(iter_poly_bbo(&p).unwrap().len(), 1);
    }

    #[test]
    fn wide_tags_read_asset_wide() {
        assert_eq!(asset_of(22, 1, 0), 1);
        assert_eq!(asset_of(22, 1, 300), 1);
        assert_eq!(asset_of(43, 44, 300), 300);
        assert_eq!(asset_of(43, 1, 1), 1);
        assert_eq!(asset_of(43, 1, 0), 1, "unpopulated asset_wide falls back to the u8");
        assert_eq!(asset_of(43, 0, 0), 0);

        let root = scratch("wide");
        let day = "20261003";
        let p = put(
            &root,
            "Kalshi_Yes_BBO",
            day,
            &hlxz(&[
                (43, 64, vec![quote(0.41, 0.42, 10, 1, venue::KALSHI_BINARY_YES, 44, 300)]),
                (22, 64, vec![quote(0.43, 0.44, 20, 2, venue::KALSHI_BINARY_YES, 44, 300)]),
            ]),
        );
        let q = iter_quotes(&p, true).unwrap();
        assert_eq!(q.iter().map(|q| q.asset_id).collect::<Vec<_>>(), [300, 44]);
        assert_eq!(q[0].source, venue::KALSHI_BINARY_YES);
        let s = kalshi_quote_series(&root, "Kalshi_Yes_BBO", day, &[300, 44], Extra::default()).unwrap();
        assert_eq!((s.rows(0).len(), s.rows(1).len()), (1, 1));
        assert_eq!(s.bid[s.rows(0).start], 4100);
    }

    #[test]
    fn windows_are_15m_only_and_resolutions_keep_outcome_ws() {
        let root = scratch("windows");
        let day = "20261003";
        let open: i64 = 1_790_000_100; // on the 900 s grid
        assert_eq!(open % 900, 0);
        let mut res = Vec::new();
        res.extend(1i64.to_le_bytes());
        res.extend(open.to_le_bytes());
        res.extend(cfield("ab12", 64));
        res.extend(101.5f64.to_le_bytes());
        res.extend([0xFF, 1]);
        res.extend(0u16.to_le_bytes());
        res.extend([0u8; 4]);
        let mut decided = res.clone();
        decided[88] = 1;
        put(
            &root,
            "Polymarket_Window",
            day,
            &hlxz(&[
                (
                    46,
                    256,
                    vec![
                        window(open, "AB12", "y1", "n1", 1, 1),
                        window(open + 300, "cd", "y2", "n2", 1, 1), // 5-minute grid on a 15m slot
                        window(open, "ef", "y3", "n3", 0, 0),       // Btc5m
                    ],
                ),
                (47, 96, vec![res, decided]),
            ]),
        );
        let w = iter_windows(day, &root).unwrap();
        assert_eq!(w.len(), 1);
        assert_eq!((w[0].asset, w[0].open_unix, w[0].condition_id.as_str()), (1, open, "ab12"));
        let r = iter_resolutions(day, &root).unwrap();
        assert_eq!(r.iter().map(|r| r.outcome_ws).collect::<Vec<_>>(), [None, Some(1)]);
        assert_eq!(norm_condition_id(&r[0].condition_id), w[0].condition_id);

        let idx = token_index(&w);
        assert_eq!(idx["y1"], TokenWindow { asset: 1, open_unix: open, outcome: 0 });
        assert_eq!(idx["n1"].outcome, 1);
        assert!(!idx.contains_key("y2"));
    }

    #[test]
    fn condition_ids_compare_normalized() {
        assert_eq!(norm_condition_id("0xABcd"), norm_condition_id("abcd"));
        assert_eq!(norm_condition_id("0XAB"), "ab");
        assert_eq!(norm_condition_id("ab"), "ab");
    }

    #[test]
    fn quote_rule_aggressor() {
        assert_eq!(aggressor(0.42, 0.41, 0.42), Some('B'));
        assert_eq!(aggressor(0.43, 0.41, 0.42), Some('B'));
        assert_eq!(aggressor(0.41, 0.41, 0.42), Some('S'));
        assert_eq!(aggressor(0.42, 0.40, 0.46), Some('S'));
        assert_eq!(aggressor(0.45, 0.40, 0.46), Some('B'));
        assert_eq!(aggressor(0.5, 0.25, 0.75), None);
        assert_eq!(aggressor(0.43, 0.0, 0.46), None);
    }

    #[test]
    fn kalshi_windows_and_the_ticker_in_force() {
        let root = scratch("kalshi");
        let day = "20261003";
        let kw = |ts: i64, ticker: &str, aid: u8, wide: u16| {
            let mut r = Vec::new();
            r.extend(ts.to_le_bytes());
            r.extend(0i64.to_le_bytes());
            r.extend(cfield(ticker, 48));
            r.extend(f64::NAN.to_le_bytes());
            r.extend([aid, 0]);
            r.extend(wide.to_le_bytes());
            r.extend([0u8; 4]);
            r
        };
        put(
            &root,
            "Kalshi_Window",
            day,
            &hlxz(&[(
                48,
                80,
                vec![kw(100, "KXBTC15M-A", 1, 1), kw(200, "KXBTC15M-B", 1, 1), kw(150, "KXETH15M-A", 3, 3)],
            )]),
        );
        let w = iter_kalshi_windows(day, &root).unwrap();
        assert_eq!(w.len(), 3);
        assert!(w[0].strike.is_nan());
        assert_eq!(kalshi_ticker_at(&w, 1, 99), None);
        assert_eq!(kalshi_ticker_at(&w, 1, 150).unwrap().ticker, "KXBTC15M-A");
        assert_eq!(kalshi_ticker_at(&w, 1, 200).unwrap().ticker, "KXBTC15M-B");
        assert_eq!(kalshi_ticker_at(&w, 3, 1_000).unwrap().ticker, "KXETH15M-A");
        assert!(iter_kalshi_windows("20261004", &root).unwrap().is_empty());
    }

    #[test]
    fn kalshi_trades_carry_symbol_source_and_wide_asset() {
        let root = scratch("trades");
        let mut r = cfield("KXBTC15M", 16);
        r.push(b'B');
        r.extend([0u8; 7]);
        r.extend(0.42f64.to_le_bytes());
        r.extend(5.0f64.to_le_bytes());
        r.extend(7u64.to_le_bytes());
        r.extend(11i64.to_le_bytes());
        r.extend(12i64.to_le_bytes());
        r.extend([venue::KALSHI_BINARY_YES, 1]);
        r.extend(1u16.to_le_bytes());
        r.extend([0u8; 4]);
        let mut unknown = r.clone();
        unknown[16] = b'?';
        let p = put(&root, "Kalshi_Yes_Trade", "20261003", &hlxz(&[(45, 72, vec![r, unknown])]));
        let t = iter_trades(&p).unwrap();
        assert_eq!(t.len(), 1);
        assert_eq!((t[0].symbol.as_str(), t[0].source, t[0].asset_id, t[0].side), ("KXBTC15M", 31, 1, 'B'));
    }
}
