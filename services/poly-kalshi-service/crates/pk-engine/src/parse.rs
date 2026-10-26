//! Venue frames, decoded with borrowed strings: no tree is built per frame.
//!
//! - `p`: a Polymarket market-channel frame, one object or a list of them
//!   (`book`, `price_change`, `best_bid_ask`, `market_resolved`, ...).
//! - `k`: a Kalshi frame (`orderbook_snapshot` / `orderbook_delta`).
//! - `c`: a control line written by this service (`window`, `reconnect`,
//!   `resync`, `resolution`).

use std::borrow::Cow;

use pk_core::num::{Num, VenueMs};
use serde::de::IgnoredAny;
use serde::{Deserialize, Deserializer};
use serde_json::value::RawValue;

/// An id as Python's `str(x)` would print it: strings as they are, numbers
/// as digits. `null` or absent is no id.
#[derive(Debug, Clone, PartialEq)]
pub struct Id<'a>(pub Cow<'a, str>);

impl<'de: 'a, 'a> Deserialize<'de> for Id<'a> {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> serde::de::Visitor<'de> for V {
            type Value = Id<'de>;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("an id")
            }
            fn visit_borrowed_str<E>(self, v: &'de str) -> Result<Id<'de>, E> {
                Ok(Id(Cow::Borrowed(v)))
            }
            fn visit_str<E>(self, v: &str) -> Result<Id<'de>, E> {
                Ok(Id(Cow::Owned(v.to_owned())))
            }
            fn visit_string<E>(self, v: String) -> Result<Id<'de>, E> {
                Ok(Id(Cow::Owned(v)))
            }
            fn visit_u64<E>(self, v: u64) -> Result<Id<'de>, E> {
                Ok(Id(Cow::Owned(v.to_string())))
            }
            fn visit_i64<E>(self, v: i64) -> Result<Id<'de>, E> {
                Ok(Id(Cow::Owned(v.to_string())))
            }
        }
        d.deserialize_any(V)
    }
}

impl Id<'_> {
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(Debug, Deserialize)]
pub struct Level {
    pub price: Num,
    pub size: Num,
}

#[derive(Debug, Deserialize)]
pub struct Change<'a> {
    #[serde(borrow, default)]
    pub asset_id: Option<Id<'a>>,
    pub price: Num,
    pub size: Num,
    #[serde(borrow, default)]
    pub side: Option<Cow<'a, str>>,
    #[serde(default)]
    pub best_bid: Option<Num>,
    #[serde(default)]
    pub best_ask: Option<Num>,
}

#[derive(Debug, Deserialize)]
pub struct PolyMsg<'a> {
    #[serde(borrow, default)]
    pub event_type: Option<Cow<'a, str>>,
    #[serde(borrow, default)]
    pub asset_id: Option<Id<'a>>,
    #[serde(default)]
    pub timestamp: VenueMs,
    #[serde(default)]
    pub bids: Option<Vec<Level>>,
    #[serde(default)]
    pub asks: Option<Vec<Level>>,
    #[serde(borrow, default)]
    pub price_changes: Option<Vec<Change<'a>>>,
    #[serde(default)]
    pub best_bid: Option<Num>,
    #[serde(default)]
    pub best_ask: Option<Num>,
    /// `last_trade_price` prints these on the frame itself.
    #[serde(default)]
    pub price: Option<Num>,
    #[serde(default)]
    pub size: Option<Num>,
    #[serde(borrow, default)]
    pub side: Option<Cow<'a, str>>,
    #[serde(borrow, default)]
    pub market: Option<Id<'a>>,
    #[serde(borrow, default)]
    pub winning_asset_id: Option<Id<'a>>,
}

impl PolyMsg<'_> {
    /// `event_type`, or `book` for an untyped frame that carries bids.
    pub fn kind(&self) -> Option<&str> {
        match self.event_type.as_deref() {
            Some(k) if !k.is_empty() => Some(k),
            _ => self.bids.is_some().then_some("book"),
        }
    }
}

/// A Polymarket frame: each object in it, in order. Elements that are not
/// objects are skipped; `Err` only when the text is not JSON at all.
pub fn poly_msgs(text: &str) -> Result<Vec<Option<PolyMsg<'_>>>, serde_json::Error> {
    let t = text.trim_start();
    if t.starts_with('[') {
        let raws: Vec<&RawValue> = serde_json::from_str(text)?;
        Ok(raws
            .into_iter()
            .map(|r| if r.get().starts_with('{') { serde_json::from_str(r.get()).ok() } else { None })
            .collect())
    } else if t.starts_with('{') {
        match serde_json::from_str::<PolyMsg<'_>>(text) {
            Ok(m) => Ok(vec![Some(m)]),
            Err(e) if e.is_data() => {
                // Valid JSON with a field of an unexpected type: not a frame we read.
                serde_json::from_str::<IgnoredAny>(text)?;
                Ok(vec![None])
            }
            Err(e) => Err(e),
        }
    } else {
        serde_json::from_str::<IgnoredAny>(text)?;
        Ok(Vec::new())
    }
}

/// A Kalshi price/size pair: `["0.45", "100.00"]` or `[45, 100]`.
#[derive(Debug, Deserialize)]
pub struct Pair(pub Num, pub Num);

