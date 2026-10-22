//! The trader and its observer, end to end, against mock venues on loopback:
//! Gamma and Kalshi REST for discovery, the Kalshi and Polymarket websockets
//! for books. Kalshi's YES mid sits 0.26 above Polymarket's, so the first
//! update inside the window is an entry; the paper venue fills it 150 ms later.
//!
//! Checks: readiness, the decision and the fill on `/v1/status`, both feeds'
//! reconnect control lines, the observer's API, and after SIGTERM the journal
//! (written by the observer) and the zstd tape with its manifest.

use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::time::{Duration, Instant};

use axum::Router;
use axum::extract::Query;
use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::routing::get;
use futures_util::StreamExt;
use rsa::pkcs8::EncodePrivateKey;
use serde_json::{Value, json};

const API_KEY: &str = "e2e-api-key-at-least-16";

fn now_s() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs() as i64
}

fn iso(t: i64) -> String {
    chrono::DateTime::<chrono::Utc>::from_timestamp(t, 0)
        .unwrap()
        .to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}

async fn gamma(Query(q): Query<std::collections::HashMap<String, String>>) -> axum::Json<Value> {
    let slug = q.get("slug").cloned().unwrap_or_default();
    let open: i64 = slug.rsplit('-').next().and_then(|s| s.parse().ok()).unwrap_or(0);
    axum::Json(json!([{ "markets": [{
        "outcomes": "[\"Up\", \"Down\"]",
        "clobTokenIds": format!("[\"Y{open}\", \"N{open}\"]"),
        "conditionId": format!("0xc{open}"),
        "eventStartTime": iso(open),
        "feeSchedule": {"rate": 0.07, "exponent": 1},
        "closed": false,
    }]}]))
}

async fn kalshi_markets(Query(q): Query<std::collections::HashMap<String, String>>) -> axum::Json<Value> {
    let min: i64 = q.get("min_close_ts").and_then(|s| s.parse().ok()).unwrap_or(0) + 1;
    let markets: Vec<Value> = [min, min + 900]
        .iter()
        .map(|close| {
            let open = close - 900;
            json!({"ticker": format!("KXBTC15M-{open}"), "open_time": iso(open), "close_time": iso(*close)})
        })
        .collect();
    axum::Json(json!({"markets": markets}))
}

async fn kalshi_ws(ws: WebSocketUpgrade) -> axum::response::Response {
    ws.on_upgrade(|mut socket: WebSocket| async move {
        let mut seq = 0;
        while let Some(Ok(msg)) = socket.next().await {
            let Message::Text(t) = msg else { continue };
            let cmd: Value = serde_json::from_str(&t).unwrap_or_default();
            let tickers: Vec<String> =
                serde_json::from_value(cmd["params"]["market_tickers"].clone()).unwrap_or_default();
            if cmd["cmd"] == "subscribe" {
                let _ = socket
                    .send(Message::text(
                        json!({"type": "subscribed", "id": cmd["id"], "msg": {"sid": 1}}).to_string(),
                    ))
                    .await;
            }
            for ticker in tickers {
                seq += 1;
                let snap = json!({"type": "orderbook_snapshot", "sid": 1, "seq": seq, "msg": {
                    "market_ticker": ticker,
                    "yes_dollars_fp": [["0.7000", "500.00"]],
                    "no_dollars_fp": [["0.2800", "500.00"]],
                }});
                let _ = socket.send(Message::text(snap.to_string())).await;
            }
        }
    })
}

async fn poly_ws(ws: WebSocketUpgrade) -> axum::response::Response {
    ws.on_upgrade(|mut socket: WebSocket| async move {
        while let Some(Ok(msg)) = socket.next().await {
            let Message::Text(t) = msg else { continue };
            if t.as_str() == "PING" {
                let _ = socket.send(Message::text("PONG")).await;
                continue;
            }
            let sub: Value = serde_json::from_str(&t).unwrap_or_default();
            let ids: Vec<String> = serde_json::from_value(sub["assets_ids"].clone()).unwrap_or_default();
            let frames: Vec<Value> = ids
                .iter()
                .map(|id| {
                    let (b, a) = if id.starts_with('Y') { ("0.44", "0.46") } else { ("0.54", "0.56") };
                    json!({"event_type": "book", "asset_id": id, "timestamp": "1",
                           "bids": [{"price": b, "size": "300"}], "asks": [{"price": a, "size": "500"}]})
                })
                .collect();
            let _ = socket.send(Message::text(Value::Array(frames).to_string())).await;
            // Something later than the paper latency, so the pending order resolves.
            for _ in 0..20 {
                tokio::time::sleep(Duration::from_millis(100)).await;
                let ping = json!({"event_type": "tick_size_change", "asset_id": ids.first()});
                if socket.send(Message::text(ping.to_string())).await.is_err() {
                    return;
                }
            }
        }
    })
}

