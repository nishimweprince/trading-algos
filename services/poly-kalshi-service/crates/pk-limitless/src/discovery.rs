//! A window's identity on both venues.
//!
//! - Kalshi: the trader's own lookup ([`pk_feeds::Discovery::kalshi_market`]).
//! - Limitless: `GET /markets/<asset>-up-or-down-15-min-<open>`. The market
//!   must open at `open`, close at `open + 900`, be a CLOB market and settle on
//!   the Chainlink 60 s TWAP; anything else is not this window.

use std::time::Duration;

use pk_core::WINDOW_S;
use serde::Serialize;
use serde_json::Value;

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct LimWindow {
    pub asset: String,
    pub open_unix: i64,
    pub kalshi_ticker: String,
    pub slug: String,
    pub yes_token: String,
    pub no_token: String,
    pub condition_id: String,
    pub exchange: String,
    pub fee_module: Option<String>,
    pub taker_delay_ms: f64,
    pub fee: bool,
}

pub fn lim_slug(asset: &str, open_unix: i64) -> String {
    format!("{}-up-or-down-15-min-{open_unix}", asset.to_lowercase())
}

fn iso_ms(s: &str) -> Option<i64> {
    chrono::DateTime::parse_from_rfc3339(s).ok().map(|t| t.timestamp_millis())
}

/// The window from a `GET /markets/:slug` body, or why it is not one.
pub fn parse_market(asset: &str, open_unix: i64, kalshi_ticker: &str, m: &Value) -> Result<LimWindow, String> {
    let s = |p: &str| m.pointer(p).and_then(Value::as_str).map(str::to_owned);
    let start = s("/startAt").and_then(|t| iso_ms(&t));
    if start != Some(open_unix * 1000) {
        return Err(format!("startAt {:?}", m.get("startAt")));
    }
    let close = m.get("expirationTimestamp").and_then(Value::as_i64);
    if close != Some((open_unix + WINDOW_S) * 1000) {
        return Err(format!("expirationTimestamp {close:?}"));
    }
    if m.get("tradeType").and_then(Value::as_str) != Some("clob") {
        return Err(format!("tradeType {:?}", m.get("tradeType")));
    }
    let stream = m.pointer("/metadata/chainlinkDataStream");
    let twap = stream.and_then(|c| c.get("streamType")).and_then(Value::as_str) == Some("twap")
        && stream.and_then(|c| c.get("twapWindowSeconds")).and_then(Value::as_i64) == Some(60);
    if !twap {
        return Err("not settled on the Chainlink 60 s TWAP".into());
    }
    let (Some(yes), Some(no), Some(exchange)) = (s("/tokens/yes"), s("/tokens/no"), s("/venue/exchange")) else {
        return Err("tokens or venue missing".into());
    };
    let delay = m
        .pointer("/settings/takerDelayMs")
        .or_else(|| m.pointer("/metadata/takerDelayMs"))
        .and_then(Value::as_f64)
        .unwrap_or(0.0);
    Ok(LimWindow {
        asset: asset.to_owned(),
        open_unix,
        kalshi_ticker: kalshi_ticker.to_owned(),
        slug: s("/slug").unwrap_or_else(|| lim_slug(asset, open_unix)),
        yes_token: yes,
        no_token: no,
        condition_id: s("/conditionId").unwrap_or_default(),
        exchange,
        fee_module: s("/venue/feeModule"),
        taker_delay_ms: delay,
        fee: m.pointer("/metadata/fee").and_then(Value::as_bool).unwrap_or(true),
    })
}

/// `"yes"` / `"no"` once the market resolved (index 0 is Up).
pub fn parse_outcome(m: &Value) -> Option<&'static str> {
    if m.get("status").and_then(Value::as_str) != Some("RESOLVED") {
        return None;
    }
    match m.get("winningOutcomeIndex").and_then(Value::as_i64) {
        Some(0) => Some("yes"),
        Some(1) => Some("no"),
        _ => None,
    }
}

pub struct LimDiscovery {
    http: reqwest::Client,
    api: String,
    pub kalshi: pk_feeds::Discovery,
}

impl LimDiscovery {
    pub fn new(api_url: &str, gamma_url: &str, kalshi_rest_url: &str) -> Self {
        let http = reqwest::Client::builder()
            .user_agent(pk_feeds::discovery::USER_AGENT)
            .timeout(Duration::from_secs(10))
            .build()
            .expect("http client");
        Self {
            http,
            api: api_url.trim_end_matches('/').to_owned(),
            kalshi: pk_feeds::Discovery::new(gamma_url, kalshi_rest_url),
        }
    }

    pub async fn market(&self, slug: &str) -> anyhow::Result<Option<Value>> {
        let resp = self.http.get(format!("{}/markets/{slug}", self.api)).send().await?;
        if resp.status().as_u16() == 404 {
            return Ok(None);
        }
        Ok(Some(resp.error_for_status()?.json().await?))
    }

    /// The window's identity, or None while a venue has not listed it.
    pub async fn discover(&self, asset: &str, open_unix: i64) -> anyhow::Result<Option<LimWindow>> {
        let Some(kalshi) = self.kalshi.kalshi_market(asset, open_unix).await? else { return Ok(None) };
        let ticker = kalshi.get("ticker").and_then(Value::as_str).unwrap_or_default().to_owned();
        let Some(m) = self.market(&lim_slug(asset, open_unix)).await? else { return Ok(None) };
        parse_market(asset, open_unix, &ticker, &m).map(Some).map_err(|why| anyhow::anyhow!("{asset} {open_unix}: {why}"))
    }

    pub async fn outcome(&self, slug: &str) -> anyhow::Result<Option<&'static str>> {
        Ok(self.market(slug).await?.as_ref().and_then(parse_outcome))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn market() -> Value {
        json!({
            "slug": "btc-up-or-down-15-min-1792748700",
            "startAt": "2026-10-23T09:45:00.000Z",
            "expirationTimestamp": 1792749600000_i64,
            "tradeType": "clob",
            "conditionId": "0x85",
            "tokens": {"yes": "391", "no": "352"},
            "venue": {"exchange": "0x05c7", "feeModule": "0x7B6f", "feeModuleType": "COLLATERAL"},
            "metadata": {"fee": true, "takerDelayMs": 500,
                         "chainlinkDataStream": {"streamType": "twap", "twapWindowSeconds": 60}},
            "settings": {"takerDelayMs": 500},
            "status": "FUNDED",
        })
    }

    #[test]
    fn window_from_market() {
        let w = parse_market("BTC", 1792748700, "KXBTC15M-X", &market()).unwrap();
        assert_eq!((w.yes_token.as_str(), w.no_token.as_str(), w.taker_delay_ms), ("391", "352", 500.0));
        assert_eq!(w.fee_module.as_deref(), Some("0x7B6f"));
        assert!(parse_market("BTC", 1792748700 + 900, "K", &market()).is_err());
        let mut spot = market();
        spot["metadata"]["chainlinkDataStream"]["streamType"] = "spot".into();
        assert!(parse_market("BTC", 1792748700, "K", &spot).is_err());
        assert_eq!(lim_slug("ETH", 5), "eth-up-or-down-15-min-5");
    }

    #[test]
    fn outcomes() {
        let mut m = market();
        assert_eq!(parse_outcome(&m), None);
        m["status"] = "RESOLVED".into();
        m["winningOutcomeIndex"] = 1.into();
        assert_eq!(parse_outcome(&m), Some("no"));
        m["winningOutcomeIndex"] = 0.into();
        assert_eq!(parse_outcome(&m), Some("yes"));
    }
}
