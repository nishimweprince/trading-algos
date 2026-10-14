//! poly-kalshi's pure core. Nothing here touches the network or a clock: the
//! engine drives it with each line's receive time, so live and replay agree.

pub mod book;
pub mod decision;
pub mod exit;
pub mod fees;
pub mod journal;
pub mod leadlag;
pub mod ledger;
pub mod metrics;
pub mod num;
pub mod risk;
pub mod sizing;
pub mod window;

pub use book::{Book, BookSide, BookState, Quote, quote_json};
pub use decision::{Action, Decision, StrategyParams, WINDOW_S, decide, net_edge_max_px};
pub use ledger::{Ledger, Position};
pub use risk::{RiskLimits, RiskState};
pub use sizing::Sizer;
pub use window::{Window, window_open};

pub const NS: i64 = 1_000_000_000;
