//! The recorder: lines in on any thread, hourly zstd files out on its own.
//!
//! `write` never blocks and never fails: a full queue drops the line and counts
//! it. The writer thread batches, compresses (zstd level 1), flushes every
//! second, rotates on the hour (writing the manifest) and guards the disk:
//! below `min_free_bytes` it closes its file and skips lines until free space
//! is back above 120% of the floor.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use chrono::{DateTime, Utc};
use crossbeam_channel::{Receiver, Sender, TrySendError, bounded};
use serde::{Deserialize, Serialize};

const HOUR_NS: i64 = 3600 * 1_000_000_000;
const FLUSH_EVERY: Duration = Duration::from_secs(1);
const DISK_CHECK_EVERY: Duration = Duration::from_secs(10);
const RESUME_HEADROOM: f64 = 1.2;
const LEVEL: i32 = 1;

enum Msg {
    Line(i64, char, String),
    Gap,
    Stop,
}

#[derive(Default)]
struct Shared {
    lines_written: AtomicU64,
    dropped: AtomicU64,
    errors: AtomicU64,
    files_closed: AtomicU64,
    skipped_for_disk: AtomicU64,
    disk_free_bytes: AtomicU64,
    disk_paused: AtomicBool,
    last_error: parking_lot_free::Slot,
    open_file: parking_lot_free::Slot,
}

/// A tiny mutex-protected string slot, so this crate needs no lock crate.
mod parking_lot_free {
    use std::sync::Mutex;

    #[derive(Default)]
    pub struct Slot(Mutex<Option<String>>);

    impl Slot {
        pub fn set(&self, v: Option<String>) {
            if let Ok(mut g) = self.0.lock() {
                *g = v;
            }
        }
        pub fn get(&self) -> Option<String> {
            self.0.lock().ok().and_then(|g| g.clone())
        }
    }
}

#[derive(Debug, Serialize)]
pub struct RecorderStats {
    pub enabled: bool,
    pub root: String,
    pub lines_written: u64,
    pub queue_depth: usize,
    pub dropped: u64,
    pub errors: u64,
    pub last_error: Option<String>,
    pub files_closed: u64,
    pub disk_free_gb: Option<f64>,
    pub min_free_gb: f64,
    pub disk_paused: bool,
    pub skipped_for_disk: u64,
    pub open_files: Vec<String>,
    pub compressor: &'static str,
}

#[derive(Debug, Default, Serialize, Deserialize)]
pub struct Manifest {
    pub hour: String,
    pub host: String,
    pub lines: u64,
    pub streams: BTreeMap<String, u64>,
    pub first_recv_ns: Option<i64>,
    pub last_recv_ns: Option<i64>,
    pub gaps: u64,
    pub sessions: u64,
    #[serde(default)]
    pub format: String,
}

pub struct Recorder {
    root: PathBuf,
    min_free_bytes: u64,
    tx: Sender<Msg>,
    shared: Arc<Shared>,
    thread: Option<JoinHandle<()>>,
}

pub fn hour_path(root: &Path, recv_ns: i64, session_ns: i64) -> (PathBuf, String) {
    let t = DateTime::<Utc>::from_timestamp_nanos(recv_ns);
    let day = t.format("%Y%m%d").to_string();
    let hour = t.format("%H").to_string();
    (root.join(&day).join(format!("pk_{day}_{hour}_{session_ns:020}.zst")), format!("{day}T{hour}"))
}

impl Recorder {
    pub fn start(
        root: impl Into<PathBuf>,
        host_tag: &str,
        min_free_bytes: u64,
        queue: usize,
    ) -> std::io::Result<Self> {
        let root = root.into();
        std::fs::create_dir_all(&root)?;
        let (tx, rx) = bounded(queue);
        let shared = Arc::new(Shared::default());
        let session_ns = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as i64)
            .unwrap_or(0);
        let mut w = Writer {
            root: root.clone(),
            host: host_tag.to_owned(),
            session_ns,
            min_free_bytes,
            shared: shared.clone(),
            open: None,
        };
        let thread = std::thread::Builder::new().name("pk-recorder".into()).spawn(move || w.run(rx))?;
        Ok(Self { root, min_free_bytes, tx, shared, thread: Some(thread) })
    }

    /// Never blocks, never fails: a full queue drops the line and counts it.
    pub fn write(&self, venue: char, recv_ns: i64, text: &str) {
        if let Err(TrySendError::Full(_) | TrySendError::Disconnected(_)) =
            self.tx.try_send(Msg::Line(recv_ns, venue, text.to_owned()))
        {
            self.shared.dropped.fetch_add(1, Ordering::Relaxed);
        }
    }

    /// Lines the tape is missing (the observer fell behind): counted in the manifest.
    pub fn note_gap(&self) {
        let _ = self.tx.try_send(Msg::Gap);
    }

    /// Close the file (manifest written) and join the writer.
    pub fn stop(&mut self) {
        let _ = self.tx.send(Msg::Stop);
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }

    pub fn stats(&self) -> RecorderStats {
        let free = self.shared.disk_free_bytes.load(Ordering::Relaxed);
        RecorderStats {
            enabled: true,
            root: self.root.display().to_string(),
            lines_written: self.shared.lines_written.load(Ordering::Relaxed),
            queue_depth: self.tx.len(),
            dropped: self.shared.dropped.load(Ordering::Relaxed),
            errors: self.shared.errors.load(Ordering::Relaxed),
            last_error: self.shared.last_error.get(),
            files_closed: self.shared.files_closed.load(Ordering::Relaxed),
            disk_free_gb: (free > 0).then(|| (free as f64 / 1e7).round() / 100.0),
            min_free_gb: (self.min_free_bytes as f64 / 1e7).round() / 100.0,
            disk_paused: self.shared.disk_paused.load(Ordering::Relaxed),
            skipped_for_disk: self.shared.skipped_for_disk.load(Ordering::Relaxed),
            open_files: self.shared.open_file.get().into_iter().collect(),
            compressor: "zstd-1",
        }
    }
}

