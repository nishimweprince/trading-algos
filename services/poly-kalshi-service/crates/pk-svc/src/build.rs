//! Which code and which settings produced a record: stamped on every journal line.
//!
//! `build_id` is the git commit (`PK_BUILD` overrides it in a packaged deploy).
//! `config_hash` fingerprints the strategy parameters. With the results epoch
//! they let results be read only for the code and settings they came from.

use std::sync::OnceLock;

use pk_core::StrategyParams;
use serde_json::Value;
use sha2::{Digest, Sha256};

pub fn build_id() -> String {
    static ID: OnceLock<String> = OnceLock::new();
    ID.get_or_init(|| {
        if let Some(b) = std::env::var("PK_BUILD").ok().filter(|b| !b.is_empty()) {
            return b;
        }
        if let Some(b) = option_env!("PK_GIT_COMMIT").filter(|b| !b.is_empty()) {
            return b.to_owned();
        }
        std::process::Command::new("git")
            .args(["rev-parse", "--short=12", "HEAD"])
            .output()
            .ok()
            .filter(|o| o.status.success())
            .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_owned())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "unknown".into())
    })
    .clone()
}

/// Unset optional parameters are left out, so adding one does not change the
/// hash of every configuration that does not use it.
///
/// With percentage sizing the order size moves with the balance, so the hash
/// takes the percentage instead of `notional_usd`; the latency is the seed. The
/// values in force are journaled as they change (`sizing`, `latency`).
pub fn config_hash(
    params: &StrategyParams,
    latency_ms: i64,
    limit_offset: f64,
    size_to_depth: bool,
    order_size_pct: f64,
) -> String {
    config_hash_with(params, latency_ms, limit_offset, size_to_depth, order_size_pct, &[])
}

/// [`config_hash`] plus the settings that are on: switches (`active_exit`,
/// `size_at_limit`) as `true`, sizes (`order_shares`, `active_exit_ticks`) as numbers.
/// A fixed share count replaces the notional and the percentage in the hash.
pub fn config_hash_with(
    params: &StrategyParams,
    latency_ms: i64,
    limit_offset: f64,
    size_to_depth: bool,
    order_size_pct: f64,
    on: &[(&str, Value)],
) -> String {
    let mut fields = serde_json::to_value(params).expect("params serialise");
    let obj = fields.as_object_mut().expect("object");
    obj.insert("paper_latency_ms".into(), latency_ms.into());
    if order_size_pct > 0.0 {
        obj.remove("notional_usd");
        obj.insert("order_size_pct".into(), order_size_pct.into());
    }
    if limit_offset != 0.0 {
        obj.insert("limit_offset".into(), limit_offset.into());
    }
    if size_to_depth {
        obj.insert("size_to_depth".into(), true.into());
    }
    for (name, value) in on {
        if *name == "order_shares" {
            obj.remove("notional_usd");
            obj.remove("order_size_pct");
        }
        obj.insert((*name).into(), value.clone());
    }
    let sorted: std::collections::BTreeMap<_, _> = obj.iter().collect();
    let body = serde_json::to_string(&sorted).expect("json");
    let digest = Sha256::digest(body.as_bytes());
    digest.iter().take(5).map(|b| format!("{b:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hash_is_stable_and_sensitive() {
        let p = StrategyParams::default();
        let a = config_hash(&p, 150, 0.0, false, 0.0);
        assert_eq!(a.len(), 10);
        assert_eq!(a, config_hash(&p, 150, 0.0, false, 0.0));
        assert_ne!(a, config_hash(&p, 100, 0.0, false, 0.0));
        assert_ne!(
            a,
            config_hash(&StrategyParams { max_poly_spread: Some(0.01), ..p.clone() }, 150, 0.0, false, 0.0)
        );
        let pct = config_hash(&p, 150, 0.0, false, 5.0);
        assert_ne!(a, pct);
        // The size moves with the balance; the hash does not.
        assert_eq!(pct, config_hash(&StrategyParams { notional_usd: 7.0, ..p.clone() }, 150, 0.0, false, 5.0));
        // Fixed shares: neither the notional nor the percentage counts.
        let shares = |n: f64, pct: f64| {
            config_hash_with(&StrategyParams { notional_usd: n, ..p.clone() }, 150, 0.0, false, pct, &[(
                "order_shares",
                10.into(),
            )])
        };
        assert_eq!(shares(7.0, 5.0), shares(3.0, 0.0));
        assert_ne!(shares(7.0, 5.0), pct);
    }
}
