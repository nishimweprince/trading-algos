//! Hard limits on entries: open notional, daily loss, the kill switch.
//!
//! A halt (`/kill`, `POST /v1/kill` or the kill file) blocks new entries until
//! acknowledged; it never stops recording or decision logging, since a blocked
//! decision is still evidence. Nothing is closed early: there is nothing to
//! flatten in a hold-to-settlement strategy.

use std::path::PathBuf;

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
        Self { limits, halted: false, halt_reason: None }
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
        if ledger.open_notional() + order_cost > self.limits.max_open_notional_usd + 1e-9 {
            return Some("max_open_notional");
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