impl Drop for Recorder {
    fn drop(&mut self) {
        self.stop();
    }
}

struct Open {
    path: PathBuf,
    hour: String,
    hour_start_ns: i64,
    enc: zstd::stream::write::Encoder<'static, File>,
    lines: u64,
    streams: BTreeMap<String, u64>,
    first_ns: Option<i64>,
    last_ns: Option<i64>,
    gaps: u64,
}

struct Writer {
    root: PathBuf,
    host: String,
    session_ns: i64,
    min_free_bytes: u64,
    shared: Arc<Shared>,
    open: Option<Open>,
}

impl Writer {
    fn error(&self, e: impl std::fmt::Display) {
        self.shared.errors.fetch_add(1, Ordering::Relaxed);
        let msg = format!("{e}");
        tracing::error!(error = %msg, "recorder_error");
        self.shared.last_error.set(Some(msg.chars().take(200).collect()));
    }

    fn run(&mut self, rx: Receiver<Msg>) {
        let mut last_flush = Instant::now();
        let mut last_disk = Instant::now() - DISK_CHECK_EVERY;
        loop {
            let first = rx.recv_timeout(Duration::from_millis(250));
            let mut stop = false;
            let mut handle = |w: &mut Writer, m: Msg| match m {
                Msg::Line(ns, v, t) => w.line(ns, v, &t),
                Msg::Gap => {
                    if let Some(o) = w.open.as_mut() {
                        o.gaps += 1;
                    }
                }
                Msg::Stop => stop = true,
            };
            match first {
                Ok(m) => {
                    handle(self, m);
                    for m in rx.try_iter().take(8192) {
                        handle(self, m);
                    }
                }
                Err(crossbeam_channel::RecvTimeoutError::Timeout) => {}
                Err(crossbeam_channel::RecvTimeoutError::Disconnected) => stop = true,
            }
            if last_disk.elapsed() >= DISK_CHECK_EVERY {
                self.check_disk();
                last_disk = Instant::now();
            }
            if last_flush.elapsed() >= FLUSH_EVERY {
                if let Some(o) = self.open.as_mut()
                    && let Err(e) = o.enc.flush()
                {
                    self.error(format!("flush: {e}"));
                }
                last_flush = Instant::now();
            }
            if stop {
                break;
            }
        }
        self.close();
    }

    fn check_disk(&mut self) {
        let free = match fs2::available_space(&self.root) {
            Ok(f) => f,
            Err(e) => {
                self.error(format!("disk: {e}"));
                return;
            }
        };
        self.shared.disk_free_bytes.store(free, Ordering::Relaxed);
        let paused = self.shared.disk_paused.load(Ordering::Relaxed);
        if !paused && free < self.min_free_bytes {
            self.shared.disk_paused.store(true, Ordering::Relaxed);
            self.close();
            tracing::error!(free_gb = free as f64 / 1e9, "recorder_disk_paused");
        } else if paused && free as f64 >= self.min_free_bytes as f64 * RESUME_HEADROOM {
            self.shared.disk_paused.store(false, Ordering::Relaxed);
            tracing::info!(free_gb = free as f64 / 1e9, "recorder_disk_resumed");
        }
    }

