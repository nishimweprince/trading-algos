//! Fire-and-forget alerts through notification-service (`POST /notifications`).
//!
//! A send never blocks the caller (it runs as a task) and never fails into it.
//! Repeats of the same key are throttled so a flapping stream cannot flood
//! Telegram; `always` bypasses the throttle (halts, start, closures).

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use parking_lot::Mutex;
use serde_json::json;

#[derive(Clone, Debug)]
pub struct NotifyConfig {
    pub enabled: bool,
    pub url: String,
    pub api_key: Option<String>,
    pub channels: Vec<String>,
    pub source: String,
    pub timeout: Duration,
}

#[derive(Clone)]
pub struct Notifier {
    cfg: NotifyConfig,
    http: reqwest::Client,
}

impl Notifier {
    pub fn new(cfg: NotifyConfig) -> Self {
        let http = reqwest::Client::builder().timeout(cfg.timeout).build().expect("http client");
        Self { cfg, http }
    }

    pub fn enabled(&self) -> bool {
        self.cfg.enabled && !self.cfg.channels.is_empty()
    }

    /// Post one notification; returns whether the service took it. Never panics.
    pub async fn send(&self, subject: &str, lines: &[String], idempotency_key: Option<&str>) -> bool {
        if !self.enabled() {
            return false;
        }
        let mut payload = json!({
            "subject": subject,
            "message": lines.join("\n"),
            "contentType": "text",
            "channels": self.cfg.channels,
            "source": self.cfg.source,
        });
        if let Some(k) = idempotency_key {
            payload["idempotencyKey"] = k.into();
        }
        let url = format!("{}/notifications", self.cfg.url.trim_end_matches('/'));
        let mut req = self.http.post(url).json(&payload);
        if let Some(key) = &self.cfg.api_key {
            req = req.bearer_auth(key).header("X-API-Key", key);
        }
        match req.send().await {
            Ok(r) if r.status().is_success() => true,
            Ok(r) => {
                tracing::warn!(subject, status = r.status().as_u16(), "notification_failed");
                false
            }
            Err(e) => {
                // The error type only: a message can carry the URL and key.
                tracing::warn!(
                    subject,
                    error = if e.is_timeout() { "timeout" } else { "send" },
                    "notification_failed"
                );
                false
            }
        }
    }
}

#[derive(Default)]
struct State {
    last: HashMap<String, Instant>,
    sent: Vec<(String, String)>,
    suppressed: u64,
}

#[derive(Clone)]
pub struct Alerts {
    notifier: Option<Notifier>,
    throttle: Duration,
    state: Arc<Mutex<State>>,
}

impl Alerts {
    pub fn new(notifier: Option<Notifier>, throttle: Duration) -> Self {
        Self { notifier, throttle, state: Arc::default() }
    }

    pub fn send(&self, key: &str, subject: &str, lines: Vec<String>, always: bool) {
        let now = Instant::now();
        {
            let mut s = self.state.lock();
            if !always && s.last.get(key).is_some_and(|t| now.duration_since(*t) < self.throttle) {
                s.suppressed += 1;
                return;
            }
            s.last.insert(key.to_owned(), now);
            s.sent.push((key.to_owned(), subject.to_owned()));
            let n = s.sent.len();
            if n > 50 {
                s.sent.drain(..n - 50);
            }
        }
        tracing::info!(key, subject, "alert");
        let Some(notifier) = self.notifier.clone() else { return };
        let Ok(handle) = tokio::runtime::Handle::try_current() else { return };
        let bucket = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs() / self.throttle.as_secs().max(1))
            .unwrap_or(0);
        let idem = format!("pk:{key}:{bucket}");
        let subject = subject.to_owned();
        handle.spawn(async move {
            notifier.send(&subject, &lines, Some(&idem)).await;
        });
    }

    pub fn snapshot(&self) -> serde_json::Value {
        let s = self.state.lock();
        let recent: Vec<_> = s.sent.iter().rev().take(10).rev().map(|(k, sub)| json!([k, sub])).collect();
        json!({"recent": recent, "suppressed": s.suppressed})
    }
}