async fn mock() -> SocketAddr {
    let app = Router::new()
        .route("/events", get(gamma))
        .route("/trade-api/v2/markets", get(kalshi_markets))
        .route("/trade-api/v2/exchange/status", get(|| async { axum::Json(json!({"trading_active": true})) }))
        .route("/trade-api/v2/markets/{t}", get(|| async { axum::Json(json!({"market": {"result": ""}})) }))
        .route("/kalshi", get(kalshi_ws))
        .route("/poly", get(poly_ws));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    addr
}

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port()
}

struct Kill(Child);

impl Drop for Kill {
    fn drop(&mut self) {
        let _ = self.0.kill();
    }
}

async fn get_json(url: &str) -> Option<(u16, Value)> {
    let r = reqwest::Client::new().get(url).header("X-API-Key", API_KEY).send().await.ok()?;
    let status = r.status().as_u16();
    Some((status, r.json().await.ok()?))
}

fn files(dir: &Path, ext: &str) -> Vec<PathBuf> {
    let mut out = Vec::new();
    for e in std::fs::read_dir(dir).into_iter().flatten().flatten() {
        let p = e.path();
        if p.is_dir() {
            out.extend(files(&p, ext));
        } else if p.to_string_lossy().ends_with(ext) {
            out.push(p);
        }
    }
    out
}

