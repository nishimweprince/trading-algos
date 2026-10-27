use std::collections::{BTreeMap, HashMap, VecDeque};

use pk_venue_core::Book;
use pk_venue_core::service::{Position, now_ms};
use serde_json::Value;

use crate::config::Config;
use crate::model::PredictMarket;
use crate::spot::{self, FairSource, Fairs, Spot};

#[derive(Clone, Debug)]
pub enum Action {
    CancelAll { reason: String },
    Maker { market: PredictMarket, token: String, outcome: String, price: f64, quantity: f64 },
    Taker { market: PredictMarket, token: String, outcome: String, price: f64, quantity: f64, edge: f64 },
    /// Pull this market's resting quotes without replacing them (neither side
    /// is quotable now, or the window is inside its close buffer). Requotes
    /// cancel per market, so nothing else removes them.
    Withdraw { market: PredictMarket },
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
    /// The last update placed at least one side.
    quoted: bool,
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
    /// Binance spot per asset (the guard, σ, and the Binance-informed fairs).
    pub spot: Spot,
    /// Per Kalshi ticker: Binance's log mid when Kalshi's fair last changed.
    kalshi_anchor: HashMap<String, f64>,
    /// Per market: first legs held off by the Binance guard last evaluation.
    guarded: HashMap<String, bool>,
    /// Per asset: how often the guard took first legs off.
    pub guard_pauses: HashMap<String, u64>,
    /// Per asset: the σ Kalshi's prices imply, smoothed `(σ per √s, ms)`.
    implied: HashMap<String, (f64, i64)>,
    /// The least σ (per √s) a Binance-informed fair uses without a fresh
    /// Kalshi-implied one (`PK_PRED_SIGMA_FLOOR`).
    pub sigma_floor: f64,
}

/// While first legs price from Binance, Kalshi is the level only: its book
/// must be alive within this, not move every `freshness_ms`.
const KALSHI_QUIET_MAX_MS: i64 = 30_000;

