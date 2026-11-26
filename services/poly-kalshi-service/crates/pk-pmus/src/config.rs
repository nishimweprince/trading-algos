//! Settings, from an env file (`--env FILE`) with the process environment
//! winning. Strategy knobs are `PK_PU_*`; Kalshi's are the names the other
//! services use. Paper only: there is no order path for Polymarket US here.

use std::collections::HashMap;
use std::path::PathBuf;

#[derive(Clone, Debug)]
pub struct Config {
    pub assets: Vec<String>,
    // --- the rule ---
    /// Kalshi's side mid − Polymarket US ask − fee must reach this…
    pub min_edge: f64,
    /// …and not pass this (a larger gap is taken for a stale book).
    pub max_edge: f64,
    /// The limit leaves at least this much edge at Kalshi's price after the fee.
    pub min_net_edge: f64,
    pub limit_offset: f64,
    pub min_px: f64,
    pub max_px: f64,
    /// No entries in a window's first `entry_after_open_s` or last `stop_before_close_s`.
    pub entry_after_open_s: i64,
    pub stop_before_close_s: i64,
    pub kalshi_max_spread: f64,
    pub kalshi_max_age_ms: i64,
    /// The Polymarket US quote must be at most this old.
    pub pm_max_age_ms: i64,
    pub max_entries_per_window: usize,
    pub shares: f64,
    /// Polymarket US taker fee `theta · C · p(1 − p)` when a market has no
    /// `feeCoefficient` (each market's own coefficient wins).
    pub fee_theta: f64,
    pub epoch: String,
    // --- paper ---
    pub paper_latency_ms: i64,
    /// Delay study: the book is also fetched this long after each decision and
    /// journaled as `pu_fill_probe` (never a position). Empty turns it off.
    pub paper_delays_ms: Vec<i64>,
    pub poll_ms: u64,
    // --- risk ---
    pub max_open_cost_usd: f64,
    pub daily_loss_usd: f64,
    /// Paper: a daily-loss breach is journaled but does not stop entries
    /// (a halt costs the sample, not money). `PK_PU_ENFORCE_HALT=1` stops them.
    pub enforce_halt: bool,
    // --- counterfactual sizing (journaled, never traded) ---
    /// Gaps from here up to `min_edge` are logged as shadow entries (no fill, top-of-book cost).
    pub shadow_min_edge: f64,
    /// Bankroll the sizing rules are computed against (fixed, not compounded).
    pub bankroll_usd: f64,
    /// Shrunk Kelly: q_adj = c + s·(q − c). Must be in (0, 1].
    pub kelly_shrink: f64,
    /// Every sized rule (not flat): cost per trade ≤ this × bankroll…
    pub trade_risk_frac: f64,
    /// …and cost per window across all coins ≤ this × bankroll.
    pub window_risk_frac: f64,
    // --- paths, page, endpoints ---
    pub state_dir: PathBuf,
    pub kill_file: PathBuf,
    pub ui_addr: Option<std::net::SocketAddr>,
    pub ui_token: Option<String>,
    pub host_tag: String,
    pub gateway_url: String,
    pub kalshi_key_id: Option<String>,
    pub kalshi_key_path: Option<PathBuf>,
    pub kalshi_ws_url: String,
    pub kalshi_rest_url: String,
}

/// Polymarket US 15-minute Up/Down slugs: `<prefix>-updown-15m-YYYY-MM-DD-HHMMz`.
pub const SUPPORTED: [(&str, &str); 4] =
    [("BTC", "cpc-btc"), ("ETH", "cpceth-eth"), ("SOL", "cpcsol-sol"), ("XRP", "cpcxrp-xrp")];

