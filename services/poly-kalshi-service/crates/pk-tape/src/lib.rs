//! The tape: every frame from every venue, as received, one line each:
//!
//! ```text
//! <recv_ns> <venue> <raw frame>
//! ```
//!
//! **One file per UTC hour for all venues**, zstd-compressed:
//!
//! ```text
//! <root>/<YYYYMMDD>/pk_<YYYYMMDD>_<HH>_<session>.zst
//! <root>/<YYYYMMDD>/pk_<YYYYMMDD>_<HH>_<session>.zst.json   (manifest)
//! ```
//!
//! `session` is the writer's start in ns, zero-padded: a restart within the
//! hour starts a new file. One writer stamps every line from one clock, so file
//! order is arrival order and a replay never merges (kpc trap 7). The lines are
//! text so `zstdcat | grep` works; zstd replaces the Python service's gzip
//! (faster at level 1 and smaller). The encoder is flushed every second, so a
//! crash loses at most that, and the reader keeps everything up to the break.
//!
//! The reader also reads the Python service's `pk_*.gz` tapes.

pub mod reader;
pub mod writer;

pub use reader::{LineStats, TapeLine, TapeReader, iter_files, read_lines};
pub use writer::{Recorder, RecorderStats};
