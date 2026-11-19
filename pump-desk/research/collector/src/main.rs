//! pd-collect: live pump.fun dataset recorder for the curve-in replay (no RPC per curve).
//!
//! LaserStream (Yellowstone gRPC, confirmed, successful txs only):
//!   filter "curve": every tx that references the pump.fun program  -> every pump.fun event-CPI payload
//!   filter "pools": txs on canonical PumpSwap pools of curves that completed in the last POOL_WATCH_S
//!                   -> the pool's post-tx base/quote vault balances
//! Records (little-endian) go to hourly zstd files OUT_DIR/cap-YYYYmmddTHH-<run start unix s>.bin.zst:
//!   common: u8 type, u64 recv_unix_ns, u64 slot, u64 tx_index, [64] signature, [32] fee payer
//!   type 1 pump event: u16 ordinal (bit 15 = from "Program data:" log), u16 len, [len] event bytes (discriminator first)
//!   type 2 pool point: [32] pool, [32] mint, u64 base, u64 quote
//!   type 3 migrate tx (references the migration authority): no body
//!   type 9 stream (re)connected: no body; the gap before it is a hole in the data
//! Env: HELIUS_API_KEY, LASERSTREAM_ENDPOINT (https://...), OUT_DIR, POOL_WATCH_S (300), MIN_FREE_GB (8)
use std::collections::HashMap;
use std::io::Write;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::Context;
use base64::Engine;
use futures::{SinkExt, StreamExt};
use solana_pubkey::Pubkey;
use yellowstone_grpc_client::{ClientTlsConfig, GeyserGrpcClient};
use yellowstone_grpc_proto::prelude::*;

const PUMP_FUN: &str = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
const PUMP_SWAP: &str = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA";
const MIGRATION_AUTHORITY: &str = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg";
const WSOL: &str = "So11111111111111111111111111111111111111112";
const EVENT_CPI_TAG: [u8; 8] = [0xe4, 0x45, 0xa5, 0x2e, 0x51, 0xcb, 0x9a, 0x1d];
// sha256("event:TradeEvent")[..8]
const TRADE_EVENT: [u8; 8] = [0xbd, 0xdb, 0x7f, 0xd3, 0x4e, 0xe6, 0x61, 0xee];

fn now_ns() -> u64 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos() as u64 }
fn pk(s: &str) -> Pubkey { s.parse().unwrap() }

fn canonical_pool(mint: &Pubkey) -> Pubkey {
    let pump = pk(PUMP_FUN);
    let swap = pk(PUMP_SWAP);
    let wsol = pk(WSOL);
    let (authority, _) = Pubkey::find_program_address(&[b"pool-authority", mint.as_ref()], &pump);
    let (pool, _) = Pubkey::find_program_address(
        &[b"pool", &[0u8, 0u8], authority.as_ref(), mint.as_ref(), wsol.as_ref()], &swap);
    pool
}

struct Out {
    dir: String,
    run: u64, // process start (unix s): each run writes its own files, never appending to a cut-off frame
    hour: String,
    enc: Option<zstd::stream::write::Encoder<'static, std::fs::File>>,
    bytes: u64,
    last_flush: Instant,
    min_free: u64,
    paused: bool,
}

