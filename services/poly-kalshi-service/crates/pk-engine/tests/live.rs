//! The order path's control lines: the measured latency, percentage sizing that
//! is recomputed only when flat, and live entries whose results arrive as
//! `live_fill` / `balance` lines. Replaying the same lines gives the same ledger.

use std::sync::{Arc, Mutex};

use pk_core::journal::Sink;
use pk_core::{Ledger, RiskLimits, RiskState, StrategyParams};
use pk_engine::{Engine, EngineConfig, PaperVenue, Role};
use serde_json::{Value, json};

const NS: i64 = 1_000_000_000;
const OPEN: i64 = 1_791_297_900;

#[derive(Clone, Default)]
struct Shared(Arc<Mutex<Vec<Value>>>);

impl Sink for Shared {
    fn write(&mut self, _t: i64, r: &Value) {
        self.0.lock().unwrap().push(r.clone());
    }
}

impl Shared {
    fn of(&self, kind: &str) -> Vec<Value> {
        self.0.lock().unwrap().iter().filter(|r| r["type"] == kind).cloned().collect()
    }
}

fn at(s: f64) -> i64 {
    OPEN * NS + (s * 1e9) as i64
}

struct Opts {
    live: bool,
    pct: f64,
    role: Role,
    cash: f64,
}

impl Default for Opts {
    fn default() -> Self {
        Self { live: false, pct: 0.0, role: Role::Full, cash: 100.0 }
    }
}

fn engine(o: Opts) -> (Engine, Shared) {
    let params = StrategyParams { gap_threshold: 0.15, notional_usd: 50.0, ..Default::default() };
    let mut ec = EngineConfig::new(params, vec!["BTC".into()]);
    ec.venue = Some(PaperVenue::new(150));
    ec.role = o.role;
    ec.live = o.live;
    ec.order_size_pct = o.pct;
    ec.follow_tape_latency = true;
    let sink = Shared::default();
    let risk =
        RiskState::new(RiskLimits { max_open_notional_usd: 1e9, daily_loss_limit_usd: 1e9, kill_file: None });
    let mode = if o.live { "live" } else { "paper" };
    let ledger = Ledger::open_mode(None, o.cash, None, mode).unwrap();
    (Engine::new(ec, ledger, risk, Box::new(sink.clone())), sink)
}

fn ctl(e: &mut Engine, s: f64, v: Value) {
    e.handle_line(at(s), 'c', &v.to_string());
}

/// A window whose Kalshi side jumps at 30 s: the rule buys YES at 0.46.
fn setup(e: &mut Engine) {
    ctl(
        e,
        0.1,
        json!({"type":"window","asset":"BTC","open_unix":OPEN,"kalshi_ticker":"KXBTC15M-X",
               "yes_token":"Y","no_token":"N","condition_id":"0xc"}),
    );
    let book = |tok: &str, b: f64, a: f64| {
        json!({"event_type":"book","asset_id":tok,"bids":[{"price":b.to_string(),"size":"500"}],
               "asks":[{"price":a.to_string(),"size":"500"}]})
        .to_string()
    };
    e.handle_line(at(0.5), 'p', &book("Y", 0.44, 0.46));
    e.handle_line(at(0.6), 'p', &book("N", 0.54, 0.56));
    let snap = json!({"type":"orderbook_snapshot","msg":{"market_ticker":"KXBTC15M-X",
        "yes_dollars_fp":[["0.4500","10"]],"no_dollars_fp":[["0.5300","10"]]}});
    e.handle_line(at(0.7), 'k', &snap.to_string());
}

fn cross(e: &mut Engine, s: f64) {
    let up = json!({"type":"orderbook_delta","msg":{"market_ticker":"KXBTC15M-X",
        "price_dollars":"0.7000","delta_fp":"10","side":"yes"}});
    let down = json!({"type":"orderbook_delta","msg":{"market_ticker":"KXBTC15M-X",
        "price_dollars":"0.5300","delta_fp":"-10","side":"no"}});
    let no = json!({"type":"orderbook_delta","msg":{"market_ticker":"KXBTC15M-X",
        "price_dollars":"0.2800","delta_fp":"10","side":"no"}});
    e.handle_line(at(s), 'k', &up.to_string());
    e.handle_line(at(s + 0.01), 'k', &down.to_string());
    e.handle_line(at(s + 0.02), 'k', &no.to_string());
}

