//! `PK_LIM_*` settings, read from the same `.env.<profile>` as the trader.
//!
//! The shared parts (Kalshi key, ports, paths, the EOA key) come from
//! [`pk_svc::Settings`]; nothing here changes how the trader reads its file.

use std::collections::HashMap;
use std::path::PathBuf;

use pk_core::StrategyParams;
use pk_svc::Settings;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Paper,
    Live,
}

impl Mode {
    pub fn as_str(self) -> &'static str {
        match self {
            Mode::Paper => "paper",
            Mode::Live => "live",
        }
    }
}

#[derive(Clone, Debug)]
pub struct LimSettings {
    pub mode: Mode,
    pub port: u16,
    pub assets: Vec<String>,
    pub epoch: String,
    pub api_url: String,
    pub ws_url: String,
    pub params: StrategyParams,
    pub shares: u64,
    pub limit_offset: f64,
    pub exit_slip: f64,
    pub active_exit: bool,
    pub fee_rate: f64,
    pub paper_cash: f64,
    /// Added to the measured round trip and the taker delay in paper fills.
    pub paper_extra_ms: f64,
    pub max_open_usd: f64,
    pub daily_loss_usd: f64,
    pub netprobe_every_s: u64,
    pub record: bool,
    pub record_dir: PathBuf,
    pub state_dir: PathBuf,
    pub api_token: Option<String>,
    pub api_secret: Option<String>,
    pub private_key: Option<String>,
    pub base_rpc_url: Option<String>,
    /// SOL hourly, priced from Helius on-chain SOL (paper only); off without a Helius key.
    pub hourly: Option<crate::hourly::HourlyCfg>,
    pub hourly_epoch: String,
    pub helius_url: String,
    pub helius_key: Option<String>,
    pub binance_spot_ws_url: String,
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

impl LimSettings {
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
        let mode = match e.or("PK_LIM_EXECUTION_MODE", "paper").to_lowercase().as_str() {
            "paper" | "off" => Mode::Paper,
            "live" => Mode::Live,
            other => anyhow::bail!("PK_LIM_EXECUTION_MODE={other} is not paper or live"),
        };
        if mode == Mode::Live {
            anyhow::ensure!(
                e.or("PK_LIM_LIVE_CONFIRM", "") == "yes",
                "PK_LIM_EXECUTION_MODE=live needs PK_LIM_LIVE_CONFIRM=yes (set only once the paper gate passed)"
            );
        }
        let assets: Vec<String> = e
            .or("PK_LIM_ASSETS", "BTC,ETH")
            .split(',')
            .map(|a| a.trim().to_uppercase())
            .filter(|a| !a.is_empty())
            .collect();
        anyhow::ensure!(!assets.is_empty(), "PK_LIM_ASSETS is empty");
        let params = StrategyParams {
            gap_threshold: e.num("PK_LIM_GAP_THRESHOLD", 0.20)?,
            max_entry_px: e.num("PK_LIM_MAX_ENTRY_PX", 0.60)?,
            notional_usd: 1e9, // sized in shares, never by notional
            entry_early_s: e.num("PK_LIM_ENTRY_EARLY_S", 20)?,
            entry_late_s: e.num("PK_LIM_ENTRY_LATE_S", 60)?,
            max_kalshi_spread: Some(e.num("PK_LIM_MAX_KALSHI_SPREAD", 0.02)?),
            max_poly_spread: Some(e.num("PK_LIM_MAX_SPREAD", 0.05)?),
            min_edge: Some(e.num("PK_LIM_MIN_EDGE", 0.10)?),
            min_entry_px: Some(e.num("PK_LIM_MIN_ENTRY_PX", 0.20)?),
            min_net_edge: Some(e.num("PK_LIM_MIN_NET_EDGE", 0.03)?),
            ..Default::default()
        };
        let lim = Self {
            mode,
            port: settings.lim_port,
            assets,
            epoch: e.or("PK_LIM_EPOCH", "1"),
            api_url: e.or("PK_LIM_API_URL", "https://api.limitless.exchange"),
            ws_url: e.or("PK_LIM_WS_URL", "wss://ws.limitless.exchange/socket.io/?EIO=4&transport=websocket"),
            params,
            shares: e.num("PK_LIM_ORDER_SHARES", 10)?,
            limit_offset: e.num("PK_LIM_LIMIT_OFFSET", 0.06)?,
            exit_slip: e.num("PK_LIM_EXIT_SLIP", 0.01)?,
            active_exit: e.flag("PK_LIM_ACTIVE_EXIT", true)?,
            fee_rate: e.num("PK_LIM_FEE_RATE", 0.05)?,
            paper_cash: e.num("PK_LIM_PAPER_CASH", 100.0)?,
            paper_extra_ms: e.num("PK_LIM_PAPER_EXTRA_MS", 0.0)?,
            max_open_usd: e.num("PK_LIM_MAX_OPEN_USD", 20.0)?,
            daily_loss_usd: e.num("PK_LIM_DAILY_LOSS_USD", 10.0)?,
            netprobe_every_s: e.num("PK_LIM_NETPROBE_S", 5)?,
            record: e.flag("PK_LIM_RECORD", true)?,
            record_dir: e.get("PK_LIM_RECORD_DIR").map(PathBuf::from).unwrap_or_else(|| {
                let mut s = settings.record_dir.clone().into_os_string();
                s.push("-limitless");
                PathBuf::from(s)
            }),
            state_dir: e
                .get("PK_LIM_STATE_DIR")
                .map(PathBuf::from)
                .unwrap_or_else(|| settings.state_dir.join("limitless")),
            api_token: e.get("LIMITLESS_API_KEY"),
            api_secret: e.get("LIMITLESS_API_SECRET"),
            private_key: e.get("LIMITLESS_PRIVATE_KEY").or_else(|| settings.poly_private_key.clone()),
            base_rpc_url: e.get("BASE_RPC_URL"),
            hourly: None,
            hourly_epoch: e.or("PK_LIMH_EPOCH", "1"),
            helius_url: settings.laserstream_url.clone(),
            helius_key: settings.helius_api_key.clone(),
            binance_spot_ws_url: settings.binance_spot_ws_url.clone(),
        };
        let mut lim = lim;
        if e.flag("PK_LIMH_ENABLED", true)? && lim.helius_key.is_some() {
            lim.hourly = Some(crate::hourly::HourlyCfg {
                min_edge: e.num("PK_LIMH_MIN_EDGE", 0.10)?,
                min_net_edge: e.num("PK_LIMH_MIN_NET_EDGE", 0.03)?,
                min_px: e.num("PK_LIMH_MIN_PX", 0.20)?,
                max_px: e.num("PK_LIMH_MAX_PX", 0.60)?,
                max_spread: e.num("PK_LIMH_MAX_SPREAD", 0.10)?,
                early_s: e.num("PK_LIMH_EARLY_S", 120)?,
                late_s: e.num("PK_LIMH_LATE_S", 120)?,
                shares: e.num("PK_LIMH_ORDER_SHARES", lim.shares as f64)?,
                limit_offset: lim.limit_offset,
                exit_slip: lim.exit_slip,
                fee_rate: lim.fee_rate,
                vol_halflife_s: e.num("PK_LIMH_VOL_HALFLIFE_S", 600.0)?,
                basis_halflife_s: e.num("PK_LIMH_BASIS_HALFLIFE_S", 300.0)?,
                paper_extra_ms: lim.paper_extra_ms,
                max_open_usd: lim.max_open_usd,
            });
        }
        anyhow::ensure!(lim.shares >= 1, "PK_LIM_ORDER_SHARES must be at least 1");
        anyhow::ensure!(
            lim.port != settings.port && lim.port != settings.observer_port && lim.port != settings.status_port,
            "PK_LIM_PORT {} clashes with another service's port",
            lim.port
        );
        if lim.mode == Mode::Live {
            anyhow::ensure!(
                lim.api_token.is_some() && lim.api_secret.is_some() && lim.private_key.is_some(),
                "live needs LIMITLESS_API_KEY, LIMITLESS_API_SECRET and a private key"
            );
            anyhow::ensure!(lim.base_rpc_url.is_some(), "live needs BASE_RPC_URL (redeem)");
        }
        Ok(lim)
    }

    /// Credentials for the order path (probes and live), if all are set.
    pub fn creds(&self) -> Option<(String, String, String)> {
        Some((self.api_token.clone()?, self.api_secret.clone()?, self.private_key.clone()?))
    }

    /// The values that define the strategy, hashed into the journal's `config`.
    pub fn config_json(&self) -> serde_json::Value {
        serde_json::json!({
            "params": self.params,
            "shares": self.shares,
            "limit_offset": self.limit_offset,
            "exit_slip": self.exit_slip,
            "active_exit": self.active_exit,
            "fee_rate": self.fee_rate,
            "paper_extra_ms": self.paper_extra_ms,
        })
    }
}