/// One live market's fairs for the status page and the scorecard.
pub struct SignalRow {
    pub market: String,
    pub label: String,
    pub asset: String,
    pub close_ms: i64,
    pub tau_s: f64,
    pub strike: Option<f64>,
    pub spot: Option<f64>,
    pub fairs: Fairs,
    pub predict_mid: Option<f64>,
    pub guarded: bool,
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
            spot: Spot::default(),
            kalshi_anchor: HashMap::new(),
            guarded: HashMap::new(),
            guard_pauses: HashMap::new(),
            implied: HashMap::new(),
            sigma_floor: 0.0,
        }
    }

    /// After a cancel-all: every market quotes from scratch on its next
    /// evaluation, not at its next refresh.
    pub fn forget_quotes(&mut self) {
        self.quotes.clear();
    }

    /// The learned Kalshi-implied σ per asset `(σ per √s, ms)`, for keeping across restarts.
    pub fn implied_snapshot(&self) -> HashMap<String, (f64, i64)> {
        self.implied.clone()
    }

    pub fn restore_implied(&mut self, implied: HashMap<String, (f64, i64)>) {
        self.implied = implied;
    }

    /// Folds the σ Kalshi's fair implies for `market` into its asset's average
    /// (over the two fields, so the market loop can call it).
    fn learn_sigma(
        implied: &mut HashMap<String, (f64, i64)>,
        prices: &Spot,
        market: &PredictMarket,
        kalshi: f64,
        now: i64,
    ) {
        let ticker = market.kalshi_ticker.as_deref().unwrap_or_default();
        let (Some(asset), Some(k), Some(close)) = (spot::asset_of_ticker(ticker), strike(market), market.close_ms())
        else {
            return;
        };
        let Some(mid) = prices.mid(asset, now) else { return };
        let Some(sigma) = spot::implied_sigma(kalshi, mid, k, (close - now) as f64 / 1_000.0) else { return };
        let entry = implied.entry(asset.to_owned()).or_insert((sigma, now));
        let dt = (now - entry.1).max(0) as f64 / 1_000.0;
        let weight = 1.0 - 0.5_f64.powf(dt / spot::IMPLIED_HALF_LIFE_S);
        *entry = (entry.0 + weight * (sigma - entry.0), now);
    }

    /// The σ the Binance-informed fairs use. With a fresh Kalshi-implied σ, the
    /// larger of it and Binance's realised. Without one (windows open near
    /// 0.5, where Kalshi implies nothing), Binance's realised with the last
    /// implied σ (kept across restarts) and `sigma_floor` under it. None
    /// with neither a realised nor any implied σ.
    pub fn sigma(&self, asset: &str, now: i64) -> Option<f64> {
        let realised = self.spot.sigma(asset, now);
        let implied = self.implied.get(asset).map(|(s, ms)| (*s, now - ms <= spot::IMPLIED_MAX_AGE_MS));
        match (realised, implied) {
            (_, Some((i, true))) => Some(realised.map_or(i, |r| r.max(i))),
            (Some(r), Some((i, false))) => Some(r.max(i).max(self.sigma_floor)),
            (Some(r), None) => Some(r.max(self.sigma_floor)),
            (None, Some((i, false))) => Some(i.max(self.sigma_floor)),
            (None, None) => None,
        }
    }

    /// Per asset: Kalshi-implied σ and the σ in use, in bp per √minute.
    pub fn sigma_status(&self, now: i64) -> Value {
        let per_min = |s: f64| s * 1e4 * 60_f64.sqrt();
        let assets: std::collections::BTreeSet<&String> = self.implied.keys().collect();
        serde_json::Value::Object(
            assets
                .into_iter()
                .map(|a| {
                    let (implied, ms) = self.implied[a];
                    (a.clone(), serde_json::json!({
                        "implied_bps_per_min": per_min(implied),
                        "implied_age_s": (now - ms) / 1_000,
                        "used_bps_per_min": self.sigma(a, now).map(per_min),
                    }))
                })
                .collect(),
        )
    }

    /// A Binance frame; true when it updated an asset.
    pub fn binance_frame(&mut self, cfg: &Config, text: &str) -> bool {
        self.spot.frame(text, now_ms(), cfg.binance_pause_bps, cfg.binance_pause_ms).is_some()
    }

    /// Kalshi's fair and the two Binance-informed fairs of `market` now.
    fn fairs(&self, market: &PredictMarket, kalshi: f64, now: i64) -> Fairs {
        let ticker = market.kalshi_ticker.as_deref().unwrap_or_default();
        let mut out = Fairs { kalshi, adjusted: None, spot: None };
        let (Some(asset), Some(close)) = (spot::asset_of_ticker(ticker), market.close_ms()) else { return out };
        let (Some(mid), Some(sigma)) = (self.spot.mid(asset, now), self.sigma(asset, now)) else { return out };
        let tau_s = (close - now) as f64 / 1_000.0;
        let d_ln = self.kalshi_anchor.get(ticker).map_or(0.0, |anchor| mid.ln() - anchor);
        out.adjusted = Some(spot::adjusted_fair(kalshi, d_ln, sigma, tau_s));
        out.spot = strike(market).map(|k| spot::spot_fair(mid, k, sigma, tau_s));
        out
    }

    /// The fairs of every market whose window is trading now.
    pub fn signal_rows(&self, now: i64) -> Vec<SignalRow> {
        let mut rows = Vec::new();
        for market in self.markets.values() {
            let live = window_open_ms(market).is_none_or(|o| o <= now) && market.close_ms().is_some_and(|c| now < c);
            if !live {
                continue;
            }
            let ticker = market.kalshi_ticker.as_deref().unwrap_or_default();
            let Some(kalshi) = self.kalshi.get(ticker).and_then(KalshiLevels::fair) else { continue };
            let asset = spot::asset_of_ticker(ticker).unwrap_or_default().to_owned();
            let close_ms = market.close_ms().unwrap_or(now);
            let book = self.venue_books.get(&market.id.to_string());
            rows.push(SignalRow {
                market: market.id.to_string(),
                label: market.category_slug.clone().unwrap_or_else(|| market.title.clone()),
                tau_s: (close_ms - now) as f64 / 1_000.0,
                close_ms,
                strike: strike(market),
                spot: self.spot.mid(&asset, now),
                fairs: self.fairs(market, kalshi, now),
                predict_mid: book.and_then(|b| b.bid.zip(b.ask)).map(|(b, a)| (b + a) / 2.0),
                guarded: self.guarded.get(&market.id.to_string()).copied().unwrap_or(false),
                asset,
            });
        }
        rows
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
            // The adjusted fair moves Kalshi's by spot's move since this change.
            if let Some(mid) = spot::asset_of_ticker(&ticker).and_then(|a| self.spot.mid(a, now)) {
                self.kalshi_anchor.insert(ticker.clone(), mid.ln());
            }
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
            // Pricing first legs from Binance, a quiet Kalshi book is still the
            // level: only a dead one (or a stale Binance) stops quoting.
            let spot_fresh = spot::asset_of_ticker(ticker).is_some_and(|a| self.spot.mid(a, now).is_some());
            let kalshi_max =
                if cfg.open_fair != FairSource::Kalshi && spot_fresh { KALSHI_QUIET_MAX_MS } else { cfg.freshness_ms };
            let kalshi_stale = self.kalshi_update.get(ticker).is_none_or(|t| now - *t > kalshi_max);
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
                if let Some(quote) = self.quotes.get_mut(&market.id.to_string())
                    && quote.quoted
                {
                    quote.quoted = false;
                    actions.push(Action::Withdraw { market: market.clone() });
                }
                continue;
            }
            let ticker = market.kalshi_ticker.as_deref().unwrap_or_default();
            let Some(kalshi_fair) = self.kalshi.get(ticker).and_then(KalshiLevels::fair) else { continue };
            let Some(book) = self.venue_books.get(&market.id.to_string()).copied() else { continue };
            let precision = market.decimal_precision;
            let tick = 10_f64.powi(-(precision as i32));
            let held = positions.get(&market.id.to_string()).cloned().unwrap_or_default();
            let inv = held.net();
            // First legs and pairing legs may price from different fairs.
            Self::learn_sigma(&mut self.implied, &self.spot, market, kalshi_fair, now);
            // Spot's move counts from Kalshi's last change, or from first sight.
            if let Some(mid) = spot::asset_of_ticker(ticker).and_then(|a| self.spot.mid(a, now)) {
                self.kalshi_anchor.entry(ticker.to_owned()).or_insert(mid.ln());
            }
            let fairs = self.fairs(market, kalshi_fair, now);
            let open_fair = fairs.pick(cfg.open_fair);
            let pair_fair = fairs.pick(cfg.pair_fair);
            let fair = open_fair;
            // Binance guard: spot moved fast, or (only while first legs price
            // from Kalshi) Kalshi is stale against Binance. Pricing first legs
            // from the adjusted fair already follows that move.
            let asset = spot::asset_of_ticker(ticker).unwrap_or_default();
            let stale_kalshi = cfg.open_fair == FairSource::Kalshi
                && fairs.adjusted.is_some_and(|a| (a - kalshi_fair).abs() >= cfg.binance_pause_gap - 1e-9);
            let guarded = cfg.binance_guard && (self.spot.paused(asset, now) || stale_kalshi);
            let was = self.guarded.insert(market.id.to_string(), guarded).unwrap_or(false);
            if guarded && !was {
                *self.guard_pauses.entry(asset.to_owned()).or_default() += 1;
            }
            let skew = (inv / cfg.risk.max_open_usd.max(1.0) * 0.02).clamp(-0.02, 0.02);
            let prices = |fair: f64| {
                let yes = (fair - cfg.half_spread - cfg.basis_buffer - skew).clamp(tick, 1.0 - tick);
                let no = (crate::model::complement_price(fair, precision) - cfg.half_spread - cfg.basis_buffer
                    + skew)
                    .clamp(tick, 1.0 - tick);
                let yes = book.ask.map_or(yes, |ask| yes.min(ask - tick)).max(tick);
                let no_touch = book.bid.map(|bid| crate::model::complement_price(bid, precision));
                let no = no_touch.map_or(no, |ask| no.min(ask - tick)).max(tick);
                (crate::model::quantize_price(yes, precision), crate::model::quantize_price(no, precision))
            };
            let (open_yes, open_no) = prices(open_fair);
            let (pair_yes, pair_no) = prices(pair_fair);
            let yes = if opens(inv, true) { open_yes } else { pair_yes };
            let no = if opens(inv, false) { open_no } else { pair_no };
            // Second leg: with shares held on one side, the other side pays at
            // most 1 − their average cost − pair_margin, so a completed pair
            // locks at least pair_margin a share (and auto-merge books it).
            // First legs (opening or adding to a one-sided position) only early
            // in the window and away from 0/1, where pairs have time to form.
            let window_s = window_open_ms(market).map(|o| (now - o) / 1000).unwrap_or(0);
            // A first leg also needs the venues to agree and Kalshi to be calm:
            // the losing first legs filled as Kalshi moved and Predict lagged.
            let venue_mid = book.bid.zip(book.ask).map(|(b, a)| (b + a) / 2.0);
            // Kalshi's calm only gates first legs priced from Kalshi: it moves
            // after Binance, so it would block just after the move we priced.
            // The Binance guard covers fast moves otherwise.
            let moved = if cfg.open_fair == FairSource::Kalshi {
                self.kalshi_trail.get(ticker).map(|t| recent_move(t, now, cfg.open_move_ms, kalshi_fair)).unwrap_or(0.0)
            } else {
                0.0
            };
            let first_ok = first_leg_ok(cfg, window_s, fair)
                && calm_and_agreed(cfg, fair, venue_mid, moved)
                && (!cfg.open_only_flat || inv.abs() < 1e-9)
                && !guarded;
            let quote_yes = side_price(yes, held.no_qty, held.no_cost, cfg.pair_margin, tick, precision)
                .filter(|px| !opens(inv, true) || (first_ok && *px >= cfg.open_min_px - 1e-9));
            let quote_no = side_price(no, held.yes_qty, held.yes_cost, cfg.pair_margin, tick, precision)
                .filter(|px| !opens(inv, false) || (first_ok && *px >= cfg.open_min_px - 1e-9));
            let yes = quote_yes.unwrap_or(-1.0);
            let no = quote_no.unwrap_or(-1.0);
            let quote = self.quotes.entry(market.id.to_string()).or_default();
            let changed = (fair - quote.fair).abs() >= cfg.requote_cents
                || (yes - quote.yes).abs() >= tick
                || (no - quote.no).abs() >= tick;
            let due = now - quote.last_ms >= cfg.refresh_ms as i64;
            // A resting first leg whose side's fair has come within
            // open_min_edge of it is requoted now, not after min_requote_ms.
            let yes_fair = if opens(inv, true) { open_fair } else { pair_fair };
            let no_fair =
                crate::model::complement_price(if opens(inv, false) { open_fair } else { pair_fair }, precision);
            let thin = |resting: f64, side_fair: f64, opening: bool| {
                opening && resting > 0.0 && resting > side_fair - cfg.open_min_edge + 1e-9
            };
            // A guarded first leg comes off now too.
            let pulled = |resting: f64, opening: bool| guarded && opening && resting > 0.0;
            let urgent = quote.quoted
                && (thin(quote.yes, yes_fair, opens(inv, true))
                    || thin(quote.no, no_fair, opens(inv, false))
                    || pulled(quote.yes, opens(inv, true))
                    || pulled(quote.no, opens(inv, false)));
            if cfg.maker && (changed || due) && (urgent || now - quote.last_ms >= cfg.min_requote_ms as i64) {
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
                    let quoted = quote_yes.is_some() || quote_no.is_some();
                    if !quoted && quote.quoted {
                        actions.push(Action::Withdraw { market: market.clone() });
                    }
                    *quote = QuoteState { last_ms: now, fair, yes, no, quoted };
                }
            }
            if cfg.taker {
                let fee = market.fee_rate_bps as f64 / 10_000.0;
                let fair = kalshi_fair;
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

/// The window's start price, which Predict sets (from Chainlink) when it opens.
fn strike(m: &PredictMarket) -> Option<f64> {
    m.variant_data.get("startPrice").and_then(Value::as_f64).filter(|k| *k > 0.0)
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

    fn quoted_sides(e: &mut Engine, positions: &HashMap<String, Position>) -> Vec<(String, f64)> {
        e.evaluate(&cfg(), positions)
            .into_iter()
            .filter_map(|a| match a {
                Action::Maker { outcome, price, .. } => Some((outcome, price)),
                _ => None,
            })
            .collect()
    }

    fn quoting_at(fair_bid: &str, no_bid: &str, venue: (f64, f64)) -> Engine {
        let mut e = Engine::new();
        e.set_markets(vec![market()]);
        let _ = e.kalshi_frame(&format!(
            r#"{{"type":"orderbook_snapshot","msg":{{"market_ticker":"KXBTC15M-ABC","yes_dollars_fp":[["{fair_bid}","3"]],"no_dollars_fp":[["{no_bid}","2"]]}}}}"#
        ));
        e.venue_book(
            "7".into(),
            Book { bid: Some(venue.0), bid_size: 5.0, ask: Some(venue.1), ask_size: 5.0, update_ms: now_ms() },
        );
        e
    }

    #[test]
    fn first_legs_skip_longshot_sides() {
        // Fair 0.80: YES bids 0.75, NO would bid 0.15, under the 0.35 floor.
        let mut e = quoting_at("0.79", "0.19", (0.78, 0.82));
        let sides = quoted_sides(&mut e, &HashMap::new());
        assert_eq!(sides.len(), 1, "{sides:?}");
        assert_eq!(sides[0].0, "YES");
    }

    #[test]
    fn a_one_sided_market_only_pairs() {
        let mut e = quoting_at("0.45", "0.50", (0.40, 0.50));
        let held = Position { yes_qty: 4.0, yes_cost: 1.6, ..Default::default() };
        let sides = quoted_sides(&mut e, &HashMap::from([("7".to_string(), held)]));
        assert_eq!(sides.len(), 1, "no adding to the YES leg: {sides:?}");
        assert_eq!(sides[0].0, "NO");
    }

    #[test]
    fn a_fast_binance_move_takes_first_legs_off() {
        let mut e = quoting_at("0.45", "0.50", (0.40, 0.50));
        assert_eq!(quoted_sides(&mut e, &HashMap::new()).len(), 2, "both first legs quoted");
        let c = cfg();
        let frame = |px: f64| format!(r#"{{"stream":"btcusdt@bookTicker","data":{{"s":"BTCUSDT","b":"{px}","a":"{px}"}}}}"#);
        assert!(e.binance_frame(&c, &frame(100_000.0)));
        assert!(e.binance_frame(&c, &frame(100_100.0)), "+10 bp at once");
        e.quotes.clear(); // re-evaluate from scratch
        assert!(quoted_sides(&mut e, &HashMap::new()).is_empty(), "guarded: no first legs");
        assert_eq!(e.guard_pauses.get("BTC"), Some(&1));
        // A pairing leg is not a first leg: still quoted under the guard.
        let held = Position { yes_qty: 4.0, yes_cost: 1.6, ..Default::default() };
        e.quotes.clear();
        let sides = quoted_sides(&mut e, &HashMap::from([("7".to_string(), held)]));
        assert_eq!(sides.len(), 1);
        assert_eq!(sides[0].0, "NO");
    }

    #[test]
    fn first_legs_follow_binance_when_priced_from_the_adjusted_fair() {
        let mut c = cfg();
        c.open_fair = FairSource::Adjusted;
        c.binance_guard = false;
        let mut m = serde_json::to_value(market()).unwrap();
        m["variantData"]["startPrice"] = json!(100_000.0);
        m["endsAt"] = json!((chrono::Utc::now() + chrono::Duration::minutes(10)).to_rfc3339());
        let mut e = Engine::new();
        e.set_markets(vec![serde_json::from_value(m).unwrap()]);
        let frame = |px: f64| format!(r#"{{"stream":"btcusdt@bookTicker","data":{{"s":"BTCUSDT","b":"{px}","a":"{px}"}}}}"#);
        let yes_bid = |e: &mut Engine| {
            e.quotes.clear();
            e.evaluate(&c, &HashMap::new())
                .into_iter()
                .find_map(|a| match a {
                    Action::Maker { outcome, price, .. } if outcome == "YES" => Some(price),
                    _ => None,
                })
        };
        // Spot 30 bp over the start; Kalshi 0.70 implies σ; Predict agrees.
        assert!(e.binance_frame(&c, &frame(100_300.0)));
        let _ = e.kalshi_frame(r#"{"type":"orderbook_snapshot","msg":{"market_ticker":"KXBTC15M-ABC","yes_dollars_fp":[["0.69","3"]],"no_dollars_fp":[["0.29","2"]]}}"#);
        e.venue_book("7".into(), Book { bid: Some(0.64), bid_size: 5.0, ask: Some(0.76), ask_size: 5.0, update_ms: now_ms() });
        let before = yes_bid(&mut e).expect("YES first leg at Kalshi's level");
        assert!((before - 0.65).abs() < 1e-9, "{before}");
        assert!(e.sigma("BTC", now_ms()).is_some(), "Kalshi implied a σ");
        // Spot +20 bp, Kalshi silent: the adjusted fair (~0.81) moves the bid now.
        assert!(e.binance_frame(&c, &frame(100_500.0)));
        e.venue_book("7".into(), Book { bid: Some(0.76), bid_size: 5.0, ask: Some(0.86), ask_size: 5.0, update_ms: now_ms() });
        let after = yes_bid(&mut e).expect("YES first leg");
        assert!(after > before + 0.05, "{before} -> {after}");
    }

    #[test]
    fn sigma_falls_back_to_a_floored_last_implied_near_the_money() {
        let mut e = Engine::new();
        e.sigma_floor = 3e-4 / 60_f64.sqrt();
        assert!(e.sigma("BTC", now_ms()).is_none(), "nothing to go on");
        // Restored from disk an hour old and under the floor: floored.
        e.restore_implied(HashMap::from([("BTC".to_string(), (2e-5, now_ms() - 3_600_000))]));
        assert!((e.sigma("BTC", now_ms()).unwrap() - e.sigma_floor).abs() < 1e-12);
        // A fresh Kalshi-implied σ is taken as is.
        e.restore_implied(HashMap::from([("BTC".to_string(), (2e-5, now_ms()))]));
        assert!((e.sigma("BTC", now_ms()).unwrap() - 2e-5).abs() < 1e-12);
    }

    #[test]
    fn a_quiet_kalshi_book_stops_quoting_only_when_first_legs_price_from_it() {
        let mut e = quoting_at("0.45", "0.50", (0.40, 0.50));
        let frame = r#"{"stream":"btcusdt@bookTicker","data":{"s":"BTCUSDT","b":"100000","a":"100000"}}"#;
        let mut c = cfg();
        assert!(e.binance_frame(&c, frame));
        e.kalshi_update.insert("KXBTC15M-ABC".into(), now_ms() - 5_000); // quiet 5 s
        assert!(matches!(e.evaluate(&c, &HashMap::new()).as_slice(), [Action::CancelAll { .. }]), "Kalshi-priced: stale");
        e.stale_cancelled = false;
        c.open_fair = FairSource::Adjusted;
        let actions = e.evaluate(&c, &HashMap::new());
        assert!(actions.iter().any(|a| matches!(a, Action::Maker { .. })), "Binance-priced: Kalshi is the level only");
    }

    #[test]
    fn quotes_are_withdrawn_once_inside_the_close_buffer() {
        let mut e = Engine::new();
        e.set_markets(vec![market()]);
        let _ = e.kalshi_frame(r#"{"type":"orderbook_snapshot","msg":{"market_ticker":"KXBTC15M-ABC","yes_dollars_fp":[["0.45","3"]],"no_dollars_fp":[["0.50","2"]]}}"#);
        e.venue_book(
            "7".into(),
            Book { bid: Some(0.40), bid_size: 5.0, ask: Some(0.50), ask_size: 5.0, update_ms: now_ms() },
        );
        let cfg = cfg();
        assert!(e.evaluate(&cfg, &HashMap::new()).iter().any(|a| matches!(a, Action::Maker { .. })));
        // The same market, now 30 s from its close (inside the 60 s buffer).
        let mut closing = serde_json::to_value(market()).unwrap();
        closing["endsAt"] = json!((chrono::Utc::now() + chrono::Duration::seconds(30)).to_rfc3339());
        e.set_markets(vec![serde_json::from_value(closing).unwrap()]);
        let actions = e.evaluate(&cfg, &HashMap::new());
        assert!(matches!(actions.as_slice(), [Action::Withdraw { .. }]), "{actions:?}");
        assert!(e.evaluate(&cfg, &HashMap::new()).is_empty(), "withdrawn once");
    }
}
