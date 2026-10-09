//! The pipe to the observer process.
//!
//! The engine thread appends `"<recv_ns> <venue> <frame>\n"` (the tape's own
//! format) to a queue; a writer thread drains it into the child's stdin. The
//! trader never waits on the observer: past `MAX_QUEUED_BYTES` behind, market
//! lines are dropped (counted, and a `gap` noted on the tape) while journal
//! records never are. Internal messages use venue `x` and never reach the tape:
//! `{"k":"j","r":record}` (a journal record) and `{"k":"gap","lines":n}`.
//!
//! The child runs in its own process group (a Ctrl-C to the trader does not
//! reach it). On stop the pipe is closed and the child drains it, closes the
//! hour file and exits. If the child dies, journal records go straight to disk
//! (`fallback`) and the next health tick starts a new child.

use std::collections::VecDeque;
use std::io::Write;
use std::os::unix::process::CommandExt;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::{Duration, Instant};

use parking_lot::{Condvar, Mutex};
use pk_core::journal::{JsonlDaily, Sink};
use serde_json::{Value, json};

use crate::now_ns;

pub const MAX_QUEUED_BYTES: usize = 64 << 20;
const STOP_TIMEOUT: Duration = Duration::from_secs(25);

#[derive(Default)]
struct Queue {
    lines: VecDeque<Vec<u8>>,
    bytes: usize,
    closed: bool,
}

struct Pipe {
    queue: Mutex<Queue>,
    ready: Condvar,
    alive: AtomicBool,
}

pub struct ObserverLink {
    argv: Vec<String>,
    child: Mutex<Option<Child>>,
    pipe: Mutex<Option<Arc<Pipe>>>,
    writer: Mutex<Option<std::thread::JoinHandle<()>>>,
    fallback: Mutex<JsonlDaily>,
    gap_lines: AtomicU64,
    pub spawns: AtomicU64,
    pub lines_sent: Arc<AtomicU64>,
    pub dropped: AtomicU64,
    pub fallback_writes: AtomicU64,
    last_exit: Mutex<Option<i32>>,
}

impl ObserverLink {
    pub fn new(argv: Vec<String>, fallback: JsonlDaily) -> Self {
        Self {
            argv,
            child: Mutex::new(None),
            pipe: Mutex::new(None),
            writer: Mutex::new(None),
            fallback: Mutex::new(fallback),
            gap_lines: AtomicU64::new(0),
            spawns: AtomicU64::new(0),
            lines_sent: Arc::default(),
            dropped: AtomicU64::new(0),
            fallback_writes: AtomicU64::new(0),
            last_exit: Mutex::new(None),
        }
    }

    pub fn spawn(&self) -> std::io::Result<()> {
        let mut cmd = Command::new(&self.argv[0]);
        cmd.args(&self.argv[1..]).stdin(Stdio::piped()).process_group(0);
        let mut child = cmd.spawn()?;
        let stdin = child.stdin.take().expect("piped");
        let pipe = Arc::new(Pipe {
            queue: Mutex::new(Queue::default()),
            ready: Condvar::new(),
            alive: AtomicBool::new(true),
        });
        let p = pipe.clone();
        let sent = self.lines_sent.clone();
        let writer = std::thread::Builder::new()
            .name("pk-observer-pipe".into())
            .spawn(move || write_loop(p, stdin, sent))?;
        tracing::info!(pid = child.id(), n = self.spawns.load(Ordering::Relaxed) + 1, "observer_started");
        if self.spawns.fetch_add(1, Ordering::Relaxed) > 0 {
            self.gap_lines.fetch_add(1, Ordering::Relaxed); // lines while it was down are not on the tape
        }
        *self.child.lock() = Some(child);
        *self.pipe.lock() = Some(pipe);
        *self.writer.lock() = Some(writer);
        Ok(())
    }

    pub fn alive(&self) -> bool {
        self.pipe.lock().as_ref().is_some_and(|p| p.alive.load(Ordering::Relaxed))
    }

    /// Health tick: if the child died, report why and start a new one.
    /// Returns the exit description when it had died.
    pub fn check(&self) -> Option<String> {
        let exited = {
            let mut child = self.child.lock();
            match child.as_mut().map(Child::try_wait) {
                Some(Ok(Some(status))) => Some(status.code()),
                Some(Err(_)) => Some(None),
                _ if !self.alive() && child.is_some() => Some(None),
                _ => None,
            }
        }?;
        *self.last_exit.lock() = exited;
        tracing::error!(returncode = ?exited, "observer_down");
        self.teardown(Duration::from_secs(1));
        if let Err(e) = self.spawn() {
            tracing::error!(error = %e, "observer_respawn_failed");
        }
        Some(format!("returncode={exited:?}"))
    }

