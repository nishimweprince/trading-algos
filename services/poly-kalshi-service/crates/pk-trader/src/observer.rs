//! The observer: lines in from the trader's pipe, tape and statistics out.
//!
//! It owns the tape (`pk_tape::Recorder`), the journal (the only writer: the
//! trader's `decision`, `fill`, `settle` and `order_timing` records come over
//! the pipe, in order), `Engine(Observe)` (shadow variants, markouts, series,
//! the uncensored gap, `window` and `resolution` records, BBO checks, feed
//! latency) and PolyBolt. It runs a loopback API that `pk-status` reads.
//!
//! PolyBolt lines are stamped by the observer's clock (the same host clock) as
//! they arrive here, so in the file they can sit a few ms out of `recv_ns`
//! order next to the market lines. Research that joins them sorts on
//! `recv_ns`; the engine ignores venue `b`.

use std::io::BufRead;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use parking_lot::Mutex;
use pk_core::journal::{JsonlDaily, Sink};
use pk_engine::Engine;
use pk_tape::Recorder;
use serde_json::{Value, json};

use crate::now_ns;

/// One journal writer shared by the observe engine and the trader's records.
#[derive(Clone)]
pub struct SharedJournal(pub Arc<Mutex<JsonlDaily>>);

impl Sink for SharedJournal {
    fn write(&mut self, t_ns: i64, record: &Value) {
        self.0.lock().write(t_ns, record);
    }
}

pub struct Observer {
    pub engine: Mutex<Engine>,
    pub journal: SharedJournal,
    pub recorder: Option<Mutex<Recorder>>,
    pub lines: AtomicU64,
    pub records: AtomicU64,
    pub gaps: AtomicU64,
    pub bad_lines: AtomicU64,
    pub engine_errors: AtomicU64,
    pub last_ns: AtomicU64,
    pub started_ns: i64,
    pub polybolt: Mutex<Option<pk_feeds::FeedHandle>>,
}

impl Observer {
    pub fn new(engine: Engine, journal: SharedJournal, recorder: Option<Recorder>) -> Self {
        Self {
            engine: Mutex::new(engine),
            journal,
            recorder: recorder.map(Mutex::new),
            lines: AtomicU64::new(0),
            records: AtomicU64::new(0),
            gaps: AtomicU64::new(0),
            bad_lines: AtomicU64::new(0),
            engine_errors: AtomicU64::new(0),
            last_ns: AtomicU64::new(0),
            started_ns: now_ns(),
            polybolt: Mutex::new(None),
        }
    }

