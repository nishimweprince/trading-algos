//! Settings: the same `.env.<profile>` keys the Python service read (`API_KEY`,
//! `PK_*`, `KALSHI_*`, `POLY_*`, `NOTIFICATION_*`), validated at startup.
//!
//! The strategy parameters and the risk limits are read once. Nothing at
//! runtime changes them: no endpoint, no Telegram command. Changing one means
//! editing the env file and restarting, and every decision records the values
//! it was made with. Two values move at runtime by design, and both are
//! journaled when they change: the order size (`PK_ORDER_SIZE_PCT` of the
//! balance, recomputed when flat) and the paper venue's latency (seeded by
//! `PK_PAPER_LATENCY_MS`, then the measured p50 of the order path).
//!
//! `PK_EXECUTION_MODE=live` sends real orders. It is refused unless
//! `PK_LIVE_CONFIRM=yes`, a `POLY_PRIVATE_KEY` and a `POLYGON_RPC_URL` are set,
//! and the wallet is one the service can redeem from: the EOA itself
//! (`POLY_SIGNATURE_TYPE=0`), or a deposit wallet (`POLY_SIGNATURE_TYPE=3`,
//! `POLY_FUNDER`) with a `POLY_RELAYER_API_KEY`. Passing the paper gates is a
//! human decision, not a setting.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::Duration;

use pk_core::StrategyParams;
use pk_core::shadow::{DEFAULT_VARIANTS, Variant, parse_variants};

use crate::alerts::NotifyConfig;

pub const SUPPORTED_ASSETS: [&str; 8] = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "HYPE", "ZEC"];
const PLACEHOLDER: &str = "replace-with";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ExecutionMode {
    /// Record, decide and log decisions; no simulated orders.
    Off,
    /// Decisions go to the paper venue: simulated fills, real fees, cash ledger.
    Paper,
    /// Real orders on Polymarket; the ledger mirrors the wallet.
    Live,
}

impl ExecutionMode {
    pub fn as_str(self) -> &'static str {
        match self {
            ExecutionMode::Off => "off",
            ExecutionMode::Paper => "paper",
            ExecutionMode::Live => "live",
        }
    }
}

#[derive(Clone, Debug)]
pub struct Settings {
    pub profile: Option<String>,
    pub api_key: String,
    pub host: String,
    pub port: u16,
    pub log_level: String,
    pub events_log_path: PathBuf,

    pub execution_mode: ExecutionMode,
    pub assets: Vec<String>,
    pub params: StrategyParams,
    pub shadow_variants: Vec<Variant>,
    /// The paper venue's latency until the order path has measured its own.
    pub paper_latency_ms: i64,
    /// Order size as a percent of the balance; 0 = the static `PK_NOTIONAL_USD`.
    pub order_size_pct: f64,
    /// Probes between decisions, so the latency estimate keeps moving (s; 0 = off).
    pub probe_cadence_s: f64,
    /// The latency estimate is the p50 of this many recent round trips…
    pub latency_window: usize,
    /// …once at least this many exist.
    pub latency_min_samples: usize,
    pub limit_offset: f64,
    pub size_to_depth: bool,
    /// Sell a held leg once Polymarket's bid reaches Kalshi's price (`pk_core::exit`).
    pub active_exit: bool,
    /// Size an entry at its limit, so a FAK never spends more than the notional.
    pub size_at_limit: bool,
    /// Shadow entries are also scored with a FAK priced this long after the decision.
    pub shadow_latency_ms: i64,
    pub paper_start_cash_usd: f64,
    pub max_open_notional_usd: f64,
    pub daily_loss_limit_usd: f64,
    pub kill_file_path: PathBuf,

    pub record_enabled: bool,
    pub record_dir: PathBuf,
    pub host_tag: String,
    pub min_free_disk_gb: f64,
    pub state_dir: PathBuf,
    pub results_epoch: String,
    pub poly_sockets: usize,
    pub heartbeat_seconds: f64,
    pub stale_feed_seconds: f64,
    pub discover_lead_seconds: i64,
    /// Write `age_outlier` records for cheap legs older than this (ms); 0 = off.
    pub age_outlier_ms: f64,

