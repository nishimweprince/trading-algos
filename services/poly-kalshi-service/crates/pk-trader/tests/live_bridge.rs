//! The live order path end to end against a mock CLOB (h2c on loopback), with
//! the redeemer stubbed: entries matched on the ack and by polling, balance
//! rejects that halt, the reconciler (balance, stray orders, position drift,
//! redemption, the daily-loss backstop), flatten, and the latency it measures.
//! The mock's HMAC and EIP-712 checks live in `pk-exec/tests/mock_clob.rs`.

use std::collections::VecDeque;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::Router;
use axum::body::Bytes;
use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::routing::{any, get, post};
use parking_lot::Mutex;
use pk_engine::OrderIntent;
use pk_exec::presign::PresignCache;
use pk_exec::{ClobExec, LiveExec, LiveLimits, RedeemReceipt, SdkSigner, WalletConfig, WarmConfig, WarmHttp};
use pk_trader::exec_bridge::{BridgeConfig, ExecBridge, ExecMsg, Held, Hooks, LivePart, LiveView, RedeemFn};
use serde_json::{Value, json};

const SECRET: &str = "c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0LTMyYg==";
const API_KEY: &str = "00000000-0000-0000-0000-000000000001";
const KEY: &str = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318";
const TOKEN: &str = "71321045679252212594626385532706912750332728571942532289631379312455583992563";
const WIN: &str = "11111111111111111111111111111111111111111111111111111111111111111111111111111";

#[derive(Default)]
struct Mock {
    /// Responses to `POST /order`, in order: (status, body).
    posts: VecDeque<(u16, Value)>,
    orders: Vec<Value>,
    /// `GET /data/order/{id}`.
    order_state: Value,
    collateral: String,
    /// token → base units.
    tokens: std::collections::HashMap<String, String>,
    open_orders: Vec<Value>,
    cancel_alls: u32,
}

type Shared = Arc<Mutex<Mock>>;

async fn serve(state: Shared) -> SocketAddr {
    let app = Router::new()
        .route(
            "/auth/api-key",
            post(|| async { axum::Json(json!({"apiKey": API_KEY, "secret": SECRET, "passphrase": "pp"})) }),
        )
        .route("/version", get(|| async { axum::Json(json!({"version": 2})) }))
        .route("/tick-size", get(|| async { axum::Json(json!({"minimum_tick_size": 0.01})) }))
        .route("/neg-risk", get(|| async { axum::Json(json!({"neg_risk": false})) }))
        .route("/fee-rate", get(|| async { axum::Json(json!({"base_fee": 0})) }))
        .route("/time", get(|| async { "1700000000" }))
        .route(
            "/book",
            get(|| async {
                axum::Json(json!({
                    "bids": [{"price": "0.44", "size": "100"}],
                    "asks": [{"price": "0.46", "size": "50"}],
                    "tick_size": "0.01", "min_order_size": "5"
                }))
            }),
        )
        .route(
            "/order",
            any(|State(st): State<Shared>, body: Bytes| async move {
                let mut m = st.lock();
                m.orders.push(serde_json::from_slice(&body).unwrap());
                let (status, body) = m.posts.pop_front().expect("an order response queued");
                (StatusCode::from_u16(status).unwrap(), axum::Json(body))
            }),
        )
        .route(
            "/data/order/{id}",
            get(|State(st): State<Shared>, Path(_id): Path<String>| async move {
                axum::Json(st.lock().order_state.clone())
            }),
        )
        .route(
            "/balance-allowance",
            get(|State(st): State<Shared>, Query(q): Query<std::collections::HashMap<String, String>>| async move {
                let m = st.lock();
                let balance = match q.get("token_id") {
                    Some(t) => m.tokens.get(t).cloned().unwrap_or_else(|| "0".into()),
                    None => m.collateral.clone(),
                };
                axum::Json(json!({"balance": balance, "allowances": {}}))
            }),
        )
        .route(
            "/data/orders",
            get(|State(st): State<Shared>| async move { axum::Json(json!({"data": st.lock().open_orders.clone()})) }),
        )
        .route(
            "/cancel-all",
            any(|State(st): State<Shared>| async move {
                let mut m = st.lock();
                m.cancel_alls += 1;
                m.open_orders.clear();
                axum::Json(json!({"canceled": [], "not_canceled": {}}))
            }),
        )
        .with_state(state);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    addr
}

#[derive(Default)]
struct Seen {
    controls: Vec<Value>,
    journal: Vec<Value>,
    alerts: Vec<String>,
    kills: Vec<String>,
    redeemed: Vec<String>,
}

