//! The array path (`reader.*_series`): top of book per key, one block at a
//! time, filtered before anything is kept, then deduplicated and grouped.
//!
//! Prices are `i16` ticks of 1e-4 (`PX`), so 0.60 compares as 6000 and a gap
//! is exact in integers; 0 is an empty side. Times are wall ns. Rows are
//! grouped by key, each group in time order.

use std::collections::HashMap;
use std::path::Path;

use super::container::{blocks, tags};
use super::records::{TAG_POLY_BBO_NS, TAG_QUOTE_NS, asset_of};
use super::{NS, path};

pub const PX: i64 = 10_000;

/// Times (wall ns) and values, in time order.
pub type Timeline = (Vec<i64>, Vec<f64>);
const GRID_NS: i64 = 900 * NS;

/// Which optional columns to keep.
#[derive(Clone, Copy, Debug, Default)]
pub struct Extra {
    pub recv: bool,
    pub publ: bool,
    pub seq: bool,
}

#[derive(Clone, Debug, Default)]
pub struct Series {
    pub t: Vec<i64>,
    pub bid: Vec<i16>,
    pub ask: Vec<i16>,
    pub bsz: Vec<f32>,
    pub asz: Vec<f32>,
    pub recv: Vec<i64>,
    pub publ: Vec<i64>,
    pub seq: Vec<u64>,
    pub starts: Vec<usize>,
    pub ends: Vec<usize>,
    /// Matched rows before dedup.
    pub raw_rows: usize,
}

impl Series {
    pub fn rows(&self, key: usize) -> std::ops::Range<usize> {
        self.starts[key]..self.ends[key]
    }

    pub fn len(&self) -> usize {
        self.t.len()
    }

    pub fn is_empty(&self) -> bool {
        self.t.is_empty()
    }
}

/// Rows of one block (or of the day), with their key, before grouping.
#[derive(Default)]
struct Part {
    key: Vec<i16>,
    t: Vec<i64>,
    bid: Vec<i16>,
    ask: Vec<i16>,
    bsz: Vec<f32>,
    asz: Vec<f32>,
    recv: Vec<i64>,
    publ: Vec<i64>,
    seq: Vec<u64>,
}

impl Part {
    fn len(&self) -> usize {
        self.t.len()
    }

    fn take(&self, idx: &[usize], into: &mut Part) {
        for &i in idx {
            into.key.push(self.key[i]);
            into.t.push(self.t[i]);
            into.bid.push(self.bid[i]);
            into.ask.push(self.ask[i]);
            into.bsz.push(self.bsz[i]);
            into.asz.push(self.asz[i]);
            if !self.recv.is_empty() {
                into.recv.push(self.recv[i]);
            }
            if !self.publ.is_empty() {
                into.publ.push(self.publ[i]);
            }
            if !self.seq.is_empty() {
                into.seq.push(self.seq[i]);
            }
        }
    }

    fn append(&mut self, other: Part) {
        self.key.extend(other.key);
        self.t.extend(other.t);
        self.bid.extend(other.bid);
        self.ask.extend(other.ask);
        self.bsz.extend(other.bsz);
        self.asz.extend(other.asz);
        self.recv.extend(other.recv);
        self.publ.extend(other.publ);
        self.seq.extend(other.seq);
    }

    /// Sorted by (key, t), keeping the original order of ties (numpy's two
    /// stable argsorts), and with `dedup` the rows whose touch repeats their
    /// key's previous row dropped, except the first row of each 900 s grid
    /// window ("updated since the window opened" must still see it).
    fn grouped(self, dedup: bool) -> Part {
        let mut order: Vec<usize> = (0..self.len()).collect();
        order.sort_by_key(|&i| (self.key[i], self.t[i]));
        let keep: Vec<usize> = if dedup {
            let mut keep = Vec::with_capacity(order.len());
            for (n, &i) in order.iter().enumerate() {
                if n > 0 {
                    let p = order[n - 1];
                    let same = self.t[i].div_euclid(GRID_NS) == self.t[p].div_euclid(GRID_NS)
                        && self.key[i] == self.key[p]
                        && self.bid[i] == self.bid[p]
                        && self.ask[i] == self.ask[p]
                        && self.bsz[i] == self.bsz[p]
                        && self.asz[i] == self.asz[p];
                    if same {
                        continue;
                    }
                }
                keep.push(i);
            }
            keep
        } else {
            order
        };
        let mut out = Part::default();
        self.take(&keep, &mut out);
        out
    }
}