    pub kalshi_api_key_id: String,
    pub kalshi_private_key_path: PathBuf,
    pub kalshi_rest_url: String,
    pub kalshi_ws_url: String,
    pub poly_gamma_url: String,
    pub poly_ws_url: String,
    pub poly_clob_url: String,

    pub observer_enabled: bool,
    pub observer_port: u16,

    pub polybolt_url: String,
    pub polybolt_credentials: Option<(String, String, String)>,

    /// Binance spot and perp best bid/ask, recorded and compared with Kalshi
    /// and Polymarket by the observer (`pk_core::leadlag`). Never traded.
    pub binance_enabled: bool,
    pub binance_spot_ws_url: String,
    pub binance_perp_ws_url: String,

    /// The order probe (`pk_exec::probe`): real, unfillable orders.
    pub order_probe: bool,
    pub poly_private_key: Option<String>,
    pub poly_funder: Option<String>,
    /// 0 EOA, 3 deposit wallet (`poly_funder`); 1 and 2 only for the probe.
    pub poly_signature_type: u8,
    /// Polymarket's relayer, which redeems for a deposit wallet (live, type 3).
    pub poly_relayer_url: String,
    pub poly_relayer_api_key: Option<String>,
    /// The address that owns the relayer key; the EOA when unset.
    pub poly_relayer_api_key_address: Option<String>,
    /// Polygon JSON-RPC, for redeeming settled positions (live).
    pub polygon_rpc_url: Option<String>,
    /// The collateral to redeem against; the exchange's own (pUSD) when unset.
    pub poly_redeem_collateral: Option<String>,

    pub status_host: String,
    pub status_port: u16,

    pub telegram_bot_token: Option<String>,
    pub telegram_admin_user_ids: Vec<i64>,

    pub notify: NotifyConfig,
}

struct Env(HashMap<String, String>);

impl Env {
    fn get(&self, k: &str) -> Option<String> {
        self.0.get(k).map(|v| v.trim().to_owned()).filter(|v| !v.is_empty())
    }
    fn or(&self, k: &str, d: &str) -> String {
        self.get(k).unwrap_or_else(|| d.to_owned())
    }
    fn num<T: std::str::FromStr>(&self, k: &str, d: T) -> anyhow::Result<T>
    where
        T::Err: std::fmt::Display,
    {
        match self.get(k) {
            None => Ok(d),
            Some(v) => v.parse().map_err(|e| anyhow::anyhow!("{k}={v}: {e}")),
        }
    }
    fn opt_num(&self, k: &str) -> anyhow::Result<Option<f64>> {
        self.get(k).map(|v| v.parse().map_err(|e| anyhow::anyhow!("{k}={v}: {e}"))).transpose()
    }
    fn flag(&self, k: &str, d: bool) -> anyhow::Result<bool> {
        match self.get(k).map(|v| v.to_lowercase()) {
            None => Ok(d),
            Some(v) if ["1", "true", "yes", "on"].contains(&v.as_str()) => Ok(true),
            Some(v) if ["0", "false", "no", "off"].contains(&v.as_str()) => Ok(false),
            Some(v) => anyhow::bail!("{k}={v} is not a boolean"),
        }
    }
    fn required(&self, k: &str) -> anyhow::Result<String> {
        let v = self.get(k).ok_or_else(|| anyhow::anyhow!("{k} is required"))?;
        anyhow::ensure!(!v.starts_with(PLACEHOLDER), "{k} still holds the .env.example placeholder value");
        Ok(v)
    }
}

fn expand(p: &str) -> PathBuf {
    match p.strip_prefix("~/") {
        Some(rest) => std::env::var_os("HOME").map(PathBuf::from).unwrap_or_default().join(rest),
        None => PathBuf::from(p),
    }
}

/// `.env.<profile>` (or `.env`) in the working directory, or in
/// `services/poly-kalshi-service/` when run from the repository root. The
/// process then works from that directory, so relative paths in the file
/// resolve next to it.
pub fn resolve_env_file(profile: Option<&str>) -> Option<PathBuf> {
    let name = profile.map(|p| format!(".env.{p}")).unwrap_or_else(|| ".env".into());
    let cwd = std::env::current_dir().ok()?;
    [cwd.join(&name), cwd.join("services/poly-kalshi-service").join(&name)].into_iter().find(|p| p.is_file())
}