/// Any line: lets the engine reach a pending order's due time.
fn tick(e: &mut Engine, s: f64) {
    ctl(e, s, json!({"type":"noop"}));
}

#[test]
fn measured_latency_moves_the_paper_fill() {
    let (mut e, sink) = engine(Opts::default());
    setup(&mut e);
    ctl(&mut e, 1.0, json!({"type":"latency","ms":500.0,"n":12,"source":"probe"}));
    assert_eq!(e.venue.unwrap().latency_ns, 500_000_000);
    assert_eq!(sink.of("latency")[0]["ms"], 500.0);
    cross(&mut e, 30.0);
    assert_eq!(sink.of("decision")[0]["action"], "enter");
    tick(&mut e, 30.3);
    assert!(sink.of("fill").is_empty(), "150 ms would have filled by now");
    tick(&mut e, 30.6);
    let fill = &sink.of("fill")[0];
    assert!(fill["latency_ms"].as_f64().unwrap() >= 500.0);
}

#[test]
fn paper_size_compounds_only_when_flat() {
    let (mut e, sink) = engine(Opts { pct: 5.0, ..Default::default() });
    setup(&mut e);
    ctl(&mut e, 1.0, json!({"type":"sizing","balance":100.0}));
    assert_eq!(e.params.notional_usd, 5.0);
    cross(&mut e, 30.0);
    // floor($5 / 0.46) = 10 shares.
    assert_eq!(sink.of("decision")[0]["contracts"], 10);
    tick(&mut e, 30.5);
    assert_eq!(sink.of("fill")[0]["shares"], 10.0);
    ctl(
        &mut e,
        905.0,
        json!({"type":"resolution","venue":"poly","asset":"BTC","open_unix":OPEN,"outcome":"yes"}),
    );
    let sizing = sink.of("sizing");
    assert_eq!(sizing.len(), 2, "{sizing:?}");
    assert_eq!(sizing[1]["source"], "settle");
    let cash = e.ledger.cash;
    assert!(cash > 100.0);
    assert_eq!(e.params.notional_usd, ((cash * 5.0) + 1e-9).floor() / 100.0);
}

fn live_run() -> (Engine, Shared) {
    let (mut e, sink) = engine(Opts { live: true, pct: 5.0, cash: 0.0, ..Default::default() });
    setup(&mut e);
    // The first wallet read seeds the size.
    ctl(&mut e, 1.0, json!({"type":"balance","balance":100.0}));
    assert_eq!(e.params.notional_usd, 5.0);
    cross(&mut e, 30.0);
    let intents = std::mem::take(&mut e.intents);
    assert_eq!(intents.len(), 1);
    let i = &intents[0];
    assert!(i.live);
    assert_eq!((i.token.as_str(), i.contracts, i.condition_id.as_str()), ("Y", 10, "0xc"));
    assert_eq!(e.ledger.in_flight(), 1);
    tick(&mut e, 30.5);
    // The paper venue only shadows a real order.
    assert!(sink.of("fill").is_empty());
    assert_eq!(sink.of("paper_fill").len(), 1);
    assert!(e.ledger.positions.is_empty());
    ctl(
        &mut e,
        30.6,
        json!({"type":"live_fill","asset":"BTC","open_unix":OPEN,"side":"yes","token":"Y",
               "order_id":"0xo","ordered":10.0,"shares":10.0,"cost":4.6,"entry_px":0.46,
               "limit_px":0.46,"status":"matched","decision_ns":at(30.02)}),
    );
    (e, sink)
}

