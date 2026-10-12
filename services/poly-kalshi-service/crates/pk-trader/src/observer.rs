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
//!
//! Binance spot (`n`) and perp (`f`) best bid/ask lines are stamped and
//! written the same way, but only when the best bid or ask changed (most
//! updates change only a size). They also drive the lead–lag monitor
//! (`pk_core::leadlag`), which the Kalshi and Polymarket mids from the engine
//! follow; its `leadlag_shock` records go to the journal.

use std::collections::HashMap;
use std::io::BufRead;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use parking_lot::Mutex;
use pk_core::journal::{JsonlDaily, Sink};
use pk_core::leadlag::LeadLag;
use pk_engine::Engine;
use pk_tape::Recorder;
use serde::Deserialize;
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
    pub leadlag: Option<Mutex<LeadLag>>,
    pub binance: Mutex<Vec<pk_feeds::FeedHandle>>,
    /// The last best bid and ask per (venue, symbol): only changes are kept.
    binance_last: Mutex<HashMap<(char, String), (String, String)>>,
}

#[derive(Deserialize)]
struct BinanceFrame<'a> {
    #[serde(borrow)]
    data: Option<BookTicker<'a>>,
}

/// A `bookTicker` update: spot has no venue time, perps stamp `E`.
#[derive(Deserialize)]
struct BookTicker<'a> {
    s: &'a str,
    b: &'a str,
    a: &'a str,
    #[serde(rename = "E")]
    event_ms: Option<i64>,
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
            leadlag: None,
            binance: Mutex::new(Vec::new()),
            binance_last: Mutex::new(HashMap::new()),
        }
    }

    /// Turn on the lead–lag monitor for the engine's assets.
    pub fn with_leadlag(mut self) -> Self {
        let ll = {
            let e = self.engine.lock();
            LeadLag::new(e.assets.clone(), e.stamp.clone())
        };
        self.leadlag = Some(Mutex::new(ll));
        self
    }

    /// One Binance frame (`n` spot, `f` perp): to the tape when the best bid or
    /// ask changed, and to the lead–lag monitor.
    pub fn binance(&self, venue: char, text: &str) {
        let t = now_ns();
        let text = text.trim();
        let Some(q) = serde_json::from_str::<BinanceFrame>(text).ok().and_then(|f| f.data) else {
            // Acknowledgements, reconnect markers, errors: kept as they came.
            if let Some(r) = &self.recorder {
                r.lock().write(venue, t, text);
            }
            return;
        };
        {
            let mut last = self.binance_last.lock();
            let key = (venue, q.s.to_owned());
            if last.get(&key).is_some_and(|(b, a)| b == q.b && a == q.a) {
                return;
            }
            last.insert(key, (q.b.to_owned(), q.a.to_owned()));
        }
        if let Some(r) = &self.recorder {
            r.lock().write(venue, t, text);
        }
        let (Some(ll), Ok(bid), Ok(ask)) = (&self.leadlag, q.b.parse::<f64>(), q.a.parse::<f64>()) else {
            return;
        };
        let records = {
            let mut ll = ll.lock();
            let Some(idx) = q.s.strip_suffix("USDT").and_then(|asset| ll.asset_index(asset)) else { return };
            ll.on_ref(idx, usize::from(venue == 'f'), t, (bid + ask) / 2.0, q.event_ms)
        };
        self.journal_all(t, &records);
    }

    fn journal_all(&self, t: i64, records: &[Value]) {
        if records.is_empty() {
            return;
        }
        let mut j = self.journal.0.lock();
        for r in records {
            j.write(t, r);
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
        let Some(ll) = &self.leadlag else { return };
        if !matches!(l.venue, 'k' | 'p') {
            return;
        }
        let mids: Vec<_> = (0..e.assets.len()).filter_map(|i| e.mids(i).map(|m| (i, m))).collect();
        drop(e);
        let records: Vec<Value> = {
            let mut ll = ll.lock();
            mids.into_iter()
                .flat_map(|(i, (open, k, p))| {
                    let mut out = ll.on_follow(i, 0, l.recv_ns, open, k);
                    out.extend(ll.on_follow(i, 1, l.recv_ns, open, p));
                    out
                })
                .collect()
        };
        self.journal_all(l.recv_ns, &records);
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
            "binance": {
                "enabled": self.leadlag.is_some(),
                "feeds": self.binance.lock().iter().map(|f| f.stats()).collect::<Vec<_>>(),
            },
            "leadlag": self.leadlag.as_ref().map(|ll| ll.lock().snapshot()),
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

    #[test]
    fn binance_keeps_bbo_changes_and_feeds_the_monitor() {
        let dir = std::env::temp_dir().join("pk-obs-bn");
        let journal = SharedJournal(Arc::new(Mutex::new(JsonlDaily::new(dir))));
        let mut cfg = EngineConfig::new(StrategyParams::default(), vec!["BTC".into(), "HYPE".into()]);
        cfg.role = Role::Observe;
        let risk = RiskState::new(RiskLimits {
            max_open_notional_usd: 1.0,
            daily_loss_limit_usd: 1.0,
            kill_file: None,
        });
        let engine = Engine::new(cfg, Ledger::new(1.0), risk, Box::new(journal.clone()));
        let obs = Observer::new(engine, journal, None).with_leadlag();
        let frame = |b: &str, a: &str, size: &str| {
            format!(
                r#"{{"stream":"hypeusdt@bookTicker","data":{{"e":"bookTicker","u":1,"s":"HYPEUSDT","b":"{b}","B":"{size}","a":"{a}","A":"1","T":1,"E":1791765297176}}}}"#
            )
        };
        obs.binance('f', &frame("40.10", "40.11", "5"));
        obs.binance('f', &frame("40.10", "40.11", "9")); // a size change only
        obs.binance('f', &frame("40.11", "40.12", "9"));
        obs.binance('f', r#"{"result":null,"id":1}"#);
        obs.binance('n', r#"{"stream":"dogeusdt@bookTicker","data":{"u":1,"s":"DOGEUSDT","b":"0.1","B":"1","a":"0.2","A":"1"}}"#);
        assert_eq!(obs.binance_last.lock().len(), 2);
        let snap = obs.status()["leadlag"].clone();
        assert!(snap["assets"]["HYPE"]["perp"]["typical_1s_bps"].is_null()); // one second seen: no move yet
        assert!(snap["assets"]["HYPE"].get("spot").is_none());
        assert!(snap["assets"]["BTC"].as_object().unwrap().is_empty());
        assert!(snap["assets"]["HYPE"]["perp"]["feed_lag_ms_p50"].as_f64().is_some());
    }
}
