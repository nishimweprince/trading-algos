//! `pk-status`: the read-only status page, a separate process on 127.0.0.1:8051.
//!
//! | Route | |
//! |---|---|
//! | `GET /` | the page |
//! | `GET /api/summary` | allowlisted live status, results, gates, open-trade ETAs |
//! | `GET /api/state` | market state only |
//! | `GET /api/series?since=` | per-second market data per asset, and decisions |
//! | `GET /api/export` | one zip with everything for offline analysis |
//! | `GET /health/live` | process up |
//! | `GET /limitless` | the Limitless page (opened from the main page's tab, in a new tab) |
//! | `GET /api/lim/status` | allowlisted `pk-limitless` status |
//! | `GET /api/lim/series?since=` | its per-second Kalshi and Limitless points |
//!
//! | `GET /okx` | the OKX page |
//! | `GET /api/okx/status` | allowlisted `pk-okx` status |
//! | `GET /api/okx/series?since=` | its per-second Kalshi and OKX points |
//!
//! | `GET /polymaker` | the Polymarket maker's page |
//! | `GET /api/pm/status` | allowlisted `pk-polymaker` status |
//! | `GET /api/pm/series` | its realised P&L after each settled window |
//!
//! `pk-limitless`, `pk-okx` and `pk-polymaker` are polled the same way, only when
//! `PK_LIM_ENABLED` / `PK_OKX_ENABLED`, each on its own schedule: an outage of
//! one never delays the others. With `PK_STATUS_HOME=okx` or `limitless` (a
//! host that runs only that process), `/` serves its page and the trader is
//! not polled. `PK_STATUS_HOME=polymaker` serves the maker's page at `/` and
//! does not poll the taker either.
//!
//! **Load on the trader is fixed, not per viewer.** One poller reads the
//! trader's `/v1/status` (and the observer's `/v1/obs/status`) every 5 s and
//! the journal every 30 s, and pulls only the new series points; requests are
//! answered from that cache. Results come from the journal on disk.
//!
//! There is no write route. The service's API key never leaves this process,
//! and every field returned is picked from an allowlist ([`pick`]). Publish it
//! only behind Cloudflare Access.

use std::collections::{HashMap, VecDeque};
use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::Router;
use axum::extract::{Query, State};
use axum::http::{HeaderValue, header};
use axum::response::{Html, IntoResponse, Response};
use axum::routing::get;
use clap::Parser;
use parking_lot::Mutex;
use pk_svc::Settings;
use serde::Deserialize;
use serde_json::{Map, Value, json};

const PAGE: &str = include_str!("page.html");
const LIM_PAGE: &str = include_str!("limitless.html");
const OKX_PAGE: &str = include_str!("okx.html");
const PM_PAGE: &str = include_str!("polymaker.html");
const LIVE_POLL: Duration = Duration::from_secs(5);
const JOURNAL_EVERY: Duration = Duration::from_secs(30);
const SERIES_POINTS: usize = 1800;
const CSP: &str = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; \
    connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";
const RECENT_FIELDS: [&str; 22] = [
    "type",
    "asset",
    "open_unix",
    "t_ns",
    "sec_into",
    "action",
    "reason",
    "gap",
    "side",
    "entry_px",
    "contracts",
    "blocked",
    "shares",
    "vwap",
    "fee",
    "touch_px",
    "outcome",
    "pnl",
    "venue",
    "notional",
    "drift",
    "proceeds",
];
const COUNTER_PREFIXES: [&str; 16] = [
    "fills",
    "no_fills",
    "settled",
    "blocked_",
    "bbo_",
    "resync",
    "reconnect",
    "invalid_",
    "windows_unidentified",
    "engine_errors",
    "age_outliers",
    "live_orders",
    "paper_",
    "resized",
    "reconcile_drift",
    "exits",
];

fn keep(body: Option<&Value>, keys: &[&str]) -> Value {
    match body {
        Some(Value::Object(o)) => Value::Object(
            keys.iter().map(|k| ((*k).to_owned(), o.get(*k).cloned().unwrap_or(Value::Null))).collect(),
        ),
        _ => Value::Null,
    }
}

