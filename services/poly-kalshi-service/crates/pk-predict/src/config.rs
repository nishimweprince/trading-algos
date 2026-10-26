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
    pub predict_api_url: String,
    pub predict_ws_url: String,
    pub predict_api_key: Option<String>,
    pub privy_private_key: Option<String>,
    pub account_address: Option<String>,
    pub bnb_rpc_url: Option<String>,
    pub kalshi_api_key_id: String,
    pub kalshi_private_key_path: PathBuf,
    pub kalshi_rest_url: String,
    pub kalshi_ws_url: String,
    pub assets: Vec<String>,
    pub freshness_ms: i64,
    pub basis_buffer: f64,
    pub quote_ttl_s: u64,
    pub half_spread: f64,
    pub requote_cents: f64,
    pub min_requote_ms: u64,
    pub refresh_ms: u64,
    pub min_taker_edge: f64,
    pub taker_slippage: f64,
    pub close_buffer_s: i64,
    pub auto_redeem: bool,
    pub auto_merge: bool,
    pub chain_id: u64,
    pub risk: RiskLimits,
    pub state_dir: PathBuf,
    pub kill_file: PathBuf,
    pub fingerprint: String,
}

impl Config {
    pub fn load(profile: Option<&str>) -> anyhow::Result<Self> {
        let file = pk_svc::config::resolve_env_file(profile)
            .ok_or_else(|| anyhow::anyhow!("profile env file was not found"))?;
        check_mode_0600(&file)?;
        let mut vars: HashMap<String, String> =
            dotenvy::from_path_iter(&file)?.filter_map(Result::ok).collect();
        for (key, value) in std::env::vars() {
            vars.insert(key, value);
        }
        Self::from_map(vars, file.parent().unwrap_or(Path::new(".")))
    }

