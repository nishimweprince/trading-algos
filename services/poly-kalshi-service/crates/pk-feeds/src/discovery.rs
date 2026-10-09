//! Window identity from the venues' REST APIs, and their outcomes.
//!
//! - Polymarket: Gamma `/events?slug=<asset>-updown-15m-<open>`; `clobTokenIds`
//!   are `[Up, Down]`; the fee schedule comes with the market.
//! - Kalshi: series `KX<ASSET>15M`, the market whose `close_time = open + 900`.
//!   One query covers this window and the next, and 429s are retried with
//!   jittered backoff (restarts hit the rate limit three times in epoch 3).
//! - Gamma answers 403 to unknown user agents, so every request sends ours.

use std::collections::HashMap;
use std::time::Duration;

use parking_lot::Mutex;
use pk_core::WINDOW_S;
use pk_core::fees::{DEFAULT_EXPONENT, DEFAULT_RATE};
use pk_core::window::{Window, poly_slug};
use serde_json::Value;

pub const USER_AGENT: &str = "poly-kalshi/0.2 (+trading-algos)";
const RETRIES_429: u32 = 4;
const BACKOFF_429_S: f64 = 0.5;
const KALSHI_CACHE_S: i64 = 6 * 3600;

#[derive(Debug)]
pub struct DiscoveryError(pub String);

impl std::fmt::Display for DiscoveryError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for DiscoveryError {}

fn iso_to_unix(s: &str) -> Option<i64> {
    chrono::DateTime::parse_from_rfc3339(&s.replace(' ', "T")).ok().map(|t| t.timestamp())
}

pub struct Discovery {
    http: reqwest::Client,
    gamma: String,
    kalshi: String,
    kalshi_markets: Mutex<HashMap<(String, i64), Value>>,
    pub retries_429: std::sync::atomic::AtomicU64,
}

impl Discovery {
    pub fn new(gamma_url: &str, kalshi_rest_url: &str) -> Self {
        let http = reqwest::Client::builder()
            .user_agent(USER_AGENT)
            .timeout(Duration::from_secs(10))
            .build()
            .expect("http client");
        Self {
            http,
            gamma: gamma_url.trim_end_matches('/').to_owned(),
            kalshi: kalshi_rest_url.trim_end_matches('/').to_owned(),
            kalshi_markets: Mutex::new(HashMap::new()),
            retries_429: Default::default(),
        }
    }

    async fn get(&self, url: &str, query: &[(&str, String)]) -> anyhow::Result<Value> {
        let mut attempt = 0;
        loop {
            let resp = self.http.get(url).query(query).send().await?;
            if resp.status().as_u16() != 429 || attempt == RETRIES_429 {
                return Ok(resp.error_for_status()?.json().await?);
            }
            self.retries_429.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            let jitter = 1.0 + rand::random::<f64>();
            tokio::time::sleep(Duration::from_secs_f64(BACKOFF_429_S * 2f64.powi(attempt as i32) * jitter))
                .await;
            attempt += 1;
        }
    }

    pub async fn poly_market(&self, asset: &str, open_unix: i64) -> anyhow::Result<Option<Value>> {
        let events =
            self.get(&format!("{}/events", self.gamma), &[("slug", poly_slug(asset, open_unix))]).await?;
        let Some(market) = events.get(0).and_then(|e| e.get("markets")).and_then(|m| m.get(0)).cloned()
        else {
            return Ok(None);
        };
        if let Some(start) = market.get("eventStartTime").and_then(Value::as_str)
            && iso_to_unix(start) != Some(open_unix)
        {
            return Err(
                DiscoveryError(format!("{asset} {open_unix}: Polymarket eventStartTime is {start}")).into()
            );
        }
        Ok(Some(market))
    }

    pub async fn kalshi_market(&self, asset: &str, open_unix: i64) -> anyhow::Result<Option<Value>> {
        let close = open_unix + WINDOW_S;
        let key = (asset.to_owned(), close);
        let cached = self.kalshi_markets.lock().get(&key).cloned();
        let market = match cached {
            Some(m) => Some(m),
            None => {
                let body = self
                    .get(
                        &format!("{}/markets", self.kalshi),
                        &[
                            ("series_ticker", format!("KX{asset}15M")),
                            ("min_close_ts", (close - 1).to_string()),
                            ("max_close_ts", (close + WINDOW_S + 1).to_string()),
                        ],
                    )
                    .await?;
                let mut by_close: HashMap<i64, Vec<Value>> = HashMap::new();
                for m in body.get("markets").and_then(Value::as_array).into_iter().flatten() {
                    let c = match (
                        m.get("close_time").and_then(Value::as_str),
                        m.get("open_time").and_then(Value::as_str),
                    ) {
                        (Some(c), _) => iso_to_unix(c),
                        (None, Some(o)) => iso_to_unix(o).map(|o| o + WINDOW_S),
                        _ => None,
                    };
                    if let Some(c) = c {
                        by_close.entry(c).or_default().push(m.clone());
                    }
                }
                let mut cache = self.kalshi_markets.lock();
                for (c, found) in by_close {
                    if found.len() > 1 {
                        return Err(
                            DiscoveryError(format!("{asset} {c}: {} Kalshi markets", found.len())).into()
                        );
                    }
                    cache.insert((asset.to_owned(), c), found.into_iter().next().expect("one"));
                }
                let cutoff = close - KALSHI_CACHE_S;
                cache.retain(|k, _| k.1 >= cutoff);
                cache.get(&key).cloned()
            }
        };
        let Some(market) = market else { return Ok(None) };
        let open = market.get("open_time").and_then(Value::as_str).and_then(iso_to_unix);
        if open != Some(open_unix) {
            return Err(DiscoveryError(format!(
                "{asset} {open_unix}: Kalshi open_time {:?}",
                market.get("open_time")
            ))
            .into());
        }
        Ok(Some(market))
    }

