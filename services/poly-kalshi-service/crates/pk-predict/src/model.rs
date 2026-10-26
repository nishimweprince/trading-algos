use chrono::{DateTime, Utc};
use pk_venue_core::{Book, MatchDecision, MatchGrade, NormalizedContract, SideMapping};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Outcome {
    pub name: String,
    #[serde(rename = "indexSet")]
    pub index_set: u32,
    #[serde(rename = "onChainId")]
    pub token_id: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PredictMarket {
    pub id: u64,
    pub title: String,
    pub question: String,
    #[serde(rename = "tradingStatus")]
    pub trading_status: String,
    #[serde(default)]
    pub status: Option<String>,
    #[serde(rename = "conditionId", default)]
    pub condition_id: Option<String>,
    #[serde(rename = "isNegRisk")]
    pub is_neg_risk: bool,
    #[serde(rename = "isYieldBearing")]
    pub is_yield_bearing: bool,
    #[serde(rename = "feeRateBps")]
    pub fee_rate_bps: u64,
    pub outcomes: Vec<Outcome>,
    #[serde(rename = "kalshiMarketTicker")]
    pub kalshi_ticker: Option<String>,
    #[serde(rename = "seriesSlug", default)]
    pub series_slug: Option<String>,
    #[serde(rename = "categorySlug", default)]
    pub category_slug: Option<String>,
    #[serde(rename = "decimalPrecision")]
    pub decimal_precision: u32,
    #[serde(rename = "marketVariant")]
    pub market_variant: String,
    #[serde(rename = "variantData", default)]
    pub variant_data: Value,
    #[serde(default)]
    pub category: Value,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

impl PredictMarket {
    pub fn price_feed_provider(&self) -> Option<&str> {
        self.variant_data.get("priceFeedProvider").and_then(Value::as_str)
    }

    /// Asset from the live series slug (`btc-updown-15m` -> `BTC`).
    pub fn series_asset(&self) -> Option<String> {
        let slug = self.series_slug.as_deref()?;
        let asset = slug.split('-').next()?.to_ascii_uppercase();
        ["BTC", "ETH"].contains(&asset.as_str()).then_some(asset)
    }

    pub fn is_15m_series(&self) -> bool {
        self.series_slug.as_deref().is_some_and(|s| s.ends_with("-updown-15m"))
    }

    /// Window open (unix seconds) from `categorySlug`
    /// (`btc-updown-15m-1793017800`). 15m windows are 900s-aligned.
    pub fn window_open_s(&self) -> Option<i64> {
        if !self.is_15m_series() {
            return None;
        }
        let category = self.category_slug.as_deref()?;
        let series = self.series_slug.as_deref()?;
        let open: i64 = category.strip_prefix(&format!("{series}-"))?.parse().ok()?;
        (open > 0 && open % 900 == 0).then_some(open)
    }

    pub fn supported(&self, assets: &[String]) -> bool {
        let linked = self.kalshi_ticker.as_deref().is_some_and(|t| assets.iter().any(|a| t.contains(a)));
        let derived = self.is_15m_series()
            && self.series_asset().is_some_and(|a| assets.iter().any(|x| x == &a))
            && self.window_open_s().is_some();
        self.trading_status == "OPEN"
            && self.market_variant == "CRYPTO_UP_DOWN"
            && (linked || derived)
            && matches!(self.price_feed_provider(), Some("PYTH" | "CHAINLINK"))
            && self.outcomes.len() == 2
            && (1..=6).contains(&self.decimal_precision)
            && self.outcome_mapping_valid()
    }

    pub fn yes_outcome(&self) -> Option<&Outcome> {
        self.outcomes
            .iter()
            .find(|o| matches!(o.name.to_ascii_lowercase().as_str(), "yes" | "up"))
            .or_else(|| self.outcomes.first())
    }

    pub fn close_ms(&self) -> Option<i64> {
        ["endsAt", "endDate", "closeTime", "closeTimeMs"]
            .into_iter()
            .find_map(|key| self.extra.get(key).or_else(|| self.category.get(key)).and_then(parse_time))
            .or_else(|| self.window_open_s().map(|open| (open + 900) * 1000))
    }

    pub fn open_ms(&self) -> Option<i64> {
        ["startsAt", "startDate", "openTime", "openTimeMs"]
            .into_iter()
            .find_map(|key| self.extra.get(key).or_else(|| self.category.get(key)).and_then(parse_time))
            .or_else(|| self.window_open_s().map(|open| open * 1000))
    }

    fn outcome_mapping_valid(&self) -> bool {
        let mut names: Vec<_> = self.outcomes.iter().map(|o| o.name.to_ascii_lowercase()).collect();
        names.sort();
        names == ["down", "up"] || names == ["no", "yes"]
    }

    pub fn normalized(&self) -> NormalizedContract {
        let ticker = self.kalshi_ticker.clone().unwrap_or_default();
        let underlying = ["BTC", "ETH"]
            .into_iter()
            .find(|a| ticker.contains(a))
            .map(str::to_owned)
            .or_else(|| self.series_asset())
            .unwrap_or_default();
        NormalizedContract {
            venue: "predict".into(),
            venue_market_id: self.id.to_string(),
            kalshi_ticker: ticker,
            event_class: "crypto_up_down_15m".into(),
            underlying,
            outcome: "up".into(),
            operator: ">=".into(),
            threshold: self.variant_data.get("startPrice").map(Value::to_string),
            open_ms: self.open_ms(),
            close_ms: self.close_ms(),
            decision_ms: self.close_ms(),
            resolution_source: self.price_feed_provider().unwrap_or_default().into(),
            averaging_window_s: Some(0),
            tie_rule: "up_on_equal".into(),
            correction_rule: "predict_final_resolution".into(),
        }
    }

    /// Identity link: Predict's explicit Kalshi ticker when present, else
    /// the Kalshi 15m market resolved by series + window timing in
    /// discovery. The venue oracle (live: CHAINLINK) differs from Kalshi's
    /// settlement index, so these mirrors are E1 and basis-buffered.
    pub fn match_decision(&self, basis: f64, kalshi: &NormalizedContract) -> MatchDecision {
        let venue = self.normalized();
        let mut rejected = Vec::new();
        if !venue.kalshi_ticker.is_empty() && venue.kalshi_ticker != kalshi.kalshi_ticker {
            rejected.push("Kalshi ticker link changed or did not resolve".into());
        }
        if venue.kalshi_ticker.is_empty() && kalshi.kalshi_ticker.is_empty() {
            rejected.push("Kalshi ticker link changed or did not resolve".into());
        }
        if venue.underlying.is_empty() || venue.underlying != kalshi.underlying {
            rejected.push("underlying asset differs".into());
        }
        if !self.outcome_mapping_valid() {
            rejected.push("YES/NO outcome mapping is ambiguous".into());
        }
        match (venue.close_ms, kalshi.close_ms) {
            (Some(a), Some(b)) if (a - b).abs() <= 2_000 => {}
            _ => rejected.push("market close/decision timestamps differ or are missing".into()),
        }
        if !matches!(self.price_feed_provider(), Some("PYTH" | "CHAINLINK")) {
            rejected.push("Predict price feed is not CHAINLINK or PYTH".into());
        }
        MatchDecision {
            grade: if rejected.is_empty() { MatchGrade::E1 } else { MatchGrade::E2 },
            kalshi_ticker: kalshi.kalshi_ticker.clone(),
            venue_market_id: venue.venue_market_id.clone(),
            kalshi_fingerprint: kalshi.fingerprint(),
            venue_fingerprint: venue.fingerprint(),
            side_mapping: SideMapping {
                kalshi_yes_is_venue_side: 0,
                venue_side_names: ["Yes".into(), "No".into()],
            },
            basis_reason: Some(format!(
                "Predict {} versus Kalshi settlement index",
                self.price_feed_provider().unwrap_or_default()
            )),
            max_basis: basis,
            maker: rejected.is_empty(),
            taker: rejected.is_empty(),
            rejected,
        }
    }
}

/// Round a price onto the market's decimal grid. Predict matches backend
/// rounding, so every outgoing price must pass through here.
pub fn quantize_price(price: f64, precision: u32) -> f64 {
    let factor = 10_f64.powi(precision.min(9) as i32);
    (price * factor).round() / factor
}

/// NO-side price as the complement of a YES price at the market's decimal
/// precision, so YES + NO == 1 on the backend grid. A raw `1.0 - price`
/// does not match backend rounding.
pub fn complement_price(yes_price: f64, precision: u32) -> f64 {
    let factor = 10_f64.powi(precision.min(9) as i32);
    (factor - (yes_price * factor).round()) / factor
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct RedeemCandidate {
    pub market_id: String,
    pub condition_id: String,
    pub index_set: u8,
    pub amount_wei: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct MergeCandidate {
    pub market_id: String,
    pub condition_id: String,
    pub amount_wei: String,
}

pub fn position_rows(value: &Value) -> &[Value] {
    value
        .pointer("/data/positions")
        .or_else(|| value.get("positions"))
        .or_else(|| value.get("data"))
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or_default()
}

/// Split venue positions into WON-side redeems and balanced YES/NO merges.
/// Rows with an unparseable amount, unknown market, or missing condition ID
/// are skipped: settlement only acts on unambiguous candidates.
pub fn settle_candidates(positions: &Value) -> (Vec<RedeemCandidate>, Vec<MergeCandidate>) {
    use std::collections::HashMap;
    let mut redeems = Vec::new();
    let mut by_market: HashMap<String, Vec<&Value>> = HashMap::new();
    for row in position_rows(positions) {
        let market = row.get("market").unwrap_or(row);
        let Some(market_id) =
            market.get("id").map(|id| id.as_str().map(str::to_owned).unwrap_or_else(|| id.to_string()))
        else {
            continue;
        };
        let amount = row.get("amount").and_then(Value::as_str).unwrap_or("0");
        let Ok(balance) = amount.parse::<u128>() else { continue };
        if balance == 0 {
            continue;
        }
        let outcome = row.get("outcome").unwrap_or(row);
        let index_set = outcome.get("indexSet").and_then(Value::as_u64).unwrap_or(0) as u8;
        let condition = market.get("conditionId").and_then(Value::as_str).unwrap_or_default();
        if outcome.get("status").and_then(Value::as_str) == Some("WON")
            && (index_set == 1 || index_set == 2)
            && !condition.is_empty()
        {
            redeems.push(RedeemCandidate {
                market_id: market_id.clone(),
                condition_id: condition.into(),
                index_set,
                amount_wei: amount.into(),
            });
            continue;
        }
        by_market.entry(market_id).or_default().push(row);
    }
    let mut merges = Vec::new();
    for (market_id, rows) in &by_market {
        if rows.iter().any(|row| {
            row.get("market").and_then(|m| m.get("status")).and_then(Value::as_str) == Some("RESOLVED")
        }) {
            continue;
        }
        let side = |row: &Value| -> Option<(u8, u128, String)> {
            let outcome = row.get("outcome").unwrap_or(row);
            let name = outcome.get("name").and_then(Value::as_str).unwrap_or_default().to_ascii_lowercase();
            let index = outcome.get("indexSet").and_then(Value::as_u64).unwrap_or(0) as u8;
            let side = if name == "yes" || name == "up" || index == 1 {
                1
            } else if name == "no" || name == "down" || index == 2 {
                2
            } else {
                return None;
            };
            let balance = row.get("amount").and_then(Value::as_str)?.parse::<u128>().ok()?;
            let condition = row.get("market").and_then(|m| m.get("conditionId")).and_then(Value::as_str)?;
            (balance > 0).then(|| (side, balance, condition.to_owned()))
        };
        let mut yes: Option<(u128, String)> = None;
        let mut no: Option<(u128, String)> = None;
        for row in rows {
            match side(row) {
                Some((1, balance, condition)) => yes = Some((balance, condition)),
                Some((2, balance, condition)) => no = Some((balance, condition)),
                _ => {}
            }
        }
        if let (Some((yes_balance, yes_condition)), Some((no_balance, _))) = (yes, no)
            && yes_balance == no_balance
        {
            merges.push(MergeCandidate {
                market_id: market_id.clone(),
                condition_id: yes_condition,
                amount_wei: yes_balance.to_string(),
            });
        }
    }
    (redeems, merges)
}

pub fn markets_from_response(value: &Value) -> Vec<PredictMarket> {
    let array = value
        .get("data")
        .and_then(|d| d.get("markets").or_else(|| d.get("items")))
        .or_else(|| value.get("markets"))
        .or_else(|| value.get("data"))
        .and_then(Value::as_array);
    array.into_iter().flatten().filter_map(|m| serde_json::from_value(m.clone()).ok()).collect()
}

pub fn book_from_ws(value: &Value) -> Option<(String, Book)> {
    let data = value.get("data").or_else(|| value.get("payload")).unwrap_or(value);
    let market = data.get("marketId")?.to_string().trim_matches('"').to_owned();
    let level = |key: &str, first: bool| -> Option<(f64, f64)> {
        let levels = data.get(key)?.as_array()?;
        let row = if first { levels.first()? } else { levels.last()? }.as_array()?;
        Some((number(row.first()?)?, number(row.get(1)?)?))
    };
    let bid = level("bids", true);
    let ask = level("asks", true);
    Some((
        market,
        Book {
            bid: bid.map(|x| x.0),
            bid_size: bid.map_or(0.0, |x| x.1),
            ask: ask.map(|x| x.0),
            ask_size: ask.map_or(0.0, |x| x.1),
            update_ms: data
                .get("updateTimestampMs")
                .and_then(Value::as_i64)
                .unwrap_or_else(pk_venue_core::service::now_ms),
        },
    ))
}

fn number(value: &Value) -> Option<f64> {
    value.as_f64().or_else(|| value.as_str()?.parse().ok())
}

fn parse_time(value: &Value) -> Option<i64> {
    value.as_i64().or_else(|| {
        let raw = value.as_str()?;
        raw.parse().ok().or_else(|| {
            DateTime::parse_from_rfc3339(raw).ok().map(|d| d.with_timezone(&Utc).timestamp_millis())
        })
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn direct_crypto_link_is_e1_and_ws_book_parses() {
        let m: PredictMarket = serde_json::from_value(json!({
            "id": 7, "title":"BTC", "question":"up?", "tradingStatus":"OPEN", "isNegRisk":false,
            "isYieldBearing":true, "feeRateBps":100, "outcomes":[{"name":"Yes","indexSet":1,"onChainId":"9"},{"name":"No","indexSet":2,"onChainId":"10"}],
            "kalshiMarketTicker":"KXBTC15M-ABC", "decimalPrecision":2, "marketVariant":"CRYPTO_UP_DOWN",
            "variantData":{"priceFeedProvider":"PYTH"}, "endsAt":"2026-10-26T10:15:00Z"
        })).unwrap();
        assert!(m.supported(&["BTC".into()]));
        let kalshi = NormalizedContract {
            venue: "kalshi".into(),
            venue_market_id: "KXBTC15M-ABC".into(),
            kalshi_ticker: "KXBTC15M-ABC".into(),
            event_class: "crypto_up_down_15m".into(),
            underlying: "BTC".into(),
            close_ms: m.close_ms(),
            resolution_source: "KALSHI_INDEX".into(),
            ..Default::default()
        };
        assert_eq!(m.match_decision(0.02, &kalshi).grade, MatchGrade::E1);
        let (_, b) = book_from_ws(&json!({"data":{"marketId":7,"bids":[[0.4,2]],"asks":[[0.5,3]]}})).unwrap();
        assert_eq!(b.mid(), Some(0.45));
    }

    fn linked_market() -> PredictMarket {
        serde_json::from_value(json!({
            "id": 7, "title":"BTC", "question":"up?", "tradingStatus":"OPEN", "isNegRisk":false,
            "isYieldBearing":true, "feeRateBps":100, "outcomes":[{"name":"Yes","indexSet":1,"onChainId":"9"},{"name":"No","indexSet":2,"onChainId":"10"}],
            "kalshiMarketTicker":"KXBTC15M-ABC", "decimalPrecision":2, "marketVariant":"CRYPTO_UP_DOWN",
            "variantData":{"priceFeedProvider":"PYTH"}, "endsAt":"2026-10-26T10:15:00Z"
        }))
        .unwrap()
    }

    fn linked_kalshi(market: &PredictMarket) -> NormalizedContract {
        NormalizedContract {
            venue: "kalshi".into(),
            venue_market_id: "KXBTC15M-ABC".into(),
            kalshi_ticker: "KXBTC15M-ABC".into(),
            event_class: "crypto_up_down_15m".into(),
            underlying: "BTC".into(),
            close_ms: market.close_ms(),
            resolution_source: "KALSHI_INDEX".into(),
            ..Default::default()
        }
    }

    #[test]
    fn link_feed_time_and_mapping_changes_all_reject() {
        let market = linked_market();
        let kalshi = linked_kalshi(&market);
        assert!(market.match_decision(0.02, &kalshi).rejected.is_empty());

        let changed_ticker = PredictMarket { kalshi_ticker: Some("KXBTC15M-OTHER".into()), ..market.clone() };
        let d = changed_ticker.match_decision(0.02, &kalshi);
        assert_eq!(d.grade, MatchGrade::E2);
        assert!(d.rejected.iter().any(|r| r.contains("ticker")));

        let changed_feed =
            PredictMarket { variant_data: json!({"priceFeedProvider":"BINANCE"}), ..market.clone() };
        assert_eq!(changed_feed.match_decision(0.02, &kalshi).grade, MatchGrade::E2);
        // Live 15m markets resolve via CHAINLINK; that feed is accepted.
        let chainlink_feed =
            PredictMarket { variant_data: json!({"priceFeedProvider":"CHAINLINK"}), ..market.clone() };
        assert_eq!(chainlink_feed.match_decision(0.02, &kalshi).grade, MatchGrade::E1);

        let drifted = PredictMarket {
            extra: serde_json::from_value(json!({"endsAt": "2026-10-26T10:30:00Z"})).unwrap(),
            ..market.clone()
        };
        assert_eq!(drifted.match_decision(0.02, &kalshi).grade, MatchGrade::E2);

        let mut bad_outcomes = market.clone();
        bad_outcomes.outcomes[0].name = "Maybe".into();
        assert_eq!(bad_outcomes.match_decision(0.02, &kalshi).grade, MatchGrade::E2);

        let wrong_asset = NormalizedContract { underlying: "ETH".into(), ..kalshi.clone() };
        assert_eq!(market.match_decision(0.02, &wrong_asset).grade, MatchGrade::E2);
    }

    fn live_market() -> PredictMarket {
        // Shape observed live 2026-10-26: no kalshiMarketTicker, CHAINLINK
        // feed, window carried only by series/category slugs.
        serde_json::from_value(json!({
            "id": 3810024, "title":"Bitcoin Up or Down - October 26, 8:30AM-8:45AM ET",
            "question":"up?", "tradingStatus":"OPEN", "isNegRisk":false,
            "isYieldBearing":false, "feeRateBps":200,
            "outcomes":[{"name":"Up","indexSet":1,"onChainId":"9"},{"name":"Down","indexSet":2,"onChainId":"10"}],
            "kalshiMarketTicker":null, "decimalPrecision":2, "marketVariant":"CRYPTO_UP_DOWN",
            "seriesSlug":"btc-updown-15m", "categorySlug":"btc-updown-15m-1793017800",
            "variantData":{"priceFeedProvider":"CHAINLINK","priceFeedSymbol":"BTCUSDT","startPrice":80517.895}
        }))
        .unwrap()
    }

    #[test]
    fn live_series_market_derives_window_and_matches_by_timing() {
        let m = live_market();
        assert!(m.supported(&["BTC".into(), "ETH".into()]));
        assert_eq!(m.window_open_s(), Some(1793017800));
        assert_eq!(m.open_ms(), Some(1793017800000));
        assert_eq!(m.close_ms(), Some(1793018700000));
        let n = m.normalized();
        assert_eq!(n.underlying, "BTC");
        assert_eq!(n.resolution_source, "CHAINLINK");
        let kalshi = NormalizedContract {
            venue: "kalshi".into(),
            venue_market_id: "KXBTC15M-26OCT260830-30".into(),
            kalshi_ticker: "KXBTC15M-26OCT260830-30".into(),
            event_class: "crypto_up_down_15m".into(),
            underlying: "BTC".into(),
            open_ms: m.open_ms(),
            close_ms: m.close_ms(),
            resolution_source: "KALSHI_INDEX".into(),
            ..Default::default()
        };
        let d = m.match_decision(0.02, &kalshi);
        assert_eq!(d.grade, MatchGrade::E1, "{:?}", d.rejected);
        assert_eq!(d.kalshi_ticker, "KXBTC15M-26OCT260830-30");
        // Timing mismatch and non-15m series fail closed to E2.
        let drifted = NormalizedContract { close_ms: Some(1793019600000), ..kalshi.clone() };
        assert_eq!(m.match_decision(0.02, &drifted).grade, MatchGrade::E2);
        let hourly = PredictMarket {
            series_slug: Some("btc-updown-1h".into()),
            category_slug: Some("btc-updown-1h-1793014800".into()),
            ..m.clone()
        };
        assert!(!hourly.supported(&["BTC".into()]));
        let five_min = PredictMarket {
            series_slug: Some("btc-updown-5m".into()),
            category_slug: Some("btc-updown-5m-1793017800".into()),
            ..m.clone()
        };
        assert!(!five_min.supported(&["BTC".into()]));
        let no_window = PredictMarket { category_slug: None, ..m.clone() };
        assert!(!no_window.supported(&["BTC".into()]));
        assert!(!m.supported(&["ETH".into()]));
    }

    #[test]
    fn book_frames_tolerate_missing_sides_and_reject_garbage() {
        let (id, book) =
            book_from_ws(&json!({"data":{"marketId":7,"bids":[],"asks":[[0.5,3]],"updateTimestampMs":9}}))
                .unwrap();
        assert_eq!(id, "7");
        assert_eq!((book.bid, book.ask), (None, Some(0.5)));
        assert!(book.valid());
        assert!(book_from_ws(&json!({"data":{"bids":[]}})).is_none());
        assert!(book_from_ws(&json!({"nope":true})).is_none());
    }

    #[test]
    fn settle_candidates_split_won_redeems_and_balanced_merges() {
        let positions = json!({"data": {"positions": [
            {"id": "p1",
             "market": {"id": 7, "conditionId": "0xaaa", "status": "RESOLVED"},
             "outcome": {"name": "Yes", "indexSet": 1, "status": "WON"},
             "amount": "5000000000000000000"},
            {"id": "p2",
             "market": {"id": 7, "conditionId": "0xaaa", "status": "RESOLVED"},
             "outcome": {"name": "No", "indexSet": 2, "status": "LOST"},
             "amount": "2000000000000000000"},
            {"id": "p3",
             "market": {"id": 9, "conditionId": "0xbbb", "status": "OPEN"},
             "outcome": {"name": "Yes", "indexSet": 1},
             "amount": "1000000000000000000"},
            {"id": "p4",
             "market": {"id": 9, "conditionId": "0xbbb", "status": "OPEN"},
             "outcome": {"name": "No", "indexSet": 2},
             "amount": "1000000000000000000"},
            {"id": "p5",
             "market": {"id": 11, "conditionId": "0xccc", "status": "OPEN"},
             "outcome": {"name": "Yes", "indexSet": 1},
             "amount": "3000000000000000000"},
            {"id": "p6",
             "market": {"id": 11, "conditionId": "0xccc", "status": "OPEN"},
             "outcome": {"name": "No", "indexSet": 2},
             "amount": "1000000000000000000"},
            {"id": "p7", "market": {"id": 13}, "outcome": {"name": "Yes"}, "amount": "bogus"},
        ]}});
        let (redeems, merges) = settle_candidates(&positions);
        assert_eq!(redeems.len(), 1);
        assert_eq!(redeems[0].market_id, "7");
        assert_eq!(redeems[0].condition_id, "0xaaa");
        assert_eq!(redeems[0].index_set, 1);
        // The LOST leg needs no action; the unbalanced and malformed legs are skipped.
        assert_eq!(merges.len(), 1);
        assert_eq!(merges[0].market_id, "9");
        assert_eq!(merges[0].amount_wei, "1000000000000000000");
    }

    #[test]
    fn complement_and_quantize_follow_backend_rounding() {
        assert_eq!(complement_price(0.492, 2), 0.51);
        assert_eq!(quantize_price(0.492, 2), 0.49);
        assert_eq!(complement_price(0.5, 2) + 0.5, 1.0);
        assert_eq!(quantize_price(0.55555, 2), 0.56);
        assert_eq!(quantize_price(0.123456, 6), 0.123456);
    }
}
