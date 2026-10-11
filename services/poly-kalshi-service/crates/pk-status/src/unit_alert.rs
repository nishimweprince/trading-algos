//! `pk-unit-alert`: alert when a systemd unit fails (`OnFailure=pk-unit-alert@%n.service`).
//!
//! A service that cannot start never sends an alert of its own (2026-10-02: the
//! Python service crash-looped for an hour on a config error, silently).
//! systemd runs this instead, each time the unit fails.
//!
//! - It reads the env file leniently (`KEY=VALUE` lines), never validating it:
//!   the configuration may be what broke.
//! - It sends through notification-service; if that fails or is not
//!   configured, straight to Telegram with the service's own bot. So a failure
//!   of notification-service itself is reported too.
//! - Rate-limited per unit (`--cooldown-minutes`, default 30): a crash loop
//!   gives one alert, and the next says how many failures were not reported.
//! - It always exits 0, so a broken alert path cannot add a second failed unit.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::Duration;

use clap::Parser;
use serde_json::{Value, json};

const SOURCE: &str = "poly-kalshi.unit-alert";
const JOURNAL_LINES: usize = 12;
const TELEGRAM_LIMIT: usize = 3900;

#[derive(Parser)]
#[command(name = "pk-unit-alert", about = "Alert that a systemd unit failed")]
struct Args {
    unit: String,
    #[arg(long)]
    env_file: Option<PathBuf>,
    #[arg(long)]
    state_dir: Option<PathBuf>,
    #[arg(long, default_value_t = 30.0)]
    cooldown_minutes: f64,
}

/// `KEY=VALUE` lines; comments, blanks and malformed lines are skipped.
fn read_env(path: Option<&Path>) -> HashMap<String, String> {
    let mut out = HashMap::new();
    let Some(text) = path.and_then(|p| std::fs::read_to_string(p).ok()) else { return out };
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') || !line.contains('=') {
            continue;
        }
        let line = line.strip_prefix("export ").unwrap_or(line);
        let (k, v) = line.split_once('=').expect("has =");
        let mut v = v.trim();
        if v.len() >= 2
            && (v.starts_with('"') && v.ends_with('"') || v.starts_with('\'') && v.ends_with('\''))
        {
            v = &v[1..v.len() - 1];
        }
        out.insert(k.trim().to_owned(), v.to_owned());
    }
    out
}

fn run(cmd: &str, args: &[&str]) -> String {
    std::process::Command::new(cmd)
        .args(args)
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_owned())
        .unwrap_or_else(|e| format!("({e})"))
}

fn unit_facts(unit: &str) -> HashMap<String, String> {
    let raw =
        run("systemctl", &["show", unit, "--property=ActiveState,SubState,Result,NRestarts,ExecMainStatus"]);
    let mut facts: HashMap<String, String> =
        raw.lines().filter_map(|l| l.split_once('=')).map(|(k, v)| (k.to_owned(), v.to_owned())).collect();
    let n = JOURNAL_LINES.to_string();
    facts.insert("journal".into(), run("journalctl", &["-u", unit, "-n", &n, "--no-pager", "-o", "cat"]));
    facts
}

/// (send?, failures suppressed since the last alert). Updates the state file.
fn should_send(state_path: &Path, now: f64, cooldown_s: f64) -> (bool, u64) {
    let state: Value =
        std::fs::read(state_path).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or(json!({}));
    let last = state.get("last_sent").and_then(Value::as_f64);
    let suppressed = state.get("suppressed").and_then(Value::as_u64).unwrap_or(0);
    let (send, new_state) = match last {
        Some(l) if now - l < cooldown_s => (false, json!({"last_sent": l, "suppressed": suppressed + 1})),
        _ => (true, json!({"last_sent": now, "suppressed": 0})),
    };
    if let Some(dir) = state_path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let _ = std::fs::write(state_path, new_state.to_string());
    (send, suppressed)
}

fn build_message(
    unit: &str,
    facts: &HashMap<String, String>,
    host: &str,
    suppressed: u64,
) -> (String, Vec<String>) {
    let g = |k: &str| facts.get(k).cloned().unwrap_or_else(|| "?".into());
    let mut lines = vec![format!(
        "state: {}/{}, result {}, exit status {}, restarts {}",
        g("ActiveState"),
        g("SubState"),
        g("Result"),
        g("ExecMainStatus"),
        g("NRestarts")
    )];
    if suppressed > 0 {
        lines.push(format!("{suppressed} more failure(s) since the last alert were not reported"));
    }
    let journal = facts.get("journal").map(|j| j.trim().to_owned()).unwrap_or_default();
    if !journal.is_empty() {
        lines.extend(["".into(), "Last log lines:".into(), journal]);
    }
    lines.extend([
        "".into(),
        format!("Check: journalctl -u {unit} -n 50 --no-pager"),
        "A start alert follows once it runs again.".into(),
    ]);
    (format!("{unit} FAILED on {host}"), lines)
}

fn post(http: &reqwest::blocking::Client, url: &str, body: &Value, headers: &[(&str, String)]) -> bool {
    let mut req = http.post(url).json(body);
    for (k, v) in headers {
        req = req.header(*k, v);
    }
    req.send().map(|r| r.status().is_success()).unwrap_or(false)
}

