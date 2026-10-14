//! The Rust engine against the Python engine's journals on the same tape
//! (`tests/golden/README.md`): the same records in the same order, field by
//! field. And the trade/observe split against the one-process engine.

use std::collections::BTreeMap;
use std::io::{BufRead, BufReader};
use std::path::Path;
use std::sync::{Arc, Mutex};

use pk_core::journal::{Sink, VecSink};
use pk_core::{Ledger, RiskLimits, RiskState, StrategyParams};
use pk_engine::{Engine, EngineConfig, PaperVenue, Role};
use serde_json::{Map, Value, json};

const NS: i64 = 1_000_000_000;

fn golden(name: &str) -> std::path::PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/golden").join(name)
}

fn tape() -> Vec<(i64, char, String)> {
    let f = std::fs::File::open(golden("tape.txt.gz")).unwrap();
    BufReader::new(flate2::read::GzDecoder::new(f))
        .lines()
        .map(|l| {
            let l = l.unwrap();
            let mut parts = l.splitn(3, ' ');
            let t = parts.next().unwrap().parse().unwrap();
            let v = parts.next().unwrap().chars().next().unwrap();
            (t, v, parts.next().unwrap().to_owned())
        })
        .collect()
}

struct Cfg {
    params: StrategyParams,
    latency_ms: i64,
    limit_offset: f64,
    size_to_depth: bool,
    cash: f64,
    max_open: f64,
    loss: f64,
}

fn cfg(name: &str) -> Cfg {
    match name {
        "base" => Cfg {
            params: StrategyParams {
                gap_threshold: 0.15,
                max_entry_px: 0.60,
                notional_usd: 50.0,
                ..Default::default()
            },
            latency_ms: 150,
            limit_offset: 0.0,
            size_to_depth: false,
            cash: 1000.0,
            max_open: 500.0,
            loss: 250.0,
        },
        "offset_depth_lag" => Cfg {
            params: StrategyParams {
                gap_threshold: 0.10,
                max_entry_px: 0.60,
                notional_usd: 40.0,
                max_poly_lag_ms: Some(250.0),
                max_poly_spread: Some(0.03),
                ..Default::default()
            },
            latency_ms: 60,
            limit_offset: 0.02,
            size_to_depth: true,
            cash: 400.0,
            max_open: 100.0,
            loss: 150.0,
        },
        other => panic!("{other}"),
    }
}

/// A sink the test can read after the engine has taken it.
#[derive(Clone, Default)]
struct Shared(Arc<Mutex<Vec<Value>>>);

impl Sink for Shared {
    fn write(&mut self, _t: i64, r: &Value) {
        self.0.lock().unwrap().push(r.clone());
    }
}

fn engine(c: &Cfg, role: Role, sink: Box<dyn Sink + Send>) -> Engine {
    let mut ec = EngineConfig::new(c.params.clone(), ["BTC", "ETH", "SOL", "XRP"].map(String::from).to_vec());
    ec.role = role;
    ec.venue = Some(PaperVenue::new(c.latency_ms));
    ec.limit_offset = c.limit_offset;
    ec.size_to_depth = c.size_to_depth;
    let risk = RiskState::new(RiskLimits {
        max_open_notional_usd: c.max_open,
        daily_loss_limit_usd: c.loss,
        kill_file: None,
    });
    Engine::new(ec, Ledger::new(c.cash), risk, sink)
}

fn final_record(e: &Engine) -> Value {
    let counters: BTreeMap<_, _> = e.counters.iter().collect();
    json!({"type": "final", "counters": counters, "ledger": e.ledger.snapshot(e.last_ns)})
}

fn diff(path: &str, a: &Value, b: &Value, out: &mut Vec<String>) {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => {
            let (x, y) = (x.as_f64().unwrap(), y.as_f64().unwrap());
            let tol = if path.ends_with("sec_into") { 1e-3 } else { 1e-9 * x.abs().max(1.0) };
            if (x - y).abs() > tol {
                out.push(format!("{path}: rust {x} python {y}"));
            }
        }
        (Value::Object(x), Value::Object(y)) => {
            for k in x.keys().chain(y.keys()).collect::<std::collections::BTreeSet<_>>() {
                match (x.get(k), y.get(k)) {
                    (Some(p), Some(q)) => diff(&format!("{path}.{k}"), p, q, out),
                    (p, q) => out.push(format!("{path}.{k}: rust {p:?} python {q:?}")),
                }
            }
        }
        (Value::Array(x), Value::Array(y)) if x.len() == y.len() => {
            for (i, (p, q)) in x.iter().zip(y).enumerate() {
                diff(&format!("{path}[{i}]"), p, q, out);
            }
        }
        _ if a == b => {}
        _ => out.push(format!("{path}: rust {a} python {b}")),
    }
}

