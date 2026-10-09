//! Where an entry goes.
//!
//! - [`clob`]: the timed order path to Polymarket's CLOB V2 on a warm HTTP/2
//!   connection ([`transport`]), with L2 auth ([`auth`]), orders signed by
//!   Polymarket's own SDK ([`signing`]) and signed ahead ([`presign`]).
//! - [`probe`]: real requests on that path that cannot fill, to measure
//!   decision → acknowledgement before any live order exists.
//!
//! Live entries stay refused: nothing here is reachable from
//! `PK_EXECUTION_MODE=live`, and the probe only sends post-only orders below
//! the best bid.

pub mod auth;
pub mod clob;
pub mod gamma;
pub mod presign;
pub mod probe;
pub mod signing;
pub mod transport;

pub use clob::{ClobExec, OrderTiming, Top, Trigger};
pub use probe::{Probe, ProbeLimits};
pub use signing::{OrderSigner, OrderSpec, SdkSigner, Side, SignedBody, Tif, WalletConfig};
pub use transport::{WarmConfig, WarmHttp};
