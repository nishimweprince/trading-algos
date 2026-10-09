//! Which code and which settings produced a record: stamped on every journal line.
//!
//! `build_id` is the git commit (`PK_BUILD` overrides it in a packaged deploy).
//! `config_hash` fingerprints the strategy parameters. With the results epoch
//! they let results be read only for the code and settings they came from.

use std::sync::OnceLock;

use pk_core::StrategyParams;
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
pub fn config_hash(
    params: &StrategyParams,
    latency_ms: i64,
    limit_offset: f64,
    size_to_depth: bool,
) -> String {
    let mut fields = serde_json::to_value(params).expect("params serialise");
    let obj = fields.as_object_mut().expect("object");
    obj.insert("paper_latency_ms".into(), latency_ms.into());
    if limit_offset != 0.0 {
        obj.insert("limit_offset".into(), limit_offset.into());
    }
    if size_to_depth {
        obj.insert("size_to_depth".into(), true.into());
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
        let a = config_hash(&p, 150, 0.0, false);
        assert_eq!(a.len(), 10);
        assert_eq!(a, config_hash(&p, 150, 0.0, false));
        assert_ne!(a, config_hash(&p, 100, 0.0, false));
        assert_ne!(a, config_hash(&StrategyParams { max_poly_spread: Some(0.01), ..p }, 150, 0.0, false));
    }
}
