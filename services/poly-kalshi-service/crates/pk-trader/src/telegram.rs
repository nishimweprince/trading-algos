//! Inbound Telegram commands on the service's own bot: `/kill` and `/status`.
//!
//! It long-polls `getUpdates` on `PK_TELEGRAM_BOT_TOKEN`, which must be a bot
//! of its own: two pollers on one token take turns receiving updates. Only
//! messages from `PK_TELEGRAM_ADMIN_USER_IDS` are acted on; anything else is
//! logged (without its content) and never interpreted. Acknowledging a halt is
//! deliberately not a Telegram command: it needs the authenticated HTTP API.
//! Updates queued while the process was down are dropped on start, so a stale
//! `/kill` cannot fire after a restart someone already dealt with.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use serde_json::{Value, json};

const POLL_SECONDS: u64 = 25;
const CONFLICT_BACKOFF: Duration = Duration::from_secs(300);

pub type Handler =
    Arc<dyn Fn(&'static str, i64) -> Pin<Box<dyn Future<Output = String> + Send>> + Send + Sync>;

/// `/kill`, `/kill@my_bot reason` → `kill`; anything else → None.
pub fn parse_command(text: &str) -> Option<&'static str> {
    let rest = text.strip_prefix('/')?;
    let word = rest.split_whitespace().next().unwrap_or_default();
    match word.split('@').next().unwrap_or_default().to_lowercase().as_str() {
        "kill" => Some("kill"),
        "status" => Some("status"),
        _ => None,
    }
}

#[derive(Default)]
pub struct Stats {
    pub handled: AtomicU64,
    pub ignored: AtomicU64,
    pub errors: AtomicU64,
    pub conflicts: AtomicU64,
}

impl Stats {
    pub fn json(&self) -> Value {
        json!({
            "handled": self.handled.load(Ordering::Relaxed),
            "ignored": self.ignored.load(Ordering::Relaxed),
            "errors": self.errors.load(Ordering::Relaxed),
            "conflicts": self.conflicts.load(Ordering::Relaxed),
        })
    }
}

pub struct TelegramCommands {
    token: String,
    admins: Vec<i64>,
    handler: Handler,
    http: reqwest::Client,
    base: String,
    pub stats: Arc<Stats>,
    on_conflict: Box<dyn Fn(&str) + Send + Sync>,
}

impl TelegramCommands {
    pub fn new(
        token: String,
        admins: Vec<i64>,
        handler: Handler,
        on_conflict: Box<dyn Fn(&str) + Send + Sync>,
    ) -> Self {
        Self {
            token,
            admins,
            handler,
            http: reqwest::Client::builder()
                .timeout(Duration::from_secs(POLL_SECONDS + 10))
                .build()
                .expect("http"),
            base: "https://api.telegram.org".into(),
            stats: Arc::default(),
            on_conflict,
        }
    }

    async fn call(&self, method: &str, params: Value) -> Result<Value, String> {
        let url = format!("{}/bot{}/{method}", self.base, self.token);
        // The error kind only: the URL carries the bot token.
        let resp =
            self.http.post(url).json(&params).send().await.map_err(|_| format!("telegram {method}: send"))?;
        let body: Value = resp.json().await.map_err(|_| format!("telegram {method}: body"))?;
        if body.get("ok") != Some(&Value::Bool(true)) {
            let d = body.get("description").and_then(Value::as_str).unwrap_or("not ok");
            return Err(format!("telegram {method}: {d}"));
        }
        Ok(body.get("result").cloned().unwrap_or(Value::Null))
    }

    pub async fn run(self) {
        let mut offset: Option<i64> = None;
        let mut backoff = Duration::from_secs(1);
        loop {
            let r = async {
                if offset.is_none() {
                    let pending = self.call("getUpdates", json!({"offset": -1, "timeout": 0})).await?;
                    let last = pending
                        .as_array()
                        .and_then(|a| a.last())
                        .and_then(|u| u.get("update_id"))
                        .and_then(Value::as_i64);
                    offset = Some(last.map(|l| l + 1).unwrap_or(0));
                }
                let mut params = json!({"timeout": POLL_SECONDS, "allowed_updates": ["message"]});
                if let Some(o) = offset.filter(|o| *o > 0) {
                    params["offset"] = o.into();
                }
                let updates = self.call("getUpdates", params).await?;
                for u in updates.as_array().into_iter().flatten() {
                    if let Some(id) = u.get("update_id").and_then(Value::as_i64) {
                        offset = Some(id + 1);
                    }
                    self.dispatch(u).await;
                }
                Ok::<(), String>(())
            }
            .await;
            match r {
                Ok(()) => backoff = Duration::from_secs(1),
                Err(e) => {
                    self.stats.errors.fetch_add(1, Ordering::Relaxed);
                    if e.contains("Conflict") {
                        let n = self.stats.conflicts.fetch_add(1, Ordering::Relaxed) + 1;
                        if n == 1 {
                            (self.on_conflict)(&e);
                            tracing::error!(error = %e, "telegram_conflict");
                        }
                        tokio::time::sleep(CONFLICT_BACKOFF).await;
                        continue;
                    }
                    tracing::warn!(error = %e, "telegram_poll_failed");
                    tokio::time::sleep(backoff).await;
                    backoff = (backoff * 2).min(Duration::from_secs(60));
                }
            }
        }
    }

    async fn dispatch(&self, update: &Value) {
        let msg = update.get("message").cloned().unwrap_or(Value::Null);
        let user = msg.get("from").and_then(|f| f.get("id")).and_then(Value::as_i64);
        let chat = msg.get("chat").and_then(|c| c.get("id")).and_then(Value::as_i64);
        let command = parse_command(msg.get("text").and_then(Value::as_str).unwrap_or_default());
        let (Some(command), Some(user)) = (command, user.filter(|u| self.admins.contains(u))) else {
            self.stats.ignored.fetch_add(1, Ordering::Relaxed);
            tracing::info!(user_id = ?user, recognised = command.is_some(), "telegram_command_ignored");
            return;
        };
        self.stats.handled.fetch_add(1, Ordering::Relaxed);
        tracing::info!(command, user_id = user, "telegram_command");
        let reply = (self.handler)(command, user).await;
        if let Some(chat) = chat {
            let text: String = reply.chars().take(4000).collect();
            if let Err(e) = self.call("sendMessage", json!({"chat_id": chat, "text": text})).await {
                tracing::warn!(error = %e, "telegram_reply_failed");
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn commands() {
        assert_eq!(parse_command("/kill"), Some("kill"));
        assert_eq!(parse_command("/KILL@pk_bot now"), Some("kill"));
        assert_eq!(parse_command("/status"), Some("status"));
        assert_eq!(parse_command("/ack"), None);
        assert_eq!(parse_command("kill"), None);
        assert_eq!(parse_command("/"), None);
    }
}
