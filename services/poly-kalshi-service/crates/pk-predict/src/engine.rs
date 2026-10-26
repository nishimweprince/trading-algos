use std::collections::{BTreeMap, HashMap};

use pk_venue_core::Book;
use pk_venue_core::service::now_ms;
use serde_json::Value;

use crate::config::Config;
use crate::model::PredictMarket;

#[derive(Clone, Debug)]
pub enum Action {
    CancelAll { reason: String },
    Maker { market: PredictMarket, token: String, outcome: String, price: f64, quantity: f64 },
    Taker { market: PredictMarket, token: String, outcome: String, price: f64, quantity: f64, edge: f64 },
}

#[derive(Default)]
struct KalshiLevels {
    yes: BTreeMap<i32, f64>,
    no: BTreeMap<i32, f64>,
}

impl KalshiLevels {
    fn fair(&self) -> Option<f64> {
        let bid = self.yes.iter().rev().find(|(_, q)| **q > 0.0).map(|(p, _)| *p as f64 / 10_000.0)?;
        let no_bid = self.no.iter().rev().find(|(_, q)| **q > 0.0).map(|(p, _)| *p as f64 / 10_000.0)?;
        Some((bid + (1.0 - no_bid)) / 2.0)
    }
}

#[derive(Default)]
struct QuoteState {
    last_ms: i64,
    fair: f64,
    yes: f64,
    no: f64,
}

pub struct Engine {
    markets: HashMap<String, PredictMarket>,
    kalshi: HashMap<String, KalshiLevels>,
    kalshi_update: HashMap<String, i64>,
    venue_books: HashMap<String, Book>,
    quotes: HashMap<String, QuoteState>,
    stale_cancelled: bool,
}

impl Engine {
    pub fn new() -> Self {
        Self {
            markets: HashMap::new(),
            kalshi: HashMap::new(),
            kalshi_update: HashMap::new(),
            venue_books: HashMap::new(),
            quotes: HashMap::new(),
            stale_cancelled: false,
        }
    }

    pub fn set_markets(&mut self, markets: Vec<PredictMarket>) {
        self.markets.clear();
        for market in markets {
            let id = market.id.to_string();
            self.markets.insert(id, market);
        }
    }

    /// Markets open now or opening within `ahead_ms` (what the venue socket follows).
    pub fn upcoming(&self, now: i64, ahead_ms: i64) -> Vec<PredictMarket> {
        self.markets
            .values()
            .filter(|m| {
                window_open_ms(m).is_none_or(|o| o <= now + ahead_ms) && m.close_ms().is_none_or(|c| now < c)
            })
            .cloned()
            .collect()
    }
    pub fn markets(&self) -> Vec<PredictMarket> {
        self.markets.values().cloned().collect()
    }

    pub fn venue_book(&mut self, market: String, book: Book) {
        self.venue_books.insert(market, book);
    }

    pub fn kalshi_frame(&mut self, text: &str) -> Option<(String, f64)> {
        let value: Value = serde_json::from_str(text).ok()?;
        let kind = value.get("type")?.as_str()?;
        if !matches!(kind, "orderbook_snapshot" | "orderbook_delta") {
            return None;
        }
        let msg = value.get("msg")?;
        let ticker = msg.get("market_ticker")?.as_str()?.to_owned();
        let levels = self.kalshi.entry(ticker.clone()).or_default();
        if kind == "orderbook_snapshot" {
            levels.yes.clear();
            levels.no.clear();
            load_levels(msg, "yes_dollars_fp", "yes", &mut levels.yes);
            load_levels(msg, "no_dollars_fp", "no", &mut levels.no);
        } else {
            let side = msg.get("side").and_then(Value::as_str)?;
            let price = msg.get("price_dollars").or_else(|| msg.get("price"))?;
            let price = parse_price(price)?;
            // Kalshi sends the size change as `delta_fp` (fixed-point string).
            let delta = msg.get("delta_fp").or_else(|| msg.get("delta")).and_then(number)?;
            let side_levels = if side == "yes" { &mut levels.yes } else { &mut levels.no };
            let key = (price * 10_000.0).round() as i32;
            let value = side_levels.get(&key).copied().unwrap_or(0.0) + delta;
            if value <= 0.0 {
                side_levels.remove(&key);
            } else {
                side_levels.insert(key, value);
            }
        }
        let fair = levels.fair()?;
        self.kalshi_update.insert(ticker.clone(), now_ms());
        Some((ticker, fair))
    }

