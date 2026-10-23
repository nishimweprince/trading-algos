//! The venue feeds and window discovery. Transport and subscriptions only:
//! parsing is the engine's. Every frame goes to `emit(venue, text)` untouched,
//! so the tape holds exactly what arrived. LaserStream's protobuf frames are
//! the exception: they are decoded into compact lines (`laserstream`).

pub mod discovery;
pub mod kalshi_sign;
pub mod laserstream;
pub mod limitless;
pub mod okx;
pub mod ws;

pub use discovery::Discovery;
pub use kalshi_sign::KalshiSigner;
pub use laserstream::{LaserHandle, spawn_laserstream};
pub use limitless::{LimitlessFeed, spawn_limitless};
pub use okx::{OkxFeed, spawn_okx};
pub use ws::{Emit, FeedHandle, FeedKind, PolyShards, spawn_feed};
