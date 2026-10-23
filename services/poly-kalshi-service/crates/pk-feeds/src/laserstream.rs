//! Helius LaserStream (`h`): on-chain SOL prices over gRPC (Yellowstone),
//! recorded and timed by the observer, never traded.
//!
//! Three accounts at processed commitment ([`SOL_ACCOUNTS`]): the Orca
//! SOL/USDC whirlpool and the Raydium CLMM pool (both 0.04%), and Pyth's
//! SOL/USD push feed. The frames are protobuf, so unlike the websocket feeds
//! the tape cannot hold them as they came: each account write is decoded here
//! and handed on as one compact line, `{"s","p","slot","mt"}` (`mt`: µs, the
//! Helius node's `created_at`), plus `conf` and `pt` (publish time, s) for
//! Pyth. A pool's price is its sqrt price squared, so one write is one quote.
//!
//! The SDK reconnects on its own and silently, so liveness is ours: a session
//! with no account write for [`SILENCE`] is dropped and a new one subscribed.
//! The pools change price in only ~0.4 slots a second (most transactions that
//! touch them fail), and quiet spells of 20 s are normal, so the bar is a minute. Each session starts with a
//! `pk_reconnect` line, as the other references' connects do.

use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::Duration;

use futures_util::StreamExt;
use helius_laserstream::grpc::subscribe_update::UpdateOneof;
use helius_laserstream::grpc::{CommitmentLevel, SubscribeRequest, SubscribeRequestFilterAccounts};
use helius_laserstream::{LaserstreamConfig, subscribe};
use parking_lot::Mutex;
use serde_json::{Value, json};

use crate::ws::Emit;

/// (name, account): the lead–lag source names of the observer.
pub const SOL_ACCOUNTS: [(&str, &str); 3] = [
    ("orca", "Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE"),
    ("raydium", "3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv"),
    ("pyth", "7UVimffxr9ow1uXYxsr4LHAcV58mLzhmwaeKvJ1pjLiE"),
];

pub const SILENCE: Duration = Duration::from_secs(60);

/// SOL is 9 decimals and USDC 6 in both pools (token A / token 0 is SOL).
const SOL_USDC_SCALE: f64 = 1e3;

fn u128_at(data: &[u8], at: usize) -> Option<u128> {
    data.get(at..at + 16).map(|b| u128::from_le_bytes(b.try_into().expect("16 bytes")))
}

fn sqrt_x64_price(sqrt: u128) -> f64 {
    let r = sqrt as f64 / 2f64.powi(64);
    r * r * SOL_USDC_SCALE
}

/// An Orca whirlpool's price: `sqrt_price` (Q64.64) at byte 65.
pub fn whirlpool_price(data: &[u8]) -> Option<f64> {
    u128_at(data, 65).filter(|s| *s > 0).map(sqrt_x64_price)
}

/// A Raydium CLMM pool's price: `sqrt_price_x64` at byte 253.
pub fn clmm_price(data: &[u8]) -> Option<f64> {
    u128_at(data, 253).filter(|s| *s > 0).map(sqrt_x64_price)
}

/// A Pyth `PriceUpdateV2`: (price, conf, publish time s). The message starts
/// after the verification level, one byte when `Full` (1), two when `Partial`.
pub fn pyth_price(data: &[u8]) -> Option<(f64, f64, i64)> {
    let msg = if *data.get(40)? == 1 { 41 } else { 42 };
    let i64_at = |at: usize| data.get(at..at + 8).map(|b| i64::from_le_bytes(b.try_into().expect("8 bytes")));
    let price = i64_at(msg + 32)?;
    let conf = i64_at(msg + 40)? as u64;
    let expo = i32::from_le_bytes(data.get(msg + 48..msg + 52)?.try_into().ok()?);
    let publish = i64_at(msg + 52)?;
    let scale = 10f64.powi(expo);
    (price > 0).then(|| (price as f64 * scale, conf as f64 * scale, publish))
}

/// The compact line for one account write, or `None` when it does not decode.
pub fn quote_line(name: &str, data: &[u8], slot: u64, created_us: Option<i64>) -> Option<String> {
    let mut v = json!({"s": name, "slot": slot, "mt": created_us});
    match name {
        "orca" => v["p"] = whirlpool_price(data)?.into(),
        "raydium" => v["p"] = clmm_price(data)?.into(),
        "pyth" => {
            let (p, conf, pt) = pyth_price(data)?;
            v["p"] = p.into();
            v["conf"] = conf.into();
            v["pt"] = pt.into();
        }
        _ => return None,
    }
    Some(v.to_string())
}

