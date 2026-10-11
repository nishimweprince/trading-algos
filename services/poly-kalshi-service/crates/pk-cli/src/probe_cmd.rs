//! `pk probe-orders`: measure the real order path from this host, with orders
//! that cannot fill (see `pk_exec::probe`).
//!
//! Each cycle, for one asset's current window (alternating its Up and Down
//! tokens): read the book, sign the probe order ahead, wait `--presign-lead-ms`,
//! read the book again, then at the "decision" post it (pre-signed if the price
//! held, else signed inline) and cancel it. Every request is written to
//! `<out>/YYYY-MM-DD.jsonl` as an `order_timing` record, and the running
//! histograms to `<out>/probe_stats.json`.
//!
//! Requires `PK_ORDER_PROBE=on` and a Polymarket wallet (`POLY_PRIVATE_KEY`,
//! optionally `POLY_FUNDER` and `POLY_SIGNATURE_TYPE`, 3 for a deposit wallet)
//! holding a few pUSD.

use std::io::Write as _;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use chrono::Utc;
use pk_exec::gamma::{WINDOW_S, window_open, window_tokens};
use pk_exec::presign::PresignCache;
use pk_exec::{
    ClobExec, OrderTiming, Probe, ProbeLimits, SdkSigner, Trigger, WalletConfig, WarmConfig, WarmHttp,
};
use rust_decimal::Decimal;
use serde::Serialize;

use crate::env;

#[derive(clap::Args, Debug)]
pub struct Args {
    /// Asset whose current 15-minute window is probed.
    #[arg(long, default_value = "BTC")]
    asset: String,
    /// Seconds between probes.
    #[arg(long, default_value_t = 60.0)]
    every: f64,
    /// Stop after this many minutes (default: run until interrupted).
    #[arg(long)]
    minutes: Option<f64>,
    /// How long before the decision the order is signed.
    #[arg(long, default_value_t = 1000)]
    presign_lead_ms: u64,
    /// Ticks below the best bid.
    #[arg(long, default_value_t = 5)]
    back_ticks: u32,
    /// Most a probe order may be worth, USD.
    #[arg(long, default_value = "3")]
    max_notional: Decimal,
    #[arg(long, default_value = "data/state/probe")]
    out: PathBuf,
    /// Cancel every open order on the account and exit.
    #[arg(long)]
    cancel_all: bool,
}

#[derive(Serialize)]
struct Record<'a> {
    #[serde(flatten)]
    timing: &'a OrderTiming,
    asset: &'a str,
    open_unix: u64,
    sec_into: u64,
    host: &'a str,
}

fn append(out: &PathBuf, line: &impl Serialize) -> anyhow::Result<()> {
    std::fs::create_dir_all(out)?;
    let path = out.join(format!("{}.jsonl", Utc::now().format("%Y-%m-%d")));
    let mut f = std::fs::OpenOptions::new().create(true).append(true).open(path)?;
    serde_json::to_writer(&mut f, line)?;
    f.write_all(b"\n")?;
    Ok(())
}

