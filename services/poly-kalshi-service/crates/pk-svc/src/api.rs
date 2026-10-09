//! The API key (`X-API-Key`) every non-health route requires, and the loopback
//! check: the kill switch lives on these APIs, so they belong on 127.0.0.1.

use axum::extract::{Request, State};
use axum::http::StatusCode;
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use serde_json::json;

#[derive(Clone)]
pub struct ApiKey(pub std::sync::Arc<String>);

/// `axum::middleware::from_fn_with_state(ApiKey(..), require_key)`.
pub async fn require_key(State(key): State<ApiKey>, req: Request, next: Next) -> Response {
    let given = req.headers().get("x-api-key").and_then(|v| v.to_str().ok()).unwrap_or_default();
    if !constant_time_eq(given.as_bytes(), key.0.as_bytes()) {
        let body =
            json!({"error": {"code": "unauthorized", "message": "A valid X-API-Key header is required"}});
        return (StatusCode::UNAUTHORIZED, axum::Json(body)).into_response();
    }
    next.run(req).await
}

fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

pub fn is_loopback(host: &str) -> bool {
    host == "localhost" || host.parse::<std::net::IpAddr>().is_ok_and(|ip| ip.is_loopback())
}

pub fn json_error(status: StatusCode, code: &str, message: &str) -> Response {
    (status, axum::Json(json!({"error": {"code": code, "message": message}}))).into_response()
}
