//! The observer: lines in from the trader's pipe, tape and statistics out.
//!
//! It owns the tape (`pk_tape::Recorder`), the journal (the only writer: the
//! trader's `decision`, `fill`, `settle` and `order_timing` records come over
//! the pipe, in order), `Engine(Observe)` (markouts, series,
//! the uncensored gap, `window` and `resolution` records, BBO checks, feed
//! latency) and PolyBolt. It runs a loopback API that `pk-status` reads.
//!
//! PolyBolt lines are stamped by the observer's clock (the same host clock) as
//! they arrive here, so in the file they can sit a few ms out of `recv_ns`
//! order next to the market lines. Research that joins them sorts on
//! `recv_ns`; the engine ignores venue `b`.
//!
//! Binance spot (`n`) and perp (`f`), Kraken (`r`), Bitstamp (`t`) and
//! Coinbase (`o`) best bid/ask lines are stamped and written the same way, but
//! only when the best bid or ask changed (most updates change only a size).
//! Binance frames are kept as they came; the others are written as one compact
//! line per quote, `{"s","b","a"}` plus the venue's stamp (`ts` RFC 3339, `mt`
//! µs), since a Bitstamp frame carries 100 levels a side and a Coinbase ticker
//! a day of statistics. Helius LaserStream (`h`) lines come decoded from
//! `pk_feeds::laserstream`, one price per account write (`{"s","p","slot","mt"}`),
//! each account its own source. They also drive the lead–lag
//! monitor (`pk_core::leadlag`), which the Kalshi and Polymarket mids from the
//! engine follow; its `leadlag_shock` records go to the journal.

use std::collections::HashMap;
use std::io::BufRead;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use parking_lot::Mutex;
use pk_core::journal::{JsonlDaily, Sink};
use pk_core::leadlag::{LeadLag, SOURCES};
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
    pub kraken: Mutex<Option<pk_feeds::FeedHandle>>,
    pub bitstamp: Mutex<Option<pk_feeds::FeedHandle>>,
    pub coinbase: Mutex<Option<pk_feeds::FeedHandle>>,
    pub laserstream: Mutex<Option<pk_feeds::LaserHandle>>,
    /// The last best bid and ask per (venue, symbol): only changes are kept.
    ref_last: Mutex<HashMap<(char, String), (f64, f64)>>,
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

/// A Kraken v2 `ticker` frame (`event_trigger: bbo`); prices are numbers.
#[derive(Deserialize)]
struct KrakenFrame<'a> {
    channel: &'a str,
    #[serde(default, borrow)]
    data: Vec<KrakenTicker<'a>>,
}

#[derive(Deserialize)]
struct KrakenTicker<'a> {
    symbol: &'a str,
    bid: f64,
    ask: f64,
    timestamp: Option<&'a str>,
}

/// A Bitstamp `order_book_<pair>` frame: the top 100 levels a side.
#[derive(Deserialize)]
struct BitstampFrame<'a> {
    channel: &'a str,
    #[serde(borrow)]
    data: BitstampBook<'a>,
}

#[derive(Deserialize)]
struct BitstampBook<'a> {
    microtimestamp: &'a str,
    bids: Top,
    asks: Top,
}

/// The price of a side's first `[price, size]` level; the other levels are
/// skipped, not allocated.
struct Top(Option<f64>);

impl<'de> Deserialize<'de> for Top {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct Levels;
        impl<'de> serde::de::Visitor<'de> for Levels {
            type Value = Top;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("a list of [price, size] levels")
            }
            fn visit_seq<A: serde::de::SeqAccess<'de>>(self, mut seq: A) -> Result<Top, A::Error> {
                let first = seq.next_element::<(&'de str, serde::de::IgnoredAny)>()?;
                while seq.next_element::<serde::de::IgnoredAny>()?.is_some() {}
                Ok(Top(first.and_then(|(px, _)| px.parse().ok())))
            }
        }
        d.deserialize_seq(Levels)
    }
}

/// A Coinbase Exchange `ticker` frame: the best bid and ask after a trade.
#[derive(Deserialize)]
struct CoinbaseTicker<'a> {
    #[serde(rename = "type")]
    kind: &'a str,
    product_id: &'a str,
    best_bid: &'a str,
    best_ask: &'a str,
    time: Option<&'a str>,
}

/// A LaserStream line: one on-chain price (Pyth adds `conf` and `pt`).
#[derive(Deserialize)]
struct OnChain<'a> {
    s: &'a str,
    p: f64,
    mt: Option<i64>,
}

