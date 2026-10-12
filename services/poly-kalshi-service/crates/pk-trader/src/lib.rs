//! The live processes.
//!
//! **`poly-kalshi-service`** (the trader) keeps only what an order depends on:
//! the feeds, the books, the live rule, the paper venue, the ledger, and the
//! order path (the probe, or live orders with their reconciler). Lines from every feed task are stamped from one strictly
//! increasing clock ([`ingest::Ingest`]) and handed to one engine thread, which
//! forwards each line to the observer pipe and then applies it.
//!
//! **`pk-observer`** (its child) runs everything else on the same lines: the
//! tape, the journal (the only writer), shadow variants, markouts, the series,
//! feed statistics and PolyBolt. If it dies, the trader keeps trading, writes
//! its own records straight to the journal, alerts, and restarts it.

pub mod api;
pub mod exec_bridge;
pub mod ingest;
pub mod link;
pub mod observer;
pub mod runtime;
pub mod telegram;

/// The current time in ns since the epoch.
pub fn now_ns() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as i64)
        .unwrap_or(0)
}

/// The engine both processes run, from the settings: `Trade` (the trader with
/// an observer), `Observe` (the observer) or `Full` (one process).
pub fn build_engine(
    settings: &pk_svc::Settings,
    role: pk_engine::Role,
    journal: Box<dyn pk_core::journal::Sink + Send>,
    ledger: pk_core::Ledger,
    risk: pk_core::RiskState,
) -> pk_engine::Engine {
    use pk_engine::{EngineConfig, PaperVenue, Role};
    let mut cfg = EngineConfig::new(settings.params.clone(), settings.assets.clone());
    cfg.role = role;
    let trades = role != Role::Observe;
    let live = settings.execution_mode == pk_svc::ExecutionMode::Live;
    // Live keeps the paper venue as the shadow of each real order.
    cfg.venue = (trades && settings.execution_mode != pk_svc::ExecutionMode::Off)
        .then(|| PaperVenue::new(settings.paper_latency_ms));
    cfg.live = trades && live;
    cfg.order_size_pct = settings.order_size_pct;
    cfg.follow_tape_latency = true;
    cfg.shadow = if role == Role::Trade {
        pk_core::shadow::Shadow::default()
    } else {
        pk_core::shadow::Shadow::new(settings.shadow_variants.clone(), &settings.params)
    };
    cfg.limit_offset = settings.limit_offset;
    cfg.size_to_depth = settings.size_to_depth;
    cfg.active_exit = trades && settings.active_exit;
    cfg.size_at_limit = settings.size_at_limit;
    cfg.shadow_latency_ns = (role != Role::Trade).then_some(settings.shadow_latency_ms * 1_000_000);
    cfg.age_outlier_ms = (trades && settings.age_outlier_ms > 0.0).then_some(settings.age_outlier_ms);
    cfg.emit_intents = trades && settings.order_path();
    let mut stamp = serde_json::Map::new();
    stamp.insert("epoch".into(), settings.results_epoch.clone().into());
    stamp.insert("build".into(), pk_svc::build::build_id().into());
    stamp.insert(
        "config".into(),
        pk_svc::build::config_hash_with(
            &settings.params,
            settings.paper_latency_ms,
            settings.limit_offset,
            settings.size_to_depth,
            settings.order_size_pct,
            &[("active_exit", settings.active_exit), ("size_at_limit", settings.size_at_limit)]
                .iter()
                .filter(|(_, on)| *on)
                .map(|(name, _)| *name)
                .collect::<Vec<_>>(),
        )
        .into(),
    );
    cfg.stamp = stamp;
    pk_engine::Engine::new(cfg, ledger, risk, journal)
}