    fn teardown(&self, wait: Duration) {
        if let Some(pipe) = self.pipe.lock().take() {
            pipe.queue.lock().closed = true;
            pipe.ready.notify_all();
        }
        if let Some(w) = self.writer.lock().take() {
            let _ = w.join();
        }
        if let Some(mut child) = self.child.lock().take() {
            let deadline = Instant::now() + wait;
            loop {
                match child.try_wait() {
                    Ok(Some(_)) => break,
                    Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(50)),
                    _ => {
                        tracing::error!(pid = child.id(), "observer_stop_timeout");
                        let _ = child.kill();
                        let _ = child.wait();
                        break;
                    }
                }
            }
        }
    }

    /// Close the pipe: the child drains it, closes the tape and exits.
    pub fn close(&self) {
        self.teardown(STOP_TIMEOUT);
    }

    fn push(&self, line: Vec<u8>, market: bool) -> bool {
        let pipe = self.pipe.lock().clone();
        let Some(pipe) = pipe.filter(|p| p.alive.load(Ordering::Relaxed)) else { return false };
        let mut q = pipe.queue.lock();
        if market && q.bytes > MAX_QUEUED_BYTES {
            self.dropped.fetch_add(1, Ordering::Relaxed);
            self.gap_lines.fetch_add(1, Ordering::Relaxed);
            return true;
        }
        let gap = self.gap_lines.swap(0, Ordering::Relaxed);
        if gap > 0 {
            let g = format!("{} x {}\n", now_ns(), json!({"k": "gap", "lines": gap}));
            q.bytes += g.len();
            q.lines.push_back(g.into_bytes());
        }
        q.bytes += line.len();
        q.lines.push_back(line);
        drop(q);
        pipe.ready.notify_one();
        true
    }

    /// One stamped market or control line.
    pub fn line(&self, recv_ns: i64, venue: char, text: &str) {
        let mut b = Vec::with_capacity(text.len() + 24);
        let _ = write!(b, "{recv_ns} {venue} ");
        b.extend_from_slice(text.as_bytes());
        b.push(b'\n');
        if !self.push(b, true) {
            self.dropped.fetch_add(1, Ordering::Relaxed);
        }
    }

    /// One journal record: to the observer, or straight to disk while it is down.
    pub fn journal(&self, t_ns: i64, record: &Value) {
        let line = format!("{t_ns} x {}\n", json!({"k": "j", "r": record}));
        if !self.push(line.into_bytes(), false) {
            self.fallback_writes.fetch_add(1, Ordering::Relaxed);
            self.fallback.lock().write(t_ns, record);
        }
    }

    pub fn stats(&self) -> Value {
        let pipe = self.pipe.lock().clone();
        json!({
            "enabled": true,
            "alive": self.alive(),
            "pid": self.child.lock().as_ref().map(Child::id),
            "spawns": self.spawns.load(Ordering::Relaxed),
            "lines_sent": self.lines_sent.load(Ordering::Relaxed),
            "dropped": self.dropped.load(Ordering::Relaxed),
            "buffer_bytes": pipe.map(|p| p.queue.lock().bytes).unwrap_or(0),
            "fallback_writes": self.fallback_writes.load(Ordering::Relaxed),
            "last_exit": *self.last_exit.lock(),
        })
    }
}

fn write_loop(pipe: Arc<Pipe>, mut stdin: ChildStdin, sent: Arc<AtomicU64>) {
    let mut batch: Vec<u8> = Vec::with_capacity(1 << 20);
    loop {
        let mut n = 0u64;
        {
            let mut q = pipe.queue.lock();
            while q.lines.is_empty() && !q.closed {
                pipe.ready.wait(&mut q);
            }
            if q.lines.is_empty() && q.closed {
                break;
            }
            while let Some(l) = q.lines.pop_front() {
                q.bytes -= l.len();
                batch.extend_from_slice(&l);
                n += 1;
                if batch.len() > (1 << 20) {
                    break;
                }
            }
        }
        if stdin.write_all(&batch).is_err() {
            pipe.alive.store(false, Ordering::Relaxed);
            break;
        }
        sent.fetch_add(n, Ordering::Relaxed);
        batch.clear();
    }
    let _ = stdin.flush();
    pipe.alive.store(false, Ordering::Relaxed);
}

/// The trade engine's journal: through the link.
pub struct LinkSink(pub Arc<ObserverLink>);

impl Sink for LinkSink {
    fn write(&mut self, t_ns: i64, record: &Value) {
        self.0.journal(t_ns, record);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pipes_lines_and_falls_back_when_the_child_dies() {
        let dir = std::env::temp_dir().join(format!("pk-link-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let out = dir.join("out.txt");
        // A stand-in observer: copies stdin to a file.
        let link = ObserverLink::new(
            vec!["sh".into(), "-c".into(), format!("cat > {}", out.display())],
            JsonlDaily::new(dir.join("journal")),
        );
        link.spawn().unwrap();
        link.line(10, 'p', "{\"a\":1}");
        link.journal(11, &json!({"type": "fill"}));
        link.close();
        let text = std::fs::read_to_string(&out).unwrap();
        assert_eq!(text, "10 p {\"a\":1}\n11 x {\"k\":\"j\",\"r\":{\"type\":\"fill\"}}\n");
        // Down: journal records go straight to disk, market lines are dropped.
        link.journal(1_791_297_900_000_000_000, &json!({"type": "settle"}));
        link.line(12, 'p', "{}");
        assert_eq!(link.fallback_writes.load(Ordering::Relaxed), 1);
        assert_eq!(link.dropped.load(Ordering::Relaxed), 1);
        let journal = pk_core::journal::read_journal(&dir.join("journal")).unwrap();
        assert_eq!(journal, vec![json!({"type": "settle"})]);
        std::fs::remove_dir_all(&dir).ok();
    }
}
