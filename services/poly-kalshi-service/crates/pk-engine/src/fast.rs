//! What the engine hands the armed trigger, and how it learns what it fired.
//!
//! When an asset's gap nears the threshold the trade engine **arms** the
//! asset: the Polymarket quotes it sees now, the rule's parameters, and per side
//! the entry the rule would send (price, limit, size). A trigger outside the
//! engine (the trader's fast path) then runs [`pk_core::decide`] on each Kalshi
//! frame against those quotes and, on an entry, sends a pre-built order before
//! the frame reaches the engine. The engine stays the record: when it reaches
//! that frame, [`FastGate::fired`] gives it the entry, which it journals,
//! and reserves like its own (`"fast": true`), without sending it again.
//!
//! One order per window either way: the trigger and the engine both **claim**
//! the window before sending ([`FastGate::claim`]); whoever claims second does
//! not send. Replays have no gate, so they decide exactly as before.

use pk_core::{Quote, StrategyParams};

/// The entry the rule would send on one side, at the armed quotes.
#[derive(Clone, Debug, PartialEq)]
pub struct ArmSide {
    pub token: String,
    pub entry_px: f64,
    pub limit_px: f64,
    pub contracts: i64,
    /// What the ledger reserves for it.
    pub reserved: f64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Arm {
    pub asset: String,
    pub open_unix: i64,
    pub kalshi_ticker: String,
    pub condition_id: String,
    pub poly_yes: Quote,
    pub poly_no: Quote,
    pub params: StrategyParams,
    pub yes: Option<ArmSide>,
    pub no: Option<ArmSide>,
    pub fee_rate: f64,
    pub fee_exponent: f64,
}

#[derive(Clone, Debug, PartialEq)]
pub enum ArmUpdate {
    Arm(Arm),
    Disarm { kalshi_ticker: String },
}

/// An entry the trigger sent.
#[derive(Clone, Debug, PartialEq)]
pub struct FastFire {
    pub asset: String,
    pub open_unix: i64,
    pub buy_yes: bool,
    pub entry_px: f64,
    pub limit_px: f64,
    pub contracts: i64,
    pub gap: f64,
    /// The triggering Kalshi frame's arrival (the trigger's clock).
    pub t_ns: i64,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Claim {
    /// The engine may send.
    Engine,
    /// The trigger already sent this window's entry.
    Fast(FastFire),
}

pub trait FastGate: Send + Sync {
    /// The trigger's entry for this window, once (the engine records it).
    fn fired(&self, asset: &str, open_unix: i64) -> Option<FastFire>;
    /// Claim the window for an entry the engine is about to send.
    fn claim(&self, asset: &str, open_unix: i64) -> Claim;
}
