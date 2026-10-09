//! The trader's real order path: every paper entry is mirrored by a probe on
//! the same token at the same moment (`pk_exec::probe`: post-only, under the
//! best bid, cancelled at once), so decision → acknowledgement is measured on
//! the decisions that matter. Paper stays the venue of record.
//!
//! The engine thread hands over [`OrderIntent`]s with the instant it decided,
//! and [`PresignHint`]s as a gap nears the threshold, so the probe order is
//! usually signed before the decision and the hot path is one POST.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use parking_lot::Mutex;
use pk_engine::{OrderIntent, PresignHint};
use pk_exec::presign::PresignCache;
use pk_exec::{
    ClobExec, OrderTiming, Probe, ProbeLimits, SdkSigner, Top, Trigger, WalletConfig, WarmConfig, WarmHttp,
};
use rust_decimal::Decimal;
use rust_decimal::prelude::FromPrimitive;
use serde_json::{Value, json};
use tokio::sync::mpsc;

pub enum ExecMsg {
    Intent(OrderIntent, Instant),
    Hint(PresignHint),
}

/// Writes a record to the journal (through the observer when it is up).
pub type JournalFn = Arc<dyn Fn(i64, Value) + Send + Sync>;

pub struct ExecBridge {
    pub probe: Arc<Probe<SdkSigner>>,
    meta: Mutex<HashMap<String, (Decimal, Decimal)>>,
    journal: JournalFn,
    pub refused: std::sync::atomic::AtomicU64,
}

fn dec(x: Option<f64>) -> Option<Decimal> {
    x.and_then(Decimal::from_f64).map(|d| d.round_dp(4))
}

impl ExecBridge {
    pub async fn connect(wallet: &WalletConfig, journal: JournalFn) -> anyhow::Result<Arc<Self>> {
        let signer = Arc::new(SdkSigner::connect(wallet).await?);
        tracing::info!(address = %signer.address(), "order path authenticated");
        let http = WarmHttp::connect(WarmConfig::new(wallet.clob_url.parse()?)).await?;
        http.spawn_maintenance();
        let exec = Arc::new(ClobExec::new(http, signer, PresignCache::new(Duration::from_secs(10), 64)));
        let probe = Arc::new(Probe::new(exec, ProbeLimits { max_open: 2, ..ProbeLimits::default() }));
        Ok(Arc::new(Self { probe, meta: Mutex::new(HashMap::new()), journal, refused: Default::default() }))
    }

    /// The tick and minimum size of a token (one REST read, then cached).
    async fn meta(&self, token: &str) -> Option<(Decimal, Decimal)> {
        if let Some(m) = self.meta.lock().get(token) {
            return Some(*m);
        }
        match self.probe.exec.top(token).await {
            Ok(t) => {
                self.meta.lock().insert(token.to_owned(), (t.tick, t.min_size));
                Some((t.tick, t.min_size))
            }
            Err(e) => {
                tracing::warn!(token, error = %e, "order path: book meta");
                None
            }
        }
    }

    async fn top(&self, token: &str, bid: Option<f64>, ask: Option<f64>) -> Option<Top> {
        let (tick, min_size) = self.meta(token).await?;
        Some(Top { best_bid: dec(bid), best_ask: dec(ask), tick, min_size })
    }

    fn record(&self, t: &OrderTiming, asset: &str, open_unix: i64) {
        let mut v = serde_json::to_value(t).unwrap_or(Value::Null);
        if let Some(o) = v.as_object_mut() {
            o.insert("asset".into(), asset.into());
            o.insert("open_unix".into(), open_unix.into());
        }
        (self.journal)(t.t_ns as i64, v);
    }

    pub async fn run(self: Arc<Self>, mut rx: mpsc::UnboundedReceiver<ExecMsg>) {
        while let Some(msg) = rx.recv().await {
            match msg {
                ExecMsg::Hint(h) => {
                    if let Some(top) = self.top(&h.token, h.best_bid, h.best_ask).await
                        && let Err(e) = self.probe.presign(&h.token, &top).await
                    {
                        tracing::warn!(error = %e, "order path: presign");
                    }
                }
                ExecMsg::Intent(i, t_decision) => {
                    let Some(top) = self.top(&i.token, i.best_bid, i.best_ask).await else { continue };
                    let out = self.probe.run_once(&i.token, &top, t_decision, Trigger::Decision).await;
                    if out.refused.is_some() {
                        self.refused.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                    }
                    for t in &out.records {
                        self.record(t, &i.asset, i.open_unix);
                    }
                }
            }
            self.probe.exec.presign.prune();
        }
    }

    pub async fn shutdown(&self) {
        for t in self.probe.cancel_everything().await {
            self.record(&t, "", 0);
        }
    }

    pub fn stats(&self) -> Value {
        json!({
            "enabled": true,
            "exec": self.probe.exec.stats(),
            "probe": &*self.probe.counters.lock(),
            "open_orders": self.probe.open_orders(),
            "refused": self.refused.load(std::sync::atomic::Ordering::Relaxed),
        })
    }
}