fn finish(parts: Vec<Part>, nkeys: usize, raw_rows: usize) -> Series {
    let mut all = Part::default();
    for p in parts {
        all.append(p);
    }
    let all = all.grouped(true);
    let mut starts = vec![0; nkeys];
    let mut ends = vec![0; nkeys];
    for k in 0..nkeys {
        starts[k] = all.key.partition_point(|&x| (x as usize) < k);
        ends[k] = all.key.partition_point(|&x| (x as usize) <= k);
    }
    Series {
        t: all.t,
        bid: all.bid,
        ask: all.ask,
        bsz: all.bsz,
        asz: all.asz,
        recv: all.recv,
        publ: all.publ,
        seq: all.seq,
        starts,
        ends,
        raw_rows,
    }
}

/// `np.rint(values * (PX / scale)).astype(int16)`.
fn px(v: f64, scale: f64) -> i16 {
    (v * (PX as f64 / scale)).round_ties_even() as i16
}

fn token_key(token: &str) -> [u8; 24] {
    let mut k = [0u8; 24];
    let b = token.as_bytes();
    let n = b.len().min(24);
    k[..n].copy_from_slice(&b[..n]);
    k
}

fn i64_at(d: &[u8], o: usize) -> i64 {
    i64::from_le_bytes(d[o..o + 8].try_into().expect("8 bytes"))
}

fn f64_at(d: &[u8], o: usize) -> f64 {
    f64::from_le_bytes(d[o..o + 8].try_into().expect("8 bytes"))
}

/// `reader.poly_bbo_series`: Polymarket BBO for `tokens` only; key `k` is
/// `tokens[k]`. Tokens are matched on the first 24 bytes of the decimal id.
pub fn poly_bbo_series(
    root: &Path,
    stream: &str,
    day: &str,
    tokens: &[String],
    extra: Extra,
) -> anyhow::Result<Series> {
    let mut lut: HashMap<[u8; 24], i16> = HashMap::with_capacity(tokens.len());
    for (k, t) in tokens.iter().enumerate() {
        if lut.insert(token_key(t), k as i16).is_some() {
            anyhow::bail!("tokens must be distinct in their first 24 bytes");
        }
    }
    let mut parts = Vec::new();
    let mut raw_rows = 0;
    if tokens.is_empty() {
        return Ok(finish(parts, 0, 0));
    }
    for raw in blocks(&path(root, stream, day), &tags(TAG_POLY_BBO_NS), 224)? {
        let scale = raw.scale;
        let mut part = Part::default();
        for r in raw.records() {
            let Some(&key) = lut.get(&<[u8; 24]>::try_from(&r[136..160]).expect("24 bytes")) else {
                continue;
            };
            part.key.push(key);
            part.t.push(i64_at(r, 8) * scale);
            part.bid.push(px(i64_at(r, 24) as f64, 1e8));
            part.ask.push(px(i64_at(r, 40) as f64, 1e8));
            part.bsz.push((i64_at(r, 32) as f64 / 1e8) as f32);
            part.asz.push((i64_at(r, 48) as f64 / 1e8) as f32);
            if extra.recv {
                part.recv.push(i64_at(r, 0) * scale);
            }
            if extra.publ {
                part.publ.push(i64_at(r, 16) * scale);
            }
        }
        if part.len() == 0 {
            continue;
        }
        raw_rows += part.len();
        parts.push(part.grouped(true));
    }
    Ok(finish(parts, tokens.len(), raw_rows))
}

/// A quote record's asset under its block's tag ([`asset_of`]).
fn quote_asset(tag: u8, r: &[u8]) -> u16 {
    asset_of(tag, r[58], u16::from_le_bytes([r[60], r[61]]))
}

/// `reader.kalshi_quote_series`: Kalshi BBO for `asset_ids` (key `k` is
/// `asset_ids[k]`), `changed` records only.
pub fn kalshi_quote_series(
    root: &Path,
    stream: &str,
    day: &str,
    asset_ids: &[u16],
    extra: Extra,
) -> anyhow::Result<Series> {
    let lut: HashMap<u16, i16> = asset_ids.iter().enumerate().map(|(k, &aid)| (aid, k as i16)).collect();
    let mut parts = Vec::new();
    let mut raw_rows = 0;
    for raw in blocks(&path(root, stream, day), &tags(TAG_QUOTE_NS), 64)? {
        let scale = raw.scale;
        let mut part = Part::default();
        for r in raw.records() {
            if r[59] != 1 {
                continue;
            }
            let Some(&key) = lut.get(&quote_asset(raw.tag, r)) else { continue };
            part.key.push(key);
            part.t.push(i64_at(r, 40) * scale);
            part.bid.push(px(f64_at(r, 0), 1.0));
            part.ask.push(px(f64_at(r, 8), 1.0));
            part.bsz.push(f64_at(r, 16) as f32);
            part.asz.push(f64_at(r, 24) as f32);
            if extra.recv {
                part.recv.push(i64_at(r, 32) * scale);
            }
            if extra.seq {
                part.seq.push(u64::from_le_bytes(r[48..56].try_into().expect("8 bytes")));
            }
        }
        if part.len() == 0 {
            continue;
        }
        raw_rows += part.len();
        parts.push(part.grouped(true));
    }
    Ok(finish(parts, asset_ids.len(), raw_rows))
}

