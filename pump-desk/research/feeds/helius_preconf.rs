//! Helius preconfSubscribe recorder (wss://beta.helius-rpc.com), filtered to pump.fun programs.
//! OUT lines: "unix_us slot sig58 status tx_index"   status: 0 failed, 1 success (Helius source), 2 unknown (BAM)
//! Text frames (ack / errors) go to stderr. Key from HELIUS_API_KEY (env file), never argv.
use std::io::Write;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use agave_transaction_view::transaction_view::SanitizedTransactionView;
use anyhow::Context;
use futures_util::{SinkExt, StreamExt};
use tokio_tungstenite::tungstenite::Message;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let key = std::env::var("HELIUS_API_KEY").context("HELIUS_API_KEY")?;
    let out = std::env::var("OUT").context("OUT")?;
    let secs: u64 = std::env::var("SECONDS").ok().and_then(|s| s.parse().ok()).unwrap_or(600);
    let url = format!("wss://beta.helius-rpc.com/?api-key={key}");
    let (ws, _) = tokio_tungstenite::connect_async(url.as_str()).await.context("connect")?;
    let (mut tx, mut rx) = ws.split();
    let sub = r#"{"jsonrpc":"2.0","id":1,"method":"preconfSubscribe","params":[{"accountInclude":["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P","pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"]}]}"#;
    tx.send(Message::Text(sub.into())).await?;
    eprintln!("subscribe sent");
    let mut w = std::io::BufWriter::new(std::fs::OpenOptions::new().create(true).append(true).open(&out)?);
    let end = Instant::now() + Duration::from_secs(secs);
    let mut ping = tokio::time::interval(Duration::from_secs(20));
    let (mut n, mut bad, mut last_log, mut last_flush) = (0u64, 0u64, Instant::now(), Instant::now());
    loop {
        tokio::select! {
            _ = ping.tick() => { tx.send(Message::Ping(Vec::new().into())).await?; }
            m = rx.next() => {
                let Some(m) = m else { eprintln!("stream ended"); break };
                match m? {
                    Message::Binary(b) => {
                        let us = SystemTime::now().duration_since(UNIX_EPOCH)?.as_micros();
                        if b.len() < 18 || b[0] != 1 { bad += 1; continue; }
                        let slot = u64::from_le_bytes(b[1..9].try_into()?);
                        let idx = u64::from_le_bytes(b[9..17].try_into()?);
                        let status = b[17];
                        match SanitizedTransactionView::try_new_sanitized(&b[18..], true) {
                            Ok(v) => {
                                if let Some(s) = v.signatures().first() {
                                    writeln!(w, "{} {} {} {} {}", us, slot, s, status, idx)?;
                                    n += 1;
                                }
                            }
                            Err(_) => bad += 1,
                        }
                    }
                    Message::Text(t) => eprintln!("text: {}", &t[..t.len().min(300)]),
                    Message::Close(c) => { eprintln!("close: {c:?}"); break; }
                    _ => {}
                }
            }
        }
        if last_flush.elapsed() > Duration::from_secs(1) { w.flush()?; last_flush = Instant::now(); }
        if last_log.elapsed() > Duration::from_secs(30) {
            eprintln!("{n} txs, {bad} undecodable"); last_log = Instant::now();
        }
        if Instant::now() > end { break; }
    }
    w.flush()?;
    eprintln!("done: {n} txs, {bad} undecodable");
    Ok(())
}
