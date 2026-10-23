//! A window's identity on both venues.
//!
//! - Kalshi: the trader's own lookup ([`pk_feeds::Discovery::kalshi_market`]).
//! - OKX: `GET /api/v5/public/instruments?instType=EVENTS&seriesId=<ASSET>-UPDOWN-15MIN`
//!   lists the coming windows about an hour ahead. This window's instrument
//!   expires at `open + 900`, is `live`, is an up/down (`PRICE_UP_DOWN`) on
//!   `<ASSET>-USDT`, and has its strike (`stk`, the index at the open) set;
//!   `stk` is 0 until the window opens.
//! - Outcome: OKX has no public settlement route for event contracts. After
//!   expiry the instrument's candles end with one row stamped at `expTime`,
//!   no volume, whose price is the settlement: 1 (Up won) or 0 (Down won).
//!   Anything else there is not a settlement yet.

use std::time::Duration;

use pk_core::WINDOW_S;
use serde::Serialize;
use serde_json::Value;

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct OkxWindow {
    pub asset: String,
    pub open_unix: i64,
    pub kalshi_ticker: String,
    pub inst_id: String,
    /// The index at the open, which the settlement average is compared with.
    pub strike: f64,
    pub exp_ms: i64,
    /// The index the contract settles on (`BTC-USDT`).
    pub underlying: String,
    pub tick: f64,
    pub min_size: f64,
}

pub fn series_id(asset: &str, series: &str) -> String {
    format!("{}-{series}", asset.to_uppercase())
}

fn num(v: &Value, k: &str) -> Option<f64> {
    v.get(k)?.as_str()?.parse().ok()
}

/// The instrument id OKX gives a window: `<ASSET>-<series>-YYMMDD-HHMM-HHMM` on UTC+8.
pub fn inst_id_for(asset: &str, series: &str, open_unix: i64) -> String {
    let tz = chrono::FixedOffset::east_opt(8 * 3600).expect("UTC+8");
    let o = chrono::DateTime::from_timestamp(open_unix, 0).unwrap_or_default().with_timezone(&tz);
    let c = chrono::DateTime::from_timestamp(open_unix + WINDOW_S, 0).unwrap_or_default().with_timezone(&tz);
    format!("{}-{}-{}", series_id(asset, series), o.format("%y%m%d-%H%M"), c.format("%H%M"))
}

/// A window known only by its asset and open (a position from before a restart):
/// enough to read its outcome, not to trade it.
pub fn bare_window(asset: &str, series: &str, open_unix: i64) -> OkxWindow {
    OkxWindow {
        asset: asset.to_owned(),
        open_unix,
        kalshi_ticker: String::new(),
        inst_id: inst_id_for(asset, series, open_unix),
        strike: 0.0,
        exp_ms: (open_unix + WINDOW_S) * 1000,
        underlying: format!("{}-USDT", asset.to_uppercase()),
        tick: 0.001,
        min_size: 0.01,
    }
}

/// This window's instrument from a `/public/instruments` list, or why there is none.
pub fn pick_instrument(asset: &str, open_unix: i64, kalshi_ticker: &str, list: &[Value]) -> Result<Option<OkxWindow>, String> {
    let exp = (open_unix + WINDOW_S) * 1000;
    let Some(m) = list.iter().find(|i| i.get("expTime").and_then(Value::as_str).and_then(|s| s.parse::<i64>().ok()) == Some(exp))
    else {
        return Ok(None);
    };
    let s = |k: &str| m.get(k).and_then(Value::as_str).unwrap_or_default();
    if s("state") != "live" {
        return Ok(None); // preopen: not this window yet
    }
    if s("method") != "PRICE_UP_DOWN" {
        return Err(format!("method {:?}", s("method")));
    }
    let underlying = format!("{}-USDT", asset.to_uppercase());
    if s("uly") != underlying {
        return Err(format!("uly {:?}", s("uly")));
    }
    let strike = num(m, "stk").unwrap_or(0.0);
    if strike <= 0.0 {
        return Ok(None); // the strike is set at the open
    }
    Ok(Some(OkxWindow {
        asset: asset.to_owned(),
        open_unix,
        kalshi_ticker: kalshi_ticker.to_owned(),
        inst_id: s("instId").to_owned(),
        strike,
        exp_ms: exp,
        underlying,
        tick: num(m, "tickSz").unwrap_or(0.001),
        min_size: num(m, "minSz").unwrap_or(0.01),
    }))
}

/// `"yes"` (Up) / `"no"` (Down) from the instrument's candles, once settled.
pub fn parse_outcome(candles: &[Value], exp_ms: i64) -> Option<&'static str> {
    let row = candles.iter().find(|r| r.get(0).and_then(Value::as_str).and_then(|t| t.parse::<i64>().ok()) == Some(exp_ms))?;
    let close: f64 = row.get(4)?.as_str()?.parse().ok()?;
    if close == 1.0 {
        Some("yes")
    } else if close == 0.0 {
        Some("no")
    } else {
        None
    }
}

pub struct OkxDiscovery {
    http: reqwest::Client,
    api: String,
    series: String,
    pub kalshi: pk_feeds::Discovery,
}