pub async fn run(args: Args) -> anyhow::Result<()> {
    if !args.cancel_all && env::var("PK_ORDER_PROBE").as_deref() != Some("on") {
        anyhow::bail!("PK_ORDER_PROBE is not on: the probe sends real (post-only, unfillable) orders");
    }
    let clob_url = env::var_or("POLY_CLOB_URL", "https://clob.polymarket.com");
    let gamma_url = env::var_or("POLY_GAMMA_URL", "https://gamma-api.polymarket.com");
    let host_tag = env::var_or("PK_HOST_TAG", "unknown");
    let wallet = WalletConfig {
        clob_url: clob_url.clone(),
        private_key: env::require("POLY_PRIVATE_KEY")?,
        funder: env::var("POLY_FUNDER"),
        signature_type: env::var_or("POLY_SIGNATURE_TYPE", "0").parse()?,
    };
    let signer = Arc::new(SdkSigner::connect(&wallet).await?);
    tracing::info!(address = %signer.address(), "authenticated");
    let http = WarmHttp::connect(WarmConfig::new(clob_url.parse()?)).await?;
    http.spawn_maintenance();
    let exec = Arc::new(ClobExec::new(http, signer, PresignCache::new(Duration::from_secs(10), 64)));
    let limits = ProbeLimits { back_ticks: args.back_ticks, max_notional: args.max_notional, max_open: 1 };
    let probe = Arc::new(Probe::new(exec.clone(), limits));

    if args.cancel_all {
        let ack = exec.cancel_all().await?;
        println!("cancelled {}, not cancelled {}", ack.canceled.len(), ack.not_canceled.len());
        return Ok(());
    }

    let gamma = reqwest::Client::builder().timeout(Duration::from_secs(10)).build()?;
    let end = args.minutes.map(|m| Instant::now() + Duration::from_secs_f64(m * 60.0));
    let mut tokens: Option<(u64, String, String)> = None;
    let mut flip = false;
    let mut tick = tokio::time::interval(Duration::from_secs_f64(args.every.max(1.0)));
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let ctrl_c = tokio::signal::ctrl_c();
    tokio::pin!(ctrl_c);

    loop {
        tokio::select! {
            _ = &mut ctrl_c => break,
            _ = tick.tick() => {}
        }
        if end.is_some_and(|e| Instant::now() >= e) || probe.halted() {
            break;
        }
        let now = Utc::now().timestamp() as u64;
        let open = window_open(now);
        if tokens.as_ref().is_none_or(|(o, ..)| *o != open) {
            match window_tokens(&gamma, &gamma_url, &args.asset, open).await {
                Ok(Some((up, down))) => tokens = Some((open, up, down)),
                Ok(None) => {
                    tracing::warn!(asset = %args.asset, open, "window not listed yet");
                    continue;
                }
                Err(e) => {
                    tracing::warn!(error = %e, "gamma");
                    continue;
                }
            }
        }
        let (_, up, down) = tokens.as_ref().expect("set above");
        flip = !flip;
        let token = if flip { up.clone() } else { down.clone() };
        if let Err(e) = cycle(&probe, &exec, &token, &args, open, &host_tag).await {
            tracing::warn!(error = %e, "probe cycle");
        }
        write_stats(&args.out, &probe, &exec)?;
    }

    for t in probe.cancel_everything().await {
        append(
            &args.out,
            &Record { timing: &t, asset: &args.asset, open_unix: 0, sec_into: 0, host: &host_tag },
        )?;
    }
    write_stats(&args.out, &probe, &exec)?;
    println!("{}", serde_json::to_string_pretty(&stats_json(&probe, &exec))?);
    if probe.halted() {
        anyhow::bail!("a probe order filled; the probe halted");
    }
    Ok(())
}

async fn cycle(
    probe: &Probe<SdkSigner>,
    exec: &ClobExec<SdkSigner>,
    token: &str,
    args: &Args,
    open: u64,
    host: &str,
) -> anyhow::Result<()> {
    let top = exec.top(token).await?;
    probe.presign(token, &top).await?;
    tokio::time::sleep(Duration::from_millis(args.presign_lead_ms)).await;
    let top = exec.top(token).await?;
    let t_decision = Instant::now();
    let out = probe.run_once(token, &top, t_decision, Trigger::Cadence).await;
    if let Some(r) = &out.refused {
        tracing::info!(reason = %r, "probe refused");
    }
    let sec_into = (Utc::now().timestamp() as u64).saturating_sub(open).min(WINDOW_S);
    for t in &out.records {
        append(&args.out, &Record { timing: t, asset: &args.asset, open_unix: open, sec_into, host })?;
        tracing::info!(
            kind = ?t.kind, ok = t.ok, presigned = t.presigned,
            ms = t.decision_to_ack_us as f64 / 1e3, error = ?t.error, "order path"
        );
    }
    Ok(())
}

fn stats_json(probe: &Probe<SdkSigner>, exec: &ClobExec<SdkSigner>) -> serde_json::Value {
    serde_json::json!({
        "t": Utc::now().to_rfc3339(),
        "exec": exec.stats(),
        "probe": &*probe.counters.lock(),
        "open_orders": probe.open_orders(),
    })
}

fn write_stats(out: &PathBuf, probe: &Probe<SdkSigner>, exec: &ClobExec<SdkSigner>) -> anyhow::Result<()> {
    std::fs::create_dir_all(out)?;
    let tmp = out.join("probe_stats.json.tmp");
    std::fs::write(&tmp, serde_json::to_vec_pretty(&stats_json(probe, exec))?)?;
    std::fs::rename(tmp, out.join("probe_stats.json"))?;
    Ok(())
}
