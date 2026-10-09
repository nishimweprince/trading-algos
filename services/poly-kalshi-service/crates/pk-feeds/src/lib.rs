//! The venue feeds and window discovery. Transport and subscriptions only:
//! parsing is the engine's. Every frame goes to `emit(venue, text)` untouched,
//! so the tape holds exactly what arrived.

pub mod discovery;
pub mod kalshi_sign;
pub mod ws;

pub use discovery::Discovery;
pub use kalshi_sign::KalshiSigner;
pub use ws::{Emit, FeedHandle, FeedKind, PolyShards, spawn_feed};