#[derive(Default)]
struct Shared {
    connected: AtomicBool,
    connects: AtomicU64,
    messages: AtomicU64,
    errors: AtomicU64,
    last_slot: AtomicU64,
    last_error: Mutex<Option<String>>,
}

/// The runtime's handle on the stream.
#[derive(Clone)]
pub struct LaserHandle {
    shared: Arc<Shared>,
}

impl LaserHandle {
    pub fn connected(&self) -> bool {
        self.shared.connected.load(Ordering::Relaxed)
    }

    pub fn stats(&self) -> Value {
        let s = &self.shared;
        json!({
            "connected": self.connected(),
            "connects": s.connects.load(Ordering::Relaxed),
            "messages": s.messages.load(Ordering::Relaxed),
            "errors": s.errors.load(Ordering::Relaxed),
            "last_error": s.last_error.lock().clone(),
            "last_slot": s.last_slot.load(Ordering::Relaxed),
            "subscribed": SOL_ACCOUNTS.len(),
        })
    }
}

fn set_error(shared: &Shared, e: String) {
    shared.errors.fetch_add(1, Ordering::Relaxed);
    *shared.last_error.lock() = Some(e.chars().take(200).collect());
}

pub fn spawn_laserstream(url: String, api_key: String, emit: Emit) -> LaserHandle {
    let shared = Arc::new(Shared::default());
    tokio::spawn(run(url, api_key, emit, shared.clone()));
    LaserHandle { shared }
}

fn request() -> SubscribeRequest {
    let accounts = SubscribeRequestFilterAccounts {
        account: SOL_ACCOUNTS.iter().map(|(_, a)| (*a).to_owned()).collect(),
        ..Default::default()
    };
    SubscribeRequest {
        accounts: HashMap::from([("sol".to_owned(), accounts)]),
        commitment: Some(CommitmentLevel::Processed as i32),
        ..Default::default()
    }
}

