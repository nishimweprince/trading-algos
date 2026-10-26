//! `PK_PM_*` settings, read from the same `.env.<profile>` as the other services.
//!
//! The wallet (`POLY_PRIVATE_KEY`, `POLY_FUNDER`, `POLY_SIGNATURE_TYPE`), the
//! relayer, the Polygon RPC and the Kalshi key come from [`pk_svc::Settings`].
//! `PK_PM_MODE=live` needs `PK_PM_LIVE_CONFIRM=yes`, and is refused while the
//! taker (`PK_EXECUTION_MODE=live`) trades the same wallet.

use std::collections::HashMap;
use std::path::PathBuf;

use pk_svc::Settings;
use pk_svc::config::ExecutionMode;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    /// Every order is decided and journaled; none is sent.
    Shadow,
    Live,
}

impl Mode {
    pub fn as_str(self) -> &'static str {
        match self {
            Mode::Shadow => "shadow",
            Mode::Live => "live",
        }
    }
}

#[derive(Clone, Debug)]
pub struct PmSettings {
    pub mode: Mode,
    pub port: u16,
    pub epoch: String,
    pub assets: Vec<String>,
    pub cfg: crate::engine::PmCfg,
    pub state_dir: PathBuf,
    pub fills_poll_ms: u64,
    pub reconcile_s: u64,
    pub feed_stale_ms: i64,
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

}

impl PmSettings {
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
        let mode = match e.or("PK_PM_MODE", "shadow").to_lowercase().as_str() {
            "shadow" => Mode::Shadow,
            "live" => {
                anyhow::ensure!(
                    e.get("PK_PM_LIVE_CONFIRM").as_deref() == Some("yes"),
                    "PK_PM_MODE=live needs PK_PM_LIVE_CONFIRM=yes"
                );
                anyhow::ensure!(
                    settings.execution_mode != ExecutionMode::Live,
                    "PK_PM_MODE=live while the taker is live on the same wallet (PK_EXECUTION_MODE=live)"
                );
                anyhow::ensure!(settings.poly_private_key.is_some(), "PK_PM_MODE=live needs POLY_PRIVATE_KEY");
                anyhow::ensure!(settings.polygon_rpc_url.is_some(), "PK_PM_MODE=live needs POLYGON_RPC_URL (redeem)");
                Mode::Live
            }
            other => anyhow::bail!("PK_PM_MODE={other} is not shadow or live"),
        };
        let assets: Vec<String> = e
            .or("PK_PM_ASSETS", "BTC,ETH,SOL,XRP,BNB,HYPE,ZEC")
            .split(',')
            .map(|a| a.trim().to_uppercase())
            .filter(|a| !a.is_empty())
            .collect();
        anyhow::ensure!(!assets.is_empty(), "PK_PM_ASSETS is empty");
        let cfg = crate::engine::PmCfg {
            half_spread: e.num("PK_PM_HALF_SPREAD", 0.02)?,
            shares: e.num("PK_PM_SHARES", 5.0)?,
            min_shares: e.num("PK_PM_MIN_SHARES", 5.0)?,
            max_net: e.num("PK_PM_MAX_NET", 20.0)?,
            skew_k: e.num("PK_PM_SKEW_K", 0.01)?,
            pause_jump: e.num("PK_PM_PAUSE_JUMP", 0.02)?,
            pause_ms: e.num("PK_PM_PAUSE_MS", 1500)?,
            max_kalshi_spread: e.num("PK_PM_MAX_KALSHI_SPREAD", 0.02)?,
            kalshi_max_age_ms: e.num("PK_PM_KALSHI_MAX_AGE_MS", 10_000)?,
            min_px: e.num("PK_PM_MIN_PX", 0.05)?,
            max_px: e.num("PK_PM_MAX_PX", 0.95)?,
            entry_early_s: e.num("PK_PM_ENTRY_EARLY_S", 20)?,
            stop_s: e.num("PK_PM_STOP_S", 60)?,
            requote_cents: e.num("PK_PM_REQUOTE_CENTS", 1.0)?,
            min_requote_ms: e.num("PK_PM_MIN_REQUOTE_MS", 300)?,
            adverse_ms: e.num("PK_PM_ADVERSE_MS", 200)?,
            reject_cooldown_ms: e.num("PK_PM_REJECT_COOLDOWN_MS", 500)?,
            max_orders_per_s: e.num("PK_PM_MAX_ORDERS_PER_S", 8.0)?,
            daily_loss_usd: e.num("PK_PM_DAILY_LOSS_USD", 15.0)?,
            max_open_cost_usd: e.num("PK_PM_MAX_OPEN_COST_USD", 60.0)?,
            pair_margin: e.num("PK_PM_PAIR_MARGIN", 0.02)?,
        };
        let pm = Self {
            mode,
            port: e.num("PK_PM_PORT", 8055)?,
            epoch: e.or("PK_PM_EPOCH", "1"),
            assets,
            cfg,
            state_dir: e.get("PK_PM_STATE_DIR").map(PathBuf::from).unwrap_or_else(|| settings.state_dir.join("polymaker")),
            fills_poll_ms: e.num("PK_PM_FILLS_POLL_MS", 1000)?,
            reconcile_s: e.num("PK_PM_RECONCILE_S", 30)?,
            feed_stale_ms: e.num("PK_PM_FEED_STALE_MS", 5000)?,
        };
        let c = &pm.cfg;
        anyhow::ensure!(c.shares >= c.min_shares, "PK_PM_SHARES is below the venue minimum PK_PM_MIN_SHARES");
        anyhow::ensure!(c.max_net >= c.shares, "PK_PM_MAX_NET must be at least PK_PM_SHARES");
        anyhow::ensure!((0.005..=0.2).contains(&c.half_spread), "PK_PM_HALF_SPREAD must be within 0.005..=0.2");
        anyhow::ensure!(c.daily_loss_usd > 0.0 && c.max_open_cost_usd > 0.0, "PK_PM risk caps must be positive");
        anyhow::ensure!(c.max_orders_per_s >= 1.0, "PK_PM_MAX_ORDERS_PER_S must be at least 1");
        anyhow::ensure!(
            ![settings.port, settings.observer_port, settings.status_port, settings.lim_port, settings.okx_port]
                .contains(&pm.port),
            "PK_PM_PORT {} clashes with another service's port",
            pm.port
        );
        Ok(pm)
    }

    /// The values that define the strategy, hashed into the journal's `config`.
    pub fn config_json(&self) -> serde_json::Value {
        let c = &self.cfg;
        serde_json::json!({
            "assets": self.assets,
            "half_spread": c.half_spread,
            "shares": c.shares,
            "max_net": c.max_net,
            "skew_k": c.skew_k,
            "pause_jump": c.pause_jump,
            "pause_ms": c.pause_ms,
            "max_kalshi_spread": c.max_kalshi_spread,
            "min_px": c.min_px,
            "max_px": c.max_px,
            "entry_early_s": c.entry_early_s,
            "stop_s": c.stop_s,
            "requote_cents": c.requote_cents,
            "min_requote_ms": c.min_requote_ms,
            "adverse_ms": c.adverse_ms,
            "daily_loss_usd": c.daily_loss_usd,
            "max_open_cost_usd": c.max_open_cost_usd,
            "pair_margin": c.pair_margin,
        })
    }
}
