//! Research on the tapes, in Rust.
//!
//! - Our tape (`pk_*.zst`, and the Python service's `pk_*.gz`): [`replay`]
//!   (and `compare`), [`nulls`], [`readout`], [`fills`], [`inventory`],
//!   [`acceptance`], and the reverse direction ([`reverse`]).
//! - The Helix kpc tape (`.hlxz`): [`helix`].
//!
//! Outputs keep the Python service's names and fields (JSON, parquet), so
//! notebooks and `research/RESULTS.md` read them as before.

pub mod acceptance;
pub mod fills;
pub mod frame;
pub mod helix;
pub mod inventory;
pub mod nulls;
pub mod readout;
pub mod replay;
pub mod reverse;
