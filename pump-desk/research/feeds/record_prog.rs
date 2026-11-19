//! Postpack recorder filtered by PROGRAM (pump.fun bonding curve + PumpSwap).
//! OUT lines: "unix_us slot sig58 leader58 tag fmt"
//!   tag: c = bonding curve in static keys, s = PumpSwap, b = both, x = neither (CPI via ALT)
//!   fmt: L = legacy/v0 (first byte < 0x80), V = other (v1 layout)
//! The signature comes from the parsed view (correct for every format); the client's
//! Preconf::signature() reads bytes 1..65, which is only right for legacy/v0.
use std::io::Write;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::Context;
use astralane_preconf_client::{Client, Config, Event, Interests, Pubkey};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let endpoint = std::env::var("PRECONF_ENDPOINT").context("PRECONF_ENDPOINT")?;
    let token = std::env::var("PRECONF_TOKEN").context("PRECONF_TOKEN")?;
    let out = std::env::var("OUT").context("OUT")?;
    let progs: Vec<Pubkey> = std::env::var("PRECONF_PROGRAMS")
        .context("PRECONF_PROGRAMS")?
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(|s| s.parse::<Pubkey>().with_context(|| format!("bad key {s:?}")))
        .collect::<anyhow::Result<_>>()?;
    let curve: Pubkey = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P".parse()?;
    let swap: Pubkey = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA".parse()?;
    eprintln!("connecting to {endpoint}, {} programs", progs.len());
    let mut w = std::io::BufWriter::new(std::fs::OpenOptions::new().create(true).append(true).open(&out)?);
    let n = Arc::new(AtomicU64::new(0));
    {
        let n = n.clone();
        tokio::spawn(async move {
            let mut total = 0;
            loop {
                tokio::time::sleep(Duration::from_secs(60)).await;
                let c = n.swap(0, Ordering::Relaxed);
                total += c;
                eprintln!("{c} transactions in the last 60 s, {total} since start");
            }
        });
    }
    let mut client = Client::connect(Config::new(endpoint, token)).await?;
    let mut stream = client.subscribe(Interests::new().programs(progs)).await?;
    eprintln!("subscribed");
    let mut last_flush = std::time::Instant::now();
    while let Some(event) = stream.next().await? {
        match event {
            Event::Preconf(tx) => {
                let us = SystemTime::now().duration_since(UNIX_EPOCH)?.as_micros();
                let fmt = if tx.data.first().copied().unwrap_or(0) < 0x80 { 'L' } else { 'V' };
                let (sig, tag) = match tx.view() {
                    Ok(v) => {
                        let sig = v
                            .signatures()
                            .first()
                            .map(|s| s.to_string())
                            .unwrap_or_else(|| tx.signature().to_string());
                        let keys = v.static_account_keys();
                        let has_c = keys.iter().any(|k| k.as_ref() == curve.as_ref());
                        let has_s = keys.iter().any(|k| k.as_ref() == swap.as_ref());
                        let tag = match (has_c, has_s) { (true, true) => 'b', (true, false) => 'c', (false, true) => 's', _ => 'x' };
                        (sig, tag)
                    }
                    Err(_) => (tx.signature().to_string(), '?'),
                };
                let leader = tx.leader.map(|l| l.to_string()).unwrap_or_else(|| "-".into());
                writeln!(w, "{} {} {} {} {} {}", us, tx.slot, sig, leader, tag, fmt)?;
                n.fetch_add(1, Ordering::Relaxed);
                if last_flush.elapsed() > Duration::from_secs(1) {
                    w.flush()?;
                    last_flush = std::time::Instant::now();
                }
            }
            Event::StreamReset => eprintln!("stream reset: reconnected, there may be a gap"),
            Event::SlotStart(_) => {}
        }
    }
    w.flush()?;
    Ok(())
}
