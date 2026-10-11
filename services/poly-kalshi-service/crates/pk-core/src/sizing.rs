//! Order size as a share of the balance, so wins compound and losses shrink it.
//!
//! The size is `pct` % of the balance, held in memory and **recomputed only at a
//! reconciliation that finds nothing at risk**: no open position and no order in
//! flight. Between those points every order uses the same size, so a window's
//! entry never depends on another window's unsettled result.
//!
//! `pct = 0` keeps the static `PK_NOTIONAL_USD` (research replays, old configs).

use serde::Serialize;

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Sizer {
    /// Percent of the balance per order; 0 = static size.
    pub pct: f64,
    /// The size every order uses until the next recompute (USD).
    pub notional: f64,
    /// The balance the current size was computed from.
    pub basis_balance: Option<f64>,
    pub updated_ns: Option<i64>,
}

impl Sizer {
    pub fn new(pct: f64, notional: f64) -> Self {
        Self { pct, notional, basis_balance: None, updated_ns: None }
    }

    pub fn enabled(&self) -> bool {
        self.pct > 0.0
    }

    /// `pct` % of `balance`, rounded down to the cent.
    pub fn size_for(&self, balance: f64) -> f64 {
        ((balance.max(0.0) * self.pct / 100.0) * 100.0 + 1e-9).floor() / 100.0
    }

    /// The new size if this reconciliation may set one: sizing is on and nothing
    /// is at risk. The size is kept either way; it only changes here.
    pub fn reconcile(
        &mut self,
        balance: f64,
        open_positions: usize,
        in_flight: usize,
        t_ns: i64,
    ) -> Option<f64> {
        if !self.enabled() || open_positions > 0 || in_flight > 0 {
            return None;
        }
        self.set(balance, t_ns)
    }

    /// Set the size from `balance` now (the startup seed, or a replayed `sizing`
    /// line). None if sizing is off.
    pub fn set(&mut self, balance: f64, t_ns: i64) -> Option<f64> {
        if !self.enabled() {
            return None;
        }
        self.notional = self.size_for(balance);
        self.basis_balance = Some(balance);
        self.updated_ns = Some(t_ns);
        Some(self.notional)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recomputes_only_when_flat() {
        let mut s = Sizer::new(5.0, 50.0);
        assert_eq!(s.reconcile(100.0, 1, 0, 1), None);
        assert_eq!(s.reconcile(100.0, 0, 1, 1), None);
        assert_eq!(s.notional, 50.0);
        assert_eq!(s.reconcile(100.0, 0, 0, 2), Some(5.0));
        assert_eq!((s.basis_balance, s.updated_ns), (Some(100.0), Some(2)));
        // Compounds: a bigger balance, a bigger size.
        assert_eq!(s.reconcile(123.45, 0, 0, 3), Some(6.17));
        assert_eq!(s.reconcile(-3.0, 0, 0, 4), Some(0.0));
    }

    #[test]
    fn static_when_off() {
        let mut s = Sizer::new(0.0, 50.0);
        assert_eq!(s.reconcile(1000.0, 0, 0, 1), None);
        assert_eq!(s.set(1000.0, 1), None);
        assert_eq!(s.notional, 50.0);
    }
}
