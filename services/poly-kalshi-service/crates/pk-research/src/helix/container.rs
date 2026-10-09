//! The `.hlxz` container: Helix's `recv3.iter_records` and `reader.iter_blocks`.
//!
//! A v1 file is `[FileHeader 32 B][Block]…[FooterIndex?][FooterTrailer?]`, and
//! a recorder restart appends another FileHeader (and its blocks) to the same
//! day file. A block is a 40 B header and one zstd frame of same-tag,
//! same-size records. The walk is inline, from byte 32:
//!
//! - a FileHeader magic: skip its 32 B (the next sub-file);
//! - not a block magic (a footer, or damage): resync at the next FileHeader, or stop;
//! - a block running past the end (a partial tail): stop.
//!
//! The footer index is never needed: on a single sub-file it lists the same
//! blocks the walk finds, and a day rolled off at midnight gets its footer
//! written a second or two late. Blocks that do not decompress are skipped.
//!
//! Record sizes come from each block header, never from a table: payloads are
//! frozen-append, so a tag's record can grow (`OrderEvent` went 56 → 64 B).
//! A reader names the smallest size it can decode (`min_rsz`, its last field's
//! end), takes every block at least that wide, and splits each block by its
//! own size. A block narrower than that (an older layout) is skipped; when
//! every block of a wanted tag is narrower, the read fails rather than return
//! zero rows from a populated tag.
//!
//! The 2026-06 recorder wrote a different format: the whole file zstd
//! compressed, holding `HLXF` frames (24 B header, then the records). The
//! typed readers read it; the block readers, like Python's, do not.

use std::fs::File;
use std::ops::Range;
use std::path::Path;

pub const FILE_MAGIC: u32 = 0x3158_4C48;
pub const BLK_MAGIC: u32 = 0x4258_4C48;
pub const FOOT_MAGIC: u64 = 0x3154_4F4F_4658_4C48;
pub const FOOTER_PRESENT: u32 = 0x1;
const FRAME_MAGIC: u32 = 0x484C_5846;
const ZSTD_MAGIC: [u8; 4] = [0x28, 0xb5, 0x2f, 0xfd];

/// Canonical (ns) tag → (legacy tag, scale to ns): `recv3._DUAL_TAG`.
pub fn legacy_of(canonical: u8) -> Option<(u8, i64)> {
    Some(match canonical {
        22 => (15, 1000),
        23 => (2, 1000),
        24 => (3, 1000),
        25 => (12, 1000),
        26 => (13, 1000),
        27 => (14, 1000),
        28 => (16, 1000),
        29 => (17, 1000),
        30 => (19, 1000),
        31 => (10, 1000),
        32 => (7, 1000),
        33 => (8, 1000),
        34 => (18, 1000),
        _ => return None,
    })
}

/// The V4 "wide" tags (from 2026-08-14): same payload as their canonical
/// twin, and the u16 `asset_wide` is the authoritative asset id.
pub fn is_wide(tag: u8) -> bool {
    (43..=48).contains(&tag)
}

/// Canonical tag → its V4 wide twin (same payload, ns): `recv3._WIDE_TAG`.
pub fn wide_of(canonical: u8) -> Option<u8> {
    Some(match canonical {
        22 => 43,
        23 => 44,
        24 => 45,
        32 => 46,
        33 => 47,
        34 => 48,
        _ => return None,
    })
}

/// `reader._tags`: every tag a canonical tag's records are written under, with
/// the scale that brings its times to ns.
pub fn tags(canonical: u8) -> Vec<(u8, i64)> {
    let mut out = vec![(canonical, 1)];
    if let Some(l) = legacy_of(canonical) {
        out.push(l);
    }
    if let Some(w) = wide_of(canonical) {
        out.push((w, 1));
    }
    out
}

/// A day file's bytes: memory-mapped, so a multi-GB day is paged, not copied.
pub enum Bytes {
    Map(memmap2::Mmap),
    Vec(Vec<u8>),
}

impl std::ops::Deref for Bytes {
    type Target = [u8];
    fn deref(&self) -> &[u8] {
        match self {
            Bytes::Map(m) => m,
            Bytes::Vec(v) => v,
        }
    }
}

pub fn open(path: &Path) -> std::io::Result<Bytes> {
    let f = File::open(path)?;
    if f.metadata()?.len() == 0 {
        return Ok(Bytes::Vec(Vec::new()));
    }
    // Safety: the file is opened read-only and only read through this map. A
    // recorder appending to today's file only grows it past the mapped length.
    #[allow(unsafe_code)]
    let map = unsafe { memmap2::Mmap::map(&f) };
    match map {
        Ok(m) => Ok(Bytes::Map(m)),
        Err(_) => std::fs::read(path).map(Bytes::Vec),
    }
}

fn u32_at(d: &[u8], off: usize) -> u32 {
    u32::from_le_bytes(d[off..off + 4].try_into().expect("4 bytes"))
}

fn u64_at(d: &[u8], off: usize) -> u64 {
    u64::from_le_bytes(d[off..off + 8].try_into().expect("8 bytes"))
}

