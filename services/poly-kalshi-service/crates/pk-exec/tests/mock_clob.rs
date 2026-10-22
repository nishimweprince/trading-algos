//! The order path end to end against a mock CLOB (h2c on loopback).
//!
//! The mock checks what the real venue would: the L2 HMAC on every
//! authenticated request, and that each order's EIP-712 signature recovers to
//! the wallet. The struct and domain are re-declared here from the exchange's
//! spec, independently of the SDK, so a drift in either shows up as a failure.

use std::collections::HashSet;
use std::net::SocketAddr;
use std::str::FromStr;
use std::sync::Arc;
use std::time::{Duration, Instant};

use alloy::core::sol;
use alloy::primitives::{Address, B256, Signature, U256};
use alloy::signers::local::PrivateKeySigner;
use alloy::sol_types::{SolStruct, eip712_domain};
use axum::Router;
use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, Method, StatusCode, Uri};
use axum::routing::{any, get, post};
use parking_lot::Mutex;
use pk_exec::auth::L2Creds;
use pk_exec::presign::PresignCache;
use pk_exec::{ClobExec, Probe, ProbeLimits, SdkSigner, Trigger, WalletConfig, WarmConfig, WarmHttp};
use serde_json::{Value, json};

sol! {
    struct Order {
        uint256 salt;
        address maker;
        address signer;
        uint256 tokenId;
        uint256 makerAmount;
        uint256 takerAmount;
        uint8 side;
        uint8 signatureType;
        uint256 timestamp;
        bytes32 metadata;
        bytes32 builder;
    }
}

const SECRET: &str = "c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0LTMyYg==";
const API_KEY: &str = "00000000-0000-0000-0000-000000000001";
const KEY: &str = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318";
const TOKEN: &str = "71321045679252212594626385532706912750332728571942532289631379312455583992563";

#[derive(Default)]
struct Mock {
    creds: Option<L2Creds>,
    hmac_ok: u64,
    hmac_bad: u64,
    orders: Vec<Value>,
    recovered: Vec<Address>,
    fail_cancel: bool,
    next_id: u64,
    cancelled: HashSet<String>,
}

type Shared = Arc<Mutex<Mock>>;

fn check_hmac(m: &mut Mock, method: &Method, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> bool {
    let h = |k: &str| headers.get(k).and_then(|v| v.to_str().ok()).unwrap_or_default().to_owned();
    let creds = m.creds.clone().expect("creds");
    let ok = h("POLY_API_KEY") == API_KEY
        && h("POLY_PASSPHRASE") == "pp"
        && h("POLY_ADDRESS") == creds.address
        && h("POLY_TIMESTAMP")
            .parse::<u64>()
            .is_ok_and(|ts| creds.verify(ts, method.as_str(), uri.path(), body, &h("POLY_SIGNATURE")));
    if ok {
        m.hmac_ok += 1;
    } else {
        m.hmac_bad += 1;
    }
    ok
}

fn recover(order: &Value) -> Address {
    let s = |k: &str| order[k].as_str().map(str::to_owned).unwrap_or_else(|| order[k].to_string());
    let u = |k: &str| U256::from_str(&s(k)).unwrap();
    let o = Order {
        salt: U256::from(order["salt"].as_u64().unwrap()),
        maker: Address::from_str(&s("maker")).unwrap(),
        signer: Address::from_str(&s("signer")).unwrap(),
        tokenId: u("tokenId"),
        makerAmount: u("makerAmount"),
        takerAmount: u("takerAmount"),
        side: if s("side") == "BUY" { 0 } else { 1 },
        signatureType: order["signatureType"].as_u64().unwrap() as u8,
        timestamp: u("timestamp"),
        metadata: B256::from_str(&s("metadata")).unwrap(),
        builder: B256::from_str(&s("builder")).unwrap(),
    };
    let domain = eip712_domain! {
        name: "Polymarket CTF Exchange",
        version: "2",
        chain_id: 137,
        verifying_contract: alloy::primitives::address!("0xE111180000d2663C0091e4f400237545B87B996B"),
    };
    let hash = o.eip712_signing_hash(&domain);
    Signature::from_str(&s("signature")).unwrap().recover_address_from_prehash(&hash).unwrap()
}

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
                    "bids": [{"price": "0.44", "size": "100"}, {"price": "0.45", "size": "20"}],
                    "asks": [{"price": "0.48", "size": "50"}, {"price": "0.47", "size": "10"}],
                    "tick_size": "0.01", "min_order_size": "5"
                }))
            }),
        )
        .route(
            "/order",
            any(|State(st): State<Shared>, method: Method, uri: Uri, headers: HeaderMap, body: Bytes| async move {
                let mut m = st.lock();
                if !check_hmac(&mut m, &method, &uri, &headers, &body) {
                    return (StatusCode::UNAUTHORIZED, axum::Json(json!({"error": "bad hmac"})));
                }
                let v: Value = serde_json::from_slice(&body).unwrap();
                if method == Method::POST {
                    let who = recover(&v["order"]);
                    m.recovered.push(who);
                    m.orders.push(v);
                    m.next_id += 1;
                    let id = format!("0xorder{}", m.next_id);
                    (StatusCode::OK, axum::Json(json!({
                        "success": true, "errorMsg": "", "orderID": id, "status": "live",
                        "makingAmount": "", "takingAmount": ""
                    })))
                } else {
                    let id = v["orderID"].as_str().unwrap().to_owned();
                    if m.fail_cancel {
                        (StatusCode::OK, axum::Json(json!({"canceled": [], "not_canceled": {id: "matched"}})))
                    } else {
                        m.cancelled.insert(id.clone());
                        (StatusCode::OK, axum::Json(json!({"canceled": [id], "not_canceled": {}})))
                    }
                }
            }),
        )
        .route(
            "/data/order/{id}",
            get(|Path(_id): Path<String>| async { axum::Json(json!({"status": "MATCHED", "size_matched": "5"})) }),
        )
        .route(
            "/cancel-all",
            any(|State(st): State<Shared>, method: Method, uri: Uri, headers: HeaderMap| async move {
                let mut m = st.lock();
                check_hmac(&mut m, &method, &uri, &headers, b"");
                axum::Json(json!({"canceled": [], "not_canceled": {}}))
            }),
        )
        .with_state(state);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    addr
}

