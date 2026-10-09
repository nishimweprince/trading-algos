//! Orders signed ahead of the decision.
//!
//! As an asset's gap approaches the threshold, the engine asks for orders at the
//! limit prices it is likely to use. When the decision comes, the hot path is a
//! lookup and one POST. A signed order can be posted once, so `take` removes it.
//! Entries older than `ttl` are dropped and re-signed.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use parking_lot::Mutex;
use serde::Serialize;

use crate::signing::{OrderSigner, OrderSpec, SignedBody};

pub struct PresignCache {
    ttl: Duration,
    max: usize,
    map: Mutex<HashMap<OrderSpec, SignedBody>>,
    hits: AtomicU64,
    misses: AtomicU64,
    signed: AtomicU64,
    expired: AtomicU64,
}

#[derive(Debug, Serialize)]
pub struct PresignStats {
    pub cached: usize,
    pub hits: u64,
    pub misses: u64,
    pub hit_rate: Option<f64>,
    pub signed_ahead: u64,
    pub expired: u64,
}

impl PresignCache {
    pub fn new(ttl: Duration, max: usize) -> Self {
        Self {
            ttl,
            max,
            map: Mutex::new(HashMap::new()),
            hits: AtomicU64::new(0),
            misses: AtomicU64::new(0),
            signed: AtomicU64::new(0),
            expired: AtomicU64::new(0),
        }
    }

    /// The signed order for exactly this spec, if one is cached and fresh.
    /// Counts a hit or a miss.
    pub fn take(&self, spec: &OrderSpec) -> Option<SignedBody> {
        let got = self.map.lock().remove(spec).filter(|s| s.signed_at.elapsed() < self.ttl);
        let counter = if got.is_some() { &self.hits } else { &self.misses };
        counter.fetch_add(1, Ordering::Relaxed);
        got
    }

    pub fn contains_fresh(&self, spec: &OrderSpec) -> bool {
        self.map.lock().get(spec).is_some_and(|s| s.signed_at.elapsed() < self.ttl)
    }

    pub fn insert(&self, signed: SignedBody) {
        let mut map = self.map.lock();
        if map.len() >= self.max {
            // Full: drop the oldest.
            if let Some(oldest) = map.iter().min_by_key(|(_, s)| s.signed_at).map(|(k, _)| k.clone()) {
                map.remove(&oldest);
            }
        }
        map.insert(signed.spec.clone(), signed);
        self.signed.fetch_add(1, Ordering::Relaxed);
    }

    /// Drop expired entries.
    pub fn prune(&self) {
        let ttl = self.ttl;
        let mut map = self.map.lock();
        let before = map.len();
        map.retain(|_, s| s.signed_at.elapsed() < ttl);
        self.expired.fetch_add((before - map.len()) as u64, Ordering::Relaxed);
    }

    /// Sign whatever of `specs` is not already cached and fresh. Off the hot path.
    pub async fn ensure<S: OrderSigner>(&self, signer: &S, specs: &[OrderSpec]) -> anyhow::Result<usize> {
        let mut n = 0;
        for spec in specs {
            if self.contains_fresh(spec) {
                continue;
            }
            self.insert(signer.sign(spec).await?);
            n += 1;
        }
        Ok(n)
    }

    pub fn stats(&self) -> PresignStats {
        let hits = self.hits.load(Ordering::Relaxed);
        let misses = self.misses.load(Ordering::Relaxed);
        PresignStats {
            cached: self.map.lock().len(),
            hits,
            misses,
            hit_rate: (hits + misses > 0).then(|| hits as f64 / (hits + misses) as f64),
            signed_ahead: self.signed.load(Ordering::Relaxed),
            expired: self.expired.load(Ordering::Relaxed),
        }
    }
}
