//! Settings, from an env file (`--env FILE`) with the process environment
//! winning. Every strategy knob is `PK_EG_*`; wallet and endpoint names are
//! the ones the other services already use.

use std::collections::HashMap;
use std::path::PathBuf;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    /// Decide, fill on paper against the live book after the latency, settle.
    Paper,
    /// Real FAK orders. Needs `PK_EG_LIVE_CONFIRM=yes`.
    Live,
}

#[derive(Clone, Debug)]
pub struct Config {
    pub mode: Mode,
    pub assets: Vec<String>,
    // --- the rule ---
    /// First second (before the close) an entry may fire.
    pub entry_from_s: i64,
    /// Last second (before the close) an entry may fire.
    pub entry_until_s: i64,
    /// Model edge after the fee: P(side) − ask − fee(ask) must reach this.
    pub min_edge: f64,
    /// Above this model edge the disagreement is taken for model error: no entry.
    pub max_edge: f64,
    /// The limit leaves at least this much edge at P(side) after the fee.
    pub min_net_edge: f64,
    /// The limit is the ask plus this, capped by `min_net_edge`.
    pub limit_offset: f64,
    pub min_px: f64,
    pub max_px: f64,
    /// Seconds of 1 s Chainlink returns behind σ.
    pub sigma_window_s: i64,
    /// Fewest returns before σ counts.
    pub sigma_min_n: usize,
    /// A floor on the model's sd (log units): our TWAP against Polymarket's.
    pub sd_floor: f64,
    /// The model's sd is multiplied by this while more than `sd_mult_from_s` remain.
    pub sd_mult_early: f64,
    pub sd_mult_from_s: i64,
    /// Fewest Chainlink seconds in the opening average for the window to count.
    pub min_open_ticks: usize,
    /// The newest Chainlink tick may be at most this old (wall clock).
    pub max_tick_age_ms: i64,
    /// Entries per window and asset (each side at most once).
    pub max_entries_per_window: usize,
    pub shares: f64,
    /// The rule set's name; results are read per epoch (PREREG.md).
    pub epoch: String,
    // --- Kalshi (read-only market data) ---
    /// Kalshi's mid for the same side must clear the ask and fee by this.
    pub kalshi_filter: bool,
    pub kalshi_min_margin: f64,
    pub kalshi_max_age_ms: i64,
    pub kalshi_max_spread: f64,
    pub kalshi_key_id: Option<String>,
    pub kalshi_key_path: Option<PathBuf>,
    pub kalshi_ws_url: String,
    // --- spot (recorded only; no rule reads it) ---
    pub spot: bool,
    pub coinbase_ws_url: String,
    pub binance_ws_url: String,
    pub kalshi_rest_url: String,
    // --- paper ---
    /// Decision → the book the paper order fills against.
    pub paper_latency_ms: i64,
    // --- risk ---
    pub max_open_cost_usd: f64,
    pub daily_loss_usd: f64,
    // --- recording ---
    /// Per-second grid lines from this many seconds before the close.
    pub grid_from_s: i64,
    // --- paths and endpoints ---
    pub state_dir: PathBuf,
    pub kill_file: PathBuf,
    // --- the monitoring page ---
    /// Where the page listens (`PK_EG_UI_ADDR`, `off` for none).
    pub ui_addr: Option<std::net::SocketAddr>,
    /// HTTP Basic password for the page; required off loopback.
    pub ui_token: Option<String>,
    /// A name for this host on the page.
    pub host_tag: String,
    pub gamma_url: String,
    pub poly_ws_url: String,
    pub rtds_url: String,
    pub clob_url: String,
    // --- wallet (live only) ---
    pub private_key: Option<String>,
    pub funder: Option<String>,
    pub signature_type: u8,
    pub relayer_url: String,
    pub relayer_api_key: Option<String>,
    pub relayer_api_key_address: Option<String>,
    pub polygon_rpc_url: Option<String>,
    pub redeem_collateral: Option<String>,
}

pub const SUPPORTED: [&str; 8] = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "HYPE", "ZEC"];

struct Env(HashMap<String, String>);