    pub fn evaluate(&mut self, cfg: &Config, inventory: &HashMap<String, f64>) -> Vec<Action> {
        let now = now_ms();
        let mut actions = Vec::new();
        // Only windows trading now are quoted or need fresh books; later
        // windows are listed hours ahead with neither.
        let live = |m: &PredictMarket| {
            window_open_ms(m).is_none_or(|o| o <= now) && m.close_ms().is_none_or(|c| now < c)
        };
        let any_stale = self.markets.values().filter(|m| live(m)).any(|m| {
            let ticker = m.kalshi_ticker.as_deref().unwrap_or_default();
            let kalshi_stale = self.kalshi_update.get(ticker).is_none_or(|t| now - *t > cfg.freshness_ms);
            let venue_stale = self
                .venue_books
                .get(&m.id.to_string())
                .is_none_or(|b| b.age_ms(now) > cfg.freshness_ms || !b.valid());
            kalshi_stale || venue_stale
        });
        if any_stale {
            if !self.stale_cancelled {
                actions.push(Action::CancelAll { reason: "stale feed".into() });
            }
            self.stale_cancelled = true;
            return actions;
        }
        self.stale_cancelled = false;
        for market in self.markets.values().filter(|m| live(m)) {
            if market.close_ms().is_some_and(|close| close - now <= cfg.close_buffer_s * 1000) {
                continue;
            }
            let ticker = market.kalshi_ticker.as_deref().unwrap_or_default();
            let Some(fair) = self.kalshi.get(ticker).and_then(KalshiLevels::fair) else { continue };
            let Some(book) = self.venue_books.get(&market.id.to_string()).copied() else { continue };
            let precision = market.decimal_precision;
            let tick = 10_f64.powi(-(precision as i32));
            let inv = inventory.get(&market.id.to_string()).copied().unwrap_or(0.0);
            let skew = (inv / cfg.risk.max_open_usd.max(1.0) * 0.02).clamp(-0.02, 0.02);
            let yes = (fair - cfg.half_spread - cfg.basis_buffer - skew).clamp(tick, 1.0 - tick);
            let no = (crate::model::complement_price(fair, precision) - cfg.half_spread - cfg.basis_buffer
                + skew)
                .clamp(tick, 1.0 - tick);
            let yes = book.ask.map_or(yes, |ask| yes.min(ask - tick)).max(tick);
            let no_touch = book.bid.map(|bid| crate::model::complement_price(bid, precision));
            let no = no_touch.map_or(no, |ask| no.min(ask - tick)).max(tick);
            let yes = crate::model::quantize_price(yes, precision);
            let no = crate::model::quantize_price(no, precision);
            let quote = self.quotes.entry(market.id.to_string()).or_default();
            let changed = (fair - quote.fair).abs() >= cfg.requote_cents
                || (yes - quote.yes).abs() >= tick
                || (no - quote.no).abs() >= tick;
            let due = now - quote.last_ms >= cfg.refresh_ms as i64;
            if cfg.maker && (changed || due) && now - quote.last_ms >= cfg.min_requote_ms as i64 {
                if let Some(yes_token) = market.yes_outcome().map(|o| o.token_id.clone()) {
                    let no_token =
                        market.outcomes.iter().find(|o| o.token_id != yes_token).map(|o| o.token_id.clone());
                    actions.push(Action::Maker {
                        market: market.clone(),
                        token: yes_token,
                        outcome: "YES".into(),
                        price: yes,
                        quantity: shares_for(yes, cfg.min_order_usd),
                    });
                    if let Some(token) = no_token {
                        actions.push(Action::Maker {
                            market: market.clone(),
                            token,
                            outcome: "NO".into(),
                            price: no,
                            quantity: shares_for(no, cfg.min_order_usd),
                        });
                    }
                    *quote = QuoteState { last_ms: now, fair, yes, no };
                }
            }
            if cfg.taker {
                let fee = market.fee_rate_bps as f64 / 10_000.0;
                if let Some(ask) = book.ask {
                    let edge = fair - ask - fee - cfg.basis_buffer - cfg.taker_slippage;
                    if edge >= cfg.min_taker_edge
                        && let Some(token) = market.yes_outcome().map(|o| o.token_id.clone())
                    {
                        actions.push(Action::Taker {
                            market: market.clone(),
                            token,
                            outcome: "YES".into(),
                            price: crate::model::quantize_price(
                                (ask + cfg.taker_slippage).min(0.99),
                                precision,
                            ),
                            quantity: shares_for((ask + cfg.taker_slippage).min(0.99), cfg.min_order_usd),
                            edge,
                        });
                    }
                }
                if let Some(bid) = book.bid {
                    let no_ask = crate::model::complement_price(bid, precision);
                    let edge = crate::model::complement_price(fair, precision)
                        - no_ask
                        - fee
                        - cfg.basis_buffer
                        - cfg.taker_slippage;
                    if edge >= cfg.min_taker_edge {
                        if let Some(token) = market
                            .outcomes
                            .iter()
                            .find(|o| market.yes_outcome().is_none_or(|y| y.token_id != o.token_id))
                            .map(|o| o.token_id.clone())
                        {
                            actions.push(Action::Taker {
                                market: market.clone(),
                                token,
                                outcome: "NO".into(),
                                price: crate::model::quantize_price(
                                    (no_ask + cfg.taker_slippage).min(0.99),
                                    precision,
                                ),
                                quantity: shares_for((no_ask + cfg.taker_slippage).min(0.99), cfg.min_order_usd),
                                edge,
                            });
                        }
                    }
                }
            }
        }
        actions
    }
}