/// The allowlisted subset of the trader's status: nothing else leaves the process.
pub fn pick(status: Option<&Value>) -> Option<Value> {
    let st = status?;
    let engine = st.get("engine").cloned().unwrap_or(Value::Null);
    let assets: Map<String, Value> = engine
        .get("assets")
        .and_then(Value::as_object)
        .map(|a| {
            a.iter()
                .map(|(name, v)| {
                    (
                        name.clone(),
                        keep(
                            Some(v),
                            &[
                                "open_unix",
                                "sec_into",
                                "identified",
                                "traded",
                                "action",
                                "last_gap",
                                "max_abs_gap",
                                "gap",
                                "preview",
                                "decision",
                                "age_ms",
                                "legs",
                                "kalshi",
                                "poly_yes",
                                "poly_no",
                            ],
                        ),
                    )
                })
                .collect()
        })
        .unwrap_or_default();
    let feeds: Map<String, Value> = st
        .get("feeds")
        .and_then(Value::as_object)
        .map(|f| {
            f.iter()
                .map(|(n, v)| {
                    (
                        n.clone(),
                        keep(
                            Some(v),
                            &["connected", "connects", "messages", "errors", "seq_gaps", "slow_consumer"],
                        ),
                    )
                })
                .collect()
        })
        .unwrap_or_default();
    let counters: Map<String, Value> = engine
        .get("counters")
        .and_then(Value::as_object)
        .map(|c| {
            c.iter()
                .filter(|(k, _)| COUNTER_PREFIXES.iter().any(|p| k.starts_with(p)))
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect()
        })
        .unwrap_or_default();
    let recent: Vec<Value> = st
        .get("recent")
        .and_then(Value::as_array)
        .map(|r| r.iter().take(30).map(|x| keep(Some(x), &RECENT_FIELDS)).collect())
        .unwrap_or_default();
    let op = st.get("order_path");
    let order_path = match op {
        Some(o) if o.get("enabled") == Some(&Value::Bool(true)) => json!({
            "enabled": true,
            "decision_to_ack_ms": o.pointer("/exec/decision_to_ack_ms"),
            "write_to_ack_ms": o.pointer("/exec/write_to_ack_ms"),
            "warm_rtt_ms": o.pointer("/exec/connection/warm_rtt_ms"),
            "presign_hit_rate": o.pointer("/exec/presign/hit_rate"),
            "posted": o.pointer("/probe/posted"),
            "rejected": o.pointer("/probe/rejected"),
            "fills": o.pointer("/probe/fills"),
            "open_orders": o.get("open_orders"),
            "mode": o.get("mode"),
            "latency": o.get("latency"),
            "live": keep(o.get("live"), &[
                "balance", "last_reconcile_ns", "reconcile_errors", "stray_orders", "position_drift",
                "redeemed", "redeem_pending", "redeem_errors", "gas_pol", "entries", "filled", "refused",
                "balance_rejects", "exits",
            ]),
        }),
        _ => json!({"enabled": false}),
    };
    Some(json!({
        "mode": st.get("execution_mode"),
        "epoch": st.get("epoch"),
        "build": st.get("build"),
        "config": st.get("config"),
        "host": st.get("host"),
        "kalshi_closed": st.get("kalshi_closed") == Some(&Value::Bool(true)),
        "params": st.get("params"),
        "assets": assets,
        "feeds": feeds,
        "feed_age_s": engine.get("feed_age_s"),
        "recorder": keep(st.get("recorder"), &["enabled", "lines_written", "dropped", "errors", "disk_free_gb", "disk_paused"]),
        "latency": engine.get("latency"),
        "observer": keep(st.get("observer"), &["enabled", "alive", "spawns", "dropped", "buffer_bytes", "fallback_writes", "behind_ms", "gaps", "engine_errors", "polybolt", "binance", "kraken", "bitstamp", "coinbase", "laserstream"]),
        "leadlag": st.get("leadlag"),
        "fast_path": st.get("fast_path"),
        "host_load": st.get("host_load"),
        "telegram": keep(st.get("telegram"), &["handled", "errors", "conflicts"]),
        "ledger": engine.get("ledger"),
        "risk": keep(engine.get("risk"), &["halted", "kill_file_present"]),
        "sizing": engine.get("sizing"),
        "counters": counters,
        "order_path": order_path,
        "recent": recent,
    }))
}

/// The allowlisted subset of `pk-limitless`'s status. Errors, paths and
/// connection details stay in the process; only counts and timings leave.
pub fn pick_lim(status: Option<&Value>) -> Option<Value> {
    let st = status?;
    let strip = |v: Option<&Value>, drop: &[&str]| -> Value {
        match v {
            Some(Value::Object(o)) => {
                Value::Object(o.iter().filter(|(k, _)| !drop.contains(&k.as_str())).map(|(k, v)| (k.clone(), v.clone())).collect())
            }
            other => other.cloned().unwrap_or(Value::Null),
        }
    };
    let feeds = st.get("feeds").and_then(Value::as_object).map(|f| {
        f.iter().map(|(k, v)| (k.clone(), strip(Some(v), &["last_error"]))).collect::<Map<String, Value>>()
    });
    let netprobe = st.get("netprobe").map(|n| {
        let mut v = strip(Some(n), &["last_error", "connection"]);
        v["connection"] = keep(n.get("connection"), &["client_age_s", "warm_rtt_ms", "warm_errors", "dns_changes", "non_h2", "requests"]);
        v
    });
    Some(json!({
        "mode": st.get("mode"),
        "stamp": st.get("stamp"),
        "host": st.get("host"),
        "uptime_s": st.get("uptime_s"),
        "params": st.get("params"),
        "shares": st.get("shares"),
        "limit_offset": st.get("limit_offset"),
        "assets": st.get("assets"),
        "ledger": st.get("ledger"),
        "gates": st.get("gates"),
        "counters": st.get("counters"),
        "response": st.get("response"),
        "decide_ns": st.get("decide_ns"),
        "rtt_ms": st.get("rtt_ms"),
        "recent": st.get("recent"),
        "feeds": feeds,
        "netprobe": netprobe,
        "order_path": strip(st.get("order_path"), &[]),
        "recorder": keep(st.get("recorder"), &["enabled", "lines_written", "dropped", "errors", "disk_free_gb", "disk_paused"]),
        "discovery": keep(st.get("discovery"), &["errors"]),
        "sol_hourly": st.get("sol_hourly"),
        "maker": strip(st.get("maker"), &["last_error"]),
    }))
}

/// The allowlisted subset of `pk-okx`'s status: `pick_lim`'s rules, with the
/// fee in force and the venues' settlement agreement.
pub fn pick_okx(status: Option<&Value>) -> Option<Value> {
    let st = status?;
    let mut p = pick_lim(Some(st))?;
    let o = p.as_object_mut()?;
    o.remove("sol_hourly");
    o.remove("order_path");
    o.insert("fee".into(), st.get("fee").cloned().unwrap_or(Value::Null));
    o.insert("settlement".into(), st.get("settlement").cloned().unwrap_or(Value::Null));
    o.insert("discovery".into(), keep(st.get("discovery"), &["errors", "awaiting_outcome"]));
    Some(p)
}

