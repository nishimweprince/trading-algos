use std::collections::HashMap;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use pk_venue_core::{RiskLimits, VenueMode};

use crate::spot::FairSource;
use sha2::{Digest, Sha256};

/// Predict's minimum order expiry is 2 minutes from now; keep a margin.
pub const MIN_QUOTE_TTL_S: u64 = 150;

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
    /// Record every raw Kalshi/Predict frame (`PK_PRED_RAW`): ~2 GB an hour.
    pub raw: bool,
    /// The bankroll the P&L view starts from in shadow (`PK_PRED_SIM_CAPITAL`).
    pub sim_capital: f64,
    /// Each order is sized to at least this much (`PK_PRED_MIN_ORDER_USD`;
    /// Predict's floor is $0.90).
    pub min_order_usd: f64,
    /// Least profit per share a completed YES+NO pair must lock (`PK_PRED_PAIR_MARGIN`).
    pub pair_margin: f64,
    /// First legs only in the window's first `open_leg_s` (`PK_PRED_OPEN_LEG_S`) …
    pub open_leg_s: i64,
    /// … and while Kalshi's fair is within this band (`PK_PRED_OPEN_MIN_FAIR` / `MAX_FAIR`).
    pub open_min_fair: f64,
    pub open_max_fair: f64,
    /// First legs only while Predict's mid is within this of Kalshi's fair
    /// (`PK_PRED_OPEN_MAX_GAP`) …
    pub open_max_gap: f64,
    /// … and Kalshi has moved at most `open_max_move` in `open_move_ms`
    /// (`PK_PRED_OPEN_MAX_MOVE`, `PK_PRED_OPEN_MOVE_MS`).
    pub open_max_move: f64,
    pub open_move_ms: i64,
    /// First legs only while the market is flat (`PK_PRED_OPEN_ONLY_FLAT`):
    /// no adding to a one-sided position. The 10-26 19:45Z window lost ~$13
    /// adding YES fills (12 BTC, 16 ETH) as the price fell.
    pub open_only_flat: bool,
    /// A first leg buys only a side quoted at least this (`PK_PRED_OPEN_MIN_PX`):
    /// pkm's longshot first legs lost 9¢/share on 141 windows.
    pub open_min_px: f64,
    /// A resting first-leg quote is requoted at once, past `min_requote_ms`,
    /// once its side's fair is within this of it (`PK_PRED_OPEN_MIN_EDGE`).
    pub open_min_edge: f64,
    /// Binance spot (`PK_PRED_BINANCE_WS_URL`, the combined-stream endpoint):
    /// the guard and the Binance-informed fairs (see `spot`).
    pub binance_ws_url: String,
    /// Pull a coin's first-leg quotes on a Binance move (`PK_PRED_BINANCE_GUARD`) …
    pub binance_guard: bool,
    /// … of `binance_pause_bps` within 1 s (`PK_PRED_BINANCE_PAUSE_BPS`), for
    /// `binance_pause_ms` (`PK_PRED_BINANCE_PAUSE_MS`), …
    pub binance_pause_bps: f64,
    pub binance_pause_ms: i64,
    /// … or while the Binance-adjusted fair is this far from Kalshi's
    /// (`PK_PRED_BINANCE_PAUSE_GAP`).
    pub binance_pause_gap: f64,
    /// The fair first legs and pairing legs are priced from
    /// (`PK_PRED_OPEN_FAIR`, `PK_PRED_PAIR_FAIR`: kalshi, adjusted or binance).
    pub open_fair: FairSource,
    pub pair_fair: FairSource,
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
            .unwrap_or_else(|| "BTC,ETH,BNB".into())
            .split(',')
            .map(|s| s.trim().to_ascii_uppercase())
            .filter(|s| !s.is_empty())
            .collect();
        anyhow::ensure!(
            assets.iter().all(|a| crate::model::ASSETS.contains(&a.as_str())),
            "PK_PRED_ASSETS: each must have a Kalshi 15-minute series ({:?})",
            crate::model::ASSETS
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
            // Predict refuses an expiry under 2 minutes from now; requotes
            // remove orders off-book, so this only bounds a signed order's life.
            quote_ttl_s: integer("PK_PRED_QUOTE_TTL_S", 180)?.max(MIN_QUOTE_TTL_S),
            half_spread: number("PK_PRED_HALF_SPREAD", 0.03)?,
            requote_cents: number("PK_PRED_REQUOTE_CENTS", 0.01)?,
            min_requote_ms: integer("PK_PRED_MIN_REQUOTE_MS", 500)?,
            refresh_ms: integer("PK_PRED_REFRESH_MS", 60_000)?,
            min_taker_edge,
            taker_slippage,
            close_buffer_s: integer("PK_PRED_CLOSE_BUFFER_S", 60)? as i64,
            auto_redeem: flag("PK_PRED_AUTO_REDEEM", false)?,
            auto_merge: flag("PK_PRED_AUTO_MERGE", false)?,
            chain_id,
            risk,
            state_dir,
            kill_file,
            raw: flag("PK_PRED_RAW", false)?,
            sim_capital: number("PK_PRED_SIM_CAPITAL", 100.0)?,
            min_order_usd: number("PK_PRED_MIN_ORDER_USD", 1.0)?.max(0.9),
            pair_margin: number("PK_PRED_PAIR_MARGIN", 0.02)?,
            open_leg_s: integer("PK_PRED_OPEN_LEG_S", 300)? as i64,
            open_min_fair: number("PK_PRED_OPEN_MIN_FAIR", 0.15)?,
            open_max_fair: number("PK_PRED_OPEN_MAX_FAIR", 0.85)?,
            open_max_gap: number("PK_PRED_OPEN_MAX_GAP", 0.04)?,
            open_max_move: number("PK_PRED_OPEN_MAX_MOVE", 0.02)?,
            open_move_ms: integer("PK_PRED_OPEN_MOVE_MS", 5_000)? as i64,
            open_only_flat: flag("PK_PRED_OPEN_ONLY_FLAT", true)?,
            open_min_px: number("PK_PRED_OPEN_MIN_PX", 0.35)?,
            open_min_edge: number("PK_PRED_OPEN_MIN_EDGE", 0.01)?,
            binance_ws_url: get("PK_PRED_BINANCE_WS_URL")
                .unwrap_or_else(|| "wss://stream.binance.com:9443/stream".into()),
            binance_guard: flag("PK_PRED_BINANCE_GUARD", true)?,
            binance_pause_bps: number("PK_PRED_BINANCE_PAUSE_BPS", 5.0)?,
            binance_pause_ms: integer("PK_PRED_BINANCE_PAUSE_MS", 2_000)? as i64,
            binance_pause_gap: number("PK_PRED_BINANCE_PAUSE_GAP", 0.03)?,
            open_fair: FairSource::parse(get("PK_PRED_OPEN_FAIR").as_deref().unwrap_or("kalshi"))?,
            pair_fair: FairSource::parse(get("PK_PRED_PAIR_FAIR").as_deref().unwrap_or("kalshi"))?,
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