#[derive(Debug, Default, Deserialize)]
pub struct KalshiMsg<'a> {
    #[serde(borrow, default)]
    pub market_ticker: Option<Id<'a>>,
    #[serde(default)]
    pub ts_ms: VenueMs,
    #[serde(default)]
    pub yes_dollars_fp: Option<Vec<Pair>>,
    #[serde(default)]
    pub yes_dollars: Option<Vec<Pair>>,
    #[serde(default)]
    pub yes: Option<Vec<Pair>>,
    #[serde(default)]
    pub no_dollars_fp: Option<Vec<Pair>>,
    #[serde(default)]
    pub no_dollars: Option<Vec<Pair>>,
    #[serde(default)]
    pub no: Option<Vec<Pair>>,
    #[serde(default)]
    pub price_dollars: Option<Num>,
    #[serde(default)]
    pub price: Option<Num>,
    #[serde(default)]
    pub delta_fp: Option<Num>,
    #[serde(default)]
    pub delta: Option<Num>,
    #[serde(borrow, default)]
    pub side: Option<Cow<'a, str>>,
}

#[derive(Debug, Deserialize)]
pub struct KalshiFrame<'a> {
    #[serde(borrow, rename = "type", default)]
    pub kind: Option<Cow<'a, str>>,
    #[serde(borrow, default)]
    pub msg: Option<KalshiMsg<'a>>,
    #[serde(default)]
    pub sending_ts_ms: VenueMs,
}

impl KalshiMsg<'_> {
    /// Snapshot levels in dollars; older frames carried integer cents.
    pub fn levels(&self, yes: bool) -> Vec<(f64, f64)> {
        let (fp, dollars, cents) = if yes {
            (&self.yes_dollars_fp, &self.yes_dollars, &self.yes)
        } else {
            (&self.no_dollars_fp, &self.no_dollars, &self.no)
        };
        if let Some(v) = fp.as_ref().or(dollars.as_ref()) {
            return v.iter().map(|Pair(p, s)| (p.0, s.0)).collect();
        }
        cents
            .as_ref()
            .map(|v| v.iter().map(|Pair(p, s)| ((p.0 as i64) as f64 / 100.0, s.0)).collect())
            .unwrap_or_default()
    }

    pub fn delta(&self) -> Option<(f64, f64)> {
        let price = match (self.price_dollars, self.price) {
            (Some(p), _) => p.0,
            (None, Some(c)) => c.0 / 100.0,
            _ => return None,
        };
        let delta = self.delta_fp.or(self.delta)?.0;
        Some((price, delta))
    }
}

#[derive(Debug, Deserialize)]
pub struct Control<'a> {
    #[serde(borrow, rename = "type", default)]
    pub kind: Option<Cow<'a, str>>,
    #[serde(borrow, default)]
    pub venue: Option<Cow<'a, str>>,
    #[serde(default)]
    pub tokens: Option<Vec<serde_json::Value>>,
    #[serde(borrow, default)]
    pub asset: Option<Cow<'a, str>>,
    #[serde(default)]
    pub open_unix: Option<Num>,
    #[serde(borrow, default)]
    pub outcome: Option<Cow<'a, str>>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn poly_frames() {
        let one = r#"{"event_type":"price_change","market":"0x","timestamp":"1700","price_changes":[{"asset_id":"7","price":"0.45","size":"10","side":"BUY","best_bid":"0.45","best_ask":"0.46"}]}"#;
        let m = poly_msgs(one).unwrap();
        let m = m[0].as_ref().unwrap();
        assert_eq!(m.kind(), Some("price_change"));
        assert_eq!(m.timestamp.0, Some(1700.0));
        assert_eq!(m.price_changes.as_ref().unwrap()[0].asset_id.as_ref().unwrap().as_str(), "7");
        let list = r#"[{"asset_id":"1","bids":[{"price":"0.4","size":"3"}],"asks":[]}, 5]"#;
        let m = poly_msgs(list).unwrap();
        assert_eq!(m.len(), 2);
        assert_eq!(m[0].as_ref().unwrap().kind(), Some("book"));
        assert!(m[1].is_none());
        assert!(poly_msgs("{not json").is_err());
        let trade = r#"{"event_type":"last_trade_price","asset_id":"7","market":"0x","price":"0.45","size":"3.5","side":"SELL","timestamp":"1700"}"#;
        let m = poly_msgs(trade).unwrap();
        let m = m[0].as_ref().unwrap();
        assert_eq!(m.kind(), Some("last_trade_price"));
        assert_eq!(m.price.as_ref().unwrap().0, 0.45);
        assert_eq!(m.size.as_ref().unwrap().0, 3.5);
        assert_eq!(m.side.as_deref(), Some("SELL"));
    }

    #[test]
    fn kalshi_frames() {
        let snap = r#"{"type":"orderbook_snapshot","msg":{"market_ticker":"KX","yes":[[45,10]],"no_dollars_fp":[["0.5300","5.00"]]}}"#;
        let f: KalshiFrame = serde_json::from_str(snap).unwrap();
        let m = f.msg.unwrap();
        assert_eq!(m.levels(true), vec![(0.45, 10.0)]);
        assert_eq!(m.levels(false), vec![(0.53, 5.0)]);
        let d =
            r#"{"type":"orderbook_delta","msg":{"market_ticker":"KX","price":45,"delta":-3.5,"side":"yes"}}"#;
        let f: KalshiFrame = serde_json::from_str(d).unwrap();
        assert_eq!(f.msg.unwrap().delta(), Some((0.45, -3.5)));
    }
}