/// The lead–lag source of a reference venue (its index in [`SOURCES`]);
/// LaserStream's accounts are each a source of their own.
fn source_of(venue: char, symbol: &str) -> Option<usize> {
    let name = match venue {
        'h' => symbol,
        'n' => "spot",
        'f' => "perp",
        'r' => "kraken",
        't' => "bitstamp",
        'o' => "coinbase",
        _ => return None,
    };
    SOURCES.iter().position(|s| *s == name)
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
            kraken: Mutex::new(None),
            bitstamp: Mutex::new(None),
            coinbase: Mutex::new(None),
            laserstream: Mutex::new(None),
            ref_last: Mutex::new(HashMap::new()),
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
            self.record(venue, t, text);
            return;
        };
        let (Ok(bid), Ok(ask)) = (q.b.parse::<f64>(), q.a.parse::<f64>()) else {
            self.record(venue, t, text);
            return;
        };
        self.quote(venue, t, q.s, q.s.strip_suffix("USDT"), (bid, ask), q.event_ms, || text.to_owned());
    }

    /// One Kraken `ticker` frame (`r`): each quote as a compact line.
    pub fn kraken(&self, text: &str) {
        let t = now_ns();
        let text = text.trim();
        let frame = serde_json::from_str::<KrakenFrame>(text).ok().filter(|f| f.channel == "ticker");
        let Some(frame) = frame.filter(|f| !f.data.is_empty()) else {
            self.record('r', t, text);
            return;
        };
        for q in &frame.data {
            let venue_ms = q
                .timestamp
                .and_then(|ts| chrono::DateTime::parse_from_rfc3339(ts).ok())
                .map(|d| d.timestamp_millis());
            let line = || json!({"s": q.symbol, "b": q.bid, "a": q.ask, "ts": q.timestamp}).to_string();
            self.quote('r', t, q.symbol, q.symbol.strip_suffix("/USD"), (q.bid, q.ask), venue_ms, line);
        }
    }

    /// One Bitstamp `order_book_<pair>` frame (`t`): its top of book as a compact line.
    pub fn bitstamp(&self, text: &str) {
        let t = now_ns();
        let text = text.trim();
        // Subscription notices, errors, an empty side: kept as they came.
        let Some(f) = serde_json::from_str::<BitstampFrame>(text).ok() else {
            self.record('t', t, text);
            return;
        };
        let (Some(bid), Some(ask)) = (f.data.bids.0, f.data.asks.0) else {
            self.record('t', t, text);
            return;
        };
        let pair = f.channel.strip_prefix("order_book_").unwrap_or(f.channel);
        let asset = pair.strip_suffix("usd").map(str::to_ascii_uppercase);
        let venue_ms = f.data.microtimestamp.parse::<i64>().ok().map(|us| us / 1000);
        let line = || json!({"s": pair, "b": bid, "a": ask, "mt": f.data.microtimestamp}).to_string();
        self.quote('t', t, pair, asset.as_deref(), (bid, ask), venue_ms, line);
    }

    /// One Coinbase frame (`o`): a ticker's best bid and ask as a compact line.
    pub fn coinbase(&self, text: &str) {
        let t = now_ns();
        let text = text.trim();
        // Reconnect markers, errors: kept as they came.
        let Some(q) = serde_json::from_str::<CoinbaseTicker>(text).ok().filter(|q| q.kind == "ticker") else {
            self.record('o', t, text);
            return;
        };
        let (Ok(bid), Ok(ask)) = (q.best_bid.parse::<f64>(), q.best_ask.parse::<f64>()) else {
            self.record('o', t, text);
            return;
        };
        let venue_ms = q.time.and_then(|ts| chrono::DateTime::parse_from_rfc3339(ts).ok()).map(|d| d.timestamp_millis());
        let line = || json!({"s": q.product_id, "b": bid, "a": ask, "ts": q.time}).to_string();
        self.quote('o', t, q.product_id, q.product_id.strip_suffix("-USD"), (bid, ask), venue_ms, line);
    }

    /// One LaserStream line (`h`): an on-chain SOL price, already compact.
    pub fn laserstream(&self, text: &str) {
        let t = now_ns();
        // Reconnect markers: kept as they came.
        let Some(q) = serde_json::from_str::<OnChain>(text).ok() else {
            self.record('h', t, text);
            return;
        };
        self.quote('h', t, q.s, Some("SOL"), (q.p, q.p), q.mt.map(|us| us / 1000), || text.to_owned());
    }

    fn record(&self, venue: char, t: i64, text: &str) {
        if let Some(r) = &self.recorder {
            r.lock().write(venue, t, text);
        }
    }

    /// A reference best bid and ask for `symbol` (`asset` in our names): to the
    /// tape as `line()` when either price changed, and to the lead–lag monitor.
    #[allow(clippy::too_many_arguments)]
    fn quote(
        &self,
        venue: char,
        t: i64,
        symbol: &str,
        asset: Option<&str>,
        (bid, ask): (f64, f64),
        venue_ms: Option<i64>,
        line: impl FnOnce() -> String,
    ) {
        {
            let mut last = self.ref_last.lock();
            let key = (venue, symbol.to_owned());
            if last.get(&key) == Some(&(bid, ask)) {
                return;
            }
            last.insert(key, (bid, ask));
        }
        self.record(venue, t, &line());
        let (Some(ll), Some(src), Some(asset)) = (&self.leadlag, source_of(venue, symbol), asset) else { return };
        let records = {
            let mut ll = ll.lock();
            let Some(idx) = ll.asset_index(asset) else { return };
            ll.on_ref(idx, src, t, (bid + ask) / 2.0, venue_ms)
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
                "enabled": !self.binance.lock().is_empty(),
                "feeds": self.binance.lock().iter().map(|f| f.stats()).collect::<Vec<_>>(),
            },
            "kraken": self.kraken.lock().as_ref().map(|f| f.stats()).unwrap_or(json!({"enabled": false})),
            "bitstamp": self.bitstamp.lock().as_ref().map(|f| f.stats()).unwrap_or(json!({"enabled": false})),
            "coinbase": self.coinbase.lock().as_ref().map(|f| f.stats()).unwrap_or(json!({"enabled": false})),
            "laserstream": self.laserstream.lock().as_ref().map(|f| f.stats()).unwrap_or(json!({"enabled": false})),
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
        assert_eq!(obs.ref_last.lock().len(), 2);
        let snap = obs.status()["leadlag"].clone();
        assert!(snap["assets"]["HYPE"]["perp"]["typical_1s_bps"].is_null()); // one second seen: no move yet
        assert!(snap["assets"]["HYPE"].get("spot").is_none());
        assert!(snap["assets"]["BTC"].as_object().unwrap().is_empty());
        assert!(snap["assets"]["HYPE"]["perp"]["feed_lag_ms_p50"].as_f64().is_some());
    }

    #[test]
    fn kraken_and_bitstamp_keep_top_of_book_changes() {
        let dir = std::env::temp_dir().join(format!("pk-obs-ref-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let journal = SharedJournal(Arc::new(Mutex::new(JsonlDaily::new(dir.join("journal")))));
        let mut cfg = EngineConfig::new(StrategyParams::default(), vec!["BTC".into(), "DOGE".into(), "SOL".into()]);
        cfg.role = Role::Observe;
        let risk = RiskState::new(RiskLimits {
            max_open_notional_usd: 1.0,
            daily_loss_limit_usd: 1.0,
            kill_file: None,
        });
        let engine = Engine::new(cfg, Ledger::new(1.0), risk, Box::new(journal.clone()));
        let rec = Recorder::start(&dir.join("tape"), "test", 0, 1000).unwrap();
        let obs = Observer::new(engine, journal, Some(rec)).with_leadlag();
        let kraken = |bid: f64, qty: f64| {
            format!(
                r#"{{"channel":"ticker","type":"update","data":[{{"symbol":"DOGE/USD","bid":{bid},"bid_qty":{qty},"ask":0.0797117,"ask_qty":50.0,"last":0.07972,"timestamp":"2026-10-22T13:55:52.582483Z"}}]}}"#
            )
        };
        obs.kraken(&kraken(0.0797116, 14379.0));
        obs.kraken(&kraken(0.0797116, 17706.0)); // a size change only
        obs.kraken(&kraken(0.0797115, 17706.0));
        obs.kraken(r#"{"channel":"status","type":"update","data":[{"system":"online","api_version":"v2"}]}"#);
        let levels = |px: f64| {
            (0..100).map(|i| format!(r#"["{:.2}","0.5"]"#, px - f64::from(i) * 0.01)).collect::<Vec<_>>().join(",")
        };
        let book = |bid: f64, us: i64| {
            format!(
                r#"{{"data":{{"timestamp":"1792677358","microtimestamp":"{us}","bids":[{}],"asks":[{}]}},"channel":"order_book_btcusd","event":"data"}}"#,
                levels(bid),
                levels(bid + 1.0)
            )
        };
        obs.bitstamp(&book(81662.93, 1792677358006727));
        obs.bitstamp(&book(81662.93, 1792677358115393)); // the same top of book
        obs.bitstamp(r#"{"event":"bts:subscription_succeeded","channel":"order_book_btcusd","data":{}}"#);
        let coinbase = |bid: &str, size: &str| {
            format!(
                r#"{{"type":"ticker","sequence":1,"product_id":"BTC-USD","price":"81567.31","open_24h":"84495.31","best_bid":"{bid}","best_bid_size":"{size}","best_ask":"81567.31","best_ask_size":"0.009","side":"buy","time":"2026-10-22T14:37:23.117725Z","trade_id":1,"last_size":"0.1"}}"#
            )
        };
        obs.coinbase(&coinbase("81567.30", "0.002"));
        obs.coinbase(&coinbase("81567.30", "0.5")); // a size change only
        obs.coinbase(r#"{"type":"error","message":"Failed to subscribe"}"#);
        let orca = r#"{"mt":1792715492123456,"p":103.7055,"s":"orca","slot":380000001}"#;
        obs.laserstream(r#"{"n":1,"type":"pk_reconnect","venue":"h"}"#);
        obs.laserstream(orca);
        obs.laserstream(r#"{"mt":1792715492173456,"p":103.7055,"s":"orca","slot":380000001}"#); // the same price
        obs.laserstream(r#"{"conf":0.05,"mt":1792715492200000,"p":103.716,"pt":1792715492,"s":"pyth","slot":380000001}"#);
        assert_eq!(obs.ref_last.lock().get(&('h', "orca".into())), Some(&(103.7055, 103.7055)));
        assert_eq!(obs.ref_last.lock().get(&('o', "BTC-USD".into())), Some(&(81567.30, 81567.31)));
        assert_eq!(obs.ref_last.lock().get(&('r', "DOGE/USD".into())), Some(&(0.0797115, 0.0797117)));
        assert_eq!(obs.ref_last.lock().get(&('t', "btcusd".into())), Some(&(81662.93, 81663.93)));
        let snap = obs.status()["leadlag"].clone();
        assert!(snap["assets"]["DOGE"]["kraken"]["feed_lag_ms_p50"].as_f64().is_some());
        assert!(snap["assets"]["BTC"]["bitstamp"]["feed_lag_ms_p50"].as_f64().is_some());
        assert!(snap["assets"]["BTC"]["coinbase"]["feed_lag_ms_p50"].as_f64().is_some());
        assert!(snap["assets"]["SOL"]["orca"]["feed_lag_ms_p50"].as_f64().is_some());
        assert!(snap["assets"]["SOL"]["pyth"].is_object() && snap["assets"]["SOL"].get("raydium").is_none());
        drop(obs); // stops the recorder: the hour file is flushed
        let lines: Vec<(char, String)> = pk_tape::read_lines(pk_tape::iter_files(&dir.join("tape"), None, None))
            .map(|l| (l.venue, l.text))
            .collect();
        let kept: Vec<&str> = lines.iter().filter(|(v, _)| *v == 'r').map(|(_, t)| t.as_str()).collect();
        assert_eq!(kept.len(), 3); // two quotes and the status frame as it came
        assert!(kept[0].starts_with(r#"{"a":0.0797117,"b":0.0797116,"s":"DOGE/USD""#), "{}", kept[0]);
        let bs: Vec<&str> = lines.iter().filter(|(v, _)| *v == 't').map(|(_, t)| t.as_str()).collect();
        assert_eq!(bs.len(), 2); // one compact quote, then the notice
        assert_eq!(bs[0], r#"{"a":81663.93,"b":81662.93,"mt":"1792677358006727","s":"btcusd"}"#);
        let cb: Vec<&str> = lines.iter().filter(|(v, _)| *v == 'o').map(|(_, t)| t.as_str()).collect();
        assert_eq!(cb.len(), 2); // one compact quote, then the error as it came
        assert_eq!(cb[0], r#"{"a":81567.31,"b":81567.3,"s":"BTC-USD","ts":"2026-10-22T14:37:23.117725Z"}"#);
        let hl: Vec<&str> = lines.iter().filter(|(v, _)| *v == 'h').map(|(_, t)| t.as_str()).collect();
        assert_eq!(hl.len(), 3); // the marker, the Orca quote, the Pyth quote
        assert_eq!(hl[1], orca);
        std::fs::remove_dir_all(&dir).ok();
    }
}