impl Out {
    fn hour_now() -> String {
        // UTC YYYYmmddTHH from the unix clock (no chrono dependency)
        let s = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs() as i64;
        let days = s.div_euclid(86_400);
        let h = (s.rem_euclid(86_400)) / 3600;
        // civil-from-days (Howard Hinnant)
        let z = days + 719_468;
        let era = z.div_euclid(146_097);
        let doe = z - era * 146_097;
        let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
        let y = yoe + era * 400;
        let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
        let mp = (5 * doy + 2) / 153;
        let d = doy - (153 * mp + 2) / 5 + 1;
        let m = if mp < 10 { mp + 3 } else { mp - 9 };
        let y = if m <= 2 { y + 1 } else { y };
        format!("{y:04}{m:02}{d:02}T{h:02}")
    }
    fn free_bytes(&self) -> u64 {
        // statvfs via df would need a dependency; read /proc/self/mountinfo is overkill: use `df` once a minute.
        let out = std::process::Command::new("df").args(["-B1", "--output=avail", &self.dir]).output();
        out.ok()
            .and_then(|o| String::from_utf8(o.stdout).ok())
            .and_then(|s| s.lines().nth(1).and_then(|l| l.trim().parse().ok()))
            .unwrap_or(u64::MAX)
    }
    fn rotate_if_needed(&mut self) -> anyhow::Result<()> {
        let h = Self::hour_now();
        if self.enc.is_some() && h == self.hour { return Ok(()); }
        if let Some(e) = self.enc.take() { e.finish()?.sync_all()?; }
        self.paused = self.free_bytes() < self.min_free;
        if self.paused { eprintln!("disk below MIN_FREE_GB: not writing this hour"); self.hour = h; return Ok(()); }
        let path = format!("{}/cap-{}-{}.bin.zst", self.dir, h, self.run);
        let f = std::fs::OpenOptions::new().create(true).append(true).open(&path)?;
        self.enc = Some(zstd::stream::write::Encoder::new(f, 6)?);
        self.hour = h;
        eprintln!("writing {path}");
        Ok(())
    }
    fn write(&mut self, rec: &[u8]) -> anyhow::Result<()> {
        self.rotate_if_needed()?;
        if let Some(e) = self.enc.as_mut() {
            e.write_all(rec)?;
            self.bytes += rec.len() as u64;
            if self.last_flush.elapsed() > Duration::from_secs(5) {
                e.flush()?; // completes a zstd block: a crash loses at most ~5 s
                self.last_flush = Instant::now();
            }
        }
        Ok(())
    }
}

fn header(t: u8, recv: u64, slot: u64, idx: u64, sig: &[u8], payer: &[u8]) -> Vec<u8> {
    let mut r = Vec::with_capacity(512);
    r.push(t);
    r.extend_from_slice(&recv.to_le_bytes());
    r.extend_from_slice(&slot.to_le_bytes());
    r.extend_from_slice(&idx.to_le_bytes());
    let mut s = [0u8; 64];
    if sig.len() == 64 { s.copy_from_slice(sig); }
    r.extend_from_slice(&s);
    let mut p = [0u8; 32];
    if payer.len() == 32 { p.copy_from_slice(payer); }
    r.extend_from_slice(&p);
    r
}

struct State {
    watch: HashMap<Pubkey, (Pubkey, Instant)>, // pool -> (mint, expiry)
    watch_dirty: bool,
}

fn build_request(st: &State) -> SubscribeRequest {
    let mut txs = HashMap::new();
    txs.insert("curve".to_string(), SubscribeRequestFilterTransactions {
        vote: Some(false), failed: Some(false),
        account_include: vec![PUMP_FUN.to_string()], ..Default::default()
    });
    if !st.watch.is_empty() {
        txs.insert("pools".to_string(), SubscribeRequestFilterTransactions {
            vote: Some(false), failed: Some(false),
            account_include: st.watch.keys().map(|p| p.to_string()).collect(), ..Default::default()
        });
    }
    SubscribeRequest {
        transactions: txs,
        commitment: Some(CommitmentLevel::Confirmed as i32),
        ..Default::default()
    }
}

struct Stats { txs: u64, events: u64, pool_pts: u64, migrates: u64 }