impl Env {
    fn get(&self, k: &str) -> Option<String> {
        self.0.get(k).map(|v| v.trim().to_owned()).filter(|v| !v.is_empty())
    }
    fn or(&self, k: &str, d: &str) -> String {
        self.get(k).unwrap_or_else(|| d.to_owned())
    }
    fn num(&self, k: &str, d: f64) -> anyhow::Result<f64> {
        match self.get(k) {
            None => Ok(d),
            Some(v) => v.parse().map_err(|_| anyhow::anyhow!("{k}={v} is not a number")),
        }
    }
    fn int(&self, k: &str, d: i64) -> anyhow::Result<i64> {
        match self.get(k) {
            None => Ok(d),
            Some(v) => v.parse().map_err(|_| anyhow::anyhow!("{k}={v} is not an integer")),
        }
    }
}

fn expand(p: &str) -> PathBuf {
    match (p.strip_prefix("~/"), std::env::var("HOME")) {
        (Some(rest), Ok(home)) => PathBuf::from(home).join(rest),
        _ => PathBuf::from(p),
    }
}

impl Config {
    /// The env file (if any), then the process environment on top.
    pub fn load(env_file: Option<&std::path::Path>) -> anyhow::Result<Self> {
        let mut vars = HashMap::new();
        if let Some(p) = env_file {
            for item in dotenvy::from_path_iter(p)? {
                let (k, v) = item?;
                vars.insert(k, v);
            }
        }
        vars.extend(std::env::vars());
        Self::from_vars(vars)
    }

