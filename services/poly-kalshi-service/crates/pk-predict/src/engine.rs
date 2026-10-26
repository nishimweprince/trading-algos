use std::collections::{BTreeMap, HashMap, VecDeque};

use pk_venue_core::Book;
use pk_venue_core::service::{Position, now_ms};
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
    /// Recent Kalshi fairs per ticker `(ms, fair)`, for the first-leg calm check.
    kalshi_trail: HashMap<String, VecDeque<(i64, f64)>>,
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
            kalshi_trail: HashMap::new(),
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
        let now = now_ms();
        self.kalshi_update.insert(ticker.clone(), now);
        let trail = self.kalshi_trail.entry(ticker.clone()).or_default();
        if trail.back().is_none_or(|(_, f)| (f - fair).abs() > 1e-9) {
            trail.push_back((now, fair));
        }
        while trail.front().is_some_and(|(t, _)| now - t > TRAIL_KEEP_MS) {
            trail.pop_front();
        }
        Some((ticker, fair))
    }

    /// `positions`: what each market already holds (YES/NO quantity and cost),
    /// for the inventory skew, the pair guard and the first-leg rules.
    pub fn evaluate(&mut self, cfg: &Config, positions: &HashMap<String, Position>) -> Vec<Action> {
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
            // Predict sends a book only when it changes; a quiet book is not stale.
            let venue_stale = self
                .venue_books
                .get(&m.id.to_string())
                .is_none_or(|b| b.age_ms(now) > cfg.freshness_ms.max(VENUE_BOOK_MAX_AGE_MS) || !b.valid());
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
            let held = positions.get(&market.id.to_string()).cloned().unwrap_or_default();
            let inv = held.net();
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
            // Second leg: with shares held on one side, the other side pays at
            // most 1 − their average cost − pair_margin, so a completed pair
            // locks at least pair_margin a share (and auto-merge books it).
            // First legs (opening or adding to a one-sided position) only early
            // in the window and away from 0/1, where pairs have time to form.
            let window_s = window_open_ms(market).map(|o| (now - o) / 1000).unwrap_or(0);
            // A first leg also needs the venues to agree and Kalshi to be calm:
            // the losing first legs filled as Kalshi moved and Predict lagged.
            let venue_mid = book.bid.zip(book.ask).map(|(b, a)| (b + a) / 2.0);
            let moved = self.kalshi_trail.get(ticker).map(|t| recent_move(t, now, cfg.open_move_ms, fair)).unwrap_or(0.0);
            let first_ok = first_leg_ok(cfg, window_s, fair) && calm_and_agreed(cfg, fair, venue_mid, moved);
            let quote_yes = side_price(yes, held.no_qty, held.no_cost, cfg.pair_margin, tick, precision)
                .filter(|_| !opens(inv, true) || first_ok);
            let quote_no = side_price(no, held.yes_qty, held.yes_cost, cfg.pair_margin, tick, precision)
                .filter(|_| !opens(inv, false) || first_ok);
            let yes = quote_yes.unwrap_or(-1.0);
            let no = quote_no.unwrap_or(-1.0);
            let quote = self.quotes.entry(market.id.to_string()).or_default();
            let changed = (fair - quote.fair).abs() >= cfg.requote_cents
                || (yes - quote.yes).abs() >= tick
                || (no - quote.no).abs() >= tick;
            let due = now - quote.last_ms >= cfg.refresh_ms as i64;
            if cfg.maker && (changed || due) && now - quote.last_ms >= cfg.min_requote_ms as i64 {
                if let Some(yes_token) = market.yes_outcome().map(|o| o.token_id.clone()) {
                    let no_token =
                        market.outcomes.iter().find(|o| o.token_id != yes_token).map(|o| o.token_id.clone());
                    if quote_yes.is_some() {
                        actions.push(Action::Maker {
                            market: market.clone(),
                            token: yes_token,
                            outcome: "YES".into(),
                            price: yes,
                            quantity: shares_for(yes, cfg.min_order_usd),
                        });
                    }
                    if let Some(token) = no_token.filter(|_| quote_no.is_some()) {
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

/// Oldest a Predict book may be and still count as live (it is sent on change).
const VENUE_BOOK_MAX_AGE_MS: i64 = 15_000;

/// Would a fill of this side open or add to a one-sided position? (YES when
/// net ≥ 0, NO when net ≤ 0; flat counts as opening.)
pub fn opens(net: f64, yes: bool) -> bool {
    if yes { net >= -1e-9 } else { net <= 1e-9 }
}

/// How long a Kalshi fair trail is kept.
const TRAIL_KEEP_MS: i64 = 30_000;

/// The largest move of Kalshi's fair from `fair` within the last `window_ms`.
pub fn recent_move(trail: &VecDeque<(i64, f64)>, now: i64, window_ms: i64, fair: f64) -> f64 {
    trail.iter().filter(|(t, _)| now - t <= window_ms).map(|(_, f)| (f - fair).abs()).fold(0.0, f64::max)
}

/// First legs only when Predict's own mid is within `open_max_gap` of
/// Kalshi's fair (both sides of its book present) and Kalshi has moved at
/// most `open_max_move` in the last `open_move_ms`.
pub fn calm_and_agreed(cfg: &Config, fair: f64, venue_mid: Option<f64>, moved: f64) -> bool {
    venue_mid.is_some_and(|m| (m - fair).abs() <= cfg.open_max_gap + 1e-9) && moved <= cfg.open_max_move + 1e-9
}

/// May a first leg be quoted `window_s` seconds into the window at fair `q`?
pub fn first_leg_ok(cfg: &Config, window_s: i64, q: f64) -> bool {
    window_s < cfg.open_leg_s && (cfg.open_min_fair - 1e-9..=cfg.open_max_fair + 1e-9).contains(&q)
}

/// A side's bid after the pair guard: with `other_qty` held on the other
/// side at total cost `other_cost`, at most 1 − their average − margin
/// (floored to the tick). None when that leaves nothing to bid.
pub fn side_price(px: f64, other_qty: f64, other_cost: f64, margin: f64, tick: f64, precision: u32) -> Option<f64> {
    let px = if other_qty > 1e-9 {
        let scale = 10_f64.powi(precision as i32);
        let cap = ((1.0 - other_cost / other_qty - margin) * scale + 1e-6).floor() / scale;
        px.min(cap)
    } else {
        px
    };
    (px >= tick - 1e-9).then_some(px)
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
    fn first_legs_need_agreement_and_a_calm_kalshi() {
        let c = cfg();
        assert!(calm_and_agreed(&c, 0.50, Some(0.52), 0.01));
        assert!(!calm_and_agreed(&c, 0.50, Some(0.56), 0.0), "venues disagree by 6c");
        assert!(!calm_and_agreed(&c, 0.50, None, 0.0), "one-sided venue book");
        assert!(!calm_and_agreed(&c, 0.50, Some(0.50), 0.03), "Kalshi moved 3c");
        let trail: VecDeque<(i64, f64)> = [(1_000, 0.55), (4_000, 0.52), (9_000, 0.50)].into_iter().collect();
        assert!((recent_move(&trail, 10_000, 5_000, 0.50) - 0.0).abs() < 1e-9, "only the last 5 s");
        assert!((recent_move(&trail, 10_000, 7_000, 0.50) - 0.02).abs() < 1e-9);
    }

    #[test]
    fn pair_guard_and_first_leg_rules() {
        // Holding 10 NO at 0.55: YES pays at most 1 − 0.55 − 0.02 = 0.43.
        assert_eq!(side_price(0.48, 10.0, 5.5, 0.02, 0.01, 2), Some(0.43));
        assert_eq!(side_price(0.30, 10.0, 5.5, 0.02, 0.01, 2), Some(0.30));
        assert_eq!(side_price(0.48, 0.0, 0.0, 0.02, 0.01, 2), Some(0.48));
        // Holding YES at 0.985: the NO cap is below a tick, so no NO bid.
        assert_eq!(side_price(0.10, 4.0, 3.94, 0.02, 0.01, 2), None);
        assert!(opens(0.0, true) && opens(0.0, false) && opens(3.0, true) && !opens(3.0, false));
        let c = cfg();
        assert!(first_leg_ok(&c, 100, 0.5));
        assert!(!first_leg_ok(&c, 300, 0.5), "after 5 min");
        assert!(!first_leg_ok(&c, 100, 0.9), "outside 0.15–0.85");
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
