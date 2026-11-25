//! The monitoring page: `GET /` (HTML), `GET /api/summary` (JSON), `GET /health`.
//!
//! With `PK_EG_UI_TOKEN` set, every route but `/health` needs HTTP Basic auth
//! with that token as the password (any user name), or `Authorization: Bearer
//! <token>`. Without a token the page refuses to bind anywhere but loopback,
//! so it is reachable only through an SSH tunnel.

use std::net::SocketAddr;
use std::sync::Arc;

use axum::Router;
use axum::extract::{Request, State};
use axum::http::{HeaderMap, StatusCode, header};
use axum::middleware::{self, Next};
use axum::response::{Html, IntoResponse, Response};
use axum::routing::get;
use base64::Engine as _;
use serde_json::Value;

pub type Snapshot = Arc<dyn Fn() -> Value + Send + Sync>;

const PAGE: &str = include_str!("ui.html");

#[derive(Clone)]
struct Web {
    token: Option<Arc<str>>,
    snap: Snapshot,
}

/// Equal-time comparison, so the token cannot be guessed byte by byte.
fn same(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

pub fn authorized(headers: &HeaderMap, token: &str) -> bool {
    let Some(h) = headers.get(header::AUTHORIZATION).and_then(|v| v.to_str().ok()) else { return false };
    if let Some(b) = h.strip_prefix("Bearer ") {
        return same(b.trim().as_bytes(), token.as_bytes());
    }
    if let Some(b) = h.strip_prefix("Basic ")
        && let Ok(raw) = base64::engine::general_purpose::STANDARD.decode(b.trim())
        && let Ok(s) = String::from_utf8(raw)
    {
        let pass = s.split_once(':').map(|(_, p)| p).unwrap_or("");
        return same(pass.as_bytes(), token.as_bytes());
    }
    false
}

async fn guard(State(w): State<Web>, req: Request, next: Next) -> Response {
    if let Some(t) = &w.token
        && !authorized(req.headers(), t)
    {
        return (StatusCode::UNAUTHORIZED, [(header::WWW_AUTHENTICATE, "Basic realm=\"pk-endgame\"")], "unauthorized")
            .into_response();
    }
    next.run(req).await
}

pub async fn serve(addr: SocketAddr, token: Option<String>, snap: Snapshot) -> anyhow::Result<()> {
    anyhow::ensure!(
        token.is_some() || addr.ip().is_loopback(),
        "PK_EG_UI_ADDR={addr} is not loopback: set PK_EG_UI_TOKEN"
    );
    let w = Web { token: token.map(Arc::from), snap };
    let app = Router::new()
        .route("/", get(|| async { ([(header::CACHE_CONTROL, "no-store")], Html(PAGE)) }))
        .route(
            "/api/summary",
            get(|State(w): State<Web>| async move {
                let v = tokio::task::spawn_blocking(move || (w.snap)()).await.unwrap_or(Value::Null);
                ([(header::CACHE_CONTROL, "no-store")], axum::Json(v))
            }),
        )
        .route_layer(middleware::from_fn_with_state(w.clone(), guard))
        .route("/health", get(|| async { "ok" }))
        .with_state(w);
    let listener = tokio::net::TcpListener::bind(addr).await?;
    tracing::info!(%addr, "eg_ui_listening");
    axum::serve(listener, app).await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::HeaderValue;

    #[test]
    fn basic_and_bearer() {
        let mut h = HeaderMap::new();
        assert!(!authorized(&h, "s3cret"));
        let b = base64::engine::general_purpose::STANDARD.encode("me:s3cret");
        h.insert(header::AUTHORIZATION, HeaderValue::from_str(&format!("Basic {b}")).unwrap());
        assert!(authorized(&h, "s3cret"));
        assert!(!authorized(&h, "other"));
        h.insert(header::AUTHORIZATION, HeaderValue::from_static("Bearer s3cret"));
        assert!(authorized(&h, "s3cret"));
        h.insert(header::AUTHORIZATION, HeaderValue::from_static("Bearer s3cre"));
        assert!(!authorized(&h, "s3cret"));
    }
}
