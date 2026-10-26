use std::collections::{BTreeMap, HashMap};

use pk_venue_core::service::now_ms;
use pk_venue_core::{Book, MatchDecision};
use serde_json::Value;

use crate::config::Config;
use crate::model::Outcome;
use crate::sign::OrderSpec;

#[derive(Clone)]
pub struct Matched {
    pub outcome: Outcome,
    pub ticker: String,
    pub decision: MatchDecision,
}
#[derive(Default)]
struct Levels {
    yes: BTreeMap<i32, f64>,
    no: BTreeMap<i32, f64>,
}
impl Levels {
    fn fair(&self) -> Option<f64> {
        let y = self.yes.iter().rev().find(|(_, q)| **q > 0.0)?.0;
        let n = self.no.iter().rev().find(|(_, q)| **q > 0.0)?.0;
        Some((*y as f64 / 1e4 + 1.0 - *n as f64 / 1e4) / 2.0)
    }
}
#[derive(Default)]
struct Quote {
    fair: f64,
    last: i64,
}
pub enum Batch {
    Cancel(String),
    Alo(Vec<(Matched, OrderSpec, String)>),
    Ioc(Vec<(Matched, OrderSpec, String, f64)>),
}
pub struct Engine {
    pub matched: Vec<Matched>,
    levels: HashMap<String, Levels>,
    kalshi_ms: HashMap<String, i64>,
    pub books: HashMap<u32, Book>,
    quotes: HashMap<u64, Quote>,
    stale_cancelled: bool,
}
impl Engine {
    pub fn new() -> Self {
        Self {
            matched: vec![],
            levels: HashMap::new(),
            kalshi_ms: HashMap::new(),
            books: HashMap::new(),
            quotes: HashMap::new(),
            stale_cancelled: false,
        }
    }
    pub fn tickers(&self) -> std::collections::BTreeSet<String> {
        self.matched.iter().map(|m| m.ticker.clone()).collect()
    }
    pub fn coins(&self) -> Vec<(String, u32)> {
        self.matched
            .iter()
            .flat_map(|m| {
                [(m.outcome.coin(0), m.outcome.side_asset(0)), (m.outcome.coin(1), m.outcome.side_asset(1))]
            })
            .collect()
    }
    pub fn kalshi(&mut self, text: &str) -> Option<(String, f64)> {
        let v: Value = serde_json::from_str(text).ok()?;
        let kind = v.get("type")?.as_str()?;
        if !matches!(kind, "orderbook_snapshot" | "orderbook_delta") {
            return None;
        }
        let m = v.get("msg")?;
        let ticker = m.get("market_ticker")?.as_str()?.to_owned();
        let levels = self.levels.entry(ticker.clone()).or_default();
        if kind == "orderbook_snapshot" {
            levels.yes.clear();
            levels.no.clear();
            load(m, "yes_dollars_fp", "yes", &mut levels.yes);
            load(m, "no_dollars_fp", "no", &mut levels.no)
        } else {
            let side = m.get("side")?.as_str()?;
            let px = number(m.get("price_dollars").or_else(|| m.get("price"))?)?;
            let px = if px > 1.0 { px / 100.0 } else { px };
            let delta = number(m.get("delta")?)?;
            let map = if side == "yes" { &mut levels.yes } else { &mut levels.no };
            let key = (px * 1e4).round() as i32;
            let q = map.get(&key).copied().unwrap_or(0.0) + delta;
            if q <= 0.0 {
                map.remove(&key);
            } else {
                map.insert(key, q);
            }
        }
        let fair = levels.fair()?;
        self.kalshi_ms.insert(ticker.clone(), now_ms());
        Some((ticker, fair))
    }
    pub fn evaluate(&mut self, cfg: &Config) -> Vec<Batch> {
        let now = now_ms();
        let stale = self.matched.iter().any(|m| {
            self.kalshi_ms.get(&m.ticker).is_none_or(|t| now - *t > cfg.freshness_ms)
                || [m.outcome.side_asset(0), m.outcome.side_asset(1)]
                    .iter()
                    .any(|a| self.books.get(a).is_none_or(|b| b.age_ms(now) > cfg.freshness_ms || !b.valid()))
        });
        if stale {
            if self.stale_cancelled {
                return vec![];
            }
            self.stale_cancelled = true;
            return vec![Batch::Cancel("stale feed".into())];
        }
        self.stale_cancelled = false;
        let mut alo = vec![];
        let mut ioc = vec![];
        for m in self.matched.clone() {
            let Some(fair) = self.levels.get(&m.ticker).and_then(Levels::fair) else { continue };
            let q = self.quotes.entry(m.outcome.outcome).or_default();
            if cfg.maker && m.decision.maker && ((fair - q.fair).abs() >= 0.01 || now - q.last >= 5000) {
                for side in 0..2 {
                    let asset = m.outcome.side_asset(side);
                    let target = if side == 0 { fair } else { 1.0 - fair };
                    let mut px = (target - cfg.half_spread - m.decision.max_basis).clamp(0.01, 0.99);
                    if let Some(ask) = self.books.get(&asset).and_then(|b| b.ask) {
                        px = px.min(ask - 0.01).max(0.01)
                    }
                    let cloid = format!("0x{:032x}", (now as u128) << 16 | side as u128);
                    alo.push((
                        m.clone(),
                        OrderSpec {
                            asset,
                            buy: true,
                            price: wire(px),
                            size: "1".into(),
                            reduce_only: false,
                            tif: "Alo",
                            cloid: Some(cloid),
                        },
                        if side == 0 { "YES".into() } else { "NO".into() },
                    ));
                }
                *q = Quote { fair, last: now };
            }
            if cfg.taker && m.decision.taker {
                for side in 0..2 {
                    let asset = m.outcome.side_asset(side);
                    let target = if side == 0 { fair } else { 1.0 - fair };
                    if let Some(ask) = self.books.get(&asset).and_then(|b| b.ask) {
                        let edge = target - ask - cfg.slippage - m.decision.max_basis;
                        if edge >= cfg.min_taker_edge {
                            ioc.push((
                                m.clone(),
                                OrderSpec {
                                    asset,
                                    buy: true,
                                    price: wire((ask + cfg.slippage).min(0.99)),
                                    size: "1".into(),
                                    reduce_only: false,
                                    tif: "Ioc",
                                    cloid: None,
                                },
                                if side == 0 { "YES".into() } else { "NO".into() },
                                edge,
                            ));
                        }
                    }
                }
            }
        }
        let mut out = vec![];
        if !ioc.is_empty() {
            out.push(Batch::Ioc(ioc))
        }
        if !alo.is_empty() {
            out.push(Batch::Alo(alo))
        }
        out
    }
}
fn load(v: &Value, dollars: &str, cents: &str, out: &mut BTreeMap<i32, f64>) {
    let rows = v.get(dollars).or_else(|| v.get(cents));
    for row in rows.and_then(Value::as_array).into_iter().flatten() {
        let Some(p) = row.as_array() else { continue };
        let Some(mut px) = p.first().and_then(number) else { continue };
        if v.get(dollars).is_none() {
            px /= 100.0
        }
        let Some(q) = p.get(1).and_then(number) else { continue };
        out.insert((px * 1e4).round() as i32, q);
    }
}
fn number(v: &Value) -> Option<f64> {
    v.as_f64().or_else(|| v.as_i64().map(|x| x as f64)).or_else(|| v.as_str()?.parse().ok())
}
fn wire(v: f64) -> String {
    let s = format!("{v:.5}");
    s.trim_end_matches('0').trim_end_matches('.').into()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{Outcome, SideSpec};
    use pk_venue_core::{MatchGrade, SideMapping};

    fn cfg() -> Config {
        Config::from_map(
            std::collections::HashMap::from([
                ("API_KEY".into(), "0123456789abcdef".into()),
                ("KALSHI_API_KEY_ID".into(), "read-only".into()),
                ("KALSHI_PRIVATE_KEY_PATH".into(), "kalshi.pem".into()),
            ]),
            std::path::Path::new("/tmp/hip4-engine"),
        )
        .unwrap()
    }

    fn matched() -> Matched {
        Matched {
            outcome: Outcome {
                outcome: 17588,
                name: "template:binaryPrice".into(),
                description: "perp:BTC|threshold:79000".into(),
                sides: vec![SideSpec { name: "Yes".into() }, SideSpec { name: "No".into() }],
                venue: "skew".into(),
            },
            ticker: "KXBTC".into(),
            decision: MatchDecision {
                grade: MatchGrade::E0,
                kalshi_ticker: "KXBTC".into(),
                venue_market_id: "skew:17588".into(),
                kalshi_fingerprint: "k".into(),
                venue_fingerprint: "v".into(),
                side_mapping: SideMapping {
                    kalshi_yes_is_venue_side: 0,
                    venue_side_names: ["Yes".into(), "No".into()],
                },
                basis_reason: None,
                max_basis: 0.0,
                maker: true,
                taker: false,
                rejected: vec![],
            },
        }
    }

    #[test]
    fn stale_feeds_cancel_once_and_fresh_books_quote_both_sides() {
        let mut e = Engine::new();
        let m = matched();
        e.matched = vec![m.clone()];
        let cfg = cfg();
        assert!(matches!(e.evaluate(&cfg).as_slice(), [Batch::Cancel(_)]));
        assert!(e.evaluate(&cfg).is_empty());

        let _ = e.kalshi(r#"{"type":"orderbook_snapshot","msg":{"market_ticker":"KXBTC","yes_dollars_fp":[["0.45","3"]],"no_dollars_fp":[["0.50","2"]]}}"#);
        for side in 0..2 {
            e.books.insert(
                m.outcome.side_asset(side),
                Book { bid: Some(0.40), bid_size: 5.0, ask: Some(0.50), ask_size: 5.0, update_ms: now_ms() },
            );
        }
        let batches = e.evaluate(&cfg);
        let [Batch::Alo(items)] = batches.as_slice() else { panic!("expected one ALO batch") };
        assert_eq!(items.len(), 2);
        assert!(items.iter().all(|(_, spec, _)| spec.tif == "Alo" && spec.cloid.is_some()));
        let assets: Vec<u32> = items.iter().map(|(_, spec, _)| spec.asset).collect();
        assert!(assets.contains(&m.outcome.side_asset(0)) && assets.contains(&m.outcome.side_asset(1)));
    }
}