fn handle_tx(u: SubscribeUpdateTransaction, recv: u64, st: &mut State, out: &mut Out, stats: &mut Stats) -> anyhow::Result<()> {
    let slot = u.slot;
    let Some(info) = u.transaction else { return Ok(()) };
    let Some(tx) = info.transaction.as_ref() else { return Ok(()) };
    let Some(msg) = tx.message.as_ref() else { return Ok(()) };
    let Some(meta) = info.meta.as_ref() else { return Ok(()) };
    if meta.err.is_some() { return Ok(()); }
    stats.txs += 1;
    let mut keys: Vec<&[u8]> = msg.account_keys.iter().map(|k| k.as_slice()).collect();
    keys.extend(meta.loaded_writable_addresses.iter().map(|k| k.as_slice()));
    keys.extend(meta.loaded_readonly_addresses.iter().map(|k| k.as_slice()));
    let pump = pk(PUMP_FUN);
    let mig = pk(MIGRATION_AUTHORITY);
    let payer = keys.first().copied().unwrap_or(&[]);
    let sig = info.signature.as_slice();
    let idx = info.index;
    let has = |p: &Pubkey| keys.iter().any(|k| *k == p.as_ref());

    if has(&pump) {
        let mut ord: u16 = 0;
        for group in &meta.inner_instructions {
            for ix in &group.instructions {
                let prog = keys.get(ix.program_id_index as usize).copied().unwrap_or(&[]);
                if prog != pump.as_ref() || ix.data.len() < 16 || ix.data[..8] != EVENT_CPI_TAG { continue; }
                let ev = &ix.data[8..];
                let mut r = header(1, recv, slot, idx, sig, payer);
                r.extend_from_slice(&ord.to_le_bytes());
                r.extend_from_slice(&(ev.len() as u16).to_le_bytes());
                r.extend_from_slice(ev);
                out.write(&r)?;
                ord += 1;
                stats.events += 1;
                // a completing trade (real tokens left == 0): watch its canonical pool
                if ev.len() >= 129 && ev[..8] == TRADE_EVENT && u64::from_le_bytes(ev[121..129].try_into()?) == 0 {
                    let mint = Pubkey::try_from(&ev[8..40]).unwrap();
                    let pool = canonical_pool(&mint);
                    if !st.watch.contains_key(&pool) { st.watch_dirty = true; }
                    st.watch.insert(pool, (mint, Instant::now() + Duration::from_secs(watch_s())));
                }
            }
        }
        if ord == 0 {
            for line in &meta.log_messages {
                if let Some(b) = line.strip_prefix("Program data: ") {
                    if let Ok(ev) = base64::engine::general_purpose::STANDARD.decode(b) {
                        if ev.len() < 8 { continue; }
                        let mut r = header(1, recv, slot, idx, sig, payer);
                        r.extend_from_slice(&(ord | 0x8000).to_le_bytes());
                        r.extend_from_slice(&(ev.len() as u16).to_le_bytes());
                        r.extend_from_slice(&ev);
                        out.write(&r)?;
                        ord += 1;
                        stats.events += 1;
                    }
                }
            }
        }
        if has(&mig) {
            out.write(&header(3, recv, slot, idx, sig, payer))?;
            stats.migrates += 1;
        }
    }
    // pool points for watched pools referenced by this tx
    if !st.watch.is_empty() {
        let wsol = WSOL.to_string();
        for (pool, (mint, _)) in st.watch.iter() {
            if !has(pool) { continue; }
            let pool_s = pool.to_string();
            let mint_s = mint.to_string();
            let mut base = None;
            let mut quote = None;
            for b in &meta.post_token_balances {
                if b.owner != pool_s { continue; }
                let amt = b.ui_token_amount.as_ref().and_then(|a| a.amount.parse::<u64>().ok());
                if b.mint == mint_s { base = amt; } else if b.mint == wsol { quote = amt; }
            }
            if let (Some(bq), Some(qq)) = (base, quote) {
                let mut r = header(2, recv, slot, idx, sig, payer);
                r.extend_from_slice(pool.as_ref());
                r.extend_from_slice(mint.as_ref());
                r.extend_from_slice(&bq.to_le_bytes());
                r.extend_from_slice(&qq.to_le_bytes());
                out.write(&r)?;
                stats.pool_pts += 1;
            }
        }
    }
    Ok(())
}

