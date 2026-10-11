//! Plumbing shared by `poly-kalshi-service`, `pk-observer`, `pk-status` and
//! `pk-unit-alert`.

pub mod alerts;
pub mod api;
pub mod build;
pub mod config;
pub mod logging;
pub mod tls;

pub use alerts::{Alerts, Notifier};
pub use config::{ExecutionMode, Settings};