    pub fn handle(&self, line: &str) {
        let Some(l) = pk_tape::reader::parse(line.trim_end_matches('\n').as_bytes()) else {
            self.bad_lines.fetch_add(1, Ordering::Relaxed);
            return;
        };
        if l.venue == 'x' {
            self.internal(l.recv_ns, &l.text);
            return;
        }
        self.lines.fetch_add(1, Ordering::Relaxed);
        self.last_ns.store(l.recv_ns as u64, Ordering::Relaxed);
        if let Some(r) = &self.recorder {
            r.lock().write(l.venue, l.recv_ns, &l.text);
        }
        let mut e = self.engine.lock();
        let ok = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            e.handle_line(l.recv_ns, l.venue, &l.text)
        }));
        if ok.is_err() {
            self.engine_errors.fetch_add(1, Ordering::Relaxed);
            tracing::error!("observer_engine_error");
        }
    }

    fn internal(&self, t_ns: i64, text: &str) {
        let Ok(body) = serde_json::from_str::<Value>(text) else {
            self.bad_lines.fetch_add(1, Ordering::Relaxed);
            return;
        };
        match body.get("k").and_then(Value::as_str) {
            Some("j") => {
                let record = body.get("r").cloned().unwrap_or(Value::Null);
                self.records.fetch_add(1, Ordering::Relaxed);
                self.journal.0.lock().write(t_ns, &record);
                self.engine.lock().note_fill(&record);
            }
            Some("gap") => {
                self.gaps.fetch_add(1, Ordering::Relaxed);
                if let Some(r) = &self.recorder {
                    r.lock().note_gap();
                }
                tracing::warn!(lines = ?body.get("lines"), "observer_gap");
            }
            _ => {
                self.bad_lines.fetch_add(1, Ordering::Relaxed);
            }
        }
    }

    /// Read the pipe until the trader closes it.
    pub fn read_stdin(&self) {
        let stdin = std::io::stdin();
        let mut reader = std::io::BufReader::with_capacity(1 << 20, stdin.lock());
        let mut buf = String::new();
        loop {
            buf.clear();
            match reader.read_line(&mut buf) {
                Ok(0) => break,
                Ok(_) => self.handle(&buf),
                Err(_) => {
                    // Not UTF-8: skip the line.
                    self.bad_lines.fetch_add(1, Ordering::Relaxed);
                }
            }
        }
    }

    pub fn status(&self) -> Value {
        let last = self.last_ns.load(Ordering::Relaxed) as i64;
        let e = self.engine.lock();
        json!({
            "lines": self.lines.load(Ordering::Relaxed),
            "journal_records": self.records.load(Ordering::Relaxed),
            "gaps": self.gaps.load(Ordering::Relaxed),
            "bad_lines": self.bad_lines.load(Ordering::Relaxed),
            "engine_errors": self.engine_errors.load(Ordering::Relaxed),
            // How far the observer runs behind the trader's clock.
            "behind_ms": (last > 0).then(|| ((now_ns() - last) as f64 / 1e5).round() / 10.0),
            "started_ns": self.started_ns,
            "engine": {
                "counters": e.counters,
                "latency": e.metrics.snapshot(),
                "markouts_open": e.markouts_open(),
            },
            "recorder": self.recorder.as_ref().map(|r| serde_json::to_value(r.lock().stats()).unwrap_or(Value::Null)).unwrap_or(json!({"enabled": false})),
            "journal_errors": self.journal.0.lock().errors,
            "polybolt": self.polybolt.lock().as_ref().map(|p| p.stats()).unwrap_or(json!({"enabled": false})),
        })
    }
}

#[cfg(test)]
mod tests {
    use pk_core::{Ledger, RiskLimits, RiskState, StrategyParams};
    use pk_engine::{EngineConfig, Role};

    use super::*;

    #[test]
    fn journals_trader_records_and_counts_gaps() {
        let dir = std::env::temp_dir().join(format!("pk-obs-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let journal = SharedJournal(Arc::new(Mutex::new(JsonlDaily::new(dir.join("journal")))));
        let mut cfg = EngineConfig::new(StrategyParams::default(), vec!["BTC".into()]);
        cfg.role = Role::Observe;
        let risk = RiskState::new(RiskLimits {
            max_open_notional_usd: 1.0,
            daily_loss_limit_usd: 1.0,
            kill_file: None,
        });
        let engine = Engine::new(cfg, Ledger::new(1.0), risk, Box::new(journal.clone()));
        let obs = Observer::new(engine, journal, None);
        obs.handle("1791297900000000000 x {\"k\":\"j\",\"r\":{\"type\":\"decision\"}}\n");
        obs.handle("1791297900000000001 x {\"k\":\"gap\",\"lines\":3}\n");
        obs.handle("1791297900000000002 p {\"event_type\":\"book\"}\n");
        obs.handle("garbage\n");
        assert_eq!(obs.records.load(Ordering::Relaxed), 1);
        assert_eq!(obs.gaps.load(Ordering::Relaxed), 1);
        assert_eq!(obs.lines.load(Ordering::Relaxed), 1);
        assert_eq!(obs.bad_lines.load(Ordering::Relaxed), 1);
        drop(obs);
        let j = pk_core::journal::read_journal(&dir.join("journal")).unwrap();
        assert_eq!(j, vec![json!({"type": "decision"})]);
        std::fs::remove_dir_all(&dir).ok();
    }
}
