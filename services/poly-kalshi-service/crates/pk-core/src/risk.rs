//! Hard limits on entries: open notional, daily loss, the kill switch.
//!
//! A halt (`/kill`, `POST /v1/kill` or the kill file) blocks new entries until
//! acknowledged; it never stops recording or decision logging, since a blocked
//! decision is still evidence. Nothing is closed early by a halt: positions are
//! held to settlement. Live, the trader also cancels every open order, and an
//! operator can flatten explicitly (`POST /v1/flatten`, Telegram `/flatten`).
//!
//! The daily loss limit reads both the realised P&L booked by the ledger and,
//! live, the wallet's equity drawdown since the day's first reconciliation.
//!
//! Two caps sized to a small wallet (epoch 16): at most `max_open_positions`
//! open or in flight, and an [`EntryThrottle`] of at most N entries in any
//! rolling window, shared with the fast path so a burst across correlated
//! assets (10-22 05:04Z: six entries in 30 s on a dead Polymarket feed) cannot
//! slip through on two threads.

use std::collections::VecDeque;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use serde::Serialize;

use crate::ledger::Ledger;

#[derive(Clone, Debug)]
pub struct RiskLimits {
    pub max_open_notional_usd: f64,
    pub daily_loss_limit_usd: f64,
    pub kill_file: Option<PathBuf>,
}

#[derive(Debug)]
pub struct RiskState {
    pub limits: RiskLimits,
    pub halted: bool,
    pub halt_reason: Option<String>,
    /// Open positions plus orders in flight; `None` = no cap.
    pub max_open_positions: Option<usize>,
    pub throttle: Option<Arc<EntryThrottle>>,
}

/// At most `max` entries in any `window_ns`, across the engine and the fast path.
#[derive(Debug)]
pub struct EntryThrottle {
    pub max: usize,
    pub window_ns: i64,
    times: Mutex<VecDeque<i64>>,
}

impl EntryThrottle {
    pub fn new(max: usize, window_ns: i64) -> Self {
        Self { max: max.max(1), window_ns, times: Mutex::new(VecDeque::new()) }
    }

    fn prune(&self, q: &mut VecDeque<i64>, now_ns: i64) {
        while q.front().is_some_and(|t| now_ns - *t >= self.window_ns) {
            q.pop_front();
        }
    }

    /// Whether an entry now would be refused (nothing is taken).
    pub fn full(&self, now_ns: i64) -> bool {
        let mut q = self.times.lock().unwrap_or_else(|e| e.into_inner());
        self.prune(&mut q, now_ns);
        q.len() >= self.max
    }

    /// Take a slot for an entry sent now; false if the window is full.
    pub fn try_take(&self, now_ns: i64) -> bool {
        let mut q = self.times.lock().unwrap_or_else(|e| e.into_inner());
        self.prune(&mut q, now_ns);
        if q.len() >= self.max {
            return false;
        }
        q.push_back(now_ns);
        true
    }
}

#[derive(Debug, Serialize)]
pub struct RiskSnapshot {
    pub halted: bool,
    pub halt_reason: Option<String>,
    pub kill_file_present: bool,
    pub max_open_notional_usd: f64,
    pub daily_loss_limit_usd: f64,
}

impl RiskState {
    pub fn new(limits: RiskLimits) -> Self {
        Self { limits, halted: false, halt_reason: None, max_open_positions: None, throttle: None }
    }

    pub fn with_entry_caps(mut self, max_open_positions: Option<usize>, throttle: Option<Arc<EntryThrottle>>) -> Self {
        self.max_open_positions = max_open_positions;
        self.throttle = throttle;
        self
    }

    /// Halt; true if this call changed the state.
    pub fn kill(&mut self, source: &str, reason: &str) -> bool {
        let fired = !self.halted;
        self.halted = true;
        let r = format!("{source}: {reason}");
        self.halt_reason = Some(r.trim_matches(|c| c == ':' || c == ' ').to_owned());
        tracing::warn!(source, reason, fired, "risk_halt");
        fired
    }