impl Settings {
    /// Load `.env.<profile>` (process environment wins), then validate.
    pub fn load(profile: Option<&str>) -> anyhow::Result<Self> {
        let file = resolve_env_file(profile).ok_or_else(|| {
            let name = profile.map(|p| format!(".env.{p}")).unwrap_or_else(|| ".env".into());
            anyhow::anyhow!("missing {name}: copy .env.example.dev and configure it")
        })?;
        if let Some(dir) = file.parent().filter(|d| !d.as_os_str().is_empty()) {
            std::env::set_current_dir(dir)?;
        }
        let mut vars: HashMap<String, String> = HashMap::new();
        for item in dotenvy::from_path_iter(&file)? {
            let (k, v) = item?;
            vars.insert(k, v);
        }
        vars.extend(std::env::vars());
        Self::from_env(vars, profile)
    }

    pub fn from_env(vars: HashMap<String, String>, profile: Option<&str>) -> anyhow::Result<Self> {
        let e = Env(vars);
        let scoped = |key: &str, template: &str, default: &str| -> PathBuf {
            match (e.get(key), profile) {
                (Some(v), _) => expand(&v),
                (None, Some(p)) => PathBuf::from(template.replace("{profile}", p)),
                (None, None) => PathBuf::from(default),
            }
        };
        let mode = match e.or("PK_EXECUTION_MODE", "paper").to_lowercase().as_str() {
            "off" => ExecutionMode::Off,
            "paper" => ExecutionMode::Paper,
            "live" => ExecutionMode::Live,
            other => anyhow::bail!("PK_EXECUTION_MODE={other} is not off, paper or live"),
        };
        let assets: Vec<String> = e
            .or("PK_ASSETS", "BTC,ETH,SOL,XRP,BNB")
            .split(',')
            .map(|a| a.trim().to_uppercase())
            .filter(|a| !a.is_empty())
            .collect();
        anyhow::ensure!(!assets.is_empty(), "PK_ASSETS is empty");
        let unknown: Vec<&String> =
            assets.iter().filter(|a| !SUPPORTED_ASSETS.contains(&a.as_str())).collect();
        anyhow::ensure!(unknown.is_empty(), "PK_ASSETS has unsupported assets: {unknown:?}");

        let params = StrategyParams {
            gap_threshold: e.num("PK_GAP_THRESHOLD", 0.20)?,
            max_entry_px: e.num("PK_MAX_ENTRY_PX", 0.60)?,
            notional_usd: e.num("PK_NOTIONAL_USD", 50.0)?,
            entry_early_s: e.num("PK_ENTRY_EARLY_S", 20)?,
            entry_late_s: e.num("PK_ENTRY_LATE_S", 60)?,
            // The review's stale-frame and spread guards, available as live params.
            max_poly_spread: e.opt_num("PK_MAX_POLY_SPREAD")?,
            max_poly_lag_ms: e.opt_num("PK_MAX_POLY_LAG_MS")?,
            ..Default::default()
        };
        anyhow::ensure!(
            0.0 < params.gap_threshold && params.gap_threshold < 1.0,
            "PK_GAP_THRESHOLD must be in (0, 1)"
        );
        anyhow::ensure!(
            0.0 < params.max_entry_px && params.max_entry_px <= 1.0,
            "PK_MAX_ENTRY_PX must be in (0, 1]"
        );
        anyhow::ensure!(params.notional_usd > 0.0, "PK_NOTIONAL_USD must be positive");
        let order_size_pct: f64 = e.num("PK_ORDER_SIZE_PCT", 5.0)?;
        anyhow::ensure!(
            (0.0..=100.0).contains(&order_size_pct),
            "PK_ORDER_SIZE_PCT must be within 0..=100 (0 = static PK_NOTIONAL_USD)"
        );
        anyhow::ensure!(
            (0..=900).contains(&params.entry_early_s) && (0..=900).contains(&params.entry_late_s),
            "PK_ENTRY_EARLY_S and PK_ENTRY_LATE_S must be within 0..=900"
        );
        anyhow::ensure!(
            params.entry_early_s + params.entry_late_s < 900,
            "PK_ENTRY_EARLY_S + PK_ENTRY_LATE_S leaves no entry window"
        );
        let shadow_text =
            e.0.get("PK_SHADOW_VARIANTS").cloned().unwrap_or_else(|| DEFAULT_VARIANTS.to_owned());
        let shadow_variants = parse_variants(&shadow_text, &params)
            .map_err(|err| anyhow::anyhow!("PK_SHADOW_VARIANTS: {err}"))?;

        let max_open: f64 = e.required("PK_MAX_OPEN_NOTIONAL_USD")?.parse()?;
        let daily_loss: f64 = e.required("PK_DAILY_LOSS_LIMIT_USD")?.parse()?;
        anyhow::ensure!(max_open > 0.0 && daily_loss > 0.0, "risk limits must be positive");
        anyhow::ensure!(
            order_size_pct > 0.0 || params.notional_usd <= max_open,
            "PK_NOTIONAL_USD cannot exceed PK_MAX_OPEN_NOTIONAL_USD"
        );
        let paper_latency_ms: i64 = e.num("PK_PAPER_LATENCY_MS", 250)?;
        anyhow::ensure!(
            (0..=5000).contains(&paper_latency_ms),
            "PK_PAPER_LATENCY_MS must be within 0..=5000"
        );
        let latency_window: usize = e.num("PK_LATENCY_WINDOW", 50)?;
        let latency_min_samples: usize = e.num("PK_LATENCY_MIN_SAMPLES", 10)?;
        anyhow::ensure!(
            latency_min_samples >= 1 && latency_min_samples <= latency_window,
            "PK_LATENCY_MIN_SAMPLES must be within 1..=PK_LATENCY_WINDOW"
        );
        let probe_cadence_s: f64 = e.num("PK_PROBE_CADENCE_S", 30.0)?;
        anyhow::ensure!(
            probe_cadence_s == 0.0 || probe_cadence_s >= 5.0,
            "PK_PROBE_CADENCE_S must be 0 (off) or at least 5"
        );
        let limit_offset: f64 = e.num("PK_LIMIT_OFFSET", 0.0)?;
        anyhow::ensure!((0.0..=0.15).contains(&limit_offset), "PK_LIMIT_OFFSET must be within 0..=0.15");
        let shadow_latency_ms: i64 = e.num("PK_SHADOW_LATENCY_MS", 250)?;
        anyhow::ensure!(
            (0..=5000).contains(&shadow_latency_ms),
            "PK_SHADOW_LATENCY_MS must be within 0..=5000"
        );

        let port: u16 = e.num("PORT", 8050)?;
        let status_port: u16 = e.num("PK_STATUS_PORT", 8051)?;
        let observer_enabled = e.flag("PK_OBSERVER", true)?;
        let observer_port: u16 = e.num("PK_OBSERVER_PORT", 8052)?;
        anyhow::ensure!(
            !observer_enabled || (observer_port != port && observer_port != status_port),
            "PK_OBSERVER_PORT must differ from PORT and PK_STATUS_PORT"
        );
        let clob =
            (e.get("POLY_CLOB_API_KEY"), e.get("POLY_CLOB_API_SECRET"), e.get("POLY_CLOB_API_PASSPHRASE"));
        let polybolt_credentials = match clob {
            (Some(k), Some(s), Some(p)) => Some((k, s, p)),
            (None, None, None) => None,
            _ => anyhow::bail!(
                "POLY_CLOB_API_KEY, POLY_CLOB_API_SECRET and POLY_CLOB_API_PASSPHRASE go together"
            ),
        };
        let telegram_bot_token = e.get("PK_TELEGRAM_BOT_TOKEN");
        if let Some(t) = &telegram_bot_token {
            anyhow::ensure!(!t.starts_with(PLACEHOLDER), "PK_TELEGRAM_BOT_TOKEN still holds its placeholder");
        }
        let telegram_admin_user_ids: Vec<i64> = e
            .or("PK_TELEGRAM_ADMIN_USER_IDS", "")
            .split(',')
            .filter(|s| !s.trim().is_empty())
            .map(|s| s.trim().parse())
            .collect::<Result<_, _>>()?;
        anyhow::ensure!(
            telegram_bot_token.is_none() || !telegram_admin_user_ids.is_empty(),
            "PK_TELEGRAM_ADMIN_USER_IDS is required with PK_TELEGRAM_BOT_TOKEN"
        );
        let order_probe = e.or("PK_ORDER_PROBE", "off").to_lowercase() == "on";
        let poly_private_key = e.get("POLY_PRIVATE_KEY");
        anyhow::ensure!(
            !order_probe || poly_private_key.is_some(),
            "PK_ORDER_PROBE=on needs POLY_PRIVATE_KEY"
        );
        let poly_signature_type: u8 = e.num("POLY_SIGNATURE_TYPE", 0)?;
        anyhow::ensure!(poly_signature_type <= 3, "POLY_SIGNATURE_TYPE must be 0, 1, 2 or 3");
        let poly_funder = e.get("POLY_FUNDER");
        anyhow::ensure!(
            poly_signature_type != 3 || poly_funder.is_some(),
            "POLY_SIGNATURE_TYPE=3 (deposit wallet) needs POLY_FUNDER: the wallet's address, \
             shown as the deposit address on polymarket.com"
        );
        let poly_relayer_api_key = e.get("POLY_RELAYER_API_KEY");
        let polygon_rpc_url = e.get("POLYGON_RPC_URL");
        if mode == ExecutionMode::Live {
            let refused = |why: &str| anyhow::anyhow!("PK_EXECUTION_MODE=live is refused: {why}");
            if e.get("PK_LIVE_CONFIRM").map(|v| v.to_lowercase()).as_deref() != Some("yes") {
                return Err(refused(
                    "set PK_LIVE_CONFIRM=yes once the paper gates pass and this host may trade",
                ));
            }
            if poly_private_key.is_none() {
                return Err(refused("POLY_PRIVATE_KEY is not set"));
            }
            if poly_signature_type != 0 && poly_signature_type != 3 {
                return Err(refused(
                    "POLY_SIGNATURE_TYPE must be 0 (an EOA) or 3 (a deposit wallet): the service \
                     cannot redeem winnings from proxy or Safe wallets",
                ));
            }
            if poly_signature_type == 3 && poly_relayer_api_key.is_none() {
                return Err(refused(
                    "POLY_RELAYER_API_KEY is not set (a deposit wallet redeems through Polymarket's \
                     relayer; create one under polymarket.com > Settings > API Keys)",
                ));
            }
            if polygon_rpc_url.is_none() {
                return Err(refused("POLYGON_RPC_URL is not set (needed to redeem settled positions)"));
            }
        }
        let api_key = e.required("API_KEY")?;
        anyhow::ensure!(api_key.len() >= 16, "API_KEY must be at least 16 characters");
        let log_level = e.or("LOG_LEVEL", "INFO").to_uppercase();
        anyhow::ensure!(
            ["DEBUG", "INFO", "WARNING", "WARN", "ERROR", "CRITICAL"].contains(&log_level.as_str()),
            "LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL"
        );

        Ok(Self {
            profile: profile.map(str::to_owned),
            api_key,
            host: e.or("HOST", "127.0.0.1"),
            port,
            log_level: if log_level == "WARNING" || log_level == "CRITICAL" {
                "WARN".into()
            } else {
                log_level
            },
            events_log_path: scoped("EVENTS_LOG_PATH", "logs/events.{profile}.jsonl", "logs/events.jsonl"),
            execution_mode: mode,
            assets,
            params,
            shadow_variants,
            paper_latency_ms,
            order_size_pct,
            probe_cadence_s,
            latency_window,
            latency_min_samples,
            limit_offset,
            size_to_depth: e.flag("PK_SIZE_TO_DEPTH", false)?,
            active_exit: e.flag("PK_ACTIVE_EXIT", true)?,
            size_at_limit: e.flag("PK_SIZE_AT_LIMIT", true)?,
            shadow_latency_ms,
            paper_start_cash_usd: e.num("PK_PAPER_START_CASH_USD", 1000.0)?,
            max_open_notional_usd: max_open,
            daily_loss_limit_usd: daily_loss,
            kill_file_path: scoped("PK_KILL_FILE_PATH", "data/KILL.{profile}", "data/KILL"),
            record_enabled: e.flag("PK_RECORD_ENABLED", true)?,
            record_dir: scoped("PK_RECORD_DIR", "data/raw/{profile}", "data/raw"),
            host_tag: e.or("PK_HOST_TAG", "mac"),
            min_free_disk_gb: e.num("PK_MIN_FREE_DISK_GB", 5.0)?,
            state_dir: scoped("PK_STATE_DIR", "data/state/{profile}", "data/state"),
            // Epoch 7 began with the Rust cutover (new journal and tape formats).
            results_epoch: e.or("PK_RESULTS_EPOCH", "7"),
            poly_sockets: e.num("PK_POLY_SOCKETS", 0)?,
            heartbeat_seconds: e.num("PK_HEARTBEAT_SECONDS", 60.0)?,
            stale_feed_seconds: e.num("PK_STALE_FEED_SECONDS", 30.0)?,
            discover_lead_seconds: e.num("PK_DISCOVER_LEAD_SECONDS", 120)?,
            age_outlier_ms: e.num("PK_AGE_OUTLIER_MS", 500.0)?,
            kalshi_api_key_id: e.required("KALSHI_API_KEY_ID")?,
            kalshi_private_key_path: expand(&e.required("KALSHI_PRIVATE_KEY_PATH")?),
            kalshi_rest_url: e.or("KALSHI_REST_URL", "https://api.elections.kalshi.com/trade-api/v2"),
            kalshi_ws_url: e.or("KALSHI_WS_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2"),
            poly_gamma_url: e.or("POLY_GAMMA_URL", "https://gamma-api.polymarket.com"),
            poly_ws_url: e.or("POLY_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"),
            poly_clob_url: e.or("POLY_CLOB_URL", "https://clob.polymarket.com"),
            observer_enabled,
            observer_port,
            polybolt_url: e.or("POLYBOLT_URL", "wss://ws-live-v2.polymarket.com/ws"),
            polybolt_credentials,
            binance_enabled: e.flag("PK_BINANCE", true)?,
            binance_spot_ws_url: e.or("BINANCE_SPOT_WS_URL", "wss://stream.binance.com:9443/stream"),
            binance_perp_ws_url: e.or("BINANCE_PERP_WS_URL", "wss://fstream.binance.com/public/stream"),
            order_probe,
            poly_private_key,
            poly_funder,
            poly_signature_type,
            poly_relayer_url: e.or("POLY_RELAYER_URL", "https://relayer-v2.polymarket.com"),
            poly_relayer_api_key,
            poly_relayer_api_key_address: e.get("POLY_RELAYER_API_KEY_ADDRESS"),
            polygon_rpc_url,
            poly_redeem_collateral: e.get("POLY_REDEEM_COLLATERAL"),
            status_host: e.or("PK_STATUS_HOST", "127.0.0.1"),
            status_port,
            telegram_bot_token,
            telegram_admin_user_ids,
            notify: NotifyConfig {
                enabled: e.flag("NOTIFICATIONS_ENABLED", false)?,
                url: e.or("NOTIFICATION_SERVICE_URL", "http://127.0.0.1:3010"),
                api_key: e.get("NOTIFICATION_API_KEY"),
                channels: e
                    .or("NOTIFICATION_CHANNELS", "TELEGRAM")
                    .split(',')
                    .map(|c| c.trim().to_uppercase())
                    .filter(|c| !c.is_empty())
                    .collect(),
                source: match profile {
                    Some(p) => format!("poly-kalshi.{p}"),
                    None => "poly-kalshi".into(),
                },
                timeout: Duration::from_secs_f64(e.num("NOTIFICATION_TIMEOUT_SECONDS", 30.0)?),
            },
        })
    }

