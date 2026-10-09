//! Reading tapes: `(recv_ns, venue, text)` in file order.
//!
//! File order is arrival order, so no merge is needed. A file that ends
//! without its compression trailer (a crash, or the hour still being written)
//! is read up to the break and counted in `truncated`. A line older than its
//! predecessor is counted in `out_of_order`, never silently re-sorted.

use std::fs::File;
use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};

#[derive(Debug, Default, Clone, serde::Serialize)]
pub struct LineStats {
    pub lines: u64,
    pub bad: u64,
    pub out_of_order: u64,
    pub truncated: u64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct TapeLine {
    pub recv_ns: i64,
    pub venue: char,
    pub text: String,
}

/// Hour files under `root` (recursively) in time order; `start`/`end` as
/// `YYYYMMDD[_HH]`, inclusive. Names sort by hour, then by session.
pub fn iter_files(root: &Path, start: Option<&str>, end: Option<&str>) -> Vec<PathBuf> {
    fn walk(dir: &Path, out: &mut Vec<PathBuf>) {
        let Ok(entries) = std::fs::read_dir(dir) else { return };
        for e in entries.flatten() {
            let p = e.path();
            if p.is_dir() {
                walk(&p, out);
            } else if let Some(name) = p.file_name().and_then(|n| n.to_str())
                && name.starts_with("pk_")
                && (name.ends_with(".gz") || name.ends_with(".zst"))
            {
                out.push(p);
            }
        }
    }
    let mut files = Vec::new();
    walk(root, &mut files);
    let name = |p: &PathBuf| p.file_name().and_then(|n| n.to_str()).unwrap_or_default().to_owned();
    files.sort_by_key(name);
    files.retain(|p| {
        let n = name(p);
        let key = &n[3..];
        start.is_none_or(|s| key.get(..s.len()).is_some_and(|k| k >= s))
            && end.is_none_or(|e| key.get(..e.len()).is_some_and(|k| k <= e))
    });
    files
}

fn open(path: &Path) -> std::io::Result<Box<dyn Read>> {
    let f = File::open(path)?;
    if path.extension().is_some_and(|e| e == "zst") {
        Ok(Box::new(zstd::stream::read::Decoder::new(f)?))
    } else {
        Ok(Box::new(flate2::read::MultiGzDecoder::new(f)))
    }
}

/// Streams every line of a list of files, in order. A file that cannot be
/// opened is skipped with a warning; a corrupt or truncated tail ends that
/// file and is counted.
pub struct TapeReader {
    paths: Vec<PathBuf>,
    next_path: usize,
    current: Option<BufReader<Box<dyn Read>>>,
    buf: Vec<u8>,
    last: i64,
    pub stats: LineStats,
}

impl TapeReader {
    pub fn new(paths: Vec<PathBuf>) -> Self {
        Self {
            paths,
            next_path: 0,
            current: None,
            buf: Vec::with_capacity(4096),
            last: i64::MIN,
            stats: LineStats::default(),
        }
    }
}

impl Iterator for TapeReader {
    type Item = TapeLine;

    fn next(&mut self) -> Option<TapeLine> {
        loop {
            if self.current.is_none() {
                let path = self.paths.get(self.next_path)?.clone();
                self.next_path += 1;
                match open(&path) {
                    Ok(r) => self.current = Some(BufReader::with_capacity(1 << 20, r)),
                    Err(e) => {
                        tracing::warn!(path = %path.display(), error = %e, "tape unreadable");
                        continue;
                    }
                }
            }
            let reader = self.current.as_mut().expect("set above");
            self.buf.clear();
            match reader.read_until(b'\n', &mut self.buf) {
                Ok(0) => {
                    self.current = None;
                    continue;
                }
                Err(_) => {
                    // A crash, or the file still being written: keep what is there.
                    self.stats.truncated += 1;
                    self.current = None;
                    continue;
                }
                Ok(_) => {}
            }
            if self.buf.last() != Some(&b'\n') {
                self.stats.bad += 1; // a half-flushed last line
                continue;
            }
            let Some(line) = parse(&self.buf[..self.buf.len() - 1]) else {
                self.stats.bad += 1;
                continue;
            };
            if line.recv_ns < self.last {
                self.stats.out_of_order += 1;
            }
            self.last = self.last.max(line.recv_ns);
            self.stats.lines += 1;
            return Some(line);
        }
    }
}

/// Every line of `paths`, in order (see [`TapeReader`]).
pub fn read_lines(paths: Vec<PathBuf>) -> TapeReader {
    TapeReader::new(paths)
}

/// `<recv_ns> <venue> <text>`.
pub fn parse(bytes: &[u8]) -> Option<TapeLine> {
    let s = std::str::from_utf8(bytes).ok()?;
    let (ns, rest) = s.split_once(' ')?;
    if ns.is_empty() || !ns.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let (venue, text) = rest.split_once(' ')?;
    let mut chars = venue.chars();
    let v = chars.next()?;
    if chars.next().is_some() {
        return None;
    }
    Some(TapeLine { recv_ns: ns.parse().ok()?, venue: v, text: text.to_owned() })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_lines() {
        assert_eq!(
            parse(b"123 p {\"a\": 1}"),
            Some(TapeLine { recv_ns: 123, venue: 'p', text: "{\"a\": 1}".into() })
        );
        assert_eq!(parse(b"x p {}"), None);
        assert_eq!(parse(b"12 pp {}"), None);
        assert_eq!(parse(b"12 p"), None);
    }
}