async fn run(url: String, api_key: String, emit: Emit, shared: Arc<Shared>) {
    let names: HashMap<Vec<u8>, &str> = SOL_ACCOUNTS
        .iter()
        .map(|(n, a)| (bs58::decode(a).into_vec().expect("a base58 account"), *n))
        .collect();
    loop {
        // No replay: a late write would read as a late quote.
        let config = LaserstreamConfig::new(url.clone(), api_key.clone()).with_replay(false);
        let (stream, _handle) = subscribe(config, request());
        let mut stream = std::pin::pin!(stream);
        let mut started = false;
        loop {
            let next = match tokio::time::timeout(SILENCE, stream.next()).await {
                Ok(Some(next)) => next,
                Ok(None) => {
                    set_error(&shared, "stream ended".into());
                    break;
                }
                Err(_) => {
                    set_error(&shared, format!("no account write for {} s", SILENCE.as_secs()));
                    break;
                }
            };
            let update = match next {
                Ok(u) => u,
                Err(e) => {
                    set_error(&shared, format!("{e}"));
                    break;
                }
            };
            let Some(UpdateOneof::Account(acc)) = update.update_oneof else { continue };
            let Some(info) = acc.account else { continue };
            if !started {
                started = true;
                let n = shared.connects.fetch_add(1, Ordering::Relaxed) + 1;
                shared.connected.store(true, Ordering::Relaxed);
                emit('h', json!({"type": "pk_reconnect", "venue": "h", "n": n}).to_string());
                tracing::info!(venue = "h", n, "feed_connected");
            }
            shared.messages.fetch_add(1, Ordering::Relaxed);
            shared.last_slot.fetch_max(acc.slot, Ordering::Relaxed);
            let Some(name) = names.get(&info.pubkey) else { continue };
            let created_us = update.created_at.map(|t| t.seconds * 1_000_000 + i64::from(t.nanos) / 1000);
            match quote_line(name, &info.data, acc.slot, created_us) {
                Some(line) => emit('h', line),
                None => set_error(&shared, format!("{name}: undecodable write at slot {}", acc.slot)),
            }
        }
        shared.connected.store(false, Ordering::Relaxed);
        tracing::warn!(venue = "h", error = ?shared.last_error.lock().clone(), "feed_error");
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const REAL_ORCA_SQRT: &str = "Uhii82YWdlIAAAAAAAAAAA==";
    const REAL_RAY_SQRT: &str = "TBAvug71dVIAAAAAAAAAAA==";
    const REAL_PYTH: &str = "IvEjY51+9M1gMUcENA3t3zcf1CRyFI8kjp0abRpesqw6zYt/1dayQwHvDYtv2izrpB2hXUCV0do5Kg0vjtDGx7wPTPrIwoC1bUKZOWoCAAAAwaI6AAAAAAD4////JazaagAAAAAkrNpqAAAAAMxjAGkCAAAAZgxAAAAAAADFuW4bAAAAAAA=";

    fn with_sqrt(len: usize, at: usize, price: f64) -> Vec<u8> {
        let mut d = vec![0u8; len];
        let sqrt = ((price / SOL_USDC_SCALE).sqrt() * 2f64.powi(64)) as u128;
        d[at..at + 16].copy_from_slice(&sqrt.to_le_bytes());
        d
    }

    #[test]
    fn pools_decode_their_sqrt_price() {
        let w = whirlpool_price(&with_sqrt(653, 65, 103.7)).unwrap();
        let r = clmm_price(&with_sqrt(1544, 253, 103.7)).unwrap();
        assert!((w - 103.7).abs() < 1e-9 && (r - 103.7).abs() < 1e-9, "{w} {r}");
        assert_eq!(whirlpool_price(&[0u8; 40]), None); // short
        assert_eq!(clmm_price(&vec![0u8; 1544]), None); // zero
    }

    #[test]
    fn real_mainnet_accounts_decode() {
        use base64::Engine;
        let b64 = |s: &str| base64::engine::general_purpose::STANDARD.decode(s).unwrap();
        // Fields at the decoded offsets of each account as read 2026-10-23.
        let mut orca = vec![0u8; 653];
        orca[65..81].copy_from_slice(&b64(REAL_ORCA_SQRT));
        let mut ray = vec![0u8; 1544];
        ray[253..269].copy_from_slice(&b64(REAL_RAY_SQRT));
        let pyth = b64(REAL_PYTH);
        let (w, r) = (whirlpool_price(&orca).unwrap(), clmm_price(&ray).unwrap());
        let (p, conf, pt) = pyth_price(&pyth).unwrap();
        for x in [w, r, p] {
            assert!((100.0..110.0).contains(&x), "{w} {r} {p}");
        }
        assert!(conf > 0.0 && conf < 1.0 && pt > 1_790_000_000, "{conf} {pt}");
    }

    #[test]
    fn pyth_partial_verification_shifts_the_message() {
        let mut d = vec![0u8; 135];
        d[40] = 0; // Partial { num_signatures }
        let msg = 42;
        d[msg + 32..msg + 40].copy_from_slice(&10_370_000_000i64.to_le_bytes());
        d[msg + 40..msg + 48].copy_from_slice(&5_000_000u64.to_le_bytes());
        d[msg + 48..msg + 52].copy_from_slice(&(-8i32).to_le_bytes());
        d[msg + 52..msg + 60].copy_from_slice(&1_792_715_492i64.to_le_bytes());
        let (p, conf, pt) = pyth_price(&d).unwrap();
        assert!((p - 103.7).abs() < 1e-9 && (conf - 0.05).abs() < 1e-12 && pt == 1_792_715_492);
    }

    #[test]
    fn lines_are_compact() {
        let line = quote_line("orca", &with_sqrt(653, 65, 100.0), 42, Some(1_792_715_492_123_456)).unwrap();
        let v: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(v["s"], "orca");
        assert_eq!(v["slot"], 42);
        assert_eq!(v["mt"], 1_792_715_492_123_456i64);
        assert!((v["p"].as_f64().unwrap() - 100.0).abs() < 1e-9);
        assert_eq!(quote_line("phoenix", &[0u8; 700], 1, None), None);
        assert!(SOL_ACCOUNTS.iter().all(|(_, a)| bs58::decode(a).into_vec().unwrap().len() == 32));
    }
}