pub fn slug_prefix(asset: &str) -> Option<&'static str> {
    SUPPORTED.iter().find(|(a, _)| a.eq_ignore_ascii_case(asset)).map(|(_, p)| *p)
}

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
        anyhow::ensure!(
            e.or("PK_PU_MODE", "paper").eq_ignore_ascii_case("paper"),
            "pk-pmus is paper only: PK_PU_MODE must be paper"
        );
        let assets: Vec<String> = e
            .or("PK_PU_ASSETS", "BTC,ETH,SOL,XRP")
            .split(',')
            .map(|a| a.trim().to_uppercase())
            .filter(|a| !a.is_empty())
            .collect();
        anyhow::ensure!(!assets.is_empty(), "PK_PU_ASSETS is empty");
        let bad: Vec<&String> = assets.iter().filter(|a| slug_prefix(a).is_none()).collect();
        anyhow::ensure!(
            bad.is_empty(),
            "PK_PU_ASSETS has assets without a Polymarket US 15m market: {bad:?}"
        );
        let state_dir = expand(&e.or("PK_PU_STATE_DIR", "data/state/pmus"));
        let kill_file =
            e.get("PK_PU_KILL_FILE").map(|p| expand(&p)).unwrap_or_else(|| state_dir.join("KILL"));
        let c = Config {
            assets,
            min_edge: e.num("PK_PU_MIN_EDGE", 0.05)?,
            max_edge: e.num("PK_PU_MAX_EDGE", 0.15)?,
            min_net_edge: e.num("PK_PU_MIN_NET_EDGE", 0.01)?,
            limit_offset: e.num("PK_PU_LIMIT_OFFSET", 0.01)?,
            min_px: e.num("PK_PU_MIN_PX", 0.05)?,
            max_px: e.num("PK_PU_MAX_PX", 0.95)?,
            entry_after_open_s: e.int("PK_PU_ENTRY_AFTER_OPEN_S", 60)?,
            stop_before_close_s: e.int("PK_PU_STOP_BEFORE_CLOSE_S", 60)?,
            kalshi_max_spread: e.num("PK_PU_KALSHI_MAX_SPREAD", 0.03)?,
            kalshi_max_age_ms: e.int("PK_PU_KALSHI_MAX_AGE_MS", 3_000)?,
            pm_max_age_ms: e.int("PK_PU_PM_MAX_AGE_MS", 1_500)?,
            max_entries_per_window: e.int("PK_PU_MAX_ENTRIES_PER_WINDOW", 1)? as usize,
            shares: e.num("PK_PU_SHARES", 10.0)?,
            fee_theta: e.num("PK_PU_FEE_THETA", 0.11)?,
            epoch: e.or("PK_PU_EPOCH", "4"),
            paper_latency_ms: e.int("PK_PU_PAPER_LATENCY_MS", 150)?,
            paper_delays_ms: {
                let raw = e.or("PK_PU_PAPER_DELAYS_MS", "25,50,100,150");
                let mut v = Vec::new();
                for x in raw.split(',').map(str::trim).filter(|x| !x.is_empty() && *x != "off") {
                    v.push(
                        x.parse::<i64>()
                            .map_err(|_| anyhow::anyhow!("PK_PU_PAPER_DELAYS_MS={raw}: {x} is not ms"))?,
                    );
                }
                v.sort_unstable();
                v.dedup();
                v
            },
            poll_ms: e.int("PK_PU_POLL_MS", 250)? as u64,
            max_open_cost_usd: e.num("PK_PU_MAX_OPEN_COST_USD", 50.0)?,
            daily_loss_usd: e.num("PK_PU_DAILY_LOSS_USD", 15.0)?,
            enforce_halt: matches!(e.or("PK_PU_ENFORCE_HALT", "0").as_str(), "1" | "true" | "yes"),
            shadow_min_edge: e.num("PK_PU_SHADOW_MIN_EDGE", 0.03)?,
            bankroll_usd: e.num("PK_PU_BANKROLL_USD", 1_000.0)?,
            kelly_shrink: e.num("PK_PU_KELLY_SHRINK", 0.5)?,
            trade_risk_frac: e.num("PK_PU_TRADE_RISK_FRAC", 0.02)?,
            window_risk_frac: e.num("PK_PU_WINDOW_RISK_FRAC", 0.05)?,
            state_dir,
            kill_file,
            ui_addr: match e.or("PK_PU_UI_ADDR", "127.0.0.1:8095").as_str() {
                "off" | "none" => None,
                a => Some(a.parse().map_err(|_| anyhow::anyhow!("PK_PU_UI_ADDR={a} is not host:port"))?),
            },
            ui_token: e.get("PK_PU_UI_TOKEN"),
            host_tag: e.or("PK_PU_HOST_TAG", ""),
            gateway_url: e
                .or("PM_US_GATEWAY_URL", "https://gateway.polymarket.us")
                .trim_end_matches('/')
                .to_owned(),
            kalshi_key_id: e.get("KALSHI_API_KEY_ID"),
            kalshi_key_path: e.get("KALSHI_PRIVATE_KEY_PATH").map(|p| expand(&p)),
            kalshi_ws_url: e.or("KALSHI_WS_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2"),
            kalshi_rest_url: e.or("KALSHI_REST_URL", "https://api.elections.kalshi.com/trade-api/v2"),
        };
        anyhow::ensure!(0.0 < c.min_edge && c.min_edge < c.max_edge, "need 0 < MIN_EDGE < MAX_EDGE");
        anyhow::ensure!(
            0.0 < c.min_px && c.min_px < c.max_px && c.max_px < 1.0,
            "need 0 < MIN_PX < MAX_PX < 1"
        );
        anyhow::ensure!(c.shares > 0.0, "PK_PU_SHARES must be positive");
        anyhow::ensure!(
            c.paper_delays_ms.iter().all(|d| (0..=5_000).contains(d)) && c.paper_delays_ms.len() <= 8,
            "PK_PU_PAPER_DELAYS_MS: at most 8 delays, each 0–5000 ms"
        );
        anyhow::ensure!(
            0.0 < c.shadow_min_edge && c.shadow_min_edge <= c.min_edge,
            "need 0 < SHADOW_MIN_EDGE <= MIN_EDGE"
        );
        anyhow::ensure!(
            0.0 < c.kelly_shrink && c.kelly_shrink <= 1.0,
            "PK_PU_KELLY_SHRINK must be in (0, 1]: never size above the quoted edge"
        );
        anyhow::ensure!(
            c.bankroll_usd > 0.0 && c.trade_risk_frac > 0.0 && c.window_risk_frac >= c.trade_risk_frac,
            "need BANKROLL > 0 and 0 < TRADE_RISK_FRAC <= WINDOW_RISK_FRAC"
        );
        anyhow::ensure!(
            c.poll_ms >= 100,
            "PK_PU_POLL_MS must be at least 100 (the gateway allows 25 requests/s)"
        );
        anyhow::ensure!(
            c.kalshi_key_id.is_some() && c.kalshi_key_path.is_some(),
            "Kalshi is the fair value: set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH"
        );
        if let Some(t) = &c.ui_token {
            anyhow::ensure!(t.len() >= 16, "PK_PU_UI_TOKEN must be at least 16 characters");
        }
        if let Some(a) = c.ui_addr {
            anyhow::ensure!(
                a.ip().is_loopback() || c.ui_token.is_some(),
                "PK_PU_UI_ADDR={a} is not loopback: set PK_PU_UI_TOKEN"
            );
        }
        Ok(c)
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;

    pub fn vars(kv: &[(&str, &str)]) -> HashMap<String, String> {
        let mut v: HashMap<String, String> =
            [("KALSHI_API_KEY_ID", "kid"), ("KALSHI_PRIVATE_KEY_PATH", "/k.pem")]
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect();
        v.extend(kv.iter().map(|(k, v)| (k.to_string(), v.to_string())));
        v
    }

    #[test]
    fn defaults_and_guards() {
        let c = Config::from_vars(vars(&[])).unwrap();
        assert_eq!(c.assets, ["BTC", "ETH", "SOL", "XRP"]);
        assert_eq!((c.min_edge, c.max_edge, c.fee_theta), (0.05, 0.15, 0.11));
        assert!(!c.enforce_halt);
        assert_eq!(c.paper_delays_ms, [25, 50, 100, 150]);
        assert!(
            Config::from_vars(vars(&[("PK_PU_PAPER_DELAYS_MS", "off")])).unwrap().paper_delays_ms.is_empty()
        );
        assert!(Config::from_vars(vars(&[("PK_PU_PAPER_DELAYS_MS", "25,x")])).is_err());
        assert!(Config::from_vars(vars(&[("PK_PU_KELLY_SHRINK", "1.5")])).is_err());
        assert!(Config::from_vars(vars(&[("PK_PU_MODE", "live")])).is_err());
        assert!(Config::from_vars(vars(&[("PK_PU_ASSETS", "BTC,DOGE")])).is_err());
        assert!(Config::from_vars(HashMap::new()).is_err()); // no Kalshi key
        assert_eq!(slug_prefix("eth"), Some("cpceth-eth"));
    }
}
