use std::collections::HashMap;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use pk_venue_core::{RiskLimits, VenueMode};
use sha2::{Digest, Sha256};

#[derive(Clone)]
pub struct Config {
    pub enabled: bool,
    pub mode: VenueMode,
    pub maker: bool,
    pub taker: bool,
    pub port: u16,
    pub api_key: String,
    pub api_url: String,
    pub ws_url: String,
    pub account: Option<String>,
    pub agent_key: Option<String>,
    pub allowlist: PathBuf,
    pub kalshi_api_key_id: String,
    pub kalshi_private_key_path: PathBuf,
    pub kalshi_rest_url: String,
    pub kalshi_ws_url: String,
    pub freshness_ms: i64,
    pub half_spread: f64,
    pub min_taker_edge: f64,
    pub slippage: f64,
    pub alo_batch_ms: u64,
    pub ioc_batch_ms: u64,
    pub action_expiry_ms: u64,
    pub risk: RiskLimits,
    pub state_dir: PathBuf,
    pub kill_file: PathBuf,
    pub fingerprint: String,
}

impl Config {
    pub fn load(profile: Option<&str>) -> anyhow::Result<Self> {
        let file = pk_svc::config::resolve_env_file(profile)
            .ok_or_else(|| anyhow::anyhow!("profile env file was not found"))?;
        let mode = std::fs::metadata(&file)?.permissions().mode() & 0o777;
        anyhow::ensure!(mode == 0o600, "{} must have mode 0600 (currently {mode:04o})", file.display());
        let mut vars: HashMap<String, String> =
            dotenvy::from_path_iter(&file)?.filter_map(Result::ok).collect();
        vars.extend(std::env::vars());
        Self::from_map(vars, file.parent().unwrap_or(Path::new(".")))
    }