    pub fn from_map(v: HashMap<String, String>, base: &Path) -> anyhow::Result<Self> {
        let get = |key: &str| v.get(key).map(|s| s.trim().to_owned()).filter(|s| !s.is_empty());
        let flag = |key: &str, default: bool| -> anyhow::Result<bool> {
            match get(key).as_deref() {
                None => Ok(default),
                Some("1" | "true" | "TRUE" | "yes" | "YES" | "on" | "ON") => Ok(true),
                Some("0" | "false" | "FALSE" | "no" | "NO" | "off" | "OFF") => Ok(false),
                Some(other) => anyhow::bail!("{key} must be yes/no, got {other:?}"),
            }
        };
        let number = |key: &str, default: f64| -> anyhow::Result<f64> {
            get(key).map_or(Ok(default), |x| x.parse().map_err(|_| anyhow::anyhow!("{key} is not numeric")))
        };
        let integer = |key: &str, default: u64| -> anyhow::Result<u64> {
            get(key)
                .map_or(Ok(default), |x| x.parse().map_err(|_| anyhow::anyhow!("{key} is not an integer")))
        };
        let required = |key: &str| get(key).ok_or_else(|| anyhow::anyhow!("{key} is required"));

        let mode = VenueMode::parse(get("PK_PRED_MODE").as_deref().unwrap_or("shadow"))?;
        let maker = flag("PK_PRED_MAKER_ENABLED", true)?;
        let taker = flag("PK_PRED_TAKER_ENABLED", false)?;
        let chain_id = integer("PREDICT_CHAIN_ID", 56)?;
        anyhow::ensure!(
            chain_id == 56 || chain_id == 97,
            "PREDICT_CHAIN_ID must be 56 (BNB mainnet) or 97 (testnet)"
        );
        if mode == VenueMode::Live {
            anyhow::ensure!(chain_id == 56, "live mode requires BNB mainnet (chain 56)");
        }
        if mode == VenueMode::Live {
            anyhow::ensure!(
                get("PK_PRED_LIVE_CONFIRM").as_deref() == Some("yes"),
                "live refused: PK_PRED_LIVE_CONFIRM=yes is required"
            );
            anyhow::ensure!(
                !maker || get("PK_PRED_MAKER_LIVE_CONFIRM").as_deref() == Some("yes"),
                "live maker refused: PK_PRED_MAKER_LIVE_CONFIRM=yes is required"
            );
            anyhow::ensure!(
                !taker || get("PK_PRED_TAKER_LIVE_CONFIRM").as_deref() == Some("yes"),
                "live taker refused: PK_PRED_TAKER_LIVE_CONFIRM=yes is required"
            );
            for key in [
                "PREDICT_API_KEY",
                "PREDICT_PRIVY_PRIVATE_KEY",
                "PREDICT_ACCOUNT_ADDRESS",
                "PREDICT_BNB_RPC_URL",
            ] {
                anyhow::ensure!(get(key).is_some(), "live refused: {key} is required");
            }
        }
        let explicit = |key: &str| -> anyhow::Result<f64> {
            let value = get(key).ok_or_else(|| anyhow::anyhow!("{key} must be explicitly configured"))?;
            value.parse().map_err(|_| anyhow::anyhow!("{key} is not numeric"))
        };
        let risk = if mode == VenueMode::Live {
            RiskLimits {
                order_usd: explicit("PK_PRED_MAX_ORDER_USD")?,
                max_open_usd: explicit("PK_PRED_MAX_OPEN_USD")?,
                daily_loss_usd: explicit("PK_PRED_DAILY_LOSS_USD")?,
                max_markets: required("PK_PRED_MAX_ACTIVE_MARKETS")?.parse()?,
                max_orders: required("PK_PRED_MAX_OPEN_ORDERS")?.parse()?,
                max_rejections: integer("PK_PRED_MAX_REJECTIONS", 5)?,
            }
        } else {
            RiskLimits {
                order_usd: number("PK_PRED_MAX_ORDER_USD", 10.0)?,
                max_open_usd: number("PK_PRED_MAX_OPEN_USD", 50.0)?,
                daily_loss_usd: number("PK_PRED_DAILY_LOSS_USD", 25.0)?,
                max_markets: integer("PK_PRED_MAX_ACTIVE_MARKETS", 4)? as usize,
                max_orders: integer("PK_PRED_MAX_OPEN_ORDERS", 16)? as usize,
                max_rejections: integer("PK_PRED_MAX_REJECTIONS", 5)?,
            }
        }
        .validate()?;
        let min_taker_edge = if mode == VenueMode::Live && taker {
            explicit("PK_PRED_MIN_TAKER_EDGE")?
        } else {
            number("PK_PRED_MIN_TAKER_EDGE", 0.08)?
        };
        let taker_slippage = if mode == VenueMode::Live && taker {
            explicit("PK_PRED_TAKER_SLIPPAGE")?
        } else {
            number("PK_PRED_TAKER_SLIPPAGE", 0.01)?
        };
        let state_dir = path(base, get("PK_PRED_STATE_DIR").as_deref().unwrap_or("data/state-predict"));
        let kill_file = path(base, get("PK_PRED_KILL_FILE").as_deref().unwrap_or("data/KILL.predict"));
        let assets: Vec<String> = get("PK_PRED_ASSETS")
            .unwrap_or_else(|| "BTC,ETH".into())
            .split(',')
            .map(|s| s.trim().to_ascii_uppercase())
            .filter(|s| !s.is_empty())
            .collect();
        anyhow::ensure!(
            assets.iter().all(|a| a == "BTC" || a == "ETH"),
            "Predict v1 supports only BTC and ETH"
        );
        let api_key = required("API_KEY")?;
        anyhow::ensure!(api_key.len() >= 16, "API_KEY must be at least 16 characters");
        let public = format!(
            "{mode:?}|{maker}|{taker}|{assets:?}|{}|{}|{}|{}|{}",
            number("PK_PRED_BASIS_BUFFER", 0.02)?,
            number("PK_PRED_HALF_SPREAD", 0.03)?,
            min_taker_edge,
            risk.max_open_usd,
            risk.daily_loss_usd
        );
        let fingerprint = format!("{:x}", Sha256::digest(public.as_bytes()));
        Ok(Self {
            enabled: flag("PK_PRED_ENABLED", false)?,
            mode,
            maker,
            taker,
            port: integer("PK_PRED_PORT", 8056)? as u16,
            api_key,
            predict_api_url: get("PREDICT_API_URL").unwrap_or_else(|| "https://api.predict.fun".into()),
            predict_ws_url: get("PREDICT_WS_URL").unwrap_or_else(|| "wss://ws.predict.fun/ws".into()),
            predict_api_key: get("PREDICT_API_KEY"),
            privy_private_key: get("PREDICT_PRIVY_PRIVATE_KEY"),
            account_address: get("PREDICT_ACCOUNT_ADDRESS"),
            bnb_rpc_url: get("PREDICT_BNB_RPC_URL"),
            kalshi_api_key_id: required("KALSHI_API_KEY_ID")?,
            kalshi_private_key_path: path(base, &required("KALSHI_PRIVATE_KEY_PATH")?),
            kalshi_rest_url: get("KALSHI_REST_URL")
                .unwrap_or_else(|| "https://api.elections.kalshi.com/trade-api/v2".into()),
            kalshi_ws_url: get("KALSHI_WS_URL")
                .unwrap_or_else(|| "wss://api.elections.kalshi.com/trade-api/ws/v2".into()),
            assets,
            freshness_ms: integer("PK_PRED_FRESHNESS_MS", 2_000)? as i64,
            basis_buffer: number("PK_PRED_BASIS_BUFFER", 0.02)?,
            quote_ttl_s: integer("PK_PRED_QUOTE_TTL_S", 20)?,
            half_spread: number("PK_PRED_HALF_SPREAD", 0.03)?,
            requote_cents: number("PK_PRED_REQUOTE_CENTS", 0.01)?,
            min_requote_ms: integer("PK_PRED_MIN_REQUOTE_MS", 500)?,
            refresh_ms: integer("PK_PRED_REFRESH_MS", 5_000)?,
            min_taker_edge,
            taker_slippage,
            close_buffer_s: integer("PK_PRED_CLOSE_BUFFER_S", 60)? as i64,
            auto_redeem: flag("PK_PRED_AUTO_REDEEM", false)?,
            auto_merge: flag("PK_PRED_AUTO_MERGE", false)?,
            chain_id,
            risk,
            state_dir,
            kill_file,
            fingerprint,
        })
    }
}