/// The allowlisted subset of `pk-polymaker`'s status: error texts stay in the process.
pub fn pick_pm(status: Option<&Value>) -> Option<Value> {
    let st = status?;
    let feeds = st.get("feeds").and_then(Value::as_object).map(|f| {
        f.iter()
            .map(|(k, v)| {
                let v = match v {
                    Value::Object(o) => Value::Object(o.iter().filter(|(k, _)| k.as_str() != "last_error").map(|(k, v)| (k.clone(), v.clone())).collect()),
                    other => other.clone(),
                };
                (k.clone(), v)
            })
            .collect::<Map<String, Value>>()
    });
    let mut out = Map::new();
    for k in [
        "mode", "halted", "halt", "kill", "realised_today", "loss_today_vs_limit", "daily_loss_usd", "realised_total",
        "realised_by_day", "open_cost", "max_open_cost_usd", "orders_tracked", "coins", "held", "totals", "counters",
        "recent", "cfg", "host", "uptime_s",
    ] {
        out.insert(k.into(), st.get(k).cloned().unwrap_or(Value::Null));
    }
    out.insert("feeds".into(), feeds.map(Value::Object).unwrap_or(Value::Null));
    out.insert("order_path".into(), keep(st.get("order_path"), &["live", "exec", "post_ack_ms", "cash", "deadman_fired"]));
    out.insert("discovery".into(), keep(st.get("discovery"), &["errors"]));
    Some(Value::Object(out))
}

/// Append a series answer's new points, capped per asset; returns the newest second.
fn merge_series(series: &mut HashMap<String, VecDeque<Value>>, mut last: i64, body: Option<Value>) -> i64 {
    for (asset, points) in body.and_then(|b| b.get("points").cloned()).and_then(|p| p.as_object().cloned()).unwrap_or_default() {
        for p in points.as_array().cloned().unwrap_or_default() {
            let sec = p.get(0).and_then(Value::as_i64).unwrap_or(0);
            let s = series.entry(asset.clone()).or_default();
            if s.len() == SERIES_POINTS {
                s.pop_front();
            }
            s.push_back(p);
            last = last.max(sec);
        }
    }
    last
}

/// The points after `since`, per asset.
fn series_after(series: &HashMap<String, VecDeque<Value>>, since: i64) -> Map<String, Value> {
    series
        .iter()
        .map(|(a, s)| {
            let pts: Vec<Value> =
                s.iter().filter(|p| p.get(0).and_then(Value::as_i64).is_some_and(|t| t > since)).cloned().collect();
            (a.clone(), Value::Array(pts))
        })
        .collect()
}

/// The trader's status with the observer's statistics folded in: the observer
/// owns the recorder, feed latencies, result delays and data-quality counters;
/// the trader owns engine time, queue delay, book ages and trade counters.
pub fn merge_observer(status: Option<Value>, obs: Option<Value>) -> Option<Value> {
    let mut status = status?;
    let mut link = status.get("observer").cloned().unwrap_or(json!({}));
    if link.get("enabled") != Some(&Value::Bool(true)) {
        return Some(status);
    }
    let Some(obs) = obs else {
        link["api"] = "down".into();
        status["observer"] = link;
        status["recorder"] = json!({"enabled": true, "observer": "unreachable"});
        return Some(status);
    };
    let merge = |a: Option<&Value>, b: Option<&Value>| -> Value {
        let mut m = a.and_then(Value::as_object).cloned().unwrap_or_default();
        for (k, v) in b.and_then(Value::as_object).cloned().unwrap_or_default() {
            m.insert(k, v);
        }
        Value::Object(m)
    };
    let engine = status.get("engine").cloned().unwrap_or(json!({}));
    let mut e = engine.clone();
    e["latency"] = merge(obs.pointer("/engine/latency"), engine.get("latency"));
    e["counters"] = merge(obs.pointer("/engine/counters"), engine.get("counters"));
    status["engine"] = e;
    status["recorder"] = obs.get("recorder").cloned().unwrap_or(json!({"enabled": false}));
    let polybolt = obs.get("polybolt").cloned().unwrap_or(json!({}));
    link["behind_ms"] = obs.get("behind_ms").cloned().unwrap_or(Value::Null);
    link["gaps"] = obs.get("gaps").cloned().unwrap_or(Value::Null);
    link["engine_errors"] = obs.get("engine_errors").cloned().unwrap_or(Value::Null);
    link["polybolt"] = if polybolt.get("enabled") == Some(&Value::Bool(false)) {
        json!({"enabled": false})
    } else {
        keep(Some(&polybolt), &["enabled", "connected", "authed", "messages", "errors"])
    };
    for venue in ["binance", "kraken", "bitstamp", "coinbase", "laserstream"] {
        link[venue] = obs.get(venue).cloned().unwrap_or(json!({"enabled": false}));
    }
    status["leadlag"] = obs.get("leadlag").cloned().unwrap_or(Value::Null);
    status["observer"] = link;
    Some(status)
}

