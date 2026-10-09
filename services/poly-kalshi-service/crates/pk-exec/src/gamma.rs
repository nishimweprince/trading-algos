//! Just enough of Gamma to find the probe's token: the current 15-minute
//! window's Up/Down token ids. The engine's full window identity lives in
//! `pk-core`.

use serde::Deserialize;

use crate::transport::USER_AGENT;

pub const WINDOW_S: u64 = 900;

pub fn window_open(unix_s: u64) -> u64 {
    unix_s / WINDOW_S * WINDOW_S
}

pub fn poly_slug(asset: &str, open_unix: u64) -> String {
    format!("{}-updown-15m-{open_unix}", asset.to_lowercase())
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Market {
    clob_token_ids: Option<String>,
    outcomes: Option<String>,
}

#[derive(Debug, Deserialize)]
struct Event {
    #[serde(default)]
    markets: Vec<Market>,
}

/// `(up_token, down_token)` for `asset`'s window opening at `open_unix`, or None
/// if Gamma has not listed it.
pub async fn window_tokens(
    http: &reqwest::Client,
    gamma_url: &str,
    asset: &str,
    open_unix: u64,
) -> anyhow::Result<Option<(String, String)>> {
    let url = format!("{}/events", gamma_url.trim_end_matches('/'));
    let events: Vec<Event> = http
        .get(url)
        .query(&[("slug", poly_slug(asset, open_unix))])
        .header("User-Agent", USER_AGENT)
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    let Some(market) = events.into_iter().next().and_then(|e| e.markets.into_iter().next()) else {
        return Ok(None);
    };
    let outcomes: Vec<String> = serde_json::from_str(market.outcomes.as_deref().unwrap_or("[]"))?;
    let tokens: Vec<String> = serde_json::from_str(market.clob_token_ids.as_deref().unwrap_or("[]"))?;
    let outcomes: Vec<String> = outcomes.iter().map(|o| o.to_lowercase()).collect();
    anyhow::ensure!(
        outcomes == ["up", "down"] && tokens.len() == 2,
        "{asset} {open_unix}: outcomes {outcomes:?}"
    );
    Ok(Some((tokens[0].clone(), tokens[1].clone())))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slug_and_open() {
        assert_eq!(window_open(1_759_800_123), 1_759_799_700);
        assert_eq!(poly_slug("BTC", 1_759_799_700), "btc-updown-15m-1759799700");
    }
}
