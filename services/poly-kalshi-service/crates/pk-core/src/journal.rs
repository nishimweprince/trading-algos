//! The journal: `<dir>/YYYY-MM-DD.jsonl` by each record's event time, one JSON
//! object per line with a `type`. The day's file stays open and every line is
//! flushed, so a reader sees it at once and a crash loses nothing written. A
//! write error is counted and never stops the engine.

use std::fs::File;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

use chrono::{DateTime, Utc};
use serde_json::Value;

const DAY_NS: i64 = 86_400 * 1_000_000_000;

pub trait Sink {
    fn write(&mut self, t_ns: i64, record: &Value);
}

/// Discards everything.
pub struct NullSink;

impl Sink for NullSink {
    fn write(&mut self, _t_ns: i64, _record: &Value) {}
}

/// Keeps every record in memory (tests, replay into research).
#[derive(Default)]
pub struct VecSink(pub Vec<Value>);

impl Sink for VecSink {
    fn write(&mut self, _t_ns: i64, record: &Value) {
        self.0.push(record.clone());
    }
}

pub struct JsonlDaily {
    dir: PathBuf,
    pub errors: u64,
    day_end_ns: i64,
    file: Option<File>,
}

impl JsonlDaily {
    pub fn new(dir: impl Into<PathBuf>) -> Self {
        Self { dir: dir.into(), errors: 0, day_end_ns: 0, file: None }
    }

    fn open_day(&mut self, t_ns: i64) -> std::io::Result<()> {
        let stamp = DateTime::<Utc>::from_timestamp_nanos(t_ns);
        let day = stamp.format("%Y-%m-%d").to_string();
        self.day_end_ns = t_ns.div_euclid(DAY_NS) * DAY_NS + DAY_NS;
        std::fs::create_dir_all(&self.dir)?;
        self.file =
            Some(File::options().create(true).append(true).open(self.dir.join(format!("{day}.jsonl")))?);
        Ok(())
    }

    pub fn write_line(&mut self, t_ns: i64, line: &[u8]) {
        let r = (|| -> std::io::Result<()> {
            if self.file.is_none() || t_ns >= self.day_end_ns || t_ns < self.day_end_ns - DAY_NS {
                self.open_day(t_ns)?;
            }
            let f = self.file.as_mut().expect("opened");
            let mut buf = Vec::with_capacity(line.len() + 1);
            buf.extend_from_slice(line);
            buf.push(b'\n');
            f.write_all(&buf)
        })();
        if r.is_err() {
            self.errors += 1;
            self.file = None;
        }
    }
}

impl Sink for JsonlDaily {
    fn write(&mut self, t_ns: i64, record: &Value) {
        match serde_json::to_vec(record) {
            Ok(line) => self.write_line(t_ns, &line),
            Err(_) => self.errors += 1,
        }
    }
}

/// Every record under `dir`, file by file in name (= date) order.
pub fn read_journal(dir: &Path) -> anyhow::Result<Vec<Value>> {
    let mut files: Vec<PathBuf> = std::fs::read_dir(dir)?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().is_some_and(|x| x == "jsonl"))
        .collect();
    files.sort();
    let mut out = Vec::new();
    for path in files {
        for line in BufReader::new(File::open(&path)?).lines() {
            let line = line?;
            if !line.trim().is_empty() {
                out.push(serde_json::from_str(&line)?);
            }
        }
    }
    Ok(out)
}

/// Records of one results epoch, and how many others were set aside. `None`
/// picks the latest epoch present; a journal with no epochs (a replay) is
/// returned whole.
pub fn in_epoch(records: Vec<Value>, epoch: Option<&str>) -> (Vec<Value>, usize) {
    let epoch_of = |r: &Value| match r.get("epoch") {
        Some(Value::String(s)) => Some(s.clone()),
        Some(Value::Number(n)) => Some(n.to_string()),
        _ => None,
    };
    let epoch = match epoch {
        Some(e) => e.to_owned(),
        None => match records.iter().rev().find_map(epoch_of) {
            Some(e) => e,
            None => return (records, 0),
        },
    };
    let total = records.len();
    let kept: Vec<Value> =
        records.into_iter().filter(|r| epoch_of(r).as_deref() == Some(epoch.as_str())).collect();
    let set_aside = total - kept.len();
    (kept, set_aside)
}
