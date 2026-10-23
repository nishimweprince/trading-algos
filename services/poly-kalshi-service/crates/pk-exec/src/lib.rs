//! Where an entry goes.
//!
//! - [`clob`]: the timed order path to Polymarket's CLOB V2 on a warm HTTP/2
//!   connection ([`transport`]), with L2 auth ([`auth`]), orders signed by
//!   Polymarket's own SDK ([`signing`]) and signed ahead ([`presign`]).
//! - [`probe`]: real requests on that path that cannot fill, to measure
//!   decision → acknowledgement; [`latency`] turns those round trips into the
//!   paper venue's latency.
//! - [`live`]: real entries (FAK buys at the limit) and flatten sells, with
//!   fill polling; [`redeem`]: settled positions back into pUSD, from the EOA
//!   or ([`relayer`]) from a deposit wallet.
//!
//! The live path is reachable only from `PK_EXECUTION_MODE=live`, which the
//! settings refuse without `PK_LIVE_CONFIRM=yes`. The probe still only sends
//! post-only orders below the best bid.

pub mod auth;
pub mod clob;
pub mod fast;
pub mod gamma;
pub mod latency;
pub mod limitless;
pub mod live;
pub mod presign;
pub mod probe;
pub mod redeem;
pub mod relayer;
pub mod signing;
pub mod transport;

pub use clob::{ClobExec, OrderTiming, RawReply, Top, Trigger};
pub use fast::{FastReply, FastSender, SendError};
pub use latency::{LatencyEstimator, LatencyNow};
pub use live::{Executed, LiveExec, LiveLimits};
pub use probe::{Probe, ProbeLimits};
pub use redeem::{CtfRedeemer, Redeem, RedeemReceipt};
pub use relayer::{RelayerConfig, WalletRedeemer};
pub use signing::{OrderSigner, OrderSpec, SdkSigner, Side, SignedBody, Tif, WalletConfig};
pub use transport::{WarmConfig, WarmHttp};