#[test]
fn live_entries_wait_for_the_venue() {
    let (mut e, sink) = live_run();
    assert_eq!(e.ledger.in_flight(), 0);
    let fill = &sink.of("fill")[0];
    assert_eq!((fill["venue"].as_str(), fill["shares"].as_f64()), (Some("live"), Some(10.0)));
    assert!((fill["fee"].as_f64().unwrap() - 10.0 * 0.07 * 0.46 * 0.54).abs() < 1e-4);
    assert_eq!(e.ledger.open_positions().count(), 1);
    // While a position is open the wallet read reconciles cash but keeps the size.
    ctl(&mut e, 60.0, json!({"type":"balance","balance":95.0}));
    assert_eq!(e.params.notional_usd, 5.0);
    // The seed read (0 → 100) and this one both drifted from the ledger.
    let drifts: Vec<f64> = sink.of("reconcile").iter().map(|r| r["drift"].as_f64().unwrap()).collect();
    assert_eq!(drifts.len(), 2);
    assert!((drifts[1] - (95.0 - (100.0 - 4.6 - 0.17388))).abs() < 1e-3, "{drifts:?}");
    ctl(
        &mut e,
        905.0,
        json!({"type":"resolution","venue":"poly","asset":"BTC","open_unix":OPEN,"outcome":"yes"}),
    );
    assert_eq!(e.ledger.open_positions().count(), 0);
    // Settled, redeemed, flat: the next wallet read sets the next size.
    assert_eq!(sink.of("sizing").len(), 1);
    ctl(&mut e, 960.0, json!({"type":"balance","balance":105.0}));
    assert_eq!(e.params.notional_usd, 5.25);
    assert_eq!(sink.of("sizing")[1]["source"], "reconcile");
}

#[test]
fn live_exit_closes_and_no_fill_releases() {
    let (mut e, sink) = live_run();
    ctl(
        &mut e,
        40.0,
        json!({"type":"live_exit","asset":"BTC","open_unix":OPEN,"shares":10.0,"proceeds":4.0,"fee":0.1}),
    );
    assert_eq!(e.ledger.open_positions().count(), 0);
    assert_eq!(sink.of("exit")[0]["remaining"], 0.0);
    assert!(e.ledger.realised_today(at(40.0)) < 0.0);

    let (mut e, sink) = engine(Opts { live: true, ..Default::default() });
    setup(&mut e);
    cross(&mut e, 30.0);
    assert_eq!(e.ledger.in_flight(), 1);
    ctl(
        &mut e,
        30.5,
        json!({"type":"live_fill","asset":"BTC","open_unix":OPEN,"side":"yes","shares":0.0,"cost":0.0,"ordered":108.0}),
    );
    assert_eq!((e.ledger.in_flight(), e.ledger.positions.len()), (0, 0));
    assert_eq!(sink.of("fill")[0]["shares"], 0.0);
}

#[test]
fn a_paper_replay_keeps_its_own_latency_and_fills() {
    let params = StrategyParams { gap_threshold: 0.15, ..Default::default() };
    let mut ec = EngineConfig::new(params, vec!["BTC".into()]);
    ec.venue = Some(PaperVenue::new(150));
    let risk =
        RiskState::new(RiskLimits { max_open_notional_usd: 1e9, daily_loss_limit_usd: 1e9, kill_file: None });
    let mut e = Engine::new(ec, Ledger::new(100.0), risk, Box::new(Shared::default()));
    setup(&mut e);
    ctl(&mut e, 1.0, json!({"type":"latency","ms":500.0}));
    ctl(&mut e, 2.0, json!({"type":"balance","balance":1.0}));
    ctl(
        &mut e,
        3.0,
        json!({"type":"live_fill","asset":"BTC","open_unix":OPEN,"side":"yes","shares":3.0,"cost":1.0}),
    );
    assert_eq!(e.venue.unwrap().latency_ns, 150_000_000);
    assert_eq!((e.ledger.cash, e.ledger.positions.len()), (100.0, 0));
}

#[test]
fn the_observer_ignores_order_path_lines() {
    let (mut e, _) = engine(Opts { role: Role::Observe, pct: 5.0, ..Default::default() });
    setup(&mut e);
    ctl(&mut e, 1.0, json!({"type":"balance","balance":1.0}));
    ctl(
        &mut e,
        2.0,
        json!({"type":"live_fill","asset":"BTC","open_unix":OPEN,"side":"yes","shares":3.0,"cost":1.0}),
    );
    assert_eq!((e.ledger.cash, e.ledger.positions.len()), (100.0, 0));
}

#[test]
fn replay_gives_the_same_ledger() {
    let (a, sa) = live_run();
    let (b, sb) = live_run();
    assert_eq!(a.ledger.cash, b.ledger.cash);
    assert_eq!(a.ledger.positions, b.ledger.positions);
    assert_eq!(*sa.0.lock().unwrap(), *sb.0.lock().unwrap());
}