fn run_full(name: &str, lines: &[(i64, char, String)]) -> Vec<Value> {
    let c = cfg(name);
    let sink = Shared::default();
    let mut e = engine(&c, Role::Full, Box::new(sink.clone()));
    for (t, v, x) in lines {
        e.handle_line(*t, *v, x);
    }
    e.handle_line(lines.last().unwrap().0 + 200 * NS, 'c', r#"{"type":"noop"}"#);
    let mut records = sink.0.lock().unwrap().clone();
    records.push(final_record(&e));
    records
}

fn check(name: &str) {
    let lines = tape();
    let ours = if name == "reverse" { run_reverse(&lines) } else { run_full(name, &lines) };
    let theirs: Vec<Value> = std::fs::read_to_string(golden(&format!("{name}.jsonl")))
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str::<Value>(l).unwrap())
        // The shadow variants were removed (epoch 14); every other record still matches.
        .filter(|r| !matches!(r["type"].as_str(), Some("shadow" | "shadow_settle")))
        .collect();
    let mut problems = Vec::new();
    if ours.len() != theirs.len() {
        problems.push(format!("{} records, python {}", ours.len(), theirs.len()));
    }
    for (i, (a, b)) in ours.iter().zip(&theirs).enumerate() {
        let mut d = Vec::new();
        diff("", a, b, &mut d);
        if !d.is_empty() {
            problems.push(format!("record {i} ({}): {}", b["type"], d.join("; ")));
        }
        if problems.len() > 10 {
            break;
        }
    }
    assert!(problems.is_empty(), "{name}: {} problems:\n{}", problems.len(), problems.join("\n"));
    let kinds = theirs.iter().fold(BTreeMap::<String, usize>::new(), |mut m, r| {
        *m.entry(r["type"].as_str().unwrap().to_owned()).or_default() += 1;
        m
    });
    eprintln!("{name}: {} records match {kinds:?}", theirs.len());
}