    pub fn from_map(v: HashMap<String, String>, base: &Path) -> anyhow::Result<Self> {
        let get = |k: &str| v.get(k).map(|x| x.trim().to_owned()).filter(|x| !x.is_empty());
        let required = |k: &str| get(k).ok_or_else(|| anyhow::anyhow!("{k} is required"));
        let flag = |k: &str, d: bool| -> anyhow::Result<bool> {
            match get(k).as_deref() {
                None => Ok(d),
                Some("yes" | "YES" | "true" | "1" | "on") => Ok(true),
                Some("no" | "NO" | "false" | "0" | "off") => Ok(false),
                Some(x) => anyhow::bail!("{k} must be yes/no, got {x:?}"),
            }
        };
        let num = |k: &str, d: f64| -> anyhow::Result<f64> {
            get(k).map_or(Ok(d), |x| x.parse().map_err(|_| anyhow::anyhow!("{k} is not numeric")))
        };
        let int = |k: &str, d: u64| -> anyhow::Result<u64> {
            get(k).map_or(Ok(d), |x| x.parse().map_err(|_| anyhow::anyhow!("{k} is not an integer")))
        };
        let explicit = |k: &str| -> anyhow::Result<f64> {
            required(k)?.parse().map_err(|_| anyhow::anyhow!("{k} is not numeric"))
        };
        let mode = VenueMode::parse(get("PK_HIP4_MODE").as_deref().unwrap_or("shadow"))?;
        let maker = flag("PK_HIP4_MAKER_ENABLED", true)?;
        let taker = flag("PK_HIP4_TAKER_ENABLED", false)?;
        if mode == VenueMode::Live {
            anyhow::ensure!(
                get("PK_HIP4_LIVE_CONFIRM").as_deref() == Some("yes"),
                "live refused: PK_HIP4_LIVE_CONFIRM=yes is required"
            );
            anyhow::ensure!(
                !maker || get("PK_HIP4_MAKER_LIVE_CONFIRM").as_deref() == Some("yes"),
                "live maker refused: PK_HIP4_MAKER_LIVE_CONFIRM=yes is required"
            );
            anyhow::ensure!(
                !taker || get("PK_HIP4_TAKER_LIVE_CONFIRM").as_deref() == Some("yes"),
                "live taker refused: PK_HIP4_TAKER_LIVE_CONFIRM=yes is required"
            );
            anyhow::ensure!(
                get("HL_ACCOUNT_ADDRESS").is_some() && get("HL_AGENT_PRIVATE_KEY").is_some(),
                "live requires HL_ACCOUNT_ADDRESS and HL_AGENT_PRIVATE_KEY"
            );
        }
        let risk = if mode == VenueMode::Live {
            RiskLimits {
                order_usd: explicit("PK_HIP4_MAX_ORDER_USD")?,
                max_open_usd: explicit("PK_HIP4_MAX_OPEN_USD")?,
                daily_loss_usd: explicit("PK_HIP4_DAILY_LOSS_USD")?,
                max_markets: required("PK_HIP4_MAX_ACTIVE_MARKETS")?.parse()?,
                max_orders: required("PK_HIP4_MAX_OPEN_ORDERS")?.parse()?,
                max_rejections: int("PK_HIP4_MAX_REJECTIONS", 5)?,
            }
        } else {
            RiskLimits {
                order_usd: num("PK_HIP4_MAX_ORDER_USD", 10.0)?,
                max_open_usd: num("PK_HIP4_MAX_OPEN_USD", 50.0)?,
                daily_loss_usd: num("PK_HIP4_DAILY_LOSS_USD", 25.0)?,
                max_markets: int("PK_HIP4_MAX_ACTIVE_MARKETS", 4)? as usize,
                max_orders: int("PK_HIP4_MAX_OPEN_ORDERS", 16)? as usize,
                max_rejections: int("PK_HIP4_MAX_REJECTIONS", 5)?,
            }
        }
        .validate()?;
        let min_taker_edge = if mode == VenueMode::Live && taker {
            explicit("PK_HIP4_MIN_TAKER_EDGE")?
        } else {
            num("PK_HIP4_MIN_TAKER_EDGE", 0.08)?
        };
        let slippage = if mode == VenueMode::Live && taker {
            explicit("PK_HIP4_TAKER_SLIPPAGE")?
        } else {
            num("PK_HIP4_TAKER_SLIPPAGE", 0.01)?
        };
        let api_key = required("API_KEY")?;
        anyhow::ensure!(api_key.len() >= 16, "API_KEY must be at least 16 characters");
        let p = |key: &str, default: &str| {
            let x = PathBuf::from(get(key).unwrap_or_else(|| default.into()));
            if x.is_absolute() { x } else { base.join(x) }
        };
        let public = format!(
            "{mode:?}|{maker}|{taker}|{}|{min_taker_edge}|{slippage}|{}",
            num("PK_HIP4_HALF_SPREAD", 0.03)?,
            risk.max_open_usd
        );
        Ok(Self {
            enabled: flag("PK_HIP4_ENABLED", false)?,
            mode,
            maker,
            taker,
            port: int("PK_HIP4_PORT", 8057)? as u16,
            api_key,
            api_url: get("HL_API_URL").unwrap_or_else(|| "https://api.hyperliquid.xyz".into()),
            ws_url: get("HL_WS_URL").unwrap_or_else(|| "wss://api.hyperliquid.xyz/ws".into()),
            account: get("HL_ACCOUNT_ADDRESS"),
            agent_key: get("HL_AGENT_PRIVATE_KEY"),
            allowlist: p("PK_HIP4_MATCH_ALLOWLIST", "config/hip4-allowlist.json"),
            kalshi_api_key_id: required("KALSHI_API_KEY_ID")?,
            kalshi_private_key_path: p("KALSHI_PRIVATE_KEY_PATH", &required("KALSHI_PRIVATE_KEY_PATH")?),
            kalshi_rest_url: get("KALSHI_REST_URL")
                .unwrap_or_else(|| "https://api.elections.kalshi.com/trade-api/v2".into()),
            kalshi_ws_url: get("KALSHI_WS_URL")
                .unwrap_or_else(|| "wss://api.elections.kalshi.com/trade-api/ws/v2".into()),
            freshness_ms: int("PK_HIP4_FRESHNESS_MS", 2000)? as i64,
            half_spread: num("PK_HIP4_HALF_SPREAD", 0.03)?,
            min_taker_edge,
            slippage,
            alo_batch_ms: int("PK_HIP4_ALO_BATCH_MS", 100)?,
            ioc_batch_ms: int("PK_HIP4_IOC_BATCH_MS", 10)?,
            action_expiry_ms: int("PK_HIP4_ACTION_EXPIRY_MS", 5000)?,
            risk,
            state_dir: p("PK_HIP4_STATE_DIR", "data/state-hip4"),
            kill_file: p("PK_HIP4_KILL_FILE", "data/KILL.hip4"),
            fingerprint: format!("{:x}", Sha256::digest(public.as_bytes())),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn base() -> HashMap<String, String> {
        HashMap::from([
            ("API_KEY".into(), "0123456789abcdef".into()),
            ("KALSHI_API_KEY_ID".into(), "read-only".into()),
            ("KALSHI_PRIVATE_KEY_PATH".into(), "kalshi.pem".into()),
        ])
    }

    #[test]
    fn shadow_defaults_safe_and_fingerprint_excludes_agent_key() {
        let mut first = base();
        first.insert("HL_AGENT_PRIVATE_KEY".into(), "secret-one".into());
        let mut second = base();
        second.insert("HL_AGENT_PRIVATE_KEY".into(), "secret-two".into());
        let first = Config::from_map(first, Path::new("/tmp/hip4-config")).unwrap();
        let second = Config::from_map(second, Path::new("/tmp/hip4-config")).unwrap();
        assert_eq!(first.mode, VenueMode::Shadow);
        assert!(first.maker);
        assert!(!first.taker);
        assert_eq!(first.fingerprint, second.fingerprint);
    }

    #[test]
    fn live_has_no_implicit_capital_or_confirmation() {
        let mut values = base();
        values.insert("PK_HIP4_MODE".into(), "live".into());
        assert!(Config::from_map(values, Path::new("/tmp/hip4-config")).is_err());
    }
}
