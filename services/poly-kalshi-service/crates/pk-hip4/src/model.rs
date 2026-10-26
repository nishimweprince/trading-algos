use std::collections::HashMap;

use chrono::{NaiveDateTime, TimeZone, Utc};
use pk_venue_core::NormalizedContract;
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SideSpec {
    pub name: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Outcome {
    pub outcome: u64,
    pub name: String,
    pub description: String,
    #[serde(rename = "sideSpecs")]
    pub sides: Vec<SideSpec>,
    pub venue: String,
}

impl Outcome {
    pub fn side_asset(&self, side: u32) -> u32 {
        100_000_000 + 10 * self.outcome as u32 + side
    }
    pub fn coin(&self, side: u32) -> String {
        format!("#{}", 10 * self.outcome + side as u64)
    }
    pub fn fields(&self) -> HashMap<String, String> {
        self.description
            .split('|')
            .filter_map(|part| part.split_once(':'))
            .map(|(k, v)| (k.into(), v.into()))
            .collect()
    }
    pub fn normalized(&self, kalshi_ticker: &str) -> Option<NormalizedContract> {
        let f = self.fields();
        match self.name.as_str() {
            "template:binaryPrice" => {
                let raw = f.get("perp")?;
                let underlying = raw.rsplit(':').next()?.to_ascii_uppercase();
                let threshold = f.get("threshold")?.clone();
                let decision_ms = parse_hl_time(f.get("time")?)?;
                let window = f.get("seconds").and_then(|x| x.parse().ok());
                let description = f.get("priceDescription").cloned().unwrap_or_default();
                let source = if description.to_ascii_lowercase().contains("pyth") {
                    "PYTH"
                } else if description.contains("1-minute candle") {
                    "HYPERLIQUID_CANDLE"
                } else {
                    "HYPERLIQUID"
                };
                Some(NormalizedContract {
                    venue: "hip4".into(),
                    venue_market_id: format!("{}:{}", self.venue, self.outcome),
                    kalshi_ticker: kalshi_ticker.into(),
                    event_class: "binary_price".into(),
                    underlying,
                    outcome: "above".into(),
                    operator: ">".into(),
                    threshold: Some(threshold),
                    decision_ms: Some(decision_ms),
                    resolution_source: source.into(),
                    averaging_window_s: window,
                    tie_rule: "no".into(),
                    correction_rule: "hyperliquid_final_resolution".into(),
                    ..Default::default()
                })
            }
            "template:fedTargetRange" | "template:policyRate" => Some(NormalizedContract {
                venue: "hip4".into(),
                venue_market_id: format!("{}:{}", self.venue, self.outcome),
                kalshi_ticker: kalshi_ticker.into(),
                event_class: "policy_rate".into(),
                underlying: "FED_FUNDS".into(),
                outcome: self.description.clone(),
                operator: "category".into(),
                resolution_source: "FEDERAL_RESERVE".into(),
                tie_rule: "exact_category".into(),
                correction_rule: "official_release".into(),
                ..Default::default()
            }),
            _ => None,
        }
    }
}

pub fn outcomes(value: &Value) -> Vec<Outcome> {
    value
        .get("outcomes")
        .or_else(|| value.pointer("/data/outcomes"))
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|x| serde_json::from_value(x.clone()).ok())
        .collect()
}
fn parse_hl_time(raw: &str) -> Option<i64> {
    let dt = NaiveDateTime::parse_from_str(raw, "%Y%m%d-%H%M").ok()?;
    Some(Utc.from_utc_datetime(&dt).timestamp_millis())
}

pub fn book(value: &Value) -> Option<pk_venue_core::Book> {
    let levels = value.pointer("/data/levels").or_else(|| value.get("levels"))?.as_array()?;
    let side = |i: usize| -> Option<(f64, f64)> {
        let row = levels.get(i)?.as_array()?.first()?;
        Some((row.get("px")?.as_str()?.parse().ok()?, row.get("sz")?.as_str()?.parse().ok()?))
    };
    let bid = side(0);
    let ask = side(1);
    Some(pk_venue_core::Book {
        bid: bid.map(|x| x.0),
        bid_size: bid.map_or(0.0, |x| x.1),
        ask: ask.map(|x| x.0),
        ask_size: ask.map_or(0.0, |x| x.1),
        update_ms: pk_venue_core::service::now_ms(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn binary() -> Outcome {
        Outcome{outcome:17588,name:"template:binaryPrice".into(),description:"perp:BTC|priceDescription:the Pyth BTC/USD price|seconds:90|threshold:79000|time:20261102-0800".into(),sides:vec![SideSpec{name:"Yes".into()},SideSpec{name:"No".into()}],venue:"skew".into()}
    }

    #[test]
    fn parses_binary_and_assets() {
        let o = binary();
        assert_eq!(o.side_asset(0), 100175880);
        assert_eq!(o.side_asset(1), 100175881);
        assert_eq!(o.coin(0), "#175880");
        assert_eq!(o.coin(1), "#175881");
        let n = o.normalized("KXBTC").unwrap();
        assert_eq!(n.resolution_source, "PYTH");
        assert_eq!(n.threshold.as_deref(), Some("79000"));
        assert_eq!(n.averaging_window_s, Some(90));
        assert_eq!(n.event_class, "binary_price");
        assert_eq!(n.venue_market_id, "skew:17588");
    }

    #[test]
    fn candle_priced_binary_is_not_pyth() {
        let mut o = binary();
        o.description = "perp:ETH|priceDescription:the 1-minute candle close|seconds:60|threshold:4000|time:20261102-0800".into();
        let n = o.normalized("KXETH").unwrap();
        assert_eq!(n.resolution_source, "HYPERLIQUID_CANDLE");
        assert_eq!(n.underlying, "ETH");
    }

    #[test]
    fn policy_rate_templates_normalize_and_unknown_ones_do_not() {
        for name in ["template:fedTargetRange", "template:policyRate"] {
            let o = Outcome {
                outcome: 9,
                name: name.into(),
                description: "FED DECISION: 25 BPS CUT".into(),
                sides: vec![],
                venue: "skew".into(),
            };
            let n = o.normalized("KXFED-26JAN").unwrap();
            assert_eq!(n.event_class, "policy_rate");
            assert_eq!(n.underlying, "FED_FUNDS");
            assert_eq!(n.resolution_source, "FEDERAL_RESERVE");
        }
        let o = Outcome { name: "template:somethingNew".into(), ..binary() };
        assert!(o.normalized("KXX").is_none());
        let mut missing = binary();
        missing.description = "perp:BTC|threshold:79000".into();
        assert!(missing.normalized("KXBTC").is_none());
    }

    #[test]
    fn l2_book_parses_best_levels() {
        let v = json!({"channel":"l2Book","data":{"coin":"#175880","levels":[[{"px":"0.41","sz":"5"}],[{"px":"0.43","sz":"7"}]]}});
        let b = book(&v).unwrap();
        assert_eq!((b.bid, b.bid_size, b.ask, b.ask_size), (Some(0.41), 5.0, Some(0.43), 7.0));
        assert!(b.valid());
        assert!(book(&json!({"data":{}})).is_none());
    }
}