fn send_notification_service(
    http: &reqwest::blocking::Client,
    env: &HashMap<String, String>,
    subject: &str,
    lines: &[String],
    key: &str,
) -> bool {
    let url = env.get("NOTIFICATION_SERVICE_URL").map(|s| s.trim().to_owned()).unwrap_or_default();
    let enabled = !matches!(
        env.get("NOTIFICATIONS_ENABLED").map(|s| s.trim().to_lowercase()).as_deref(),
        Some("0" | "false" | "no")
    );
    let mut channels: Vec<String> = env
        .get("NOTIFICATION_CHANNELS")
        .map(|c| c.split(',').map(|x| x.trim().to_owned()).filter(|x| !x.is_empty()).collect())
        .unwrap_or_default();
    channels.sort();
    if url.is_empty() || !enabled || channels.is_empty() {
        return false;
    }
    let mut headers = Vec::new();
    if let Some(k) = env.get("NOTIFICATION_API_KEY").map(|s| s.trim().to_owned()).filter(|s| !s.is_empty()) {
        headers.push(("Authorization", format!("Bearer {k}")));
        headers.push(("X-API-Key", k));
    }
    let body = json!({
        "subject": subject, "message": lines.join("\n"), "contentType": "text",
        "channels": channels, "source": SOURCE, "idempotencyKey": key,
    });
    post(http, &format!("{}/notifications", url.trim_end_matches('/')), &body, &headers)
}

fn send_telegram(
    http: &reqwest::blocking::Client,
    env: &HashMap<String, String>,
    subject: &str,
    lines: &[String],
) -> bool {
    let token = env.get("PK_TELEGRAM_BOT_TOKEN").map(|s| s.trim().to_owned()).unwrap_or_default();
    let chats: Vec<String> = env
        .get("PK_TELEGRAM_ADMIN_USER_IDS")
        .map(|c| c.split(',').map(|x| x.trim().to_owned()).filter(|x| !x.is_empty()).collect())
        .unwrap_or_default();
    if token.is_empty() || chats.is_empty() {
        return false;
    }
    let text: String = format!("{subject}\n\n{}", lines.join("\n")).chars().take(TELEGRAM_LIMIT).collect();
    let mut sent = false;
    for chat in chats {
        let body = json!({"chat_id": chat, "text": text, "disable_web_page_preview": true});
        sent |= post(http, &format!("https://api.telegram.org/bot{token}/sendMessage"), &body, &[]);
    }
    sent
}

fn main() {
    pk_svc::tls::install_default_provider();
    let args = Args::parse();
    let result = std::panic::catch_unwind(|| {
        let state_dir = args
            .state_dir
            .clone()
            .or_else(|| std::env::var_os("STATE_DIRECTORY").map(PathBuf::from))
            .unwrap_or_else(|| PathBuf::from("/tmp/pk-unit-alert"));
        let now = chrono::Utc::now().timestamp_millis() as f64 / 1e3;
        let (send, suppressed) =
            should_send(&state_dir.join(format!("{}.json", args.unit)), now, args.cooldown_minutes * 60.0);
        if !send {
            println!("{}: failure noted, alert suppressed (cooldown)", args.unit);
            return;
        }
        let env = read_env(args.env_file.as_deref());
        let host = run("hostname", &[]);
        let (subject, lines) = build_message(&args.unit, &unit_facts(&args.unit), &host, suppressed);
        let key = format!("unit-alert:{}:{}", args.unit, now as i64);
        let Ok(http) = reqwest::blocking::Client::builder().timeout(Duration::from_secs(10)).build() else {
            return;
        };
        if send_notification_service(&http, &env, &subject, &lines, &key) {
            println!("{}: alert sent via notification-service", args.unit);
        } else if send_telegram(&http, &env, &subject, &lines) {
            println!("{}: alert sent via Telegram (notification-service unavailable)", args.unit);
        } else {
            eprintln!("{}: alert NOT sent: no working channel", args.unit);
        }
    });
    if result.is_err() {
        eprintln!("{}: alert error", args.unit);
    }
    // Never fail on top of the failure.
    std::process::exit(0);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn env_and_cooldown() {
        let dir = std::env::temp_dir().join(format!("pk-ua-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let env = dir.join(".env");
        std::fs::write(&env, "# c\nexport A=\"1\"\nB = '2'\nbad\nC=\n").unwrap();
        let e = read_env(Some(&env));
        assert_eq!((e["A"].as_str(), e["B"].as_str(), e["C"].as_str()), ("1", "2", ""));
        let state = dir.join("u.json");
        assert_eq!(should_send(&state, 1000.0, 60.0), (true, 0));
        assert_eq!(should_send(&state, 1010.0, 60.0), (false, 0));
        assert_eq!(should_send(&state, 1020.0, 60.0), (false, 1));
        assert_eq!(should_send(&state, 1100.0, 60.0), (true, 2));
        let (subject, lines) = build_message("pk.service", &HashMap::new(), "h", 2);
        assert_eq!(subject, "pk.service FAILED on h");
        assert!(lines[1].contains("2 more failure"));
        std::fs::remove_dir_all(dir).ok();
    }
}