    fn line(&mut self, recv_ns: i64, venue: char, text: &str) {
        if self.shared.disk_paused.load(Ordering::Relaxed) {
            self.shared.skipped_for_disk.fetch_add(1, Ordering::Relaxed);
            return;
        }
        if self
            .open
            .as_ref()
            .is_some_and(|o| !(o.hour_start_ns..o.hour_start_ns + HOUR_NS).contains(&recv_ns))
        {
            self.close();
        }
        if self.open.is_none() {
            let (path, hour) = hour_path(&self.root, recv_ns, self.session_ns);
            let opened = (|| -> std::io::Result<Open> {
                if let Some(dir) = path.parent() {
                    std::fs::create_dir_all(dir)?;
                }
                // Append: after a disk pause the same session reopens its file
                // as a second zstd frame, which the reader reads straight through.
                let f = File::options().create(true).append(true).open(&path)?;
                Ok(Open {
                    path: path.clone(),
                    hour,
                    hour_start_ns: recv_ns - recv_ns.rem_euclid(HOUR_NS),
                    enc: zstd::stream::write::Encoder::new(f, LEVEL)?,
                    lines: 0,
                    streams: BTreeMap::new(),
                    first_ns: None,
                    last_ns: None,
                    gaps: 0,
                })
            })();
            match opened {
                Ok(o) => {
                    self.shared.open_file.set(Some(o.path.display().to_string()));
                    self.open = Some(o);
                }
                Err(e) => {
                    self.error(format!("open {}: {e}", path.display()));
                    return;
                }
            }
        }
        let o = self.open.as_mut().expect("opened");
        let mut buf = itoa(recv_ns);
        buf.push(' ');
        buf.push(venue);
        buf.push(' ');
        buf.push_str(text);
        buf.push('\n');
        if let Err(e) = o.enc.write_all(buf.as_bytes()) {
            self.error(format!("write: {e}"));
            return;
        }
        o.lines += 1;
        *o.streams.entry(venue.to_string()).or_default() += 1;
        o.first_ns.get_or_insert(recv_ns);
        o.last_ns = Some(recv_ns);
        self.shared.lines_written.fetch_add(1, Ordering::Relaxed);
    }

    fn close(&mut self) {
        let Some(o) = self.open.take() else { return };
        self.shared.open_file.set(None);
        if let Err(e) = o.enc.finish().and_then(|mut f| f.flush()) {
            self.error(format!("close: {e}"));
        }
        let manifest = o.path.with_file_name(format!(
            "{}.json",
            o.path.file_name().and_then(|n| n.to_str()).unwrap_or_default()
        ));
        let previous: Manifest =
            std::fs::read(&manifest).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default();
        let mut streams = previous.streams;
        for (k, v) in o.streams {
            *streams.entry(k).or_default() += v;
        }
        let body = Manifest {
            hour: o.hour,
            host: self.host.clone(),
            lines: previous.lines + o.lines,
            streams,
            first_recv_ns: previous.first_recv_ns.or(o.first_ns),
            last_recv_ns: o.last_ns,
            gaps: previous.gaps + o.gaps,
            sessions: previous.sessions + 1,
            format: "zstd text lines: <recv_ns> <venue> <frame>".into(),
        };
        match serde_json::to_vec_pretty(&body) {
            Ok(b) => {
                if let Err(e) = std::fs::write(&manifest, b) {
                    self.error(format!("manifest: {e}"));
                }
            }
            Err(e) => self.error(format!("manifest: {e}")),
        }
        self.shared.files_closed.fetch_add(1, Ordering::Relaxed);
    }
}

fn itoa(v: i64) -> String {
    let mut s = String::with_capacity(32);
    use std::fmt::Write as _;
    let _ = write!(s, "{v}");
    s
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::reader::{iter_files, read_lines};

    #[test]
    fn writes_rotates_and_reads_back() {
        let root = std::env::temp_dir().join(format!("pk-tape-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let mut r = Recorder::start(&root, "test", 0, 1000).unwrap();
        let h0 = 1_791_297_900 * 1_000_000_000i64;
        r.write('c', h0, r#"{"type":"window"}"#);
        r.write('p', h0 + 1, r#"{"a":1}"#);
        r.write('k', h0 + HOUR_NS + 5, r#"{"b":2}"#);
        r.note_gap();
        r.stop();
        let files = iter_files(&root, None, None);
        assert_eq!(files.len(), 2);
        let mut reader = read_lines(files.clone());
        let lines: Vec<_> = reader.by_ref().collect();
        assert_eq!(lines.len(), 3);
        assert_eq!((lines[0].venue, lines[2].venue), ('c', 'k'));
        assert_eq!(reader.stats.out_of_order, 0);
        let m: Manifest =
            serde_json::from_slice(&std::fs::read(format!("{}.json", files[1].display())).unwrap()).unwrap();
        assert_eq!((m.lines, m.gaps, m.sessions), (1, 1, 1));
        assert_eq!(r.stats().lines_written, 3);
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn reads_a_truncated_file_up_to_the_break() {
        let root = std::env::temp_dir().join(format!("pk-tape-trunc-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let path = root.join("pk_20261009_00_1.zst");
        let mut enc = zstd::stream::write::Encoder::new(File::create(&path).unwrap(), 1).unwrap();
        for i in 0..1000 {
            writeln!(enc, "{} p {{\"i\":{i}}}", 1_000 + i).unwrap();
        }
        enc.flush().unwrap(); // flushed but never finished: a crash
        drop(enc);
        let mut reader = read_lines(iter_files(&root, None, None));
        assert_eq!(reader.by_ref().count(), 1000);
        std::fs::remove_dir_all(&root).ok();
    }
}