fn path(base: &Path, value: &str) -> PathBuf {
    let p = PathBuf::from(value);
    if p.is_absolute() { p } else { base.join(p) }
}

fn check_mode_0600(path: &Path) -> anyhow::Result<()> {
    let mode = std::fs::metadata(path)?.permissions().mode() & 0o777;
    anyhow::ensure!(mode == 0o600, "{} must have mode 0600 (currently {mode:04o})", path.display());
    Ok(())
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
    fn shadow_defaults_safe_and_fingerprint_excludes_secrets() {
        let mut first = base();
        first.insert("PREDICT_PRIVY_PRIVATE_KEY".into(), "secret-one".into());
        let mut second = base();
        second.insert("PREDICT_PRIVY_PRIVATE_KEY".into(), "secret-two".into());
        let first = Config::from_map(first, Path::new("/tmp/predict-config")).unwrap();
        let second = Config::from_map(second, Path::new("/tmp/predict-config")).unwrap();
        assert_eq!(first.mode, VenueMode::Shadow);
        assert!(first.maker);
        assert!(!first.taker);
        assert_eq!(first.fingerprint, second.fingerprint);
    }

    #[test]
    fn live_has_no_implicit_capital_or_confirmation() {
        let mut values = base();
        values.insert("PK_PRED_MODE".into(), "live".into());
        assert!(Config::from_map(values, Path::new("/tmp/predict-config")).is_err());
    }

    #[test]
    fn live_mode_requires_mainnet_while_shadow_allows_testnet() {
        let mut shadow = base();
        shadow.insert("PREDICT_CHAIN_ID".into(), "97".into());
        assert_eq!(Config::from_map(shadow, Path::new("/tmp/predict-config")).unwrap().chain_id, 97);
        let mut bad = base();
        bad.insert("PREDICT_CHAIN_ID".into(), "1".into());
        assert!(Config::from_map(bad, Path::new("/tmp/predict-config")).is_err());
        let mut live = base();
        for (key, value) in [
            ("PK_PRED_MODE", "live"),
            ("PK_PRED_LIVE_CONFIRM", "yes"),
            ("PK_PRED_MAKER_LIVE_CONFIRM", "yes"),
            ("PK_PRED_ORDER_USD", "5"),
            ("PK_PRED_MAX_OPEN_USD", "20"),
            ("PK_PRED_DAILY_LOSS_USD", "10"),
            ("PK_PRED_MAX_ACTIVE_MARKETS", "2"),
            ("PK_PRED_MAX_OPEN_ORDERS", "4"),
            ("PK_PRED_MAKER_MIN_EDGE", "0.05"),
            ("PREDICT_API_KEY", "key"),
            ("PREDICT_PRIVY_PRIVATE_KEY", "0x01"),
            ("PREDICT_ACCOUNT_ADDRESS", "0x02"),
            ("PREDICT_BNB_RPC_URL", "https://rpc"),
            ("PREDICT_CHAIN_ID", "97"),
        ] {
            live.insert(key.into(), value.into());
        }
        assert!(Config::from_map(live, Path::new("/tmp/predict-config")).is_err());
    }
}