    /// Fail at startup with a fix, not a stack trace.
    pub fn check_paths(&self) -> anyhow::Result<()> {
        anyhow::ensure!(
            self.kalshi_private_key_path.is_file(),
            "KALSHI_PRIVATE_KEY_PATH={} is not a file. Point it at the PEM private key downloaded \
             with the Kalshi API key (chmod 600).",
            self.kalshi_private_key_path.display()
        );
        if self.record_enabled {
            let mut existing: &Path = &self.record_dir;
            while !existing.exists() {
                match existing.parent() {
                    Some(p) if !p.as_os_str().is_empty() => existing = p,
                    _ => {
                        existing = Path::new(".");
                        break;
                    }
                }
            }
            let probe = existing.join(format!(".pk-write-check-{}", std::process::id()));
            let writable = std::fs::write(&probe, b"").is_ok();
            let _ = std::fs::remove_file(&probe);
            anyhow::ensure!(
                writable,
                "PK_RECORD_DIR={} is not writable by this user ({}). Create it with \
                 `sudo mkdir -p {0} && sudo chown $USER: {0}`.",
                self.record_dir.display(),
                existing.display()
            );
        }
        Ok(())
    }

    /// Whether the trader opens the real order path (probe or live orders).
    pub fn order_path(&self) -> bool {
        self.order_probe || self.execution_mode == ExecutionMode::Live
    }