fn intent(open: i64, contracts: i64) -> OrderIntent {
    OrderIntent {
        asset: "BTC".into(),
        open_unix: open,
        token: TOKEN.into(),
        side: "yes",
        entry_px: 0.46,
        limit_px: 0.46,
        contracts,
        decision_ns: 1,
        best_bid: Some(0.44),
        best_ask: Some(0.46),
        condition_id: "0xc".into(),
        fee_rate: 0.07,
        fee_exponent: 1.0,
        reserved: 5.0,
        live: true,
    }
}

async fn wait_for(seen: &Arc<Mutex<Seen>>, kind: &str, n: usize) -> Vec<Value> {
    let t0 = Instant::now();
    loop {
        let got: Vec<Value> = seen.lock().controls.iter().filter(|c| c["type"] == kind).cloned().collect();
        if got.len() >= n {
            return got;
        }
        assert!(t0.elapsed() < Duration::from_secs(10), "waiting for {n} {kind}: {got:?}");
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

#[tokio::test]
async fn live_entries_reconcile_redeem_and_flatten() {
    let state: Shared = Arc::default();
    state.lock().collateral = "123450000".into();
    let addr = serve(state.clone()).await;
    let base = format!("http://{addr}/");
    let signer = SdkSigner::connect(&WalletConfig {
        clob_url: base.clone(),
        private_key: KEY.into(),
        funder: None,
        signature_type: 0,
    })
    .await
    .unwrap();
    let http = WarmHttp::connect(WarmConfig::new(base.parse().unwrap())).await.unwrap();
    let exec =
        Arc::new(ClobExec::new(http, Arc::new(signer), PresignCache::new(Duration::from_secs(10), 16)));

    let seen: Arc<Mutex<Seen>> = Arc::default();
    let view: Arc<Mutex<LiveView>> = Arc::default();
    let hooks = {
        let (s1, s2, s3, s4, v) = (seen.clone(), seen.clone(), seen.clone(), seen.clone(), view.clone());
        Hooks {
            journal: Arc::new(move |_, r| s1.lock().journal.push(r)),
            control: Arc::new(move |c| s2.lock().controls.push(c)),
            alert: Arc::new(move |key, _, _, _| s3.lock().alerts.push(key.to_owned())),
            kill: Arc::new(move |source, _| s4.lock().kills.push(source.to_owned())),
            view: Arc::new(move || v.lock().clone()),
        }
    };
    let redeem: RedeemFn = {
        let (seen, state) = (seen.clone(), state.clone());
        Arc::new(move |c: String| {
            let (seen, state) = (seen.clone(), state.clone());
            Box::pin(async move {
                seen.lock().redeemed.push(c.clone());
                // Redeeming pays out: the shares go, the collateral comes.
                let mut m = state.lock();
                m.tokens.insert(WIN.into(), "0".into());
                m.collateral = "133450000".into();
                Ok(RedeemReceipt { condition_id: c, tx_hash: "0xtx".into(), block: 7 })
            })
        })
    };
    let limits = LiveLimits { poll_every: Duration::from_millis(10), ..LiveLimits::default() };
    let live = LivePart { exec: LiveExec::new(exec.clone(), limits), redeem, gas: None, signature_type: 0 };
    let cfg = BridgeConfig { latency_seed_ms: 150.0, latency_window: 50, latency_min_samples: 1 };
    let bridge = ExecBridge::new(exec, hooks, cfg, Some(live));
    let (tx, rx) = tokio::sync::mpsc::unbounded_channel();
    tokio::spawn(bridge.clone().run(rx));

    // 1. Matched on the acknowledgement.
    state.lock().posts.push_back((
        200,
        json!({"success": true, "orderID": "0xo1", "status": "matched", "makingAmount": "4.6", "takingAmount": "10"}),
    ));
    tx.send(ExecMsg::Intent(intent(900, 10), Instant::now())).unwrap();
    let fill = &wait_for(&seen, "live_fill", 1).await[0];
    assert_eq!((fill["shares"].as_f64(), fill["cost"].as_f64()), (Some(10.0), Some(4.6)));
    assert_eq!((fill["cost_basis"].as_str(), fill["order_id"].as_str()), (Some("ack"), Some("0xo1")));
    {
        let m = state.lock();
        let order = &m.orders[0];
        assert_eq!((order["orderType"].as_str(), order["postOnly"].as_bool()), (Some("FAK"), Some(false)));
        assert_eq!(order["order"]["side"], "BUY");
        // 10 shares at 0.46: pays 4.6 pUSD (6 decimals).
        assert_eq!(order["order"]["makerAmount"], "4600000");
    }
    // The round trip became the paper venue's latency.
    let latency = &wait_for(&seen, "latency", 1).await[0];
    assert_eq!(latency["source"], "measured");

    // 2. Not on the ack: polled until no longer live.
    state.lock().posts.push_back((200, json!({"success": true, "orderID": "0xo2", "status": "delayed"})));
    state.lock().order_state = json!({"status": "MATCHED", "size_matched": "7", "price": "0.46"});
    tx.send(ExecMsg::Intent(intent(1800, 10), Instant::now())).unwrap();
    let fill = &wait_for(&seen, "live_fill", 2).await[1];
    assert_eq!((fill["shares"].as_f64(), fill["cost_basis"].as_str()), (Some(7.0), Some("limit")));
    assert!((fill["cost"].as_f64().unwrap() - 3.22).abs() < 1e-9);

    // 3. Three balance rejects in a row halt trading.
    for _ in 0..3 {
        state.lock().posts.push_back((400, json!({"error": "not enough balance / allowance"})));
    }
    for k in 0..3 {
        tx.send(ExecMsg::Intent(intent(2700 + k, 10), Instant::now())).unwrap();
    }
    let fills = wait_for(&seen, "live_fill", 5).await;
    assert!(
        fills[2..].iter().all(|f| f["shares"] == 0.0 && f["error"].as_str().unwrap().contains("balance"))
    );
    assert_eq!(seen.lock().alerts.iter().filter(|a| *a == "balance_reject").count(), 3);
    assert_eq!(seen.lock().kills, vec!["balance_rejects"]);

    // 4. A refused entry is never posted.
    let posted = state.lock().orders.len();
    tx.send(ExecMsg::Intent(intent(3600, 3), Instant::now())).unwrap();
    let refused = &wait_for(&seen, "live_fill", 6).await[5];
    assert!(refused["error"].as_str().unwrap().contains("under the minimum"));
    assert_eq!(state.lock().orders.len(), posted);

    // 5. Reconcile: the wallet, a stray order, drift, a winner, the daily loss.
    {
        let mut m = state.lock();
        m.open_orders = vec![json!({"id": "0xstray"})];
        m.tokens.insert(TOKEN.into(), "8000000".into());
        m.tokens.insert(WIN.into(), "5000000".into());
    }
    *view.lock() = LiveView {
        open: vec![Held {
            asset: "BTC".into(),
            open_unix: 900,
            side: "yes".into(),
            shares: 10.0,
            token: Some(TOKEN.into()),
            ..Default::default()
        }],
        winners: vec![Held {
            asset: "ETH".into(),
            open_unix: 0,
            side: "no".into(),
            shares: 5.0,
            token: Some(WIN.into()),
            condition_id: Some("0xwin".into()),
            ..Default::default()
        }],
        in_flight: 0,
        day_drawdown: Some(12.0),
        daily_loss_limit: 10.0,
    };
    bridge.reconcile().await;
    let balances = wait_for(&seen, "balance", 2).await;
    assert_eq!(balances[0]["balance"], 123.45);
    // Read again after the redemption paid out.
    assert_eq!(balances[1]["balance"], 133.45);
    {
        let s = seen.lock();
        assert_eq!(s.redeemed, vec!["0xwin"]);
        assert!(s.journal.iter().any(|r| r["type"] == "redeem" && r["tx_hash"] == "0xtx"));
        for key in ["stray_orders", "position_drift"] {
            assert!(s.alerts.iter().any(|a| a == key), "{key}: {:?}", s.alerts);
        }
        assert!(!s.alerts.iter().any(|a| a == "redeem_no_effect"));
        assert_eq!(s.kills, vec!["balance_rejects", "daily_loss"]);
    }
    assert_eq!(state.lock().cancel_alls, 1);
    // Redeemed once: the next pass leaves it alone.
    bridge.reconcile().await;
    assert_eq!(seen.lock().redeemed.len(), 1);

    // 6. An order in flight holds the balance back (its cost would count twice).
    let before = seen.lock().controls.iter().filter(|c| c["type"] == "balance").count();
    view.lock().in_flight = 1;
    bridge.reconcile().await;
    assert_eq!(seen.lock().controls.iter().filter(|c| c["type"] == "balance").count(), before);
    view.lock().in_flight = 0;

    // 7. Flatten sells what is held, under the bid.
    state.lock().posts.push_back((
        200,
        json!({"success": true, "orderID": "0xs1", "status": "matched", "makingAmount": "10", "takingAmount": "4.2"}),
    ));
    let out = bridge.flatten().await;
    assert_eq!(out[0]["sold"], 10.0);
    let exit = &wait_for(&seen, "live_exit", 1).await[0];
    assert_eq!((exit["shares"].as_f64(), exit["proceeds"].as_f64()), (Some(10.0), Some(4.2)));
    let m = state.lock();
    let sell = m.orders.last().unwrap();
    assert_eq!((sell["order"]["side"].as_str(), sell["orderType"].as_str()), (Some("SELL"), Some("FAK")));

    let stats = bridge.stats();
    assert_eq!(stats["mode"], "live");
    assert_eq!(stats["live"]["redeemed"], 1);
}