#[tokio::test]
async fn probe_posts_signed_and_cancels_on_a_warm_h2_connection() {
    let state: Shared = Arc::default();
    let addr = serve(state.clone()).await;
    let base = format!("http://{addr}/");

    let signer = SdkSigner::connect(&WalletConfig {
        clob_url: base.clone(),
        private_key: KEY.into(),
        funder: None,
        signature_type: 0,
    })
    .await
    .expect("authenticate against the mock");
    let wallet = PrivateKeySigner::from_str(KEY).unwrap().address();
    assert_eq!(signer.address(), wallet.to_checksum(None));
    state.lock().creds = Some(L2Creds::new(signer.address(), API_KEY.into(), SECRET, "pp".into()).unwrap());

    let mut warm = WarmConfig::new(base.parse().unwrap());
    warm.warm_every = Duration::from_millis(50);
    let http = WarmHttp::connect(warm).await.unwrap();
    http.spawn_maintenance();
    let exec =
        Arc::new(ClobExec::new(http, Arc::new(signer), PresignCache::new(Duration::from_secs(10), 16)));
    let probe = Probe::new(exec.clone(), ProbeLimits::default());

    let top = exec.top(TOKEN).await.unwrap();
    assert_eq!(top.best_bid, Some("0.45".parse().unwrap()));
    assert_eq!(top.best_ask, Some("0.47".parse().unwrap()));

    // Pre-signed: the hot path is one POST.
    probe.presign(TOKEN, &top).await.unwrap();
    let out = probe.run_once(TOKEN, &top, Instant::now(), Trigger::Cadence).await;
    assert!(out.refused.is_none() && !out.filled, "{out:?}");
    assert_eq!(out.records.len(), 2);
    let (post, cancel) = (&out.records[0], &out.records[1]);
    assert!(post.ok && post.presigned, "{post:?}");
    assert_eq!(post.sign_us, 0);
    assert_eq!(post.http, "HTTP/2.0");
    assert!(post.decision_to_ack_us >= post.write_to_headers_us);
    assert_eq!(post.price, Some("0.25".parse().unwrap()));
    assert!(cancel.ok, "{cancel:?}");
    assert_eq!(probe.open_orders(), 0);

    // Not pre-signed: signed inline, counted as a miss.
    let out = probe.run_once(TOKEN, &top, Instant::now(), Trigger::Decision).await;
    assert!(out.records[0].ok && !out.records[0].presigned && out.records[0].sign_us > 0);

    {
        let m = state.lock();
        assert_eq!(m.hmac_bad, 0);
        assert_eq!(m.hmac_ok, 4);
        assert_eq!(m.recovered, vec![wallet, wallet], "EIP-712 signatures recover to the wallet");
        let body = &m.orders[0];
        assert_eq!(body["postOnly"], json!(true));
        assert_eq!(body["orderType"], json!("GTC"));
        assert_eq!(body["order"]["side"], json!("BUY"));
        // 5 shares at 0.25 (20 ticks under the bid): pays 1.25 pUSD for 5 tokens (6 decimals).
        assert_eq!(body["order"]["makerAmount"], json!("1250000"));
        assert_eq!(body["order"]["takerAmount"], json!("5000000"));
        assert_eq!(m.cancelled.len(), 2);
    }

    // A cancel that fails because the order filled halts the probe.
    state.lock().fail_cancel = true;
    let out = probe.run_once(TOKEN, &top, Instant::now(), Trigger::Cadence).await;
    assert!(out.filled);
    assert!(probe.halted());
    let out = probe.run_once(TOKEN, &top, Instant::now(), Trigger::Cadence).await;
    assert!(out.records.is_empty() && out.refused.is_some());

    tokio::time::sleep(Duration::from_millis(150)).await;
    let stats = exec.stats();
    assert_eq!(stats.posts, 3);
    assert_eq!(stats.presign.hits, 1);
    assert_eq!(stats.presign.misses, 2);
    assert_eq!(stats.decision_to_ack_ms.n, 3);
    assert_eq!(stats.cancel_errors, 1);
    assert!(stats.connection.warm_rtt_ms.n >= 1);
    assert_eq!(stats.connection.non_h2, 0);
    let c = probe.counters.lock();
    assert_eq!((c.posted, c.cancelled, c.cancel_failed, c.fills), (3, 2, 1, 1));
}
