//! The OKX v5 REST path (event contracts). Only `pk-okx` uses it.
//!
//! - Private requests carry `OK-ACCESS-KEY`, `OK-ACCESS-PASSPHRASE`,
//!   `OK-ACCESS-TIMESTAMP` (ISO-8601 UTC with milliseconds) and
//!   `OK-ACCESS-SIGN` = base64(HMAC-SHA256(secret, `ts + METHOD + path?query + body`)).
//! - Every answer is `{"code": "0", "data": [..], "msg": ""}`; a non-zero code
//!   (top level, or `sCode` per order) is OKX's refusal and is returned as is.
//! - Requests ride a [`WarmHttp`] connection and are timed from send to headers.

use std::sync::Arc;
use std::time::Instant;

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use hmac::{Hmac, Mac};
use reqwest::Method;
use serde_json::Value;
use sha2::Sha256;

use crate::transport::WarmHttp;

pub struct OkxKey {
    key: String,
    secret: Vec<u8>,
    passphrase: String,
}

impl OkxKey {
    pub fn new(key: &str, secret: &str, passphrase: &str) -> Self {
        Self { key: key.to_owned(), secret: secret.as_bytes().to_vec(), passphrase: passphrase.to_owned() }
    }

    /// `OK-ACCESS-SIGN` for one request.
    pub fn sign(&self, ts: &str, method: &str, path_and_query: &str, body: &str) -> String {
        let mut mac = Hmac::<Sha256>::new_from_slice(&self.secret).expect("hmac takes any key length");
        mac.update(format!("{ts}{method}{path_and_query}{body}").as_bytes());
        STANDARD.encode(mac.finalize().into_bytes())
    }
}

/// ISO-8601 UTC with milliseconds, as OKX checks it.
pub fn ts_now() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
}

/// One answer: the body, the HTTP status and send → headers / → body (µs).
#[derive(Debug, Clone)]
pub struct Reply {
    pub status: u16,
    pub body: Value,
    pub headers_us: u64,
    pub body_us: u64,
}

impl Reply {
    /// OKX's own code ("0" is success).
    pub fn code(&self) -> &str {
        self.body.get("code").and_then(Value::as_str).unwrap_or("?")
    }

    pub fn ok(&self) -> bool {
        self.status == 200 && self.code() == "0"
    }

    pub fn data(&self) -> &[Value] {
        self.body.get("data").and_then(Value::as_array).map(Vec::as_slice).unwrap_or(&[])
    }
}

pub struct OkxClient {
    pub http: Arc<WarmHttp>,
    key: Option<OkxKey>,
}

impl OkxClient {
    pub fn new(http: Arc<WarmHttp>, key: Option<OkxKey>) -> Self {
        Self { http, key }
    }

    pub fn has_key(&self) -> bool {
        self.key.is_some()
    }

    async fn request(&self, method: Method, path_and_query: &str, body: Option<&Value>) -> anyhow::Result<Reply> {
        let body = body.map(Value::to_string);
        let mut headers: Vec<(&'static str, String)> = Vec::new();
        if let Some(k) = &self.key {
            let ts = ts_now();
            let sign = k.sign(&ts, method.as_str(), path_and_query, body.as_deref().unwrap_or(""));
            headers.push(("OK-ACCESS-KEY", k.key.clone()));
            headers.push(("OK-ACCESS-SIGN", sign));
            headers.push(("OK-ACCESS-TIMESTAMP", ts));
            headers.push(("OK-ACCESS-PASSPHRASE", k.passphrase.clone()));
        }
        let t0 = Instant::now();
        let resp = self.http.send(method, path_and_query, &headers, body.map(String::into_bytes)).await?;
        let headers_us = t0.elapsed().as_micros() as u64;
        let status = resp.status().as_u16();
        let bytes = resp.bytes().await?;
        let body_us = t0.elapsed().as_micros() as u64;
        let body = serde_json::from_slice(&bytes).unwrap_or_else(|_| Value::String(String::from_utf8_lossy(&bytes).chars().take(300).collect()));
        Ok(Reply { status, body, headers_us, body_us })
    }

    pub async fn get(&self, path_and_query: &str) -> anyhow::Result<Reply> {
        self.request(Method::GET, path_and_query, None).await
    }

    pub async fn post(&self, path: &str, body: &Value) -> anyhow::Result<Reply> {
        anyhow::ensure!(self.key.is_some(), "a private OKX request needs OKX_API_KEY, OKX_API_SECRET and OKX_API_PASSPHRASE");
        self.request(Method::POST, path, Some(body)).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signature() {
        // Reference value from Python: base64(hmac.new(b"secret", msg, sha256).digest()).
        let k = OkxKey::new("k", "secret", "p");
        let s = k.sign("2020-12-08T09:08:57.715Z", "GET", "/api/v5/account/balance?ccy=USDT", "");
        assert_eq!(s, "rU3eBbGNF6XZT9cVLJVmOKO/wndaidjMkElDI6kMkzY=");
        let s = k.sign("2020-12-08T09:08:57.715Z", "POST", "/api/v5/trade/order", r#"{"instId":"X"}"#);
        assert_eq!(s, "m4dF/6YTSqpWKOrCPJR7MiO4scSbOOvqauAErMkWBYA=");
    }
}
