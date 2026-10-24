//! The maker arm: paper quotes on every Limitless crypto mirror of Polymarket.

pub mod engine;
pub mod markets;

pub use engine::{Maker, MakerCfg};
pub use markets::{parse_active, parse_poly, parse_prints};
