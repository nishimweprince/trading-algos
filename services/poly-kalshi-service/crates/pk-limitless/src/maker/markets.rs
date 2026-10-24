//! The maker's markets: Limitless's recurring crypto up/down mirrors of Polymarket.
//!
//! - Limitless lists them under `GET /markets/active` (paged). The hourly and
//!   daily slugs end in a creation id, so they cannot be built from the clock;
//!   the series is the slug without its trailing number.
//! - Each is a Polymarket mirror (`metadata.isPolyArbitrage`) with the same
//!   rules and oracle; `metadata.externalSlug` is the Polymarket event, whose
//!   Gamma market carries the Up/Down CLOB token ids (`clobTokenIds`, a JSON
//!   string, in `outcomes` order).
//! - Trades come from `GET /markets/<slug>/events`: one row per taker fill,
//!   `side` 0 = the taker bought `tokenId` at `price`, 1 = sold it;
//!   `matchedSize` in 1e-6 shares.

use serde::Serialize;
use serde_json::Value;

/// The 14 crypto mirrors that trade (2026-10-24): BTC/ETH 15-min, and the six
/// coins hourly and daily. The stock and gold dailies had no volume.
pub const DEFAULT_SERIES: [&str; 14] = [
    "btc-up-or-down-15-min",
    "eth-up-or-down-15-min",
    "btc-up-or-down-hourly-p",
    "eth-up-or-down-hourly-p",
    "solana-up-or-down-hourly-p",
    "xrp-up-or-down-hourly-p",
    "bnb-up-or-down-hourly-p",
    "doge-up-or-down-hourly-p",
    "btc-up-or-down-daily-p",
    "eth-up-or-down-daily-p",
    "solana-up-or-down-daily-p",
    "xrp-up-or-down-daily-p",
    "bnb-up-or-down-daily-p",
    "dogecoin-up-or-down-daily-p",
];

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub enum Kind {
    M15,
    H1,
    D1,
}