    pub fn telegram_enabled(&self) -> bool {
        self.telegram_bot_token.is_some()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn base() -> HashMap<String, String> {
        [
            ("API_KEY", "test-api-key-at-least-16"),
            ("KALSHI_API_KEY_ID", "kid"),
            ("KALSHI_PRIVATE_KEY_PATH", "/tmp/k.pem"),
            ("PK_MAX_OPEN_NOTIONAL_USD", "500"),
            ("PK_DAILY_LOSS_LIMIT_USD", "250"),
            ("PK_GAP_THRESHOLD", "0.15"),
        ]
        .into_iter()
        .map(|(k, v)| (k.to_owned(), v.to_owned()))
        .collect()
    }

    #[test]
    fn loads_and_scopes_paths() {
        let s = Settings::from_env(base(), Some("dev")).unwrap();
        assert_eq!(s.params.gap_threshold, 0.15);
        assert_eq!(s.record_dir, PathBuf::from("data/raw/dev"));
        assert_eq!(s.state_dir, PathBuf::from("data/state/dev"));
        assert_eq!(s.shadow_variants.len(), 17);
        assert_eq!(s.execution_mode, ExecutionMode::Paper);
        assert!(!s.order_probe && !s.order_path());
        assert_eq!((s.order_size_pct, s.latency_window, s.latency_min_samples), (5.0, 50, 10));
    }

    #[test]
    fn refuses_live_and_bad_values() {
        let with = |k: &str, v: &str| {
            let mut m = base();
            m.insert(k.into(), v.into());
            Settings::from_env(m, None)
        };
        assert!(with("PK_EXECUTION_MODE", "live").unwrap_err().to_string().contains("PK_LIVE_CONFIRM"));
        assert!(with("PK_EXECUTION_MODE", "maybe").is_err());
        assert!(with("PK_ASSETS", "BTC,FOO").is_err());
        // A static size above the open cap is refused; a percentage is capped per order.
        assert!(with("PK_NOTIONAL_USD", "600").is_ok());
        let mut m = base();
        m.extend([("PK_NOTIONAL_USD".into(), "600".into()), ("PK_ORDER_SIZE_PCT".into(), "0".into())]);
        assert!(Settings::from_env(m, None).is_err());
        assert!(with("PK_ORDER_SIZE_PCT", "101").is_err());
        assert!(with("PK_LATENCY_MIN_SAMPLES", "0").is_err());
        assert!(with("PK_PROBE_CADENCE_S", "1").is_err());
        assert!(with("PK_ORDER_PROBE", "on").is_err());
        assert!(with("POLY_CLOB_API_KEY", "k").is_err());
        assert!(with("API_KEY", "replace-with-a-long-random-key").is_err());
        assert!(with("PK_SHADOW_VARIANTS", "[{\"name\":\"live\"}]").is_err());
        assert_eq!(with("PK_MAX_POLY_LAG_MS", "250").unwrap().params.max_poly_lag_ms, Some(250.0));
        assert!(with("PK_SHADOW_VARIANTS", "").unwrap().shadow_variants.is_empty());
    }

    #[test]
    fn every_supported_asset_loads_and_every_feed_covers_it() {
        // Onboarding a coin means every table at once: PK_ASSETS validation
        // here, the observer's PolyBolt symbols and the Binance lead-lag
        // streams. A gap in any table silently drops the coin from that feed.
        let mut m = base();
        m.insert("PK_ASSETS".into(), SUPPORTED_ASSETS.join(","));
        let s = Settings::from_env(m, None).unwrap();
        assert_eq!(s.assets, SUPPORTED_ASSETS);
        for a in SUPPORTED_ASSETS {
            assert!(
                pk_feeds::ws::POLYBOLT_SYMBOLS.iter().any(|(x, _)| x == &a),
                "{a} has no PolyBolt symbol"
            );
            assert!(pk_feeds::ws::binance_stream(a, true).is_some(), "{a} has no Binance perp stream");
            // HYPE is perp only (no Binance spot market); every other coin
            // needs the spot leg too.
            assert!(
                a == "HYPE" || pk_feeds::ws::binance_stream(a, false).is_some(),
                "{a} has no Binance spot stream"
            );
        }
        for (x, _) in pk_feeds::ws::POLYBOLT_SYMBOLS {
            assert!(SUPPORTED_ASSETS.contains(&x), "{x} is not a supported asset");
        }
    }

    #[test]
    fn live_needs_every_guard() {
        let full = || {
            let mut m = base();
            for (k, v) in [
                ("PK_EXECUTION_MODE", "live"),
                ("PK_LIVE_CONFIRM", "yes"),
                ("POLY_PRIVATE_KEY", "0xabc"),
                ("POLYGON_RPC_URL", "https://polygon.example"),
            ] {
                m.insert(k.into(), v.into());
            }
            m
        };
        let s = Settings::from_env(full(), None).unwrap();
        assert_eq!(s.execution_mode, ExecutionMode::Live);
        assert!(s.order_path() && !s.order_probe);
        let without = |k: &str| {
            let mut m = full();
            m.remove(k);
            Settings::from_env(m, None).unwrap_err().to_string()
        };
        assert!(without("PK_LIVE_CONFIRM").contains("PK_LIVE_CONFIRM"));
        assert!(without("POLY_PRIVATE_KEY").contains("POLY_PRIVATE_KEY"));
        assert!(without("POLYGON_RPC_URL").contains("POLYGON_RPC_URL"));
        let mut proxy = full();
        proxy.insert("POLY_SIGNATURE_TYPE".into(), "1".into());
        assert!(Settings::from_env(proxy, None).unwrap_err().to_string().contains("POLY_SIGNATURE_TYPE"));

        // A deposit wallet: its address, and a relayer key to redeem with.
        let deposit = |extra: &[(&str, &str)]| {
            let mut m = full();
            m.insert("POLY_SIGNATURE_TYPE".into(), "3".into());
            m.extend(extra.iter().map(|(k, v)| ((*k).to_owned(), (*v).to_owned())));
            Settings::from_env(m, None)
        };
        assert!(deposit(&[]).unwrap_err().to_string().contains("POLY_FUNDER"));
        let no_key = deposit(&[("POLY_FUNDER", "0xw")]).unwrap_err().to_string();
        assert!(no_key.contains("POLY_RELAYER_API_KEY"), "{no_key}");
        let s = deposit(&[("POLY_FUNDER", "0xw"), ("POLY_RELAYER_API_KEY", "rk")]).unwrap();
        assert_eq!((s.poly_signature_type, s.poly_funder.as_deref()), (3, Some("0xw")));
        assert_eq!(s.poly_relayer_url, "https://relayer-v2.polymarket.com");
    }
}