    /// The window's identity, or None if a venue has not listed it yet.
    pub async fn discover(&self, asset: &str, open_unix: i64) -> anyhow::Result<Option<Window>> {
        let Some(poly) = self.poly_market(asset, open_unix).await? else { return Ok(None) };
        let Some(kalshi) = self.kalshi_market(asset, open_unix).await? else { return Ok(None) };
        let parse_list = |k: &str| -> Vec<String> {
            poly.get(k)
                .and_then(Value::as_str)
                .and_then(|s| serde_json::from_str::<Vec<Value>>(s).ok())
                .unwrap_or_default()
                .into_iter()
                .map(|v| v.as_str().map(str::to_owned).unwrap_or_else(|| v.to_string()))
                .collect()
        };
        let outcomes: Vec<String> = parse_list("outcomes").iter().map(|o| o.to_lowercase()).collect();
        let tokens = parse_list("clobTokenIds");
        if outcomes != ["up", "down"] || tokens.len() != 2 {
            return Err(DiscoveryError(format!("{asset} {open_unix}: outcomes {outcomes:?}")).into());
        }
        let fees_enabled = poly.get("feesEnabled").and_then(Value::as_bool).unwrap_or(true);
        let fees = poly.get("feeSchedule").cloned().unwrap_or(Value::Null);
        let (rate, exponent) = if fees_enabled {
            (
                fees.get("rate").and_then(Value::as_f64).unwrap_or(DEFAULT_RATE),
                fees.get("exponent").and_then(Value::as_f64).unwrap_or(DEFAULT_EXPONENT),
            )
        } else {
            (0.0, fees.get("exponent").and_then(Value::as_f64).unwrap_or(DEFAULT_EXPONENT))
        };
        Ok(Some(Window {
            asset: asset.to_owned(),
            open_unix,
            kalshi_ticker: kalshi.get("ticker").and_then(Value::as_str).unwrap_or_default().to_owned(),
            yes_token: tokens[0].clone(),
            no_token: tokens[1].clone(),
            condition_id: poly.get("conditionId").and_then(Value::as_str).unwrap_or_default().to_owned(),
            fee_rate: rate,
            fee_exponent: exponent,
        }))
    }

    /// `"yes"` / `"no"` once Polymarket has resolved the window.
    pub async fn poly_outcome(&self, w: &Window) -> anyhow::Result<Option<&'static str>> {
        let Some(m) = self.poly_market(&w.asset, w.open_unix).await? else { return Ok(None) };
        if m.get("closed").and_then(Value::as_bool) != Some(true) {
            return Ok(None);
        }
        let prices: Vec<f64> = m
            .get("outcomePrices")
            .and_then(Value::as_str)
            .and_then(|s| serde_json::from_str::<Vec<Value>>(s).ok())
            .unwrap_or_default()
            .iter()
            .filter_map(|v| v.as_str().and_then(|s| s.parse().ok()).or_else(|| v.as_f64()))
            .collect();
        Ok(match prices.as_slice() {
            [a, b] if *a == 1.0 && *b == 0.0 => Some("yes"),
            [a, b] if *a == 0.0 && *b == 1.0 => Some("no"),
            [] => None,
            other => {
                tracing::warn!(prices = ?other, "poly_outcome_unclear");
                None
            }
        })
    }

    /// False while Kalshi is closed (its weekly Thursday 03:00-05:00 ET closure).
    pub async fn kalshi_trading_active(&self) -> anyhow::Result<bool> {
        let body = self.get(&format!("{}/exchange/status", self.kalshi), &[]).await?;
        Ok(body.get("trading_active").and_then(Value::as_bool).unwrap_or(true))
    }

    pub async fn kalshi_outcome(&self, w: &Window) -> anyhow::Result<Option<&'static str>> {
        let body = self.get(&format!("{}/markets/{}", self.kalshi, w.kalshi_ticker), &[]).await?;
        Ok(match body.get("market").and_then(|m| m.get("result")).and_then(Value::as_str) {
            Some("yes") => Some("yes"),
            Some("no") => Some("no"),
            _ => None,
        })
    }
}