#[tokio::test(flavor = "multi_thread")]
async fn trader_and_observer_end_to_end() {
    // The mock feeds cross right after connecting and then go quiet, which the
    // Polymarket silence gates (tested in pk-engine) would rightly block: off here.
    // Not in the last 50 s of a window: the test needs the window to stay put
    // while it waits for a fill (entries run to the close: PK_ENTRY_LATE_S=0).
    let into = now_s() % 900;
    if into > 850 {
        tokio::time::sleep(Duration::from_secs((905 - into) as u64)).await;
    }
    let venues = mock().await;
    let dir = std::env::temp_dir().join(format!("pk-e2e-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let key = rsa::RsaPrivateKey::new(&mut rand::thread_rng(), 2048).unwrap();
    std::fs::write(dir.join("kalshi.pem"), key.to_pkcs8_pem(rsa::pkcs8::LineEnding::LF).unwrap().as_bytes())
        .unwrap();
    let (port, obs_port, status_port) = (free_port(), free_port(), free_port());
    let env = format!(
        "API_KEY={API_KEY}\nPORT={port}\nPK_OBSERVER_PORT={obs_port}\nPK_STATUS_PORT={}\nPK_ASSETS=BTC\n\
         PK_GAP_THRESHOLD=0.15\nPK_ENTRY_EARLY_S=0\nPK_ENTRY_LATE_S=0\nPK_MAX_OPEN_NOTIONAL_USD=500\nPK_DAILY_LOSS_LIMIT_USD=250\n\
         PK_RESULTS_EPOCH=7\nPK_HOST_TAG=e2e\nPK_MIN_FREE_DISK_GB=0\nKALSHI_API_KEY_ID=kid\n\
         KALSHI_PRIVATE_KEY_PATH={}\nKALSHI_REST_URL=http://{venues}/trade-api/v2\nKALSHI_WS_URL=ws://{venues}/kalshi\n\
         POLY_GAMMA_URL=http://{venues}\nPOLY_WS_URL=ws://{venues}/poly\nPK_HEARTBEAT_SECONDS=5\nLOG_LEVEL=INFO\n\
         PK_MAX_POLY_SILENCE_MS=0\nPK_MAX_ASSET_SILENCE_MS=0\nPK_POLY_SILENCE_RECONNECT_S=0\n",
        status_port,
        dir.join("kalshi.pem").display()
    );
    std::fs::write(dir.join(".env.e2e"), env).unwrap();
    let child = Command::new(env!("CARGO_BIN_EXE_poly-kalshi-service"))
        .args(["--profile", "e2e"])
        .current_dir(&dir)
        .env_remove("HTTPS_PROXY")
        .env_remove("https_proxy")
        .env_remove("HTTP_PROXY")
        .env_remove("http_proxy")
        .env("NO_PROXY", "127.0.0.1,localhost")
        .spawn()
        .unwrap();
    let mut child = Kill(child);

    let base = format!("http://127.0.0.1:{port}");
    let deadline = Instant::now() + Duration::from_secs(30);
    let mut status = Value::Null;
    loop {
        assert!(Instant::now() < deadline, "no fill in 30 s; last status: {status:#}");
        tokio::time::sleep(Duration::from_millis(300)).await;
        let Some((200, s)) = get_json(&format!("{base}/v1/status")).await else { continue };
        status = s;
        let fills = status["engine"]["counters"]["fills"].as_u64().unwrap_or(0);
        if fills >= 1 {
            break;
        }
    }
    let (ready, body) = get_json(&format!("{base}/health/ready")).await.unwrap();
    assert_eq!(ready, 200, "{body}");
    let btc = &status["engine"]["assets"]["BTC"];
    assert_eq!(btc["decision"]["action"], "enter", "{btc:#}");
    assert_eq!(btc["decision"]["side"], "yes");
    assert_eq!(btc["decision"]["entry_px"], 0.46);
    assert_eq!(status["feeds"]["kalshi"]["connected"], true);
    assert_eq!(status["feeds"]["poly"]["connected"], true);
    assert_eq!(status["observer"]["alive"], true, "{:#}", status["observer"]);
    assert_eq!(status["engine"]["ledger"]["open_positions"], 1);
    assert_eq!(status["epoch"], "7");
    // Unauthenticated: refused.
    let r = reqwest::get(format!("{base}/v1/status")).await.unwrap();
    assert_eq!(r.status().as_u16(), 401);
    let (_, obs) = get_json(&format!("http://127.0.0.1:{obs_port}/v1/obs/status")).await.unwrap();
    assert!(obs["lines"].as_u64().unwrap() > 0, "{obs:#}");
    assert!(obs["journal_records"].as_u64().unwrap() >= 2, "{obs:#}");

    // The status page, against the live trader and observer.
    let status_bin = Path::new(env!("CARGO_BIN_EXE_poly-kalshi-service")).with_file_name("pk-status");
    if status_bin.exists() {
        let status_child = Kill(
            Command::new(&status_bin)
                .args(["--profile", "e2e"])
                .current_dir(&dir)
                .env("NO_PROXY", "127.0.0.1,localhost")
                .spawn()
                .unwrap(),
        );
        let page = format!("http://127.0.0.1:{status_port}");
        let t0 = Instant::now();
        let summary = loop {
            assert!(t0.elapsed() < Duration::from_secs(20), "pk-status did not answer");
            tokio::time::sleep(Duration::from_millis(300)).await;
            if let Some((200, s)) = get_json(&format!("{page}/api/summary")).await
                && s["service_up"] == true
            {
                break s;
            }
        };
        assert_eq!(summary["live"]["mode"], "paper");
        assert!(summary["live"].get("api_key").is_none());
        assert_eq!(summary["results"]["funnel"]["crossings"], 1, "{:#}", summary["results"]["funnel"]);
        assert_eq!(summary["epoch"], "7");
        let html = reqwest::get(&page).await.unwrap();
        assert!(html.headers()["content-security-policy"].to_str().unwrap().contains("default-src 'none'"));
        assert!(html.text().await.unwrap().contains("<html"));
        let zip = reqwest::get(format!("{page}/api/export")).await.unwrap().bytes().await.unwrap();
        assert_eq!(&zip[..2], b"PK");
        drop(status_child);
    } else {
        eprintln!("pk-status not built; run `cargo build --workspace` first to cover it");
    }

    // Shut down: the observer drains the pipe and closes the tape.
    let pid = child.0.id();
    Command::new("kill").args(["-TERM", &pid.to_string()]).status().unwrap();
    let t0 = Instant::now();
    while child.0.try_wait().unwrap().is_none() {
        assert!(t0.elapsed() < Duration::from_secs(40), "trader did not stop");
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    let journal = pk_core::journal::read_journal(&dir.join("data/state/e2e/journal")).unwrap();
    let kinds: Vec<&str> = journal.iter().filter_map(|r| r["type"].as_str()).collect();
    assert!(kinds.contains(&"decision") && kinds.contains(&"fill"), "{kinds:?}");
    let decision = journal.iter().find(|r| r["type"] == "decision").unwrap();
    assert_eq!(decision["epoch"], "7");
    assert!(decision["build"].is_string() && decision["config"].is_string());
    let tapes = files(&dir.join("data/raw/e2e"), ".zst");
    assert_eq!(tapes.len(), 1, "{tapes:?}");
    let mut reader = pk_tape::read_lines(tapes.clone());
    let lines: Vec<_> = reader.by_ref().collect();
    let venues: std::collections::BTreeSet<char> = lines.iter().map(|l| l.venue).collect();
    assert!(venues.contains(&'c') && venues.contains(&'k') && venues.contains(&'p'), "{venues:?}");
    assert!(lines.iter().any(|l| l.venue == 'c' && l.text.contains("\"reconnect\"")));
    assert!(lines.iter().any(|l| l.venue == 'c' && l.text.contains("\"window\"")));
    assert_eq!(reader.stats.out_of_order, 0);
    assert_eq!(reader.stats.truncated, 0, "the tape was closed cleanly");
    let manifest: Value =
        serde_json::from_slice(&std::fs::read(format!("{}.json", tapes[0].display())).unwrap()).unwrap();
    assert_eq!(manifest["lines"].as_u64().unwrap() as usize, lines.len());
    assert!(dir.join("data/state/e2e/ledger.json").is_file());
    std::fs::remove_dir_all(&dir).ok();
}