fn run_reverse(lines: &[(i64, char, String)]) -> Vec<Value> {
    let params =
        StrategyParams { gap_threshold: 0.10, max_entry_px: 0.60, notional_usd: 50.0, ..Default::default() };
    let mut ec = EngineConfig::new(params, ["BTC", "ETH", "SOL", "XRP"].map(String::from).to_vec());
    ec.venue = Some(PaperVenue::new(47));
    ec.limit_offset = 0.02;
    ec.direction = pk_engine::Direction::Reverse;
    let risk = RiskState::new(RiskLimits {
        max_open_notional_usd: 1e12,
        daily_loss_limit_usd: 1e12,
        kill_file: None,
    });
    let sink = Shared::default();
    let mut e = Engine::new(ec, Ledger::new(1e9), risk, Box::new(sink.clone()));
    for (t, v, x) in lines {
        e.handle_line(*t, *v, x);
    }
    e.handle_line(lines.last().unwrap().0 + 200 * NS, 'c', r#"{"type":"noop"}"#);
    let mut records = sink.0.lock().unwrap().clone();
    records.push(final_record(&e));
    records
}

#[test]
fn matches_python_reverse() {
    check("reverse");
}

#[test]
fn matches_python_base() {
    check("base");
}

#[test]
fn matches_python_offset_depth_lag() {
    check("offset_depth_lag");
}

/// The trader and the observer, fed the same lines (the observer told of each
/// fill right after the line that made it), write exactly the one-process
/// engine's records. Markouts' gap-closure fields may differ: the observer's
/// first closure check is one line later.
#[test]
fn trade_plus_observe_equals_full() {
    let lines = tape();
    for name in ["base", "offset_depth_lag"] {
        let c = cfg(name);
        let full = Shared::default();
        let mut f = engine(&c, Role::Full, Box::new(full.clone()));
        let trade = Shared::default();
        let mut t = engine(&c, Role::Trade, Box::new(trade.clone()));
        let obs = Shared::default();
        let mut o = engine(&c, Role::Observe, Box::new(obs.clone()));
        for (ns, v, x) in &lines {
            f.handle_line(*ns, *v, x);
            let before = trade.0.lock().unwrap().len();
            t.handle_line(*ns, *v, x);
            o.handle_line(*ns, *v, x);
            let new: Vec<Value> = trade.0.lock().unwrap()[before..].to_vec();
            for r in &new {
                o.note_fill(r);
            }
        }
        let canon = |v: &Value| {
            let mut v = v.clone();
            if v["type"] == "markout" {
                let m: &mut Map<String, Value> = v.as_object_mut().unwrap();
                for k in ["exit_gap_s", "exit_gap_bid", "exit_gap_bid_size"] {
                    m.remove(k);
                }
            }
            v.to_string()
        };
        let mut a: Vec<String> = full.0.lock().unwrap().iter().map(canon).collect();
        let mut b: Vec<String> =
            trade.0.lock().unwrap().iter().chain(obs.0.lock().unwrap().iter()).map(canon).collect();
        a.sort();
        b.sort();
        assert_eq!(a.len(), b.len(), "{name}");
        assert!(a == b, "{name}: split differs from full");
        assert_eq!(t.ledger.cash, f.ledger.cash);
    }
}

#[test]
fn null_sink_runs() {
    let mut e = engine(&cfg("base"), Role::Full, Box::new(VecSink::default()));
    e.handle_line(1, 'c', "{");
    assert_eq!(e.counters.get("bad_json"), Some(&1));
}

/// The `age_outlier` record (opt-in): a decision on a Polymarket book older
/// than the limit says whether the feed was silent or the book merely quiet.
#[test]
fn age_outlier_explains_an_old_book() {
    let open: i64 = 1_791_297_900;
    let at = |s: f64| open * NS + (s * 1e9) as i64;
    let params = StrategyParams { gap_threshold: 0.15, ..Default::default() };
    let mut ec = EngineConfig::new(params, vec!["BTC".into()]);
    ec.venue = Some(PaperVenue::new(150));
    ec.age_outlier_ms = Some(500.0);
    let sink = Shared::default();
    let risk =
        RiskState::new(RiskLimits { max_open_notional_usd: 1e9, daily_loss_limit_usd: 1e9, kill_file: None });
    let mut e = Engine::new(ec, Ledger::new(1e6), risk, Box::new(sink.clone()));
    let w = json!({"type":"window","asset":"BTC","open_unix":open,"kalshi_ticker":"KXBTC15M-X",
        "yes_token":"Y","no_token":"N","condition_id":"0xc"});
    e.handle_line(at(0.1), 'c', &w.to_string());
    let book = |tok: &str, b: f64, a: f64| {
        json!({"event_type":"book","asset_id":tok,"bids":[{"price":b.to_string(),"size":"100"}],
               "asks":[{"price":a.to_string(),"size":"100"}]})
        .to_string()
    };
    e.handle_line(at(0.5), 'p', &book("Y", 0.44, 0.46));
    e.handle_line(at(0.6), 'p', &book("N", 0.54, 0.56));
    let snap = json!({"type":"orderbook_snapshot","msg":{"market_ticker":"KXBTC15M-X",
        "yes_dollars_fp":[["0.5000","10"]],"no_dollars_fp":[["0.4800","10"]]}});
    e.handle_line(at(0.7), 'k', &snap.to_string());
    // 30 s later Kalshi jumps; Polymarket has been silent since 0.6 s.
    let delta = json!({"type":"orderbook_delta","msg":{"market_ticker":"KXBTC15M-X",
        "price_dollars":"0.7000","delta_fp":"10","side":"yes"}});
    e.handle_line(at(30.0), 'k', &delta.to_string());
    let delta2 = json!({"type":"orderbook_delta","msg":{"market_ticker":"KXBTC15M-X",
        "price_dollars":"0.2800","delta_fp":"10","side":"no"}});
    let cancel = json!({"type":"orderbook_delta","msg":{"market_ticker":"KXBTC15M-X",
        "price_dollars":"0.4800","delta_fp":"-10","side":"no"}});
    e.handle_line(at(29.9), 'k', &cancel.to_string());
    e.handle_line(at(30.1), 'k', &delta2.to_string());
    let records = sink.0.lock().unwrap();
    let outlier = records.iter().find(|r| r["type"] == "age_outlier").expect("an age_outlier record");
    assert_eq!(outlier["leg"], "poly_yes");
    assert!(outlier["age_ms"].as_f64().unwrap() > 29_000.0);
    assert!(outlier["poly_feed_silence_ms"].as_f64().unwrap() > 29_000.0);
    assert!(records.iter().any(|r| r["type"] == "decision" && r["action"] == "enter"));
}