fn i64_at(d: &[u8], off: usize) -> i64 {
    i64::from_le_bytes(d[off..off + 8].try_into().expect("8 bytes"))
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FileHeader {
    pub magic: u32,
    pub version: u16,
    pub source: u16,
    pub flags: u32,
}

pub fn file_header(d: &[u8]) -> Option<FileHeader> {
    (d.len() >= 32).then(|| FileHeader {
        magic: u32_at(d, 0),
        version: u16::from_le_bytes([d[4], d[5]]),
        source: u16::from_le_bytes([d[8], d[9]]),
        flags: u32_at(d, 20),
    })
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Block {
    pub offset: usize,
    pub tag: u8,
    pub rsz: usize,
    pub count: usize,
    pub uncomp: usize,
    pub first_ts: i64,
    pub last_ts: i64,
    pub body: Range<usize>,
}

/// Every block header in file order, and the number of sub-files.
pub fn walk(d: &[u8]) -> (Vec<Block>, usize) {
    let mut out = Vec::new();
    let mut subs = 1;
    let (mut off, n) = (32usize, d.len());
    let file_magic = FILE_MAGIC.to_le_bytes();
    while off + 40 <= n {
        if u32_at(d, off) == FILE_MAGIC {
            off += 32;
            subs += 1;
            continue;
        }
        if u32_at(d, off) != BLK_MAGIC {
            match find(&d[off + 4..], &file_magic) {
                Some(i) => {
                    off = off + 4 + i;
                    continue;
                }
                None => break,
            }
        }
        let comp = u32_at(d, off + 12) as usize;
        let body = off + 40;
        if body + comp > n {
            break; // partial tail
        }
        out.push(Block {
            offset: off,
            tag: d[off + 4],
            rsz: u16::from_le_bytes([d[off + 6], d[off + 7]]) as usize,
            count: u32_at(d, off + 8) as usize,
            uncomp: u32_at(d, off + 16) as usize,
            first_ts: i64_at(d, off + 20),
            last_ts: i64_at(d, off + 28),
            body: body..body + comp,
        });
        off = body + comp;
    }
    (out, subs)
}

fn find(hay: &[u8], needle: &[u8]) -> Option<usize> {
    hay.windows(needle.len()).position(|w| w == needle)
}

/// A block's records (`count × rsz` bytes), or None if its frame does not decode.
pub fn decompress(d: &[u8], b: &Block) -> Option<Vec<u8>> {
    let mut raw = zstd::bulk::decompress(&d[b.body.clone()], b.uncomp).ok()?;
    raw.truncate(b.count * b.rsz);
    Some(raw)
}

/// The blocks of `wanted` tags at least `min_rsz` wide. Fails when blocks of
/// a wanted tag exist and every one of them is narrower (a reader bug or a
/// layout this reader does not know, never an empty stream).
fn select(path: &Path, list: Vec<Block>, wanted: &[u8], min_rsz: usize) -> anyhow::Result<Vec<Block>> {
    let mut keep = Vec::new();
    let mut narrow: Option<(u8, usize)> = None;
    let mut ok_tags = Vec::new();
    for b in list {
        if !wanted.contains(&b.tag) {
            continue;
        }
        if b.rsz >= min_rsz && b.rsz > 0 {
            ok_tags.push(b.tag);
            keep.push(b);
        } else {
            narrow.get_or_insert((b.tag, b.rsz));
        }
    }
    if keep.is_empty()
        && let Some((tag, rsz)) = narrow
    {
        anyhow::bail!(
            "{}: every block of tag {tag} is {rsz} B, narrower than the {min_rsz} B this reader decodes",
            path.display()
        );
    }
    Ok(keep)
}

/// One decompressed block: its records (`count × rsz` bytes), the time scale
/// of its tag, the tag, and the record size from its header.
#[derive(Clone, Debug)]
pub struct Raw {
    pub data: Vec<u8>,
    pub scale: i64,
    pub tag: u8,
    pub rsz: usize,
}

impl Raw {
    /// The records, each `rsz` bytes (at least the reader's `min_rsz`).
    pub fn records(&self) -> std::slice::ChunksExact<'_, u8> {
        self.data.chunks_exact(self.rsz)
    }
}

/// `reader.iter_blocks`: each decompressed block whose tag is in `tags` and
/// whose record size is at least `min_rsz`, with the tag's time scale, in
/// file order. Errors on a file that is not a v1 container.
pub fn blocks(path: &Path, tags: &[(u8, i64)], min_rsz: usize) -> anyhow::Result<BlockIter> {
    let data = open(path)?;
    if data.len() >= 32 {
        let h = file_header(&data).expect("32 bytes");
        if h.magic != FILE_MAGIC || h.version != 1 {
            anyhow::bail!("{}: not a v1 .hlxz container", path.display());
        }
    }
    let list = if data.len() < 32 { Vec::new() } else { walk(&data).0 };
    let wanted: Vec<u8> = tags.iter().map(|(t, _)| *t).collect();
    let list = select(path, list, &wanted, min_rsz)?;
    Ok(BlockIter { data, blocks: list, i: 0, tags: tags.to_vec() })
}

pub struct BlockIter {
    data: Bytes,
    blocks: Vec<Block>,
    i: usize,
    tags: Vec<(u8, i64)>,
}

impl Iterator for BlockIter {
    type Item = Raw;
    fn next(&mut self) -> Option<Self::Item> {
        while self.i < self.blocks.len() {
            let b = &self.blocks[self.i];
            self.i += 1;
            if let Some(data) = decompress(&self.data, b) {
                let scale = self.tags.iter().find(|(t, _)| *t == b.tag).map(|(_, s)| *s).unwrap_or(1);
                return Some(Raw { data, scale, tag: b.tag, rsz: b.rsz });
            }
        }
        None
    }
}

/// `recv3.iter_records(path, tag, rsz)`: one tag's records, whole (each as
/// wide as its block says, at least `min_rsz`), in file order. Reads the HLXF
/// frame-stream format too.
pub fn records(path: &Path, tag: u8, min_rsz: usize) -> anyhow::Result<Vec<Vec<u8>>> {
    let data = open(path)?;
    let mut out = Vec::new();
    if data.len() < 8 {
        return Ok(out);
    }
    if data[..4] == ZSTD_MAGIC {
        let body = decompress_all(&data);
        if body.len() >= 4 && u32_at(&body, 0) == FRAME_MAGIC {
            hlxf_records(&body, tag, min_rsz, &mut out);
        }
        return Ok(out);
    }
    let Some(h) = file_header(&data) else { return Ok(out) };
    if h.magic != FILE_MAGIC || h.version != 1 {
        return Ok(out);
    }
    for b in select(path, walk(&data).0, &[tag], min_rsz)? {
        if let Some(raw) = decompress(&data, &b) {
            out.extend(raw.chunks_exact(b.rsz).map(<[u8]>::to_vec));
        }
    }
    Ok(out)
}

/// `recv3._iter_dual_tag`: the W twin's records, then the canonical tag's, then
/// the legacy tag's (three passes, as Python reads them), each with its scale
/// and the tag it was written under (which decides the asset field).
pub fn dual(path: &Path, canonical: u8, min_rsz: usize) -> anyhow::Result<Vec<(Vec<u8>, i64, u8)>> {
    let mut out = Vec::new();
    if let Some(w) = wide_of(canonical) {
        out.extend(records(path, w, min_rsz)?.into_iter().map(|r| (r, 1, w)));
    }
    out.extend(records(path, canonical, min_rsz)?.into_iter().map(|r| (r, 1, canonical)));
    if let Some((legacy, scale)) = legacy_of(canonical) {
        out.extend(records(path, legacy, min_rsz)?.into_iter().map(|r| (r, scale, legacy)));
    }
    Ok(out)
}

/// A zstd body of one or many frames; a damaged or partial last frame keeps
/// what decoded before it.
fn decompress_all(data: &[u8]) -> Vec<u8> {
    use std::io::Read;
    let mut out = Vec::new();
    let Ok(mut dec) = zstd::stream::read::Decoder::new(data) else { return out };
    let mut buf = vec![0u8; 1 << 16];
    loop {
        match dec.read(&mut buf) {
            Ok(0) | Err(_) => break,
            Ok(n) => out.extend_from_slice(&buf[..n]),
        }
    }
    out
}

fn hlxf_records(d: &[u8], tag: u8, min_rsz: usize, out: &mut Vec<Vec<u8>>) {
    let (mut off, n) = (0usize, d.len());
    while off + 24 <= n {
        let (magic, ftag) = (u32_at(d, off), d[off + 6]);
        let (count, rsz) = (u32_at(d, off + 8) as usize, u32_at(d, off + 12) as usize);
        if magic != FRAME_MAGIC || rsz == 0 {
            break;
        }
        let body = off + 24;
        let end = body + count * rsz;
        if end > n {
            break;
        }
        if ftag == tag && rsz >= min_rsz {
            out.extend(d[body..end].chunks_exact(rsz).map(<[u8]>::to_vec));
        }
        off = end;
    }
}

/// The 24 B `FooterTrailer`: `u64 magic, u32 entries, u32 index_len, u64 total_records`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Trailer {
    pub entries: u32,
    pub index_len: u32,
    pub total_records: u64,
}

/// The footer trailer, if the file has one. A day file read just after its
/// midnight roll may not have one yet: the footer is for seeking only.
pub fn trailer(d: &[u8]) -> Option<Trailer> {
    if d.len() < 32 + 24 {
        return None;
    }
    let t = d.len() - 24;
    (u64_at(d, t) == FOOT_MAGIC).then(|| Trailer {
        entries: u32_at(d, t + 8),
        index_len: u32_at(d, t + 12),
        total_records: u64_at(d, t + 16),
    })
}

/// One footer index entry: (offset, comp, count, tag, first, last).
pub fn index_entry(d: &[u8], off: usize) -> (u64, u32, u32, u8, i64, i64) {
    (
        u64_at(d, off),
        u32_at(d, off + 8),
        u32_at(d, off + 12),
        d[off + 16],
        i64_at(d, off + 20),
        i64_at(d, off + 28),
    )
}
