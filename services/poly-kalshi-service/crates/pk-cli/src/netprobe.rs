//! `pk netprobe`: round trip to each venue's API from this host on warm HTTP/2
//! connections (PLAN 5.1, and the AWS eu-west-1 vs Azure side-by-side).
//!
//! One GET per venue per `--every` seconds: Polymarket's CLOB `/time` and
//! Kalshi's `/exchange/status`. Not ICMP: a ping measures the CDN's edge. Writes
//! `netprobe_<start>.json` with p50/p99/max per venue. Move hosts only if p50
//! improves by > 2 ms or p99 by > 10 ms.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use chrono::Utc;
use pk_exec::{WarmConfig, WarmHttp};
use reqwest::Method;
use serde_json::json;

#[derive(clap::Args, Debug)]
pub struct Args {
    #[arg(long, default_value_t = 60.0)]
    minutes: f64,
    #[arg(long, default_value_t = 1.0)]
    every: f64,
    #[arg(long, default_value = "research/data/netprobe")]
    out: PathBuf,
    #[arg(long, env = "POLY_CLOB_URL", default_value = "https://clob.polymarket.com")]
    clob_url: String,
    #[arg(long, env = "KALSHI_REST_URL", default_value = "https://api.elections.kalshi.com/trade-api/v2")]
    kalshi_url: String,
}

fn pct(mut v: Vec<f64>) -> serde_json::Value {
    if v.is_empty() {
        return json!({"n": 0});
    }
    v.sort_by(f64::total_cmp);
    let at = |q: f64| (v[((v.len() - 1) as f64 * q) as usize] * 100.0).round() / 100.0;
    json!({"n": v.len(), "p50": at(0.5), "p99": at(0.99), "max": at(1.0), "min": at(0.0)})
}

pub async fn run(args: Args) -> anyhow::Result<()> {
    let start = Utc::now().format("%Y%m%dT%H%M%SZ").to_string();
    let kalshi = url::Url::parse(&args.kalshi_url)?;
    let targets = [
        ("polymarket_clob", args.clob_url.parse::<url::Url>()?, "/time".to_owned()),
        ("kalshi", kalshi.join("/")?, format!("{}/exchange/status", kalshi.path().trim_end_matches('/'))),
    ];
    let mut conns = Vec::new();
    for (name, base, path) in &targets {
        let mut cfg = WarmConfig::new(base.clone());
        cfg.warm_path = path.clone();
        conns.push((*name, WarmHttp::connect(cfg).await?, path.clone()));
    }
    let mut rtt: BTreeMap<&str, Vec<f64>> = BTreeMap::new();
    let mut errors: BTreeMap<&str, u64> = BTreeMap::new();
    let end = Instant::now() + Duration::from_secs_f64(args.minutes * 60.0);
    let mut tick = tokio::time::interval(Duration::from_secs_f64(args.every));
    while Instant::now() < end {
        tick.tick().await;
        for (name, http, path) in &conns {
            let t0 = Instant::now();
            let ok = match http.send(Method::GET, path, &[], None).await {
                Ok(r) if r.status().is_success() => r.bytes().await.is_ok(),
                _ => false,
            };
            if ok {
                rtt.entry(name).or_default().push(t0.elapsed().as_secs_f64() * 1e3);
            } else {
                *errors.entry(name).or_default() += 1;
            }
        }
    }
    let host = std::process::Command::new("hostname")
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_owned())
        .unwrap_or_default();
    let result = json!({
        "start": start,
        "host": host,
        "minutes": args.minutes,
        "http": "2, warm (connection set up before the first sample)",
        "rtt_ms": rtt.into_iter().map(|(k, v)| (k, pct(v))).collect::<BTreeMap<_, _>>(),
        "errors": errors,
        "connections": conns.iter().map(|(n, h, _)| (n.to_string(), h.stats())).collect::<BTreeMap<_, _>>(),
    });
    std::fs::create_dir_all(&args.out)?;
    std::fs::write(args.out.join(format!("netprobe_{start}.json")), serde_json::to_vec_pretty(&result)?)?;
    println!("{}", serde_json::to_string_pretty(&result)?);
    Ok(())
}
