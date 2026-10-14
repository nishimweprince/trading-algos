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
    active_exit: bool,
    size_at_limit: bool,
    limit_offset: f64,
    order_shares: Option<i64>,
    min_net_edge: Option<f64>,
}

impl Default for Opts {
    fn default() -> Self {
        Self {
            live: false,
            pct: 0.0,
            role: Role::Full,
            cash: 100.0,
            active_exit: false,
            size_at_limit: false,
            limit_offset: 0.0,
            order_shares: None,
            min_net_edge: None,
        }
    }
}

fn engine(o: Opts) -> (Engine, Shared) {
    let params =
        StrategyParams { gap_threshold: 0.15, notional_usd: 50.0, min_net_edge: o.min_net_edge, ..Default::default() };
    let mut ec = EngineConfig::new(params, vec!["BTC".into()]);
    ec.venue = Some(PaperVenue::new(150));
    ec.role = o.role;
    ec.live = o.live;
    ec.order_size_pct = o.pct;
    ec.follow_tape_latency = true;
    ec.active_exit = o.active_exit;
    ec.size_at_limit = o.size_at_limit;
    ec.limit_offset = o.limit_offset;
    ec.order_shares = o.order_shares;
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
    // A real order is not simulated: its result arrives as `live_fill`.
    assert!(sink.of("fill").is_empty());
    assert!(sink.of("paper_fill").is_empty());
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
    // A drift is applied once two reads agree; the size stays while a position is open.
    let ledger_cash = 100.0 - 4.6 - 0.17388;
    ctl(&mut e, 60.0, json!({"type":"balance","balance":95.0}));
    assert!((e.ledger.cash - ledger_cash).abs() < 1e-3);
    ctl(&mut e, 90.0, json!({"type":"balance","balance":95.0}));
    assert_eq!(e.ledger.cash, 95.0);
    assert_eq!(e.params.notional_usd, 5.0);
    // The seed read (0 → 100), the unconfirmed one and the applied one.
    let rec = sink.of("reconcile");
    let drifts: Vec<f64> = rec.iter().map(|r| r["drift"].as_f64().unwrap()).collect();
    assert_eq!(drifts.len(), 3);
    assert_eq!(rec[1]["skipped"], "unconfirmed");
    assert!(rec[2].get("skipped").is_none());
    assert!((drifts[2] - (95.0 - ledger_cash)).abs() < 1e-3, "{drifts:?}");
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

/// Polymarket's YES book catches up with Kalshi (0.70 / 0.72; Kalshi 0.70 / 0.72).
fn poly_catches_up(e: &mut Engine, s: f64) {
    let pc = json!({"event_type":"price_change","market":"0xc","price_changes":[
        {"asset_id":"Y","price":"0.46","size":"0","side":"SELL"},
        {"asset_id":"Y","price":"0.72","size":"100","side":"SELL"},
        {"asset_id":"Y","price":"0.70","size":"100","side":"BUY"}]});
    e.handle_line(at(s), 'p', &pc.to_string());
}

#[test]
fn paper_active_exit_sells_at_kalshi_fair_and_sizes_at_the_limit() {
    let opts = Opts { active_exit: true, size_at_limit: true, limit_offset: 0.02, ..Default::default() };
    let (mut e, sink) = engine(opts);
    setup(&mut e);
    cross(&mut e, 30.0);
    // $50 at the 0.48 limit, not the 0.46 touch: 104 shares, not 108.
    assert_eq!(sink.of("decision")[0]["contracts"], 104);
    tick(&mut e, 30.5);
    assert_eq!(sink.of("fill")[0]["shares"], 104.0);
    poly_catches_up(&mut e, 35.0);
    let exit = &sink.of("exit")[0];
    assert_eq!((exit["reason"].as_str(), exit["venue"].as_str()), (Some("fair"), Some("paper")));
    assert_eq!((exit["exit_px"].as_f64(), exit["remaining"].as_f64()), (Some(0.7), Some(0.0)));
    assert!((exit["held_s"].as_f64().unwrap() - 4.5).abs() < 0.2);
    let settle = &sink.of("settle")[0];
    assert_eq!(settle["outcome"], "exit");
    assert!(settle["pnl"].as_f64().unwrap() > 0.0);
    assert_eq!(e.ledger.open_positions().count(), 0);
    // Polymarket's later resolution finds nothing left to settle.
    let resolved = json!({"type":"resolution","venue":"poly","asset":"BTC","open_unix":OPEN,"outcome":"no"});
    ctl(&mut e, 905.0, resolved);
    assert_eq!(sink.of("settle").len(), 1);
}

#[test]
fn live_active_exit_is_an_intent_and_its_result_closes() {
    let (mut e, sink) = engine(Opts { live: true, cash: 0.0, active_exit: true, ..Default::default() });
    setup(&mut e);
    ctl(&mut e, 1.0, json!({"type":"balance","balance":100.0}));
    cross(&mut e, 30.0);
    e.intents.clear();
    ctl(
        &mut e,
        30.6,
        json!({"type":"live_fill","asset":"BTC","open_unix":OPEN,"side":"yes","token":"Y",
               "order_id":"0xo","ordered":10.0,"shares":10.0,"cost":4.6,"entry_px":0.46,
               "limit_px":0.46,"status":"matched","decision_ns":at(30.02)}),
    );
    poly_catches_up(&mut e, 35.0);
    let exits = std::mem::take(&mut e.exits);
    assert_eq!(exits.len(), 1);
    let x = &exits[0];
    assert_eq!((x.token.as_str(), x.shares, x.bid, x.fair), ("Y", 10.0, 0.7, 0.71));
    assert_eq!(sink.of("exit_signal").len(), 1);
    // Not repeated while the order is in flight.
    poly_catches_up(&mut e, 36.0);
    assert!(e.exits.is_empty());
    ctl(
        &mut e,
        36.5,
        json!({"type":"live_exit","asset":"BTC","open_unix":OPEN,"reason":"fair","shares":10.0,
               "proceeds":6.9,"fee":0.15,"order_id":"0xs"}),
    );
    let exit = &sink.of("exit")[0];
    assert_eq!((exit["reason"].as_str(), exit["venue"].as_str()), (Some("fair"), Some("live")));
    assert_eq!(sink.of("settle")[0]["outcome"], "exit");
    assert_eq!(e.ledger.open_positions().count(), 0);
}

#[test]
fn a_missed_live_exit_rearms_at_once_or_after_a_balance_second() {
    let (mut e, sink) = engine(Opts { live: true, cash: 0.0, active_exit: true, ..Default::default() });
    setup(&mut e);
    ctl(&mut e, 1.0, json!({"type":"balance","balance":100.0}));
    cross(&mut e, 30.0);
    ctl(
        &mut e,
        30.6,
        json!({"type":"live_fill","asset":"BTC","open_unix":OPEN,"side":"yes","token":"Y",
               "order_id":"0xo","ordered":10.0,"shares":10.0,"cost":4.6,"entry_px":0.46,
               "limit_px":0.46,"status":"matched","decision_ns":at(30.02)}),
    );
    poly_catches_up(&mut e, 35.0);
    let x = std::mem::take(&mut e.exits).pop().unwrap();
    // The floor: the 0.46 entry, its fee and a cent, under the 0.70 bid.
    assert!(x.min_px > 0.47 && x.min_px < 0.70, "{}", x.min_px);
    // The FAK was killed: the next update may try again at once.
    ctl(&mut e, 35.2, json!({"type":"exit_miss","asset":"BTC","open_unix":OPEN,"reason":"killed"}));
    poly_catches_up(&mut e, 35.3);
    assert_eq!(std::mem::take(&mut e.exits).len(), 1);
    // The shares were not credited yet: wait a second.
    ctl(&mut e, 35.5, json!({"type":"exit_miss","asset":"BTC","open_unix":OPEN,"reason":"balance"}));
    poly_catches_up(&mut e, 35.9);
    assert!(e.exits.is_empty());
    poly_catches_up(&mut e, 36.6);
    assert_eq!(std::mem::take(&mut e.exits).len(), 1);
    let misses = sink.of("exit_miss");
    assert_eq!(misses.iter().map(|m| m["misses"].as_u64().unwrap()).collect::<Vec<_>>(), [1, 2]);
    // Past the cap a miss waits the full retry.
    for i in 0..6 {
        ctl(&mut e, 37.0 + i as f64 * 0.01, json!({"type":"exit_miss","asset":"BTC","open_unix":OPEN,"reason":"killed"}));
    }
    poly_catches_up(&mut e, 38.0);
    assert!(e.exits.is_empty());
    poly_catches_up(&mut e, 42.2);
    assert_eq!(e.exits.len(), 1);
}

#[test]
fn a_wallet_read_during_an_exit_is_not_counted_twice() {
    let (mut e, sink) = engine(Opts { live: true, cash: 0.0, active_exit: true, ..Default::default() });
    setup(&mut e);
    ctl(&mut e, 1.0, json!({"type":"balance","balance":100.0}));
    cross(&mut e, 30.0);
    ctl(
        &mut e,
        30.6,
        json!({"type":"live_fill","asset":"BTC","open_unix":OPEN,"side":"yes","token":"Y",
               "order_id":"0xo","ordered":10.0,"shares":10.0,"cost":4.6,"entry_px":0.46,
               "limit_px":0.46,"status":"matched","decision_ns":at(30.02)}),
    );
    poly_catches_up(&mut e, 35.0);
    assert_eq!(std::mem::take(&mut e.exits).len(), 1);
    let after_exit = e.ledger.cash + 6.9 - 0.15;
    // The wallet already holds the sale's proceeds; its result has not arrived.
    ctl(&mut e, 36.0, json!({"type":"balance","balance":after_exit}));
    assert_eq!(sink.of("reconcile").last().unwrap()["skipped"], "in_flight");
    ctl(
        &mut e,
        36.5,
        json!({"type":"live_exit","asset":"BTC","open_unix":OPEN,"reason":"fair","shares":10.0,
               "proceeds":6.9,"fee":0.15,"order_id":"0xs"}),
    );
    assert!((e.ledger.cash - after_exit).abs() < 1e-9);
    // The wallet catching up later is no drift at all.
    ctl(&mut e, 66.0, json!({"type":"balance","balance":after_exit}));
    assert_eq!(sink.of("reconcile").len(), 2, "the seed and the skipped read only");
    // A read that misses the proceeds just credited, once, changes nothing.
    ctl(&mut e, 96.0, json!({"type":"balance","balance":after_exit - 6.75}));
    assert!((e.ledger.cash - after_exit).abs() < 1e-9);
    ctl(&mut e, 126.0, json!({"type":"balance","balance":after_exit}));
    assert!((e.ledger.cash - after_exit).abs() < 1e-9);
}

#[test]
fn net_edge_caps_the_limit_and_refuses_a_dear_ask() {
    // Kalshi's YES is 0.71 after the cross; both fees leave 20c only up to 0.47.
    let opts = Opts { limit_offset: 0.06, min_net_edge: Some(0.20), ..Default::default() };
    let (mut e, sink) = engine(opts);
    setup(&mut e);
    cross(&mut e, 30.0);
    tick(&mut e, 30.5);
    assert_eq!(sink.of("fill")[0]["limit_px"], 0.47);
    // 25c is more than a 0.46 ask leaves: no entry, and the window stays open.
    let (mut e, sink) = engine(Opts { limit_offset: 0.06, min_net_edge: Some(0.25), ..Default::default() });
    setup(&mut e);
    cross(&mut e, 30.0);
    tick(&mut e, 30.5);
    assert!(sink.of("fill").is_empty());
    assert!(!e.state[0].traded);
}

#[test]
fn fixed_shares_size_every_entry() {
    let opts = Opts { order_shares: Some(10), size_at_limit: true, limit_offset: 0.06, ..Default::default() };
    let (mut e, sink) = engine(opts);
    setup(&mut e);
    cross(&mut e, 30.0);
    assert_eq!(sink.of("decision")[0]["contracts"], 10);
    assert_eq!(e.contracts_for(0.05, None), 10);
    tick(&mut e, 30.5);
    assert_eq!(sink.of("fill")[0]["ordered"], 10);
}

#[derive(Default)]
struct MockGate {
    fire: Mutex<Option<pk_engine::FastFire>>,
    fast_claimed: bool,
    claims: Mutex<u32>,
}

impl pk_engine::FastGate for MockGate {
    fn fired(&self, _asset: &str, _open: i64) -> Option<pk_engine::FastFire> {
        self.fire.lock().unwrap().take()
    }
    fn claim(&self, asset: &str, open: i64) -> pk_engine::Claim {
        *self.claims.lock().unwrap() += 1;
        if self.fast_claimed {
            let f = pk_engine::FastFire {
                asset: asset.into(),
                open_unix: open,
                buy_yes: true,
                entry_px: 0.46,
                limit_px: 0.46,
                contracts: 10,
                gap: 0.25,
                t_ns: 0,
            };
            pk_engine::Claim::Fast(f)
        } else {
            pk_engine::Claim::Engine
        }
    }
}

/// Kalshi to 0.57 / 0.59 (mid 0.58): a 0.13 gap, near the 0.15 threshold.
fn near(e: &mut Engine, s: f64) {
    let bid = json!({"type":"orderbook_delta","msg":{"market_ticker":"KXBTC15M-X",
        "price_dollars":"0.5700","delta_fp":"10","side":"yes"}});
    let drop_ask = json!({"type":"orderbook_delta","msg":{"market_ticker":"KXBTC15M-X",
        "price_dollars":"0.5300","delta_fp":"-10","side":"no"}});
    let ask = json!({"type":"orderbook_delta","msg":{"market_ticker":"KXBTC15M-X",
        "price_dollars":"0.4100","delta_fp":"10","side":"no"}});
    e.handle_line(at(s), 'k', &bid.to_string());
    e.handle_line(at(s + 0.01), 'k', &drop_ask.to_string());
    e.handle_line(at(s + 0.02), 'k', &ask.to_string());
}

fn gated(gate: Arc<MockGate>) -> (Engine, Shared) {
    let (mut e, sink) = engine(Opts { live: true, pct: 5.0, cash: 0.0, ..Default::default() });
    e.set_fast_gate(gate);
    setup(&mut e);
    ctl(&mut e, 1.0, json!({"type":"balance","balance":100.0}));
    (e, sink)
}

#[test]
fn the_engine_arms_near_the_threshold_and_records_a_fast_entry() {
    let gate = Arc::new(MockGate::default());
    let (mut e, sink) = gated(gate.clone());
    near(&mut e, 25.0);
    let arms = std::mem::take(&mut e.arms);
    let arm = arms
        .iter()
        .find_map(|u| match u {
            pk_engine::ArmUpdate::Arm(a) => Some(a.clone()),
            _ => None,
        })
        .expect("armed");
    let yes = arm.yes.as_ref().unwrap();
    assert_eq!((yes.token.as_str(), yes.entry_px, yes.contracts), ("Y", 0.46, 10));
    assert_eq!(arm.poly_yes.ask, Some(0.46));
    // The trigger sent the entry on the next Kalshi frame; the engine records it.
    *gate.fire.lock().unwrap() = Some(pk_engine::FastFire {
        asset: "BTC".into(),
        open_unix: OPEN,
        buy_yes: true,
        entry_px: 0.46,
        limit_px: 0.46,
        contracts: 10,
        gap: 0.25,
        t_ns: at(30.0),
    });
    cross(&mut e, 30.0);
    let d = &sink.of("decision")[0];
    assert_eq!(d["action"], "enter");
    assert_eq!((d["fast"].as_bool(), d["contracts"].as_i64()), (Some(true), Some(10)));
    assert!(e.intents.is_empty(), "a fast entry is not sent again");
    assert_eq!(e.ledger.in_flight(), 1);
    assert_eq!(*gate.claims.lock().unwrap(), 0);
    assert!(e.arms.iter().any(|u| matches!(u, pk_engine::ArmUpdate::Disarm { .. })));
}

#[test]
fn the_engine_does_not_send_a_window_the_trigger_claimed() {
    let gate = Arc::new(MockGate { fast_claimed: true, ..Default::default() });
    let (mut e, sink) = gated(gate.clone());
    cross(&mut e, 30.0);
    assert_eq!(sink.of("decision")[0]["action"], "enter");
    assert!(e.intents.is_empty());
    assert_eq!(*gate.claims.lock().unwrap(), 1);
    assert_eq!(e.counters.get("fast_raced"), Some(&1));
}
