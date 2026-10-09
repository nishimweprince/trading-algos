//! A warm HTTP/2 connection to one host.
//!
//! The order path must never pay for DNS, TCP or TLS. So:
//! - the host is resolved at startup and re-resolved off the hot path every
//!   `dns_every`; requests use the pinned addresses, never the system resolver;
//! - one HTTP/2 connection is kept open with protocol PINGs (`keepalive`) and a
//!   cheap warm GET (`warm_path`, e.g. `/time`) every `warm_every`, whose round
//!   trip is recorded;
//! - when the address set changes, a new client is built and warmed *before* it
//!   replaces the old one, so the swap itself costs the hot path nothing.

use std::net::SocketAddr;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use parking_lot::{Mutex, RwLock};
use pk_stats::Hist;
use reqwest::{Client, Method, Response, Version};
use serde::Serialize;
use url::Url;

pub const USER_AGENT: &str = "poly-kalshi/0.2 (+trading-algos)";

#[derive(Clone, Debug)]
pub struct WarmConfig {
    pub base: Url,
    pub warm_path: String,
    pub warm_every: Duration,
    pub keepalive: Duration,
    pub dns_every: Duration,
    pub timeout: Duration,
}

impl WarmConfig {
    pub fn new(base: Url) -> Self {
        Self {
            base,
            warm_path: "/time".into(),
            warm_every: Duration::from_secs(2),
            keepalive: Duration::from_secs(5),
            dns_every: Duration::from_secs(60),
            timeout: Duration::from_secs(5),
        }
    }
}

#[derive(Default)]
struct Counters {
    warm_errors: AtomicU64,
    dns_changes: AtomicU64,
    dns_errors: AtomicU64,
    non_h2: AtomicU64,
    requests: AtomicU64,
}

pub struct WarmHttp {
    cfg: WarmConfig,
    host: String,
    client: RwLock<Client>,
    addrs: Mutex<Vec<SocketAddr>>,
    built: Mutex<Instant>,
    warm_rtt_us: Mutex<Hist>,
    counters: Counters,
}

#[derive(Debug, Serialize)]
pub struct WarmStats {
    pub host: String,
    pub addrs: Vec<String>,
    pub client_age_s: u64,
    pub warm_rtt_ms: pk_stats::SummaryF,
    pub warm_errors: u64,
    pub dns_changes: u64,
    pub dns_errors: u64,
    pub non_h2: u64,
    pub requests: u64,
}

async fn resolve(host: &str, port: u16) -> std::io::Result<Vec<SocketAddr>> {
    let mut addrs: Vec<SocketAddr> = tokio::net::lookup_host((host, port)).await?.collect();
    addrs.sort();
    addrs.dedup();
    Ok(addrs)
}

impl WarmHttp {
    /// Resolve, build and warm. Fails if the host can't be resolved or reached once.
    pub async fn connect(cfg: WarmConfig) -> anyhow::Result<Arc<Self>> {
        let host = cfg.base.host_str().ok_or_else(|| anyhow::anyhow!("no host in {}", cfg.base))?.to_owned();
        let port = cfg.base.port_or_known_default().unwrap_or(443);
        let addrs = match resolve(&host, port).await {
            Ok(a) => a,
            // Behind an explicit proxy (a sandbox, a corporate egress) the proxy
            // resolves; the connection can't be pinned and the stats say so.
            Err(e) if behind_proxy() => {
                tracing::warn!(%host, error = %e, "no local DNS; going through the proxy unpinned");
                Vec::new()
            }
            Err(e) => return Err(e.into()),
        };
        let client = build(&cfg, &host, &addrs)?;
        let this = Arc::new(Self {
            host,
            client: RwLock::new(client),
            addrs: Mutex::new(addrs),
            built: Mutex::new(Instant::now()),
            warm_rtt_us: Mutex::new(Hist::new()),
            counters: Counters::default(),
            cfg,
        });
        // The first request opens the connection: its time is not a round trip.
        let current = this.client();
        this.warm_once(&current, false).await?;
        Ok(this)
    }

