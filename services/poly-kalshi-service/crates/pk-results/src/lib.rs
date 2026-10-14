//! Results from the journal, as the status page and the read-out show them.
//! Pure functions over journal records, so showing results costs the trading
//! process nothing. Field names match the Python service's, so the page reads
//! them unchanged; `tests/parity.rs` holds them to the Python numbers.

pub mod gates;
pub mod results;
pub mod stats;

pub use gates::evaluate;
pub use results::{open_trade_etas, paper_results};
