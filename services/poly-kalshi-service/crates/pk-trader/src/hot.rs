//! The hot thread: the Kalshi feed and the armed trigger, alone on a core.
//!
//! A current-thread tokio runtime on its own OS thread, pinned to one core
//! (`PK_HOT_CORE`, default the last). The Kalshi websocket task is spawned on
//! it, so a frame is read, checked by the trigger and its order written without
//! leaving this thread, and without sharing it with the Polymarket shards, the
//! HTTP client or the API.
//!
//! With `PK_HOT_SPIN` (default on) the runtime never sleeps: a spinner task
//! yields on every turn, so the scheduler polls the socket for readiness with a
//! zero timeout instead of parking in `epoll_wait` until the kernel wakes it
//! (`event_interval(1)`: the driver is polled on every tick). That trades one
//! busy core for the wake-up latency (tens of µs, and worse at the tail).

use std::time::Duration;

use serde_json::{Value, json};

pub struct HotThread {
    pub handle: tokio::runtime::Handle,
    pub core: Option<usize>,
    pub pinned: bool,
    pub spin: bool,
}

impl HotThread {
    /// Start it: `core` `None` = the last core.
    pub fn start(core: Option<usize>, spin: bool) -> anyhow::Result<Self> {
        let ids = core_affinity::get_core_ids().unwrap_or_default();
        let core = core.or_else(|| ids.iter().map(|c| c.id).max());
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::Builder::new().name("pk-hot".into()).spawn(move || {
            let pinned = core
                .and_then(|c| ids.into_iter().find(|i| i.id == c))
                .is_some_and(core_affinity::set_for_current);
            let built = tokio::runtime::Builder::new_current_thread().enable_all().event_interval(1).build();
            let rt = match built {
                Ok(rt) => rt,
                Err(e) => {
                    let _ = tx.send(Err(anyhow::anyhow!("hot runtime: {e}")));
                    return;
                }
            };
            let _ = tx.send(Ok((rt.handle().clone(), pinned)));
            rt.block_on(async move {
                if spin {
                    loop {
                        tokio::task::yield_now().await;
                    }
                } else {
                    std::future::pending::<()>().await;
                }
            });
        })?;
        let (handle, pinned) = rx.recv_timeout(Duration::from_secs(5))??;
        tracing::info!(?core, pinned, spin, "hot_thread_started");
        Ok(Self { handle, core, pinned, spin })
    }

    pub fn stats(&self) -> Value {
        json!({"core": self.core, "pinned": self.pinned, "spin": self.spin})
    }
}
