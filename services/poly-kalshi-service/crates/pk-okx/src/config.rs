//! `PK_OKX_*` settings, read from the same `.env.<profile>` as the other services.
//!
//! The shared parts (Kalshi key, ports, paths) come from [`pk_svc::Settings`].
//! There is no live mode yet: `PK_OKX_EXECUTION_MODE=live` is refused until an
//! order path exists and the paper gate has passed.

use std::collections::HashMap;
use std::path::PathBuf;

use pk_core::StrategyParams;
use pk_svc::Settings;

#[derive(Clone, Debug)]
pub struct OkxSettings {
    pub port: u16,
    pub assets: Vec<String>,
    pub epoch: String,
    pub api_url: String,
    pub ws_url: String,
    /// The event series per asset: `<ASSET>-<series>`, e.g. `BTC-UPDOWN-15MIN`.
    pub series: String,
    pub params: StrategyParams,
    pub shares: f64,
    pub limit_offset: f64,
    pub exit_slip: f64,
    pub active_exit: bool,
    /// Taker fee per share: `rate · (p(1 − p))^exponent` (exponent 0: flat per share).
    pub fee_rate: f64,
    pub fee_exponent: f64,
    /// Taken from each winning share's 1 USDT at settlement (not on exits).
    pub settle_fee: f64,
    pub paper_cash: f64,
    /// Seed for the paper round trip until the net probe has measured it.
    pub paper_latency_ms: f64,
    pub paper_extra_ms: f64,
    pub max_open_usd: f64,
    pub daily_loss_usd: f64,
    pub netprobe_every_s: u64,
    pub record: bool,
    pub record_dir: PathBuf,
    pub state_dir: PathBuf,
    pub api_key: Option<String>,
    pub api_secret: Option<String>,
    pub api_passphrase: Option<String>,
}

struct Env(HashMap<String, String>);

impl Env {
    fn get(&self, k: &str) -> Option<String> {
        self.0.get(k).map(|v| v.trim().to_owned()).filter(|v| !v.is_empty())
    }

    fn or(&self, k: &str, d: &str) -> String {
        self.get(k).unwrap_or_else(|| d.to_owned())
    }

    fn num<T: std::str::FromStr>(&self, k: &str, d: T) -> anyhow::Result<T> {
        match self.get(k) {
            Some(v) => v.parse().map_err(|_| anyhow::anyhow!("{k}={v} is not a number")),
            None => Ok(d),
        }
    }

    fn flag(&self, k: &str, d: bool) -> anyhow::Result<bool> {
        match self.get(k).map(|v| v.to_lowercase()) {
            None => Ok(d),
            Some(v) if ["1", "true", "yes", "on"].contains(&v.as_str()) => Ok(true),
            Some(v) if ["0", "false", "no", "off"].contains(&v.as_str()) => Ok(false),
            Some(v) => anyhow::bail!("{k}={v} is not a boolean"),
        }
    }
}

impl OkxSettings {
    /// After `Settings::load` (which moved into the env file's directory).
    pub fn load(settings: &Settings) -> anyhow::Result<Self> {
        let mut vars = HashMap::new();
        if let Some(file) = pk_svc::config::resolve_env_file(settings.profile.as_deref()) {
            for item in dotenvy::from_path_iter(&file)? {
                let (k, v) = item?;
                vars.insert(k, v);
            }
        }
        vars.extend(std::env::vars());
        Self::from_env(vars, settings)
    }