#[derive(Default)]
struct Cache {
    live: Option<Value>,
    live_at: Option<f64>,
    journal: Option<Value>,
    journal_at: Option<Instant>,
    rates: Map<String, Value>,
    last_messages: HashMap<String, (f64, f64)>,
    series: HashMap<String, VecDeque<Value>>,
    series_last: i64,
    lim: Option<Value>,
    lim_at: Option<f64>,
    lim_series: HashMap<String, VecDeque<Value>>,
    lim_series_last: i64,
    okx: Option<Value>,
    okx_at: Option<f64>,
    okx_series: HashMap<String, VecDeque<Value>>,
    okx_series_last: i64,
    pm: Option<Value>,
    pm_at: Option<f64>,
    pm_series: Option<Value>,
}

struct App {
    settings: Settings,
    http: reqwest::Client,
    cache: Mutex<Cache>,
    export_lock: tokio::sync::Mutex<()>,
}

impl App {
    async fn get(&self, url: &str, query: &[(&str, String)]) -> Option<Value> {
        let r =
            self.http.get(url).query(query).header("X-API-Key", &self.settings.api_key).send().await.ok()?;
        if r.status().as_u16() != 200 {
            return None;
        }
        r.json().await.ok()
    }

    async fn refresh_live(&self) {
        let base = format!("http://127.0.0.1:{}", self.settings.port);
        let obs = format!("http://127.0.0.1:{}", self.settings.observer_port);
        let status = self.get(&format!("{base}/v1/status"), &[]).await;
        let status = if self.settings.observer_enabled {
            merge_observer(status, self.get(&format!("{obs}/v1/obs/status"), &[]).await)
        } else {
            status
        };
        let live = pick(status.as_ref());
        let now = chrono::Utc::now().timestamp_millis() as f64 / 1e3;
        let mut c = self.cache.lock();
        if let Some(feeds) = live.as_ref().and_then(|l| l.get("feeds")).and_then(Value::as_object) {
            for (name, feed) in feeds {
                let Some(msgs) = feed.get("messages").and_then(Value::as_f64) else { continue };
                if let Some((prev, at)) = c.last_messages.get(name).copied()
                    && now > at
                    && msgs >= prev
                {
                    c.rates.insert(name.clone(), json!(((msgs - prev) / (now - at) * 10.0).round() / 10.0));
                }
                c.last_messages.insert(name.clone(), (msgs, now));
            }
        }
        c.live = live;
        c.live_at = Some(now);
    }

    async fn refresh_lim(&self) {
        let base = format!("http://127.0.0.1:{}", self.settings.lim_port);
        let status = pick_lim(self.get(&format!("{base}/v1/lim/status"), &[]).await.as_ref());
        let since = self.cache.lock().lim_series_last;
        let series = self.get(&format!("{base}/v1/lim/series"), &[("since", since.to_string())]).await;
        let now = chrono::Utc::now().timestamp_millis() as f64 / 1e3;
        let mut c = self.cache.lock();
        c.lim = status;
        c.lim_at = Some(now);
        let last = c.lim_series_last;
        c.lim_series_last = merge_series(&mut c.lim_series, last, series);
    }

    async fn refresh_okx(&self) {
        let base = format!("http://127.0.0.1:{}", self.settings.okx_port);
        let status = pick_okx(self.get(&format!("{base}/v1/okx/status"), &[]).await.as_ref());
        let since = self.cache.lock().okx_series_last;
        let series = self.get(&format!("{base}/v1/okx/series"), &[("since", since.to_string())]).await;
        let now = chrono::Utc::now().timestamp_millis() as f64 / 1e3;
        let mut c = self.cache.lock();
        c.okx = status;
        c.okx_at = Some(now);
        let last = c.okx_series_last;
        c.okx_series_last = merge_series(&mut c.okx_series, last, series);
    }

    async fn refresh_pm(&self) {
        let base = format!("http://127.0.0.1:{}", self.settings.pm_port);
        let status = pick_pm(self.get(&format!("{base}/v1/pm/status"), &[]).await.as_ref());
        let series = self.get(&format!("{base}/v1/pm/series"), &[]).await;
        let now = chrono::Utc::now().timestamp_millis() as f64 / 1e3;
        let mut c = self.cache.lock();
        c.pm = status;
        c.pm_at = Some(now);
        if series.is_some() {
            c.pm_series = series;
        }
    }

    async fn refresh_series(&self) {
        let source =
            if self.settings.observer_enabled { self.settings.observer_port } else { self.settings.port };
        let since = self.cache.lock().series_last;
        let Some(body) =
            self.get(&format!("http://127.0.0.1:{source}/v1/series"), &[("since", since.to_string())]).await
        else {
            return;
        };
        let mut c = self.cache.lock();
        for (asset, points) in body.get("points").and_then(Value::as_object).cloned().unwrap_or_default() {
            if !self.settings.assets.contains(&asset) {
                continue;
            }
            for p in points.as_array().cloned().unwrap_or_default() {
                let sec = p.get(0).and_then(Value::as_i64).unwrap_or(0);
                let s = c.series.entry(asset.clone()).or_default();
                if s.len() == SERIES_POINTS {
                    s.pop_front();
                }
                s.push_back(p);
                c.series_last = c.series_last.max(sec);
            }
        }
    }

    fn read_results(&self) -> Value {
        let dir = self.settings.state_dir.join("journal");
        let records = pk_core::journal::read_journal(&dir).unwrap_or_default();
        // Only the current results epoch: earlier epochs were invalidated by a fix.
        let (kept, excluded) = pk_core::journal::in_epoch(records, Some(&self.settings.results_epoch));
        json!({
            "epoch": self.settings.results_epoch,
            "excluded_records": excluded,
            "gates": pk_results::evaluate(&kept, self.settings.params.max_entry_px),
            "results": pk_results::paper_results(&kept, 50),
            "open_trades": pk_results::open_trade_etas(&kept),
        })
    }