fn watch_s() -> u64 { std::env::var("POOL_WATCH_S").ok().and_then(|s| s.parse().ok()).unwrap_or(300) }

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let _ = rustls::crypto::ring::default_provider().install_default();
    let key = std::env::var("HELIUS_API_KEY").context("HELIUS_API_KEY")?;
    let endpoint = std::env::var("LASERSTREAM_ENDPOINT").context("LASERSTREAM_ENDPOINT")?;
    let dir = std::env::var("OUT_DIR").context("OUT_DIR")?;
    let min_free_gb: u64 = std::env::var("MIN_FREE_GB").ok().and_then(|s| s.parse().ok()).unwrap_or(8);
    std::fs::create_dir_all(&dir)?;
    let mut out = Out { dir, run: now_ns() / 1_000_000_000, hour: String::new(), enc: None, bytes: 0, last_flush: Instant::now(),
                        min_free: min_free_gb << 30, paused: false };
    let mut st = State { watch: HashMap::new(), watch_dirty: false };
    let mut stats = Stats { txs: 0, events: 0, pool_pts: 0, migrates: 0 };
    let mut backoff = 1u64;
    loop {
        let res: anyhow::Result<()> = async {
            let mut client = GeyserGrpcClient::build_from_shared(endpoint.clone())?
                .x_token(Some(key.clone()))?
                .tls_config(ClientTlsConfig::new().with_native_roots())?
                .max_decoding_message_size(64 << 20)
                .connect_timeout(Duration::from_secs(10))
                .connect().await?;
            let (mut sink, mut stream) = client.subscribe_with_request(Some(build_request(&st))).await?;
            eprintln!("subscribed ({} pools watched)", st.watch.len());
            out.write(&header(9, now_ns(), 0, 0, &[], &[]))?;
            backoff = 1;
            let mut ping = tokio::time::interval(Duration::from_secs(10));
            let mut tick = tokio::time::interval(Duration::from_secs(60));
            let mut last_bytes = out.bytes;
            let mut ping_id = 0;
            loop {
                tokio::select! {
                    _ = ping.tick() => {
                        ping_id += 1;
                        sink.send(SubscribeRequest { ping: Some(SubscribeRequestPing { id: ping_id }), ..Default::default() }).await?;
                        // expire watched pools; re-send filters when the set changed
                        let now = Instant::now();
                        let before = st.watch.len();
                        st.watch.retain(|_, (_, exp)| *exp > now);
                        if st.watch.len() != before { st.watch_dirty = true; }
                        if st.watch_dirty {
                            sink.send(build_request(&st)).await?;
                            st.watch_dirty = false;
                        }
                    }
                    _ = tick.tick() => {
                        eprintln!("txs {} events {} pool_pts {} migrates {} watched {} raw_kB/min {}",
                                  stats.txs, stats.events, stats.pool_pts, stats.migrates, st.watch.len(),
                                  (out.bytes - last_bytes) / 1024);
                        last_bytes = out.bytes;
                    }
                    m = stream.next() => {
                        let Some(m) = m else { anyhow::bail!("stream ended") };
                        let m = m?;
                        let recv = now_ns();
                        match m.update_oneof {
                            Some(subscribe_update::UpdateOneof::Transaction(u)) => handle_tx(u, recv, &mut st, &mut out, &mut stats)?,
                            Some(subscribe_update::UpdateOneof::Ping(_)) => {
                                sink.send(SubscribeRequest { ping: Some(SubscribeRequestPing { id: 1 }), ..Default::default() }).await?;
                            }
                            _ => {}
                        }
                    }
                }
            }
        }.await;
        if let Err(e) = res { eprintln!("stream error: {e:#}; reconnecting in {backoff}s"); }
        tokio::time::sleep(Duration::from_secs(backoff)).await;
        backoff = (backoff * 2).min(30);
    }
}