/// The fewest whole shares worth at least `min_usd` at `price` (Predict
/// refuses an order under $0.90).
pub fn shares_for(price: f64, min_usd: f64) -> f64 {
    if price <= 0.0 {
        return 1.0;
    }
    (min_usd / price - 1e-9).ceil().max(1.0)
}

/// When the market's window starts trading: the 15-minute window's open
/// (from its slug) before any listing time.
fn window_open_ms(m: &PredictMarket) -> Option<i64> {
    m.window_open_s().map(|s| s * 1000).or_else(|| m.open_ms())
}

fn load_levels(msg: &Value, dollars: &str, cents: &str, out: &mut BTreeMap<i32, f64>) {
    let value = msg.get(dollars).or_else(|| msg.get(cents));
    for row in value.and_then(Value::as_array).into_iter().flatten() {
        let Some(pair) = row.as_array() else { continue };
        let Some(mut price) = pair.first().and_then(parse_price) else { continue };
        if msg.get(dollars).is_none() {
            price /= 100.0;
        }
        let Some(size) = pair.get(1).and_then(number) else { continue };
        out.insert((price * 10_000.0).round() as i32, size);
    }
}

fn parse_price(value: &Value) -> Option<f64> {
    number(value).map(|p| if p > 1.0 { p / 100.0 } else { p })
}
fn number(value: &Value) -> Option<f64> {
    value.as_f64().or_else(|| value.as_i64().map(|n| n as f64)).or_else(|| value.as_str()?.parse().ok())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn market() -> PredictMarket {
        serde_json::from_value(json!({
            "id": 7, "title":"BTC", "question":"up?", "tradingStatus":"OPEN", "isNegRisk":false,
            "isYieldBearing":true, "feeRateBps":100, "outcomes":[{"name":"Yes","indexSet":1,"onChainId":"9"},{"name":"No","indexSet":2,"onChainId":"10"}],
            "kalshiMarketTicker":"KXBTC15M-ABC", "decimalPrecision":2, "marketVariant":"CRYPTO_UP_DOWN",
            "variantData":{"priceFeedProvider":"PYTH"}, "endsAt":"2027-01-01T00:00:00Z"
        }))
        .unwrap()
    }

    fn cfg() -> Config {
        Config::from_map(
            std::collections::HashMap::from([
                ("API_KEY".into(), "0123456789abcdef".into()),
                ("KALSHI_API_KEY_ID".into(), "read-only".into()),
                ("KALSHI_PRIVATE_KEY_PATH".into(), "kalshi.pem".into()),
            ]),
            std::path::Path::new("/tmp/predict-engine"),
        )
        .unwrap()
    }

    #[test]
    fn snapshot_computes_yes_mid_from_yes_and_no_bids() {
        let mut e = Engine::new();
        let frame = r#"{"type":"orderbook_snapshot","msg":{"market_ticker":"KXBTC","yes_dollars_fp":[["0.45","3"]],"no_dollars_fp":[["0.50","2"]]}}"#;
        let (_, fair) = e.kalshi_frame(frame).unwrap();
        assert!((fair - 0.475).abs() < 1e-9);
    }

    #[test]
    fn deltas_move_the_fair_and_empty_levels_drop_out() {
        let mut e = Engine::new();
        let _ = e.kalshi_frame(r#"{"type":"orderbook_snapshot","msg":{"market_ticker":"KX","yes_dollars_fp":[["0.45","3"]],"no_dollars_fp":[["0.50","2"]]}}"#);
        let (_, fair) = e
            .kalshi_frame(r#"{"type":"orderbook_delta","msg":{"market_ticker":"KX","side":"yes","price_dollars":"0.47","delta":"1"}}"#)
            .unwrap();
        assert!((fair - 0.485).abs() < 1e-9);
        assert!(
            e.kalshi_frame(r#"{"type":"ticker","msg":{"market_ticker":"KX"}}"#).is_none(),
            "non-book frames are ignored"
        );
    }

    #[test]
    fn orders_meet_the_venue_minimum() {
        assert_eq!(shares_for(0.40, 1.0), 3.0);
        assert_eq!(shares_for(0.10, 1.0), 10.0);
        assert_eq!(shares_for(0.50, 1.0), 2.0);
        assert_eq!(shares_for(0.99, 1.0), 2.0);
        assert!(shares_for(0.01, 1.0) * 0.01 >= 0.9);
    }

    #[test]
    fn kalshi_delta_fp_moves_the_fair() {
        let mut e = Engine::new();
        let _ = e.kalshi_frame(r#"{"type":"orderbook_snapshot","msg":{"market_ticker":"KX","yes_dollars_fp":[["0.45","3"]],"no_dollars_fp":[["0.50","2"]]}}"#);
        let (_, fair) = e
            .kalshi_frame(r#"{"type":"orderbook_delta","msg":{"market_ticker":"KX","price_dollars":"0.4700","delta_fp":"1.00","side":"yes"}}"#)
            .unwrap();
        assert!((fair - 0.485).abs() < 1e-9);
    }

    #[test]
    fn stale_feeds_cancel_once_until_recovery() {
        let mut e = Engine::new();
        e.set_markets(vec![market()]);
        let cfg = cfg();
        let first = e.evaluate(&cfg, &HashMap::new());
        assert!(matches!(first.as_slice(), [Action::CancelAll { .. }]));
        assert!(e.evaluate(&cfg, &HashMap::new()).is_empty());
    }

    #[test]
    fn maker_quotes_land_on_grid_behind_the_touch() {
        let mut e = Engine::new();
        e.set_markets(vec![market()]);
        let _ = e.kalshi_frame(r#"{"type":"orderbook_snapshot","msg":{"market_ticker":"KXBTC15M-ABC","yes_dollars_fp":[["0.45","3"]],"no_dollars_fp":[["0.50","2"]]}}"#);
        e.venue_book(
            "7".into(),
            Book { bid: Some(0.40), bid_size: 5.0, ask: Some(0.50), ask_size: 5.0, update_ms: now_ms() },
        );
        let cfg = cfg();
        let actions = e.evaluate(&cfg, &HashMap::new());
        let mut seen = 0;
        for action in &actions {
            let Action::Maker { price, outcome, .. } = action else { continue };
            seen += 1;
            assert!(((price * 100.0) - (price * 100.0).round()).abs() < 1e-9, "{outcome} off grid");
            if outcome == "YES" {
                assert!(*price <= 0.50 - 0.01 + 1e-9, "yes crosses the touch");
            } else {
                assert!(*price <= 0.60 - 0.01 + 1e-9, "no crosses the touch");
            }
        }
        assert_eq!(seen, 2);
    }
}