    async fn refresh_journal(self: &Arc<Self>) {
        let this = self.clone();
        if let Ok(j) = tokio::task::spawn_blocking(move || this.read_results()).await {
            let mut c = self.cache.lock();
            c.journal = Some(j);
            c.journal_at = Some(Instant::now());
        }
    }

    async fn ensure_fresh(self: &Arc<Self>) {
        // Only the first request after a start may wait for data.
        let (live, journal) = {
            let c = self.cache.lock();
            (c.live_at.is_some(), c.journal.is_some())
        };
        if !live {
            self.refresh_live().await;
            self.refresh_series().await;
        }
        if !journal {
            self.refresh_journal().await;
        }
    }

    fn summary(&self) -> Value {
        let c = self.cache.lock();
        let j = c.journal.clone().unwrap_or(json!({}));
        json!({
            "fetched_at": c.live_at,
            "service_up": c.live.is_some(),
            "live": c.live,
            "rates": c.rates,
            "epoch": j.get("epoch"),
            "excluded_records": j.get("excluded_records"),
            "gates": j.get("gates"),
            "results": j.get("results"),
            "open_trades": j.get("open_trades"),
        })
    }
}

async fn lim_poller(app: Arc<App>) {
    loop {
        app.refresh_lim().await;
        tokio::time::sleep(LIVE_POLL).await;
    }
}

async fn okx_poller(app: Arc<App>) {
    loop {
        app.refresh_okx().await;
        tokio::time::sleep(LIVE_POLL).await;
    }
}

async fn pm_poller(app: Arc<App>) {
    loop {
        app.refresh_pm().await;
        tokio::time::sleep(LIVE_POLL).await;
    }
}

async fn poller(app: Arc<App>) {
    loop {
        app.refresh_live().await;
        app.refresh_series().await;
        let due = app.cache.lock().journal_at.is_none_or(|t| t.elapsed() >= JOURNAL_EVERY);
        if due {
            app.refresh_journal().await;
        }
        tokio::time::sleep(LIVE_POLL).await;
    }
}

#[derive(Deserialize)]
struct Since {
    since: Option<i64>,
}

fn json_response(v: Value) -> Response {
    ([(header::CONTENT_TYPE, "application/json")], v.to_string()).into_response()
}

const EXPORT_README: &str = "| File in the zip | What |
|---|---|
| README.txt | this table, the profile, the export time |
| summary.json | gates, paper results and breakdowns, live status and latency at export |
| journal.jsonl | every decision, fill, settlement, resolution, window summary and order timing |
| events.jsonl | the services' structured log (warnings, reconnects, heartbeats) |
| ledger.json | the paper cash ledger and every position |
| recordings.jsonl | one manifest per recorded hour file (lines, venues, gaps) |
| series.jsonl | the per-second price points the charts show, per asset |

Nothing secret is included: no .env, no keys. The raw tape is not included and
stays on the host (it is GBs); replay it there with `pk replay run`.
";

fn manifests(root: &Path, out: &mut Vec<PathBuf>) {
    for e in std::fs::read_dir(root).into_iter().flatten().flatten() {
        let p = e.path();
        if p.is_dir() {
            manifests(&p, out);
        } else if p.to_string_lossy().ends_with(".zst.json") || p.to_string_lossy().ends_with(".gz.json") {
            out.push(p);
        }
    }
}

fn build_export(
    settings: &Settings,
    summary: &Value,
    series: &HashMap<String, Vec<Value>>,
) -> anyhow::Result<Vec<u8>> {
    let mut buf = std::io::Cursor::new(Vec::new());
    let mut zip = zip::ZipWriter::new(&mut buf);
    let opts = zip::write::SimpleFileOptions::default().compression_method(zip::CompressionMethod::Deflated);
    let mut counts = Map::new();
    let mut jsonl = |zip: &mut zip::ZipWriter<_>, name: &str, files: &[PathBuf]| -> anyhow::Result<()> {
        zip.start_file(name, opts)?;
        let mut n = 0u64;
        for path in files {
            for line in std::io::BufReader::new(std::fs::File::open(path)?).lines() {
                let line = line?;
                if !line.trim().is_empty() {
                    zip.write_all(line.as_bytes())?;
                    zip.write_all(b"\n")?;
                    n += 1;
                }
            }
        }
        counts.insert(name.into(), n.into());
        Ok(())
    };
    let mut journal: Vec<PathBuf> = std::fs::read_dir(settings.state_dir.join("journal"))
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "jsonl"))
        .collect();
    journal.sort();
    jsonl(&mut zip, "journal.jsonl", &journal)?;
    let events: Vec<PathBuf> =
        [settings.events_log_path.clone()].into_iter().filter(|p| p.is_file()).collect();
    jsonl(&mut zip, "events.jsonl", &events)?;
    let mut found = Vec::new();
    manifests(&settings.record_dir, &mut found);
    found.sort();
    zip.start_file("recordings.jsonl", opts)?;
    for m in &found {
        let mut body: Value = serde_json::from_slice(&std::fs::read(m)?).unwrap_or(json!({}));
        let rel = m.strip_prefix(&settings.record_dir).unwrap_or(m).display().to_string();
        body["file"] = rel.trim_end_matches(".json").into();
        writeln!(zip, "{body}")?;
    }
    counts.insert("recordings.jsonl".into(), found.len().into());
    zip.start_file("series.jsonl", opts)?;
    let fields = ["sec", "open_unix", "kalshi_mid", "poly_yes_mid", "yes_bid", "yes_ask", "no_bid", "no_ask"];
    for (asset, points) in series {
        for p in points {
            let mut row = Map::new();
            row.insert("asset".into(), asset.clone().into());
            for (k, v) in fields.iter().zip(p.as_array().cloned().unwrap_or_default()) {
                row.insert((*k).into(), v);
            }
            writeln!(zip, "{}", Value::Object(row))?;
        }
    }
    zip.start_file("ledger.json", opts)?;
    zip.write_all(&std::fs::read(settings.state_dir.join("ledger.json")).unwrap_or_else(|_| b"{}".to_vec()))?;
    zip.start_file("summary.json", opts)?;
    zip.write_all(serde_json::to_string_pretty(summary)?.as_bytes())?;
    zip.start_file("README.txt", opts)?;
    write!(
        zip,
        "poly-kalshi export\nprofile={:?} host={} exported_at={}\nlines={}\n\n{EXPORT_README}",
        settings.profile,
        settings.host_tag,
        chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true),
        Value::Object(counts)
    )?;
    zip.finish()?;
    Ok(buf.into_inner())
}

