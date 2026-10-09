//! The paper cash ledger: buys debit cash, settlement **credits the payout back**.
//!
//! kpc's simulator debited every buy and booked realised P&L at settlement
//! without returning the $1/$0 payout to the wallet, so cash drained and every
//! order was then rejected with no alarm (kpc trap 4). Here cash is the only
//! balance, a settlement is a cash credit, and every reject is counted.
//!
//! State is written to `ledger.json` after every change, so open positions
//! survive a restart and settle when their resolution arrives.

use std::path::{Path, PathBuf};

use chrono::{DateTime, Utc};
use indexmap::IndexMap;
use serde::{Deserialize, Serialize};

use crate::decision::WINDOW_S;
use crate::num::round_dp;

pub fn utc_day(t_ns: i64) -> String {
    DateTime::<Utc>::from_timestamp_nanos(t_ns).format("%Y-%m-%d").to_string()
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Position {
    pub asset: String,
    pub open_unix: i64,
    /// "yes" | "no": the Polymarket leg held.
    pub side: String,
    pub shares: f64,
    /// USDC paid for the shares, excluding the fee.
    pub cost: f64,
    pub fee: f64,
    pub entry_ns: i64,
    #[serde(default = "open_status")]
    pub status: String,
    #[serde(default)]
    pub outcome: Option<String>,
    #[serde(default)]
    pub payout: Option<f64>,
    #[serde(default)]
    pub pnl: Option<f64>,
    #[serde(default)]
    pub settled_ns: Option<i64>,
}

fn open_status() -> String {
    "open".into()
}

impl Position {
    pub fn key(&self) -> String {
        format!("{}:{}", self.asset, self.open_unix)
    }

    pub fn vwap(&self) -> f64 {
        if self.shares != 0.0 { self.cost / self.shares } else { 0.0 }
    }

    pub fn is_open(&self) -> bool {
        self.status == "open"
    }
}

#[derive(Debug, Serialize, Deserialize)]
struct Stored {
    start_cash: f64,
    cash: f64,
    positions: IndexMap<String, Position>,
    #[serde(default)]
    realised_by_day: IndexMap<String, f64>,
    #[serde(default)]
    rejects: u64,
    #[serde(default)]
    epoch: Option<String>,
}

#[derive(Debug)]
pub struct Ledger {
    pub start_cash: f64,
    pub cash: f64,
    pub positions: IndexMap<String, Position>,
    pub realised_by_day: IndexMap<String, f64>,
    pub rejects: u64,
    pub path: Option<PathBuf>,
    pub epoch: Option<String>,
    pub save_errors: u64,
}

impl Ledger {
    pub fn new(start_cash: f64) -> Self {
        Self {
            start_cash,
            cash: start_cash,
            positions: IndexMap::new(),
            realised_by_day: IndexMap::new(),
            rejects: 0,
            path: None,
            epoch: None,
            save_errors: 0,
        }
    }

    /// Load the ledger; a different results epoch archives it and starts fresh.
    pub fn open(path: Option<&Path>, start_cash: f64, epoch: Option<&str>) -> anyhow::Result<Self> {
        let mut fresh = Self::new(start_cash);
        fresh.path = path.map(Path::to_path_buf);
        fresh.epoch = epoch.map(str::to_owned);
        let Some(path) = path.filter(|p| p.is_file()) else { return Ok(fresh) };
        let stored: Stored = serde_json::from_slice(&std::fs::read(path)?)?;
        if epoch.is_some() && stored.epoch.as_deref() != epoch {
            let old = stored.epoch.as_deref().unwrap_or("legacy");
            let archive = path.with_file_name(format!("ledger.epoch-{old}.json"));
            std::fs::rename(path, &archive)?;
            tracing::info!(archive = %archive.display(), new_epoch = ?epoch, "ledger_archived");
            return Ok(fresh);
        }
        let ledger = Self {
            start_cash: stored.start_cash,
            cash: stored.cash,
            positions: stored.positions,
            realised_by_day: stored.realised_by_day,
            rejects: stored.rejects,
            path: Some(path.to_path_buf()),
            epoch: stored.epoch,
            save_errors: 0,
        };
        tracing::info!(cash = ledger.cash, open_positions = ledger.open_positions().count(), "ledger_loaded");
        Ok(ledger)
    }

    pub fn save(&mut self) {
        let Some(path) = self.path.clone() else { return };
        let stored = Stored {
            start_cash: self.start_cash,
            cash: self.cash,
            positions: self.positions.clone(),
            realised_by_day: self.realised_by_day.clone(),
            rejects: self.rejects,
            epoch: self.epoch.clone(),
        };
        let write = || -> anyhow::Result<()> {
            if let Some(dir) = path.parent() {
                std::fs::create_dir_all(dir)?;
            }
            let tmp = path.with_extension("tmp");
            std::fs::write(&tmp, serde_json::to_vec_pretty(&stored)?)?;
            std::fs::rename(&tmp, &path)?;
            Ok(())
        };
        if let Err(e) = write() {
            self.save_errors += 1;
            tracing::error!(error = %e, "ledger save failed");
        }
    }

    pub fn open_positions(&self) -> impl Iterator<Item = &Position> {
        self.positions.values().filter(|p| p.is_open())
    }

    pub fn open_notional(&self) -> f64 {
        self.open_positions().map(|p| p.cost + p.fee).sum()
    }

    pub fn realised_today(&self, t_ns: i64) -> f64 {
        self.realised_by_day.get(&utc_day(t_ns)).copied().unwrap_or(0.0)
    }

    pub fn can_afford(&self, amount: f64) -> bool {
        amount <= self.cash + 1e-9
    }

    pub fn reject(&mut self, reason: &str, asset: &str, cost: f64) {
        self.rejects += 1;
        tracing::warn!(reason, asset, cost, "ledger_reject");
        self.save();
    }

    pub fn buy(&mut self, position: Position) -> anyhow::Result<()> {
        let key = position.key();
        anyhow::ensure!(!self.positions.contains_key(&key), "second position in one window: {key}");
        self.cash -= position.cost + position.fee;
        self.positions.insert(key, position);
        self.save();
        Ok(())
    }

    /// Credit the payout to cash. Idempotent: a settled position is left alone.
    pub fn settle(&mut self, asset: &str, open_unix: i64, outcome: &str, t_ns: i64) -> Option<Position> {
        let position = self.positions.get_mut(&format!("{asset}:{open_unix}"))?;
        if !position.is_open() {
            return None;
        }
        let payout = if position.side == outcome { position.shares } else { 0.0 };
        let pnl = payout - position.cost - position.fee;
        position.payout = Some(payout);
        position.pnl = Some(pnl);
        position.outcome = Some(outcome.to_owned());
        position.status = "settled".into();
        position.settled_ns = Some(t_ns);
        let settled = position.clone();
        self.cash += payout;
        *self.realised_by_day.entry(utc_day(t_ns)).or_insert(0.0) += pnl;
        self.save();
        Some(settled)
    }

    pub fn snapshot(&self, t_ns: i64) -> serde_json::Value {
        let open: Vec<&Position> = self.open_positions().collect();
        let settled_pnl: f64 =
            self.positions.values().filter(|p| !p.is_open()).map(|p| p.pnl.unwrap_or(0.0)).sum();
        let settled = self.positions.values().filter(|p| !p.is_open()).count();
        let oldest = open.iter().map(|p| p.open_unix).min();
        let oldest_entry = open.iter().map(|p| p.entry_ns).min();
        let notional = self.open_notional();
        serde_json::json!({
            "cash": round_dp(self.cash, 2),
            "start_cash": self.start_cash,
            "open_positions": open.len(),
            "open_notional": round_dp(notional, 2),
            "equity_at_cost": round_dp(self.cash + notional, 2),
            "realised_today": round_dp(self.realised_today(t_ns), 2),
            "realised_total": round_dp(settled_pnl, 2),
            "settled": settled,
            "rejects": self.rejects,
            "largest_open_cost": round_dp(open.iter().map(|p| p.cost + p.fee).fold(0.0, f64::max), 2),
            "oldest_open_age_s": oldest.map(|o| (round_dp(t_ns as f64 / 1e9 - (o + WINDOW_S) as f64, 0) as i64).max(0)),
            "oldest_entry_age_s": oldest_entry.map(|e| (round_dp((t_ns - e) as f64 / 1e9, 0) as i64).max(0)),
            "realised_basis": "settle_day",
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pos(side: &str) -> Position {
        Position {
            asset: "BTC".into(),
            open_unix: 900,
            side: side.into(),
            shares: 100.0,
            cost: 46.0,
            fee: 1.7,
            entry_ns: 1,
            status: "open".into(),
            outcome: None,
            payout: None,
            pnl: None,
            settled_ns: None,
        }
    }

    #[test]
    fn payout_is_credited() {
        let mut l = Ledger::new(1000.0);
        l.buy(pos("yes")).unwrap();
        assert!((l.cash - 952.3).abs() < 1e-9);
        assert!(l.buy(pos("yes")).is_err());
        let p = l.settle("BTC", 900, "yes", 2_000_000_000).unwrap();
        assert!((p.pnl.unwrap() - 52.3).abs() < 1e-9);
        assert!((l.cash - 1052.3).abs() < 1e-9);
        assert!(l.settle("BTC", 900, "yes", 3).is_none());
        assert!((l.realised_today(2_000_000_000) - 52.3).abs() < 1e-9);
    }

    #[test]
    fn persists_and_archives_by_epoch() {
        let dir = std::env::temp_dir().join(format!("pk-ledger-{}", std::process::id()));
        let path = dir.join("ledger.json");
        let mut l = Ledger::open(Some(&path), 100.0, Some("6")).unwrap();
        l.buy(pos("no")).unwrap();
        let again = Ledger::open(Some(&path), 100.0, Some("6")).unwrap();
        assert_eq!(again.open_positions().count(), 1);
        let fresh = Ledger::open(Some(&path), 100.0, Some("7")).unwrap();
        assert_eq!(fresh.positions.len(), 0);
        assert!(dir.join("ledger.epoch-6.json").exists());
        std::fs::remove_dir_all(dir).ok();
    }
}