    /// Start the keep-warm and DNS refresh loops; they stop when `self` is dropped.
    pub fn spawn_maintenance(self: &Arc<Self>) {
        let weak = Arc::downgrade(self);
        let every = self.cfg.warm_every;
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(every);
            tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            loop {
                tick.tick().await;
                let Some(this) = weak.upgrade() else { return };
                let client = this.client();
                if this.warm_once(&client, true).await.is_err() {
                    this.counters.warm_errors.fetch_add(1, Ordering::Relaxed);
                }
            }
        });
        let weak = Arc::downgrade(self);
        let every = self.cfg.dns_every;
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(every);
            tick.tick().await;
            loop {
                tick.tick().await;
                let Some(this) = weak.upgrade() else { return };
                this.refresh_dns().await;
            }
        });
    }

    async fn refresh_dns(&self) {
        if self.addrs.lock().is_empty() {
            return;
        }
        let port = self.cfg.base.port_or_known_default().unwrap_or(443);
        let addrs = match resolve(&self.host, port).await {
            Ok(a) if !a.is_empty() => a,
            _ => {
                self.counters.dns_errors.fetch_add(1, Ordering::Relaxed);
                return;
            }
        };
        if *self.addrs.lock() == addrs {
            return;
        }
        let Ok(client) = build(&self.cfg, &self.host, &addrs) else {
            self.counters.dns_errors.fetch_add(1, Ordering::Relaxed);
            return;
        };
        // Warm the new connection before anyone can use it.
        if self.warm_once(&client, false).await.is_err() {
            self.counters.warm_errors.fetch_add(1, Ordering::Relaxed);
            return;
        }
        *self.client.write() = client;
        *self.addrs.lock() = addrs;
        *self.built.lock() = Instant::now();
        self.counters.dns_changes.fetch_add(1, Ordering::Relaxed);
        tracing::info!(host = %self.host, "clob addresses changed; swapped to a warmed client");
    }

    async fn warm_once(&self, client: &Client, record: bool) -> anyhow::Result<()> {
        let url = self.cfg.base.join(&self.cfg.warm_path)?;
        let t0 = Instant::now();
        let resp = client.get(url).send().await?.error_for_status()?;
        let version = resp.version();
        resp.bytes().await?;
        if version != Version::HTTP_2 {
            self.counters.non_h2.fetch_add(1, Ordering::Relaxed);
        }
        if record {
            self.warm_rtt_us.lock().record(t0.elapsed().as_micros() as u64);
        }
        Ok(())
    }

    pub fn client(&self) -> Client {
        self.client.read().clone()
    }

    pub fn url(&self, path: &str) -> Url {
        let mut url = self.cfg.base.clone();
        url.set_path(path);
        url
    }

    /// Send one request on the warm connection. Returns the response at its
    /// headers; the caller reads the body (and stamps when it did).
    pub async fn send(
        &self,
        method: Method,
        path_and_query: &str,
        headers: &[(&'static str, String)],
        body: Option<Vec<u8>>,
    ) -> reqwest::Result<Response> {
        self.counters.requests.fetch_add(1, Ordering::Relaxed);
        let url = match self.cfg.base.join(path_and_query) {
            Ok(u) => u,
            Err(_) => self.url(path_and_query),
        };
        let mut req = self.client().request(method, url);
        for (k, v) in headers {
            req = req.header(*k, v);
        }
        if let Some(body) = body {
            req = req.header("content-type", "application/json").body(body);
        }
        let resp = req.send().await?;
        if resp.version() != Version::HTTP_2 {
            self.counters.non_h2.fetch_add(1, Ordering::Relaxed);
        }
        Ok(resp)
    }

    pub fn stats(&self) -> WarmStats {
        WarmStats {
            host: self.host.clone(),
            addrs: self.addrs.lock().iter().map(ToString::to_string).collect(),
            client_age_s: self.built.lock().elapsed().as_secs(),
            warm_rtt_ms: self.warm_rtt_us.lock().summary_scaled(1e3),
            warm_errors: self.counters.warm_errors.load(Ordering::Relaxed),
            dns_changes: self.counters.dns_changes.load(Ordering::Relaxed),
            dns_errors: self.counters.dns_errors.load(Ordering::Relaxed),
            non_h2: self.counters.non_h2.load(Ordering::Relaxed),
            requests: self.counters.requests.load(Ordering::Relaxed),
        }
    }
}

fn behind_proxy() -> bool {
    ["HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"].iter().any(|k| std::env::var_os(k).is_some())
}

fn build(cfg: &WarmConfig, host: &str, addrs: &[SocketAddr]) -> reqwest::Result<Client> {
    let mut b = Client::builder()
        .user_agent(USER_AGENT)
        .timeout(cfg.timeout)
        .connect_timeout(cfg.timeout)
        .tcp_nodelay(true)
        .pool_idle_timeout(None)
        .pool_max_idle_per_host(2)
        .http2_keep_alive_interval(cfg.keepalive)
        .http2_keep_alive_timeout(Duration::from_secs(3))
        .http2_keep_alive_while_idle(true)
        .http2_adaptive_window(true);
    if cfg.base.scheme() == "http" {
        // Tests run a plain-text mock; h2c needs prior knowledge.
        b = b.http2_prior_knowledge();
    }
    if !addrs.is_empty() {
        b = b.resolve_to_addrs(host, addrs);
    }
    b.build()
}
