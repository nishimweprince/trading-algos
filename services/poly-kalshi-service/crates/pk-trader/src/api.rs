//! The trader's API (`HOST:PORT`, loopback): health without a key; status,
//! recent records, the series, and the kill switch with `X-API-Key`.

use std::sync::Arc;

use axum::extract::{Query, State};
use axum::http::{StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use pk_svc::api::{ApiKey, json_error, require_key};
use serde::Deserialize;
use serde_json::{Value, json};

use crate::runtime::Runtime;

#[derive(Deserialize)]
struct Limit {
    limit: Option<usize>,
}

#[derive(Deserialize)]
struct Since {
    since: Option<i64>,
}

#[derive(Deserialize, Default)]
struct KillBody {
    #[serde(default)]
    reason: String,
}

fn raw_json(v: Value) -> Response {
    ([(header::CONTENT_TYPE, "application/json")], v.to_string()).into_response()
}

pub fn router(rt: Arc<Runtime>) -> Router {
    let key = ApiKey(Arc::new(rt.settings.api_key.clone()));
    let authed = Router::new()
        .route("/v1/status", get(|State(rt): State<Arc<Runtime>>| async move { raw_json(rt.status()) }))
        .route(
            "/v1/recent",
            get(|State(rt): State<Arc<Runtime>>, Query(q): Query<Limit>| async move {
                let limit = q.limit.unwrap_or(100).clamp(1, 200);
                let recent: Vec<Value> = rt.engine.lock().recent.iter().rev().take(limit).cloned().collect();
                Json(json!({"recent": recent}))
            }),
        )
        .route(
            "/v1/series",
            get(|State(rt): State<Arc<Runtime>>, Query(q): Query<Since>| async move {
                // `[sec, open_unix, kalshi_mid, poly_yes_mid, yes_bid, yes_ask, no_bid, no_ask]`;
                // the observer samples them (the trader's engine does not).
                Json(json!({"points": rt.engine.lock().series_since(q.since.unwrap_or(0))}))
            }),
        )
        .route(
            "/v1/kill",
            post(|State(rt): State<Arc<Runtime>>, body: Option<Json<KillBody>>| async move {
                let reason = body.map(|b| b.0.reason).unwrap_or_default();
                let reason: String = reason.chars().take(200).collect();
                let fired = rt.kill("http", &reason);
                Json(json!({"halted": true, "fired": fired}))
            }),
        )
        .route(
            "/v1/kill/ack",
            post(|State(rt): State<Arc<Runtime>>| async move {
                let (ok, message) = rt.engine.lock().risk.ack();
                if !ok {
                    return json_error(StatusCode::CONFLICT, "ack_refused", &message);
                }
                Json(json!({"halted": false, "message": message})).into_response()
            }),
        )
        .layer(axum::middleware::from_fn_with_state(key, require_key));
    Router::new()
        .route("/health/live", get(|| async { Json(json!({"status": "ok"})) }))
        .route(
            "/health/ready",
            get(|State(rt): State<Arc<Runtime>>| async move {
                let (ok, details) = rt.ready();
                let body =
                    Json(json!({"status": if ok { "ready" } else { "not_ready" }, "details": details}));
                if ok {
                    body.into_response()
                } else {
                    (StatusCode::SERVICE_UNAVAILABLE, body).into_response()
                }
            }),
        )
        .merge(authed)
        .with_state(rt)
}