fn router(app: Arc<App>) -> Router {
    let home = match app.settings.status_home.as_str() {
        "okx" => OKX_PAGE,
        "polymaker" => PM_PAGE,
        "limitless" => LIM_PAGE,
        _ => PAGE,
    };
    // The taker's routes read its journal (tens of MB) on demand: a host whose
    // home is another page serves none of them, so stale tabs cannot load it.
    let taker = if app.settings.status_home == "polymarket" {
        Router::new()
            .route(
                "/api/summary",
                get(|State(app): State<Arc<App>>| async move {
                    app.ensure_fresh().await;
                    json_response(app.summary())
                }),
            )
            .route(
                "/api/state",
                get(|State(app): State<Arc<App>>| async move {
                    app.ensure_fresh().await;
                    let c = app.cache.lock();
                    let live = c.live.clone().unwrap_or(json!({}));
                    json_response(json!({
                        "fetched_at": c.live_at,
                        "service_up": c.live.is_some(),
                        "kalshi_closed": live.get("kalshi_closed") == Some(&Value::Bool(true)),
                        "params": live.get("params"),
                        "feed_age_s": live.get("feed_age_s"),
                        "rates": c.rates,
                        "assets": live.get("assets").cloned().unwrap_or(json!({})),
                    }))
                }),
            )
            .route(
                "/api/series",
                get(|State(app): State<Arc<App>>, Query(q): Query<Since>| async move {
                    app.ensure_fresh().await;
                    let since = q.since.unwrap_or(0);
                    let c = app.cache.lock();
                    let live = c.live.clone().unwrap_or(json!({}));
                    let decisions: Vec<Value> = live
                        .get("recent")
                        .and_then(Value::as_array)
                        .map(|r| {
                            r.iter().filter(|x| x.get("type") == Some(&json!("decision"))).cloned().collect()
                        })
                        .unwrap_or_default();
                    let points: Map<String, Value> = app
                        .settings
                        .assets
                        .iter()
                        .map(|a| {
                            let pts: Vec<Value> = c
                                .series
                                .get(a)
                                .map(|s| {
                                    s.iter()
                                        .filter(|p| p.get(0).and_then(Value::as_i64).is_some_and(|t| t > since))
                                        .cloned()
                                        .collect()
                                })
                                .unwrap_or_default();
                            (a.clone(), Value::Array(pts))
                        })
                        .collect();
                    let params = live.get("params").cloned().unwrap_or(json!({}));
                    json_response(json!({
                        "threshold": params.get("gap_threshold"),
                        "entry_early_s": params.get("entry_early_s"),
                        "entry_late_s": params.get("entry_late_s"),
                        "points": points,
                        "decisions": decisions,
                    }))
                }),
            )
            .route(
                "/api/export",
                get(|State(app): State<Arc<App>>| async move {
                    app.ensure_fresh().await;
                    let _one = app.export_lock.lock().await; // one export at a time
                    let summary = app.summary();
                    let series: HashMap<String, Vec<Value>> = app
                        .cache
                        .lock()
                        .series
                        .iter()
                        .map(|(k, v)| (k.clone(), v.iter().cloned().collect()))
                        .collect();
                    let settings = app.settings.clone();
                    let built =
                        tokio::task::spawn_blocking(move || build_export(&settings, &summary, &series)).await;
                    match built {
                        Ok(Ok(bytes)) => {
                            let name = format!(
                                "pk-export-{}-{}-{}.zip",
                                app.settings.host_tag,
                                app.settings.profile.as_deref().unwrap_or("default"),
                                chrono::Utc::now().format("%Y%m%dT%H%M%SZ")
                            );
                            (
                                [
                                    (header::CONTENT_TYPE, "application/zip".to_owned()),
                                    (header::CONTENT_DISPOSITION, format!("attachment; filename=\"{name}\"")),
                                ],
                                bytes,
                            )
                                .into_response()
                        }
                        _ => (axum::http::StatusCode::INTERNAL_SERVER_ERROR, "export failed").into_response(),
                    }
                }),
            )
    } else {
        Router::new()
    };
    Router::new()
        .route("/", get(move || async move { Html(home) }))
        .merge(taker)
        .route("/limitless", get(|| async { Html(LIM_PAGE) }))
        .route(
            "/api/lim/status",
            get(|State(app): State<Arc<App>>| async move {
                let c = app.cache.lock();
                json_response(json!({
                    "enabled": app.settings.lim_enabled,
                    "fetched_at": c.lim_at,
                    "service_up": c.lim.is_some(),
                    "status": c.lim,
                }))
            }),
        )
        .route(
            "/api/lim/series",
            get(|State(app): State<Arc<App>>, Query(q): Query<Since>| async move {
                let c = app.cache.lock();
                let points = series_after(&c.lim_series, q.since.unwrap_or(0));
                let params = c.lim.as_ref().and_then(|l| l.get("params")).cloned().unwrap_or(json!({}));
                json_response(json!({"threshold": params.get("gap_threshold"), "points": points}))
            }),
        )
        .route("/okx", get(|| async { Html(OKX_PAGE) }))
        .route(
            "/api/okx/status",
            get(|State(app): State<Arc<App>>| async move {
                let c = app.cache.lock();
                json_response(json!({
                    "enabled": app.settings.okx_enabled,
                    "fetched_at": c.okx_at,
                    "service_up": c.okx.is_some(),
                    "status": c.okx,
                }))
            }),
        )
        .route(
            "/api/okx/series",
            get(|State(app): State<Arc<App>>, Query(q): Query<Since>| async move {
                let c = app.cache.lock();
                let points = series_after(&c.okx_series, q.since.unwrap_or(0));
                let params = c.okx.as_ref().and_then(|l| l.get("params")).cloned().unwrap_or(json!({}));
                json_response(json!({"threshold": params.get("gap_threshold"), "points": points}))
            }),
        )
        .route("/polymaker", get(|| async { Html(PM_PAGE) }))
        .route(
            "/api/pm/status",
            get(|State(app): State<Arc<App>>| async move {
                let c = app.cache.lock();
                json_response(json!({
                    "enabled": app.settings.pm_enabled,
                    "fetched_at": c.pm_at,
                    "service_up": c.pm.is_some(),
                    "status": c.pm,
                }))
            }),
        )
        .route(
            "/api/pm/series",
            get(|State(app): State<Arc<App>>| async move {
                json_response(app.cache.lock().pm_series.clone().unwrap_or(json!([])))
            }),
        )
        .route("/health/live", get(|| async { axum::Json(json!({"status": "ok"})) }))
        .layer(tower_http::compression::CompressionLayer::new())
        .layer(tower_http::set_header::SetResponseHeaderLayer::overriding(
            header::CONTENT_SECURITY_POLICY,
            HeaderValue::from_static(CSP),
        ))
        .layer(tower_http::set_header::SetResponseHeaderLayer::overriding(
            header::CACHE_CONTROL,
            HeaderValue::from_static("no-store"),
        ))
        .layer(tower_http::set_header::SetResponseHeaderLayer::overriding(
            header::HeaderName::from_static("x-robots-tag"),
            HeaderValue::from_static("noindex, nofollow"),
        ))
        .layer(tower_http::set_header::SetResponseHeaderLayer::overriding(
            header::X_CONTENT_TYPE_OPTIONS,
            HeaderValue::from_static("nosniff"),
        ))
        .layer(tower_http::set_header::SetResponseHeaderLayer::overriding(
            header::REFERRER_POLICY,
            HeaderValue::from_static("no-referrer"),
        ))
        .with_state(app)
}