    pub fn from_vars(vars: HashMap<String, String>) -> anyhow::Result<Self> {
        let e = Env(vars);
        let mode = match e.or("PK_EG_MODE", "paper").to_lowercase().as_str() {
            "paper" => Mode::Paper,
            "live" => Mode::Live,
            other => anyhow::bail!("PK_EG_MODE={other} is not paper or live"),
        };
        let assets: Vec<String> = e
            .or("PK_EG_ASSETS", "BTC,ETH,SOL,XRP,BNB,DOGE,HYPE")
            .split(',')
            .map(|a| a.trim().to_uppercase())
            .filter(|a| !a.is_empty())
            .collect();
        anyhow::ensure!(!assets.is_empty(), "PK_EG_ASSETS is empty");
        let bad: Vec<&String> = assets.iter().filter(|a| !SUPPORTED.contains(&a.as_str())).collect();
        anyhow::ensure!(bad.is_empty(), "PK_EG_ASSETS has unsupported assets: {bad:?}");
        let state_dir = expand(&e.or("PK_EG_STATE_DIR", "data/state/endgame"));
        let kill_file = e.get("PK_EG_KILL_FILE").map(|p| expand(&p)).unwrap_or_else(|| state_dir.join("KILL"));
        let c = Config {
            mode,
            assets,
            entry_from_s: e.int("PK_EG_ENTRY_FROM_S", 60)?,
            entry_until_s: e.int("PK_EG_ENTRY_UNTIL_S", 2)?,
            min_edge: e.num("PK_EG_MIN_EDGE", 0.04)?,
            max_edge: e.num("PK_EG_MAX_EDGE", 0.12)?,
            min_net_edge: e.num("PK_EG_MIN_NET_EDGE", 0.02)?,
            limit_offset: e.num("PK_EG_LIMIT_OFFSET", 0.01)?,
            min_px: e.num("PK_EG_MIN_PX", 0.05)?,
            max_px: e.num("PK_EG_MAX_PX", 0.70)?,
            sigma_window_s: e.int("PK_EG_SIGMA_WINDOW_S", 300)?,
            sigma_min_n: e.int("PK_EG_SIGMA_MIN_N", 120)? as usize,
            sd_floor: e.num("PK_EG_SD_FLOOR", 0.00005)?,
            sd_mult_early: e.num("PK_EG_SD_MULT_EARLY", 1.5)?,
            sd_mult_from_s: e.int("PK_EG_SD_MULT_FROM_S", 30)?,
            min_open_ticks: e.int("PK_EG_MIN_OPEN_TICKS", 45)? as usize,
            max_tick_age_ms: e.int("PK_EG_MAX_TICK_AGE_MS", 3_000)?,
            max_entries_per_window: e.int("PK_EG_MAX_ENTRIES_PER_WINDOW", 1)? as usize,
            shares: e.num("PK_EG_SHARES", 10.0)?,
            epoch: e.or("PK_EG_EPOCH", "2"),
            kalshi_filter: match e.or("PK_EG_KALSHI_FILTER", "yes").to_lowercase().as_str() {
                "yes" | "true" | "1" | "on" => true,
                "no" | "false" | "0" | "off" => false,
                v => anyhow::bail!("PK_EG_KALSHI_FILTER={v} is not yes or no"),
            },
            kalshi_min_margin: e.num("PK_EG_KALSHI_MIN_MARGIN", 0.0)?,
            kalshi_max_age_ms: e.int("PK_EG_KALSHI_MAX_AGE_MS", 3_000)?,
            kalshi_max_spread: e.num("PK_EG_KALSHI_MAX_SPREAD", 0.05)?,
            kalshi_key_id: e.get("KALSHI_API_KEY_ID"),
            kalshi_key_path: e.get("KALSHI_PRIVATE_KEY_PATH").map(|p| expand(&p)),
            kalshi_ws_url: e.or("KALSHI_WS_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2"),
            spot: !matches!(e.or("PK_EG_SPOT", "yes").to_lowercase().as_str(), "no" | "false" | "0" | "off"),
            coinbase_ws_url: e.or("COINBASE_WS_URL", "wss://ws-feed.exchange.coinbase.com"),
            binance_ws_url: e.or("BINANCE_WS_URL", "wss://stream.binance.com:9443/ws"),
            kalshi_rest_url: e.or("KALSHI_REST_URL", "https://api.elections.kalshi.com/trade-api/v2"),
            paper_latency_ms: e.int("PK_EG_PAPER_LATENCY_MS", 250)?,
            max_open_cost_usd: e.num("PK_EG_MAX_OPEN_COST_USD", 50.0)?,
            daily_loss_usd: e.num("PK_EG_DAILY_LOSS_USD", 15.0)?,
            grid_from_s: e.int("PK_EG_GRID_FROM_S", 90)?,
            state_dir,
            kill_file,
            ui_addr: match e.or("PK_EG_UI_ADDR", "127.0.0.1:8095").as_str() {
                "off" | "none" => None,
                a => Some(a.parse().map_err(|_| anyhow::anyhow!("PK_EG_UI_ADDR={a} is not host:port"))?),
            },
            ui_token: e.get("PK_EG_UI_TOKEN"),
            host_tag: e.or("PK_EG_HOST_TAG", ""),
            gamma_url: e.or("POLY_GAMMA_URL", "https://gamma-api.polymarket.com"),
            poly_ws_url: e.or("POLY_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"),
            rtds_url: e.or("POLY_RTDS_URL", "wss://ws-live-data.polymarket.com"),
            clob_url: e.or("POLY_CLOB_URL", "https://clob.polymarket.com"),
            private_key: e.get("POLY_PRIVATE_KEY").or_else(|| {
                e.get("POLY_PRIVATE_KEY_FILE")
                    .and_then(|p| std::fs::read_to_string(expand(&p)).ok())
                    .map(|s| s.trim().to_owned())
            }),
            funder: e.get("POLY_FUNDER"),
            signature_type: e.int("POLY_SIGNATURE_TYPE", 3)? as u8,
            relayer_url: e.or("POLY_RELAYER_URL", "https://relayer-v2.polymarket.com"),
            relayer_api_key: e.get("POLY_RELAYER_API_KEY"),
            relayer_api_key_address: e.get("POLY_RELAYER_API_KEY_ADDRESS"),
            polygon_rpc_url: e.get("POLYGON_RPC_URL"),
            redeem_collateral: e.get("POLY_REDEEM_COLLATERAL"),
        };
        anyhow::ensure!(
            c.entry_from_s > c.entry_until_s && c.entry_until_s >= 0 && c.entry_from_s <= 600,
            "PK_EG_ENTRY_FROM_S must be above PK_EG_ENTRY_UNTIL_S (≥ 0), at most 600"
        );
        anyhow::ensure!(c.min_edge > 0.0 && c.min_net_edge >= 0.0, "edges must be positive");
        anyhow::ensure!(c.max_edge > c.min_edge, "PK_EG_MAX_EDGE must be above PK_EG_MIN_EDGE");
        anyhow::ensure!(c.sd_mult_early > 0.0 && c.sd_mult_from_s >= 0, "PK_EG_SD_MULT_* must be positive");
        anyhow::ensure!(
            !c.kalshi_filter || (c.kalshi_key_id.is_some() && c.kalshi_key_path.is_some()),
            "PK_EG_KALSHI_FILTER=yes needs KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH (or set it to no)"
        );
        anyhow::ensure!(0.0 < c.min_px && c.min_px < c.max_px && c.max_px < 1.0, "need 0 < MIN_PX < MAX_PX < 1");
        anyhow::ensure!(c.shares >= 5.0, "PK_EG_SHARES must be at least 5 (the venue minimum)");
        anyhow::ensure!(c.max_open_cost_usd > 0.0 && c.daily_loss_usd > 0.0, "risk limits must be positive");
        anyhow::ensure!(c.sigma_min_n >= 30, "PK_EG_SIGMA_MIN_N must be at least 30");
        if let Some(t) = &c.ui_token {
            anyhow::ensure!(t.len() >= 16, "PK_EG_UI_TOKEN must be at least 16 characters");
        }
        if let Some(a) = c.ui_addr {
            anyhow::ensure!(a.ip().is_loopback() || c.ui_token.is_some(), "PK_EG_UI_ADDR={a} is not loopback: set PK_EG_UI_TOKEN");
        }
        if c.mode == Mode::Live {
            anyhow::ensure!(
                e.get("PK_EG_LIVE_CONFIRM").as_deref() == Some("yes"),
                "PK_EG_MODE=live needs PK_EG_LIVE_CONFIRM=yes"
            );
            anyhow::ensure!(c.private_key.is_some(), "live needs POLY_PRIVATE_KEY (or POLY_PRIVATE_KEY_FILE)");
            anyhow::ensure!(c.signature_type <= 3, "POLY_SIGNATURE_TYPE must be 0..=3");
            if c.signature_type == 3 {
                anyhow::ensure!(c.funder.is_some(), "a deposit wallet (type 3) needs POLY_FUNDER");
                anyhow::ensure!(c.relayer_api_key.is_some(), "a deposit wallet needs POLY_RELAYER_API_KEY to redeem");
            } else {
                anyhow::ensure!(c.polygon_rpc_url.is_some(), "an EOA needs POLYGON_RPC_URL to redeem");
            }
        }
        Ok(c)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn vars(kv: &[(&str, &str)]) -> HashMap<String, String> {
        kv.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect()
    }

    #[test]
    fn paper_by_default() {
        assert!(Config::from_vars(vars(&[])).is_err()); // the Kalshi filter needs a key
        let c = Config::from_vars(vars(&[("PK_EG_KALSHI_FILTER", "no")])).unwrap();
        assert_eq!(c.mode, Mode::Paper);
        assert_eq!((c.max_px, c.max_edge, c.min_open_ticks, c.sd_mult_early), (0.70, 0.12, 45, 1.5));
        assert_eq!(c.assets.len(), 7);
        assert_eq!(c.entry_from_s, 60);
    }

    #[test]
    fn live_needs_the_confirm_and_a_key() {
        assert!(Config::from_vars(vars(&[("PK_EG_MODE", "live"), ("PK_EG_KALSHI_FILTER", "no")])).is_err());
        let e = Config::from_vars(vars(&[("PK_EG_MODE", "live"), ("PK_EG_LIVE_CONFIRM", "yes"), ("PK_EG_KALSHI_FILTER", "no")])).unwrap_err();
        assert!(e.to_string().contains("POLY_PRIVATE_KEY"));
        let ok = Config::from_vars(vars(&[
            ("PK_EG_MODE", "live"),
            ("PK_EG_LIVE_CONFIRM", "yes"),
            ("POLY_PRIVATE_KEY", "0xabc"),
            ("POLY_FUNDER", "0xdef"),
            ("POLY_RELAYER_API_KEY", "k"),
            ("KALSHI_API_KEY_ID", "kid"),
            ("KALSHI_PRIVATE_KEY_PATH", "/k.pem"),
        ]));
        assert!(ok.is_ok());
    }

    #[test]
    fn rejects_bad_windows() {
        let off = ("PK_EG_KALSHI_FILTER", "no");
        assert!(Config::from_vars(vars(&[off, ("PK_EG_ENTRY_FROM_S", "2"), ("PK_EG_ENTRY_UNTIL_S", "5")])).is_err());
        assert!(Config::from_vars(vars(&[off, ("PK_EG_ASSETS", "BTC,FOO")])).is_err());
        assert!(Config::from_vars(vars(&[off, ("PK_EG_MAX_EDGE", "0.03")])).is_err());
    }
}