    pub fn kill_file_present(&self) -> bool {
        self.limits.kill_file.as_ref().is_some_and(|p| p.exists())
    }

    pub fn ack(&mut self) -> (bool, String) {
        if self.kill_file_present() {
            let path = self.limits.kill_file.as_ref().map(|p| p.display().to_string()).unwrap_or_default();
            return (false, format!("kill file {path} still exists"));
        }
        self.halted = false;
        self.halt_reason = None;
        tracing::info!("risk_ack");
        (true, "cleared".into())
    }

    /// None if the entry may go ahead, else why not.
    pub fn check(&mut self, ledger: &Ledger, order_cost: f64, t_ns: i64) -> Option<&'static str> {
        if self.kill_file_present() && !self.halted {
            let path = self.limits.kill_file.as_ref().map(|p| p.display().to_string()).unwrap_or_default();
            self.kill("kill_file", &path);
        }
        if self.halted {
            return Some("halted");
        }
        if ledger.realised_today(t_ns) <= -self.limits.daily_loss_limit_usd {
            return Some("daily_loss_limit");
        }
        // Live: the wallet's own drawdown since the day's first reconciliation,
        // so a loss the ledger did not book (a fee, a missed fill) still halts.
        if ledger.day_drawdown(t_ns).is_some_and(|d| d >= self.limits.daily_loss_limit_usd) {
            return Some("daily_loss_limit");
        }
        if ledger.open_notional() + order_cost > self.limits.max_open_notional_usd + 1e-9 {
            return Some("max_open_notional");
        }
        if self.max_open_positions.is_some_and(|m| ledger.open_positions().count() + ledger.in_flight() >= m) {
            return Some("max_open_positions");
        }
        if self.throttle.as_ref().is_some_and(|t| t.full(t_ns)) {
            return Some("entry_burst");
        }
        if !ledger.can_afford(order_cost) {
            return Some("insufficient_cash");
        }
        None
    }

    pub fn snapshot(&self) -> RiskSnapshot {
        RiskSnapshot {
            halted: self.halted,
            halt_reason: self.halt_reason.clone(),
            kill_file_present: self.kill_file_present(),
            max_open_notional_usd: self.limits.max_open_notional_usd,
            daily_loss_limit_usd: self.limits.daily_loss_limit_usd,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wallet_drawdown_halts() {
        let mut r = RiskState::new(RiskLimits {
            max_open_notional_usd: 100.0,
            daily_loss_limit_usd: 10.0,
            kill_file: None,
        });
        let mut l = Ledger::new(100.0);
        let t = 1_000_000_000;
        assert_eq!(r.check(&l, 5.0, t), None);
        l.set_cash(100.0, t);
        l.set_cash(91.0, t);
        assert_eq!(r.check(&l, 5.0, t), None);
        l.set_cash(90.0, t);
        assert_eq!(r.check(&l, 5.0, t), Some("daily_loss_limit"));
        l.reserve("BTC:0", 96.0);
        l.set_cash(100.0, t);
        assert_eq!(r.check(&l, 5.0, t), Some("max_open_notional"));
    }

    #[test]
    fn position_cap_and_burst() {
        let throttle = Arc::new(EntryThrottle::new(2, 30_000_000_000));
        let mut r = RiskState::new(RiskLimits {
            max_open_notional_usd: 1e9,
            daily_loss_limit_usd: 1e9,
            kill_file: None,
        })
        .with_entry_caps(Some(2), Some(throttle.clone()));
        let mut l = Ledger::new(1000.0);
        let t = 1_000_000_000;
        assert_eq!(r.check(&l, 5.0, t), None);
        l.reserve("BTC:0", 5.0);
        l.reserve("ETH:0", 5.0);
        assert_eq!(r.check(&l, 5.0, t), Some("max_open_positions"));
        l.release("ETH:0");
        assert!(throttle.try_take(t) && throttle.try_take(t + 1));
        assert!(!throttle.try_take(t + 2));
        assert_eq!(r.check(&l, 5.0, t + 2), Some("entry_burst"));
        // The window rolls.
        assert_eq!(r.check(&l, 5.0, t + 30_000_000_001), None);
    }
}
