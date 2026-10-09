//! The one entry point for a line: stamp it from one strictly increasing
//! clock, in arrival order, and queue it for the engine thread.
//!
//! The stamp and the send happen under one lock, so queue order is stamp order
//! and the tape stays a faithful, ordered record of what the engine saw. The
//! feed tasks never wait on the engine: the queue is unbounded and its depth
//! and delay are measured.

use crossbeam_channel::{Receiver, Sender, unbounded};
use parking_lot::Mutex;

use crate::now_ns;

pub struct Line {
    pub recv_ns: i64,
    pub venue: char,
    pub text: String,
}

pub struct Ingest {
    state: Mutex<(i64, Sender<Line>)>,
}

impl Ingest {
    pub fn new() -> (Self, Receiver<Line>) {
        let (tx, rx) = unbounded();
        (Self { state: Mutex::new((0, tx)) }, rx)
    }

    pub fn push(&self, venue: char, text: String) {
        // The tape is one frame per line. A raw newline in valid JSON can only be
        // whitespace (inside strings it is escaped), so this loses nothing.
        let text = if text.contains(['\n', '\r']) {
            text.replace(['\r', '\n'], " ").trim().to_owned()
        } else {
            text
        };
        let mut g = self.state.lock();
        let recv_ns = (g.0 + 1).max(now_ns());
        g.0 = recv_ns;
        let _ = g.1.send(Line { recv_ns, venue, text });
    }

    pub fn queued(&self) -> usize {
        self.state.lock().1.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stamps_strictly_increase_in_order() {
        let (ingest, rx) = Ingest::new();
        for i in 0..1000 {
            ingest.push('p', format!("{{\"i\":{i}}}\n"));
        }
        let lines: Vec<Line> = rx.try_iter().collect();
        assert_eq!(lines.len(), 1000);
        assert!(lines.windows(2).all(|w| w[1].recv_ns > w[0].recv_ns));
        assert!(!lines[0].text.contains('\n'));
    }
}