impl OkxDiscovery {
    pub fn new(api_url: &str, series: &str, gamma_url: &str, kalshi_rest_url: &str) -> Self {
        let http = reqwest::Client::builder()
            .user_agent(pk_feeds::discovery::USER_AGENT)
            .timeout(Duration::from_secs(10))
            .build()
            .expect("http client");
        Self {
            http,
            api: api_url.trim_end_matches('/').to_owned(),
            series: series.to_owned(),
            kalshi: pk_feeds::Discovery::new(gamma_url, kalshi_rest_url),
        }
    }

    async fn data(&self, path: &str) -> anyhow::Result<Vec<Value>> {
        let body: Value = self.http.get(format!("{}{path}", self.api)).send().await?.error_for_status()?.json().await?;
        let code = body.get("code").and_then(Value::as_str).unwrap_or("?");
        anyhow::ensure!(code == "0", "{path}: code {code} {}", body.get("msg").and_then(Value::as_str).unwrap_or_default());
        Ok(body.get("data").and_then(Value::as_array).cloned().unwrap_or_default())
    }

    pub async fn instruments(&self, asset: &str) -> anyhow::Result<Vec<Value>> {
        self.data(&format!("/api/v5/public/instruments?instType=EVENTS&seriesId={}", series_id(asset, &self.series))).await
    }

    /// The window's identity, or None while a venue has not listed (or opened) it.
    pub async fn discover(&self, asset: &str, open_unix: i64) -> anyhow::Result<Option<OkxWindow>> {
        let Some(kalshi) = self.kalshi.kalshi_market(asset, open_unix).await? else { return Ok(None) };
        let ticker = kalshi.get("ticker").and_then(Value::as_str).unwrap_or_default().to_owned();
        let list = self.instruments(asset).await?;
        pick_instrument(asset, open_unix, &ticker, &list).map_err(|why| anyhow::anyhow!("{asset} {open_unix}: {why}"))
    }

    pub async fn outcome(&self, w: &OkxWindow) -> anyhow::Result<Option<&'static str>> {
        let rows = self.data(&format!("/api/v5/market/history-candles?instId={}&bar=1m&limit=3", w.inst_id)).await?;
        Ok(parse_outcome(&rows, w.exp_ms))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    // 2026-10-23 14:30 UTC = 22:30 UTC+8, the instId's clock.
    const OPEN: i64 = 1_792_765_800;

    fn inst(exp_ms: i64, state: &str, stk: &str) -> Value {
        json!({"instId": "BTC-UPDOWN-15MIN-261023-2230-2245", "instType": "EVENTS", "expTime": exp_ms.to_string(),
               "state": state, "stk": stk, "method": "PRICE_UP_DOWN", "uly": "BTC-USDT", "tickSz": "0.001",
               "minSz": "0.01", "seriesId": "BTC-UPDOWN-15MIN"})
    }

    #[test]
    fn instrument_for_the_window() {
        let exp = (OPEN + 900) * 1000;
        let list = vec![inst(exp + 900_000, "live", "0"), inst(exp, "live", "81462.2")];
        let w = pick_instrument("BTC", OPEN, "KXBTC15M-X", &list).unwrap().unwrap();
        assert_eq!((w.inst_id.as_str(), w.strike, w.exp_ms), ("BTC-UPDOWN-15MIN-261023-2230-2245", 81462.2, exp));
        assert_eq!(chrono::DateTime::from_timestamp(OPEN, 0).unwrap().to_rfc3339(), "2026-10-23T14:30:00+00:00");
        // Not open yet: no strike, or still preopen.
        assert_eq!(pick_instrument("BTC", OPEN, "K", &[inst(exp, "live", "0")]).unwrap(), None);
        assert_eq!(pick_instrument("BTC", OPEN, "K", &[inst(exp, "preopen", "1")]).unwrap(), None);
        // Another kind of contract is refused.
        let mut other = inst(exp, "live", "1");
        other["method"] = "PRICE_ABOVE".into();
        assert!(pick_instrument("BTC", OPEN, "K", &[other]).is_err());
        assert!(pick_instrument("ETH", OPEN, "K", &[inst(exp, "live", "1")]).is_err());
        assert_eq!(series_id("btc", "UPDOWN-15MIN"), "BTC-UPDOWN-15MIN");
        assert_eq!(inst_id_for("BTC", "UPDOWN-15MIN", OPEN), "BTC-UPDOWN-15MIN-261023-2230-2245");
        // The last window of a UTC+8 day closes at 00:00 of the next.
        assert_eq!(inst_id_for("SOL", "UPDOWN-15MIN", OPEN + 5 * 900), "SOL-UPDOWN-15MIN-261023-2345-0000");
    }

    #[test]
    fn outcome_from_the_settlement_candle() {
        let exp = 1_792_766_700_000_i64;
        let up = vec![
            json!(["1792766700000", "1", "1", "1", "1", "0", "0", "0", "0"]),
            json!(["1792766640000", "1", "1", "1", "1", "102.6", "102.6", "102.4974", "1"]),
        ];
        assert_eq!(parse_outcome(&up, exp), Some("yes"));
        let down = vec![json!(["1792766700000", "0", "0", "0", "0", "0", "0", "0", "0"])];
        assert_eq!(parse_outcome(&down, exp), Some("no"));
        // Still trading, or no settlement row: unknown.
        assert_eq!(parse_outcome(&[json!(["1792766700000", "0.97", "1", "0.9", "0.97", "3", "3", "3", "0"])], exp), None);
        assert_eq!(parse_outcome(&up[1..], exp), None);
    }
}