    pub fn from_env(vars: HashMap<String, String>, settings: &Settings) -> anyhow::Result<Self> {
        let e = Env(vars);
        match e.or("PK_OKX_EXECUTION_MODE", "paper").to_lowercase().as_str() {
            "paper" | "off" => {}
            "live" => anyhow::bail!("PK_OKX_EXECUTION_MODE=live: pk-okx has no live order path yet (paper only)"),
            other => anyhow::bail!("PK_OKX_EXECUTION_MODE={other} is not paper"),
        }
        let assets: Vec<String> = e
            .or("PK_OKX_ASSETS", "BTC,ETH,SOL")
            .split(',')
            .map(|a| a.trim().to_uppercase())
            .filter(|a| !a.is_empty())
            .collect();
        anyhow::ensure!(!assets.is_empty(), "PK_OKX_ASSETS is empty");
        let params = StrategyParams {
            gap_threshold: e.num("PK_OKX_GAP_THRESHOLD", 0.20)?,
            max_entry_px: e.num("PK_OKX_MAX_ENTRY_PX", 0.60)?,
            notional_usd: 1e9, // sized in shares, never by notional
            entry_early_s: e.num("PK_OKX_ENTRY_EARLY_S", 20)?,
            entry_late_s: e.num("PK_OKX_ENTRY_LATE_S", 60)?,
            max_kalshi_spread: Some(e.num("PK_OKX_MAX_KALSHI_SPREAD", 0.02)?),
            max_poly_spread: Some(e.num("PK_OKX_MAX_SPREAD", 0.05)?),
            min_edge: Some(e.num("PK_OKX_MIN_EDGE", 0.10)?),
            min_entry_px: Some(e.num("PK_OKX_MIN_ENTRY_PX", 0.20)?),
            min_net_edge: Some(e.num("PK_OKX_MIN_NET_EDGE", 0.03)?),
            ..Default::default()
        };
        let okx = Self {
            port: settings.okx_port,
            assets,
            epoch: e.or("PK_OKX_EPOCH", "1"),
            api_url: e.or("PK_OKX_API_URL", "https://www.okx.com"),
            ws_url: e.or("PK_OKX_WS_URL", "wss://ws.okx.com:8443/ws/v5/public"),
            series: e.or("PK_OKX_SERIES", "UPDOWN-15MIN"),
            params,
            shares: e.num("PK_OKX_ORDER_SHARES", 10.0)?,
            limit_offset: e.num("PK_OKX_LIMIT_OFFSET", 0.06)?,
            exit_slip: e.num("PK_OKX_EXIT_SLIP", 0.01)?,
            active_exit: e.flag("PK_OKX_ACTIVE_EXIT", true)?,
            fee_rate: e.num("PK_OKX_FEE_RATE", 0.07)?,
            fee_exponent: e.num("PK_OKX_FEE_EXPONENT", 1.0)?,
            settle_fee: e.num("PK_OKX_SETTLE_FEE", 0.0)?,
            paper_cash: e.num("PK_OKX_PAPER_CASH", 100.0)?,
            paper_latency_ms: e.num("PK_OKX_PAPER_LATENCY_MS", 30.0)?,
            paper_extra_ms: e.num("PK_OKX_PAPER_EXTRA_MS", 0.0)?,
            max_open_usd: e.num("PK_OKX_MAX_OPEN_USD", 20.0)?,
            daily_loss_usd: e.num("PK_OKX_DAILY_LOSS_USD", 10.0)?,
            netprobe_every_s: e.num("PK_OKX_NETPROBE_S", 5)?,
            record: e.flag("PK_OKX_RECORD", true)?,
            record_dir: e.get("PK_OKX_RECORD_DIR").map(PathBuf::from).unwrap_or_else(|| {
                let mut s = settings.record_dir.clone().into_os_string();
                s.push("-okx");
                PathBuf::from(s)
            }),
            state_dir: e.get("PK_OKX_STATE_DIR").map(PathBuf::from).unwrap_or_else(|| settings.state_dir.join("okx")),
            api_key: e.get("OKX_API_KEY"),
            api_secret: e.get("OKX_API_SECRET"),
            api_passphrase: e.get("OKX_API_PASSPHRASE"),
        };
        anyhow::ensure!(okx.shares >= 0.01, "PK_OKX_ORDER_SHARES must be at least 0.01");
        anyhow::ensure!((0.0..=1.0).contains(&okx.settle_fee), "PK_OKX_SETTLE_FEE is per share, within 0..=1");
        anyhow::ensure!(
            okx.port != settings.port
                && okx.port != settings.observer_port
                && okx.port != settings.status_port
                && okx.port != settings.lim_port,
            "PK_OKX_PORT {} clashes with another service's port",
            okx.port
        );
        Ok(okx)
    }

    /// The API key, secret and passphrase, if all are set.
    pub fn creds(&self) -> Option<(String, String, String)> {
        Some((self.api_key.clone()?, self.api_secret.clone()?, self.api_passphrase.clone()?))
    }

    /// The values that define the strategy, hashed into the journal's `config`.
    pub fn config_json(&self) -> serde_json::Value {
        serde_json::json!({
            "params": self.params,
            "series": self.series,
            "shares": self.shares,
            "limit_offset": self.limit_offset,
            "exit_slip": self.exit_slip,
            "active_exit": self.active_exit,
            "fee_rate": self.fee_rate,
            "fee_exponent": self.fee_exponent,
            "settle_fee": self.settle_fee,
            "paper_extra_ms": self.paper_extra_ms,
        })
    }
}