impl Kind {
    pub fn of(series: &str) -> Self {
        if series.contains("15-min") {
            Kind::M15
        } else if series.contains("hourly") {
            Kind::H1
        } else {
            Kind::D1
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Kind::M15 => "15m",
            Kind::H1 => "1h",
            Kind::D1 => "1d",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct MakerMarket {
    pub slug: String,
    pub series: String,
    pub asset: String,
    pub kind: Kind,
    pub open_ms: i64,
    pub close_ms: i64,
    pub yes_token: String,
    pub no_token: String,
    pub external_slug: String,
    pub poly_up: String,
    pub poly_down: String,
    pub taker_delay_ms: f64,
    /// Reward eligibility, as the market states it (shares, and distance from the mid).
    pub min_size: f64,
    pub max_spread: Option<f64>,
    pub rebate_mult: Option<f64>,
}

/// The slug without its trailing number: `btc-up-or-down-hourly-p-1792865106835` → `btc-up-or-down-hourly-p`.
pub fn series_of(slug: &str) -> &str {
    match slug.rsplit_once('-') {
        Some((head, tail)) if !tail.is_empty() && tail.bytes().all(|b| b.is_ascii_digit()) => head,
        _ => slug,
    }
}

/// `BTC`, `ETH`, `SOL`, `XRP`, `BNB`, `DOGE` from a series.
pub fn asset_of(series: &str) -> String {
    let head = series.split("-up-or-down").next().unwrap_or(series);
    match head {
        "solana" => "SOL".into(),
        "dogecoin" => "DOGE".into(),
        other => other.to_uppercase(),
    }
}

fn iso_ms(s: &str) -> Option<i64> {
    chrono::DateTime::parse_from_rfc3339(s).ok().map(|t| t.timestamp_millis())
}

/// A listed market in one of `series`, without its Polymarket tokens yet.
pub fn parse_active(m: &Value, series: &[String]) -> Option<MakerMarket> {
    let s = |p: &str| m.pointer(p).and_then(Value::as_str).map(str::to_owned);
    let slug = s("/slug")?;
    let ser = series_of(&slug).to_owned();
    if !series.iter().any(|x| *x == ser) || m.get("tradeType").and_then(Value::as_str) != Some("clob") {
        return None;
    }
    let md = m.get("metadata")?;
    if md.get("isPolyArbitrage").and_then(Value::as_bool) != Some(true) {
        return None;
    }
    let external = md.get("externalSlug").and_then(Value::as_str)?.to_owned();
    Some(MakerMarket {
        asset: asset_of(&ser),
        kind: Kind::of(&ser),
        series: ser,
        open_ms: s("/startAt").and_then(|t| iso_ms(&t))?,
        close_ms: m.get("expirationTimestamp").and_then(Value::as_i64)?,
        yes_token: s("/tokens/yes")?,
        no_token: s("/tokens/no")?,
        external_slug: external,
        poly_up: String::new(),
        poly_down: String::new(),
        taker_delay_ms: m
            .pointer("/settings/takerDelayMs")
            .or_else(|| md.get("takerDelayMs"))
            .and_then(Value::as_f64)
            .unwrap_or(0.0),
        min_size: md.get("minSize").and_then(Value::as_f64).unwrap_or(0.0) / 1e6,
        max_spread: md.get("maxSpread").and_then(Value::as_f64),
        rebate_mult: md.get("makerRebateMult").and_then(Value::as_f64),
        slug,
    })
}

/// Polymarket's (Up, Down) CLOB tokens from a Gamma market.
pub fn parse_poly(m: &Value) -> Option<(String, String)> {
    let arr = |k: &str| -> Option<Vec<String>> {
        match m.get(k)? {
            Value::String(s) => serde_json::from_str(s).ok(),
            Value::Array(a) => a.iter().map(|x| x.as_str().map(str::to_owned)).collect(),
            _ => None,
        }
    };
    let ids = arr("clobTokenIds")?;
    let names = arr("outcomes")?;
    let up = names.iter().position(|n| n.eq_ignore_ascii_case("up") || n.eq_ignore_ascii_case("yes"))?;
    let down = names.iter().position(|n| n.eq_ignore_ascii_case("down") || n.eq_ignore_ascii_case("no"))?;
    Some((ids.get(up)?.clone(), ids.get(down)?.clone()))
}

/// One taker fill on Limitless, in YES terms.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Print {
    pub t_ms: i64,
    /// The YES price of the fill (1 − price for the NO token).
    pub yes_px: f64,
    /// The taker bought YES exposure (bought YES or sold NO).
    pub taker_buys_yes: bool,
    pub shares: f64,
    pub key: String,
}

/// The rows of a `/markets/<slug>/events` page that belong to this market.
pub fn parse_prints(body: &Value, yes_token: &str, no_token: &str) -> Vec<Print> {
    let rows = body.get("events").and_then(Value::as_array).cloned().unwrap_or_default();
    rows.iter()
        .filter_map(|e| {
            let token = e.get("tokenId")?.as_str()?;
            let is_yes = if token == yes_token {
                true
            } else if token == no_token {
                false
            } else {
                return None;
            };
            let created = e.get("createdAt")?.as_str()?;
            let price = e.get("price")?.as_f64()?;
            let taker_buy = e.get("side")?.as_i64()? == 0;
            let raw = e.get("matchedSize")?;
            let shares = raw.as_str().and_then(|s| s.parse::<f64>().ok()).or_else(|| raw.as_f64())? / 1e6;
            let tx = e.get("txHash").and_then(Value::as_str).unwrap_or_default();
            Some(Print {
                t_ms: iso_ms(created)?,
                yes_px: if is_yes { price } else { 1.0 - price },
                taker_buys_yes: is_yes == taker_buy,
                shares,
                key: format!("{created}|{token}|{price}|{shares}|{tx}"),
            })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn series_and_assets() {
        assert_eq!(series_of("btc-up-or-down-hourly-p-1792865106835"), "btc-up-or-down-hourly-p");
        assert_eq!(series_of("btc-up-or-down-15-min-1792877400"), "btc-up-or-down-15-min");
        assert_eq!(series_of("what-price"), "what-price");
        assert_eq!(asset_of("solana-up-or-down-daily-p"), "SOL");
        assert_eq!(asset_of("dogecoin-up-or-down-daily-p"), "DOGE");
        assert_eq!(asset_of("doge-up-or-down-hourly-p"), "DOGE");
        assert_eq!(Kind::of("eth-up-or-down-15-min"), Kind::M15);
        assert_eq!(Kind::of("xrp-up-or-down-hourly-p"), Kind::H1);
        assert_eq!(Kind::of("bnb-up-or-down-daily-p"), Kind::D1);
    }

    fn listed(slug: &str) -> Value {
        json!({"slug": slug, "tradeType": "clob", "startAt": "2026-10-24T21:00:00.000Z",
               "expirationTimestamp": 1792879200000_i64, "tokens": {"yes": "Y", "no": "N"},
               "metadata": {"isPolyArbitrage": true, "externalSlug": "bitcoin-up-or-down-october-24-2026-5pm-et",
                            "minSize": 50000000, "maxSpread": 0.035, "makerRebateMult": 0.3, "takerDelayMs": 500}})
    }

    #[test]
    fn active_market_filter() {
        let series: Vec<String> = DEFAULT_SERIES.iter().map(|s| s.to_string()).collect();
        let m = parse_active(&listed("btc-up-or-down-hourly-p-1792865106835"), &series).unwrap();
        assert_eq!((m.asset.as_str(), m.kind, m.min_size, m.taker_delay_ms), ("BTC", Kind::H1, 50.0, 500.0));
        assert_eq!(m.close_ms - m.open_ms, 3_600_000);
        assert!(parse_active(&listed("spy-daily-up-or-down-p-1792775149821"), &series).is_none());
        let mut not_mirror = listed("btc-up-or-down-hourly-p-1");
        not_mirror["metadata"]["isPolyArbitrage"] = false.into();
        assert!(parse_active(&not_mirror, &series).is_none());
    }

    #[test]
    fn polymarket_tokens_in_outcome_order() {
        let m = json!({"clobTokenIds": "[\"111\", \"222\"]", "outcomes": "[\"Up\", \"Down\"]"});
        assert_eq!(parse_poly(&m), Some(("111".into(), "222".into())));
        let flipped = json!({"clobTokenIds": "[\"111\", \"222\"]", "outcomes": "[\"Down\", \"Up\"]"});
        assert_eq!(parse_poly(&flipped), Some(("222".into(), "111".into())));
    }

    #[test]
    fn prints_in_yes_terms() {
        // From the live feed (2026-10-24): side 0 is a taker buy of tokenId.
        let body = json!({"events": [
            {"createdAt": "2026-10-24T21:41:33.652Z", "matchedSize": "79991000", "price": 0.917, "side": 0, "tokenId": "N", "txHash": "0xa"},
            {"createdAt": "2026-10-24T21:21:01.086Z", "matchedSize": "2000000", "price": 0.916, "side": 0, "tokenId": "Y", "txHash": "0xb"},
            {"createdAt": "2026-10-24T21:20:00.000Z", "matchedSize": "5000000", "price": 0.40, "side": 1, "tokenId": "Y", "txHash": "0xc"},
            {"createdAt": "2026-10-24T21:20:00.000Z", "matchedSize": "5000000", "price": 0.40, "side": 0, "tokenId": "OTHER"}
        ]});
        let p = parse_prints(&body, "Y", "N");
        assert_eq!(p.len(), 3);
        // Bought NO at 0.917: sold YES exposure at 0.083.
        assert!((p[0].yes_px - 0.083).abs() < 1e-9 && !p[0].taker_buys_yes && (p[0].shares - 79.991).abs() < 1e-9);
        assert!(p[1].taker_buys_yes && (p[1].yes_px - 0.916).abs() < 1e-9);
        // Sold YES at 0.40.
        assert!(!p[2].taker_buys_yes && (p[2].yes_px - 0.40).abs() < 1e-9);
    }
}