#[derive(Parser)]
#[command(name = "pk-status", about = "pk-status: read-only poly-kalshi status page")]
struct Args {
    #[arg(long)]
    profile: Option<String>,
}

fn main() -> anyhow::Result<()> {
    pk_svc::tls::install_default_provider();
    let args = Args::parse();
    let settings = Settings::load(args.profile.as_deref())?;
    let _guard = pk_svc::logging::init(&settings.log_level, None);
    tokio::runtime::Builder::new_multi_thread().worker_threads(2).enable_all().build()?.block_on(async move {
        let app = Arc::new(App {
            http: reqwest::Client::builder().timeout(Duration::from_secs(5)).no_proxy().build()?,
            cache: Mutex::new(Cache::default()),
            export_lock: tokio::sync::Mutex::new(()),
            settings: settings.clone(),
        });
        if settings.status_home == "polymarket" {
            tokio::spawn(poller(app.clone()));
        }
        if settings.lim_enabled {
            tokio::spawn(lim_poller(app.clone()));
        }
        if settings.okx_enabled {
            tokio::spawn(okx_poller(app.clone()));
        }
        if settings.pm_enabled {
            tokio::spawn(pm_poller(app.clone()));
        }
        let listener =
            tokio::net::TcpListener::bind((settings.status_host.as_str(), settings.status_port)).await?;
        tracing::info!(addr = %listener.local_addr()?, "pk-status listening");
        axum::serve(listener, router(app)).await?;
        Ok(())
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pick_keeps_only_the_allowlist() {
        let status = json!({
            "execution_mode": "paper", "epoch": "7", "kalshi_closed": false,
            "engine": {"assets": {"BTC": {"gap": 0.1, "secret": 1}}, "counters": {"fills": 2, "something_else": 3},
                       "risk": {"halted": false, "kill_file_present": false, "halt_reason": "x"}},
            "feeds": {"kalshi": {"connected": true, "last_error": "token=abc"}},
            "order_path": {"enabled": true, "exec": {"decision_to_ack_ms": {"n": 3, "p50": 12.5}}, "probe": {"fills": 0},
                           "mode": "live", "live": {"balance": 12.5, "last_error": "secret detail"}},
            "recent": [{"type": "decision", "asset": "BTC", "kalshi": {"bid": 1}}],
            "api_key_leak": "never",
        });
        let p = pick(Some(&status)).unwrap();
        assert!(p.get("api_key_leak").is_none());
        assert!(p["assets"]["BTC"].get("secret").is_none());
        assert_eq!(p["counters"], json!({"fills": 2}));
        assert!(p["feeds"]["kalshi"].get("last_error").is_none());
        assert!(p["risk"].get("halt_reason").is_none());
        assert_eq!(p["order_path"]["decision_to_ack_ms"]["p50"], 12.5);
        assert_eq!(p["order_path"]["live"]["balance"], 12.5);
        assert!(p["order_path"]["live"].get("last_error").is_none());
        assert!(p["recent"][0].get("kalshi").is_none());
    }

    #[test]
    fn pick_lim_drops_errors_and_paths() {
        let st = json!({
            "mode": "paper", "assets": {"BTC": {"gap": 0.1}},
            "feeds": {"kalshi": {"connected": true, "last_error": "secret"}},
            "netprobe": {"n": 3, "last_error": "x", "connection": {"host": "h", "addrs": ["1.2.3.4:443"], "requests": 9}},
            "recorder": {"enabled": true, "root": "/data/pk", "lines_written": 4},
            "discovery": {"errors": 1, "last_error": "e"},
            "maker": {"enabled": true, "fills": 3, "last_error": "events x: 429"},
            "anything_else": 1,
        });
        let p = pick_lim(Some(&st)).unwrap();
        assert_eq!(p["maker"]["fills"], 3);
        assert!(p["maker"].get("last_error").is_none());
        assert!(p["feeds"]["kalshi"].get("last_error").is_none());
        assert_eq!(p["feeds"]["kalshi"]["connected"], true);
        assert!(p["netprobe"].get("last_error").is_none());
        assert!(p["netprobe"]["connection"].get("addrs").is_none());
        assert_eq!(p["netprobe"]["connection"]["requests"], 9);
        assert!(p["recorder"].get("root").is_none());
        assert!(p["discovery"].get("last_error").is_none());
        assert!(p.get("anything_else").is_none());
        assert_eq!(p["assets"]["BTC"]["gap"], 0.1);
    }

    #[test]
    fn pick_pm_drops_errors_and_unknown_keys() {
        let st = json!({
            "mode": "live", "halted": false, "coins": [{"asset": "BTC"}], "secret": "x",
            "feeds": {"kalshi": {"connected": true, "last_error": "token"}, "ok": true},
            "order_path": {"live": true, "cash": 50.0, "key": "k"},
            "discovery": {"errors": 2, "last_error": "url?key"},
        });
        let p = pick_pm(Some(&st)).unwrap();
        assert!(p.get("secret").is_none());
        assert_eq!(p["feeds"]["kalshi"], json!({"connected": true}));
        assert_eq!(p["feeds"]["ok"], json!(true));
        assert!(p["order_path"].get("key").is_none());
        assert_eq!(p["order_path"]["cash"], json!(50.0));
        assert_eq!(p["discovery"], json!({"errors": 2}));
    }

    #[test]
    fn pick_okx_keeps_fee_and_settlement_only() {
        let st = json!({
            "mode": "paper", "assets": {"BTC": {"gap": 0.1}},
            "fee": {"rate": 0.07}, "settlement": {"windows": 3, "okx_vs_kalshi": 1},
            "feeds": {"okx": {"connected": true, "last_error": "secret"}},
            "discovery": {"errors": 1, "last_error": "e", "awaiting_outcome": 2},
            "order_path": {"x": 1}, "sol_hourly": {"y": 1}, "anything_else": 1,
        });
        let p = pick_okx(Some(&st)).unwrap();
        assert_eq!(p["fee"]["rate"], 0.07);
        assert_eq!(p["settlement"]["okx_vs_kalshi"], 1);
        assert!(p["feeds"]["okx"].get("last_error").is_none());
        assert_eq!(p["discovery"], json!({"errors": 1, "awaiting_outcome": 2}));
        assert!(p.get("order_path").is_none() && p.get("sol_hourly").is_none() && p.get("anything_else").is_none());
    }

    #[test]
    fn observer_down_is_reported() {
        let status = json!({"observer": {"enabled": true, "alive": true}, "engine": {}});
        let merged = merge_observer(Some(status), None).unwrap();
        assert_eq!(merged["observer"]["api"], "down");
        let status = json!({"observer": {"enabled": true}, "engine": {"latency": {"engine_us": 1}, "counters": {"fills": 1}}});
        let obs = json!({"engine": {"latency": {"feed_ms_p": 2}, "counters": {"bbo_checks_pc": 5}}, "behind_ms": 3.0, "recorder": {"enabled": true}});
        let m = merge_observer(Some(status), Some(obs)).unwrap();
        assert_eq!(m["engine"]["latency"], json!({"engine_us": 1, "feed_ms_p": 2}));
        assert_eq!(m["observer"]["behind_ms"], 3.0);
        assert!(m["leadlag"].is_null());
        let status = json!({"observer": {"enabled": true}, "engine": {}});
        let obs = json!({"leadlag": {"totals": {"perp": {}}}, "binance": {"enabled": true, "feeds": []},
                         "kraken": {"connected": true, "messages": 7}});
        let m = merge_observer(Some(status), Some(obs)).unwrap();
        assert_eq!(m["observer"]["binance"]["enabled"], true);
        assert_eq!(m["observer"]["bitstamp"]["enabled"], false);
        let p = pick(Some(&m)).unwrap();
        assert_eq!(p["leadlag"]["totals"], json!({"perp": {}}));
        assert_eq!(p["observer"]["binance"]["enabled"], true);
        assert_eq!(p["observer"]["kraken"]["messages"], 7);
    }
}