/// `reader.chainlink_prices`: the Chainlink price per *Spot asset id, `(t, price)`
/// in time order. Empty when the day has no Chainlink stream.
pub fn chainlink_prices(root: &Path, day: &str, asset_ids: &[u16]) -> anyhow::Result<HashMap<u16, Timeline>> {
    let p = path(root, "PolymarketChainlink_BBO", day);
    let mut out: HashMap<u16, Vec<(i64, f64)>> = HashMap::new();
    if !p.exists() {
        return Ok(HashMap::new());
    }
    for raw in blocks(&p, &tags(TAG_QUOTE_NS), 64)? {
        for r in raw.records() {
            let aid = quote_asset(raw.tag, r);
            if asset_ids.contains(&aid) {
                out.entry(aid).or_default().push((i64_at(r, 40) * raw.scale, f64_at(r, 0)));
            }
        }
    }
    Ok(out
        .into_iter()
        .map(|(aid, mut rows)| {
            rows.sort_by_key(|r| r.0);
            (aid, rows.into_iter().unzip())
        })
        .collect())
}

/// `reader.kalshi_contract_start`: index of the first row of the window's own
/// Kalshi contract in one asset's rows. The stream keeps the old contract's
/// terminal book for a few seconds past the open, then the new contract's
/// `seq` restarts. A break is `seq` going down or jumping by more than 50,000;
/// the break nearest the open within [open − 10 s, open + 60 s] is the
/// switch, else the first row at or after the open.
pub fn kalshi_contract_start(t: &[i64], seq: &[u64], open_ns: i64) -> usize {
    let lo = t.partition_point(|&x| x < open_ns - 10 * NS);
    let hi = t.partition_point(|&x| x < open_ns + 60 * NS);
    let at_open = t.partition_point(|&x| x < open_ns);
    if hi.saturating_sub(lo) < 2 {
        return at_open;
    }
    let mut best: Option<(i64, usize)> = None;
    for j in lo + 1..hi {
        let d = seq[j] as i64 - seq[j - 1] as i64;
        if !(0..=50_000).contains(&d) {
            let dist = (t[j] - open_ns).abs();
            if best.is_none_or(|(b, _)| dist < b) {
                best = Some((dist, j));
            }
        }
    }
    best.map(|(_, j)| j).unwrap_or(at_open)
}

/// `np.searchsorted(a, v, side="right") - 1`: the row in force at `v`, if any.
pub fn asof(a: &[i64], v: i64) -> Option<usize> {
    a.partition_point(|&x| x <= v).checked_sub(1)
}

/// `np.interp` on float64, as numpy computes it.
pub fn interp(x: f64, xp: &[f64], fp: &[f64]) -> f64 {
    let n = xp.len();
    if n == 0 {
        return f64::NAN;
    }
    if x < xp[0] {
        return fp[0];
    }
    if x > xp[n - 1] {
        return fp[n - 1];
    }
    if n == 1 {
        return fp[0];
    }
    let j = xp.partition_point(|&v| v <= x).saturating_sub(1);
    if j >= n - 1 {
        return fp[n - 1];
    }
    if xp[j] == x {
        return fp[j];
    }
    let slope = (fp[j + 1] - fp[j]) / (xp[j + 1] - xp[j]);
    let r = slope * (x - xp[j]) + fp[j];
    if r.is_nan() { slope * (x - xp[j + 1]) + fp[j + 1] } else { r }
}

/// The receive-to-wall mapping of a day: Polymarket YES rows sorted by
/// monotonic receive time, every `len / 100_000`th, and wall − recv at each.
/// Monotonic times (trades, books) map to wall through it.
pub struct MonoToWall {
    mono: Vec<f64>,
    off: Vec<f64>,
}

impl MonoToWall {
    pub fn new(s: &Series, target: usize) -> Self {
        let mut order: Vec<usize> = (0..s.recv.len()).collect();
        order.sort_by_key(|&i| s.recv[i]);
        let step = (s.recv.len() / target).max(1);
        let picks: Vec<usize> = order.into_iter().step_by(step).collect();
        Self {
            mono: picks.iter().map(|&i| s.recv[i] as f64).collect(),
            off: picks.iter().map(|&i| (s.t[i] - s.recv[i]) as f64).collect(),
        }
    }

    /// `recv + np.interp(recv, mono, off).astype(int64)`.
    pub fn wall(&self, recv: i64) -> i64 {
        recv + interp(recv as f64, &self.mono, &self.off) as i64
    }

    pub fn offset(&self, recv: i64) -> f64 {
        interp(recv as f64, &self.mono, &self.off)
    }
}
