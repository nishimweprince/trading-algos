//! `pk` research subcommands on our own tape.

use std::collections::BTreeSet;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use pk_core::StrategyParams;
use pk_core::journal::JsonlDaily;
use pk_research::replay::{ALL_ASSETS, ReplayConfig, RunArgs};
use serde_json::{Value, json};

#[derive(clap::Args, Debug, Clone)]
pub struct Strategy {
    #[arg(long, default_value = ALL_ASSETS)]
    assets: String,
    #[arg(long, default_value_t = 0.10)]
    gap: f64,
    #[arg(long, default_value_t = 0.60)]
    cap: f64,
    #[arg(long, default_value_t = 50.0)]
    notional: f64,
    #[arg(long, default_value_t = 20)]
    early: i64,
    #[arg(long, default_value_t = 60)]
    late: i64,
    #[arg(long, default_value_t = 150)]
    latency_ms: i64,
    #[arg(long, default_value_t = 0.0)]
    limit_offset: f64,
    /// Per-venue delay, e.g. k=9,p=-4 (PLAN 3.1).
    #[arg(long, default_value = "")]
    shift_ms: String,
}

impl Strategy {
    fn cfg(&self, shadow: String) -> ReplayConfig {
        ReplayConfig {
            params: StrategyParams {
                gap_threshold: self.gap,
                max_entry_px: self.cap,
                notional_usd: self.notional,
                entry_early_s: self.early,
                entry_late_s: self.late,
                ..Default::default()
            },
            assets: self
                .assets
                .split(',')
                .map(|a| a.trim().to_uppercase())
                .filter(|a| !a.is_empty())
                .collect(),
            latency_ms: self.latency_ms,
            start_cash: 1e9,
            shadow,
            limit_offset: self.limit_offset,
        }
    }
}

#[derive(clap::Subcommand, Debug)]
pub enum Replay {
    /// Replay the tape through the live engine: journal/, grid.parquet, summary.json.
    Run {
        #[arg(long)]
        record_dir: PathBuf,
        #[arg(long)]
        out: PathBuf,
        #[arg(long)]
        start: Option<String>,
        #[arg(long)]
        end: Option<String>,
        #[command(flatten)]
        strategy: Strategy,
        /// Shadow variants JSON ('' for none); default the live service's.
        #[arg(long, default_value = pk_core::shadow::DEFAULT_VARIANTS)]
        shadow: String,
        /// Polymarket signals, Kalshi executes and settles (research).
        #[arg(long)]
        reverse: bool,
    },
    /// Decisions of a live and a replayed journal, matched by window.
    Compare {
        #[arg(long)]
        live: PathBuf,
        #[arg(long)]
        replay: PathBuf,
    },
}

#[derive(clap::Subcommand, Debug)]
pub enum Fills {
    /// Replay once and tap every entry's ask ladder for a second.
    Run {
        #[arg(long)]
        record_dir: PathBuf,
        #[arg(long)]
        out: PathBuf,
        #[arg(long)]
        start: Option<String>,
        #[arg(long)]
        end: Option<String>,
        #[command(flatten)]
        strategy: Strategy,
    },
    /// Fill every latency x limit offset from a run.
    Grid {
        #[arg(long)]
        run: PathBuf,
        #[arg(long, default_value_t = 0.60)]
        cap: f64,
    },
}

fn print(v: &Value) {
    println!("{}", serde_json::to_string_pretty(v).unwrap_or_default());
}

pub fn replay(cmd: Replay) -> anyhow::Result<()> {
    match cmd {
        Replay::Compare { live, replay } => {
            let a = pk_core::journal::read_journal(&live)?;
            let b = pk_core::journal::read_journal(&replay)?;
            print(&pk_research::replay::compare(&a, &b));
        }
        Replay::Run { record_dir, out, start, end, strategy, shadow: _, reverse } if reverse => {
            let files = pk_tape::iter_files(&record_dir, start.as_deref(), end.as_deref());
            anyhow::ensure!(!files.is_empty(), "no recordings under {}", record_dir.display());
            std::fs::create_dir_all(&out)?;
            let cfg = strategy.cfg(String::new());
            let mut ec = pk_engine::EngineConfig::new(cfg.params.clone(), cfg.assets.clone());
            ec.venue = Some(pk_engine::PaperVenue::new(cfg.latency_ms));
            ec.limit_offset = cfg.limit_offset;
            ec.direction = pk_engine::Direction::Reverse;
            let risk = pk_core::RiskState::new(pk_core::RiskLimits {
                max_open_notional_usd: 1e12,
                daily_loss_limit_usd: 1e12,
                kill_file: None,
            });
            let mut engine = pk_engine::Engine::new(
                ec,
                pk_core::Ledger::new(1e9),
                risk,
                Box::new(JsonlDaily::new(out.join("journal"))),
            );
            engine.metrics = pk_core::metrics::Metrics::new(2_000_000);
            let t0 = Instant::now();
            let mut reader = pk_tape::read_lines(files.clone());
            let shift = pk_research::replay::parse_shift(&strategy.shift_ms)?;
            if shift.values().any(|v| *v != 0.0) {
                pk_research::replay::replay(
                    pk_research::replay::shift_lines(reader.by_ref(), &shift),
                    &mut engine,
                    None,
                    |_, _, _| {},
                );
            } else {
                pk_research::replay::replay(reader.by_ref(), &mut engine, None, |_, _, _| {});
            }
            let records = pk_core::journal::read_journal(&out.join("journal")).unwrap_or_default();
            let latency = engine.metrics.snapshot();
            let mut summary = json!({
                "files": files.len(), "lines": reader.stats.lines, "elapsed_s": t0.elapsed().as_secs_f64(),
                "reader": reader.stats, "counters": engine.counters, "feed_ms_k": latency.get("feed_ms_k"),
                "feed_ms_p": latency.get("feed_ms_p"), "direction": "reverse",
            });
            if let (Some(s), Some(r)) =
                (summary.as_object_mut(), pk_research::reverse::summarize(&records).as_object())
            {
                s.extend(r.clone());
            }
            std::fs::write(out.join("summary.json"), serde_json::to_vec_pretty(&summary)?)?;
            print(&summary);
        }
        Replay::Run { record_dir, out, start, end, strategy, shadow, .. } => {
            let args = RunArgs {
                record_dir,
                out,
                start,
                end,
                shift_ms: pk_research::replay::parse_shift(&strategy.shift_ms)?,
                cfg: strategy.cfg(shadow),
            };
            print(&pk_research::replay::run(&args)?);
        }
    }
    Ok(())
}

pub fn nulls(run: PathBuf, cap: f64, early: i64, late: i64, seed: u64) -> anyhow::Result<()> {
    let grid = pk_research::frame::Frame::read_parquet(&run.join("grid.parquet"))?;
    let journal = pk_core::journal::read_journal(&run.join("journal"))?;
    let result =
        pk_research::nulls::summarise(&pk_research::nulls::arms(&grid, &journal, cap, early, late, seed));
    std::fs::write(run.join("nulls.json"), serde_json::to_vec_pretty(&result)?)?;
    print(&result);
    Ok(())
}

pub fn readout(journal: PathBuf, cap: f64, epoch: Option<String>, exclude: String) -> anyhow::Result<()> {
    let records = pk_core::journal::read_journal(&journal)?;
    let ex: BTreeSet<String> =
        exclude.split(',').map(|a| a.trim().to_uppercase()).filter(|a| !a.is_empty()).collect();
    print(&pk_research::readout::readout(records, cap, epoch.as_deref(), &ex));
    Ok(())
}

pub fn fills(cmd: Fills) -> anyhow::Result<()> {
    match cmd {
        Fills::Grid { run, cap } => {
            let rows = pk_research::fills::read_rows(&run.join("fills.jsonl.zst"))?;
            let journal = pk_core::journal::read_journal(&run.join("journal"))?;
            let summary: Value = serde_json::from_slice(&std::fs::read(run.join("summary.json"))?)?;
            let latency = summary["params"]["latency_ms"].as_i64().unwrap_or(150);
            let result = pk_research::fills::grid(&rows, &journal, latency, cap);
            std::fs::write(run.join("fill_grid.json"), serde_json::to_vec_pretty(&result)?)?;
            let mut short = result.clone();
            short.as_object_mut().map(|o| o.remove("cells"));
            print(&short);
        }
        Fills::Run { record_dir, out, start, end, strategy } => {
            let files = pk_tape::iter_files(&record_dir, start.as_deref(), end.as_deref());
            anyhow::ensure!(!files.is_empty(), "no recordings under {}", record_dir.display());
            std::fs::create_dir_all(&out)?;
            let cfg = strategy.cfg(String::new());
            let mut engine =
                pk_research::replay::build_engine(&cfg, Box::new(JsonlDaily::new(out.join("journal"))))?;
            let mut tap = pk_research::fills::LadderTap::default();
            let mut reader = pk_tape::read_lines(files.clone());
            let shift = pk_research::replay::parse_shift(&strategy.shift_ms)?;
            let mut step = |e: &pk_engine::Engine, l: &pk_tape::TapeLine, before: bool| {
                if before { tap.before(e, l.recv_ns) } else { tap.after(e, l.recv_ns) }
            };
            if shift.values().any(|v| *v != 0.0) {
                pk_research::replay::replay(
                    pk_research::replay::shift_lines(reader.by_ref(), &shift),
                    &mut engine,
                    None,
                    &mut step,
                );
            } else {
                pk_research::replay::replay(reader.by_ref(), &mut engine, None, &mut step);
            }
            tap.write(&out.join("fills.jsonl.zst"))?;
            let latency = engine.metrics.snapshot();
            let summary = json!({
                "files": files.iter().map(|f| f.display().to_string()).collect::<Vec<_>>(),
                "reader": reader.stats, "counters": engine.counters,
                "params": {"latency_ms": cfg.latency_ms, "gap": cfg.params.gap_threshold, "cap": cfg.params.max_entry_px},
                "feed_ms_k": latency.get("feed_ms_k"), "feed_ms_p": latency.get("feed_ms_p"),
            });
            std::fs::write(out.join("summary.json"), serde_json::to_vec_pretty(&summary)?)?;
            print(&json!({"reader": summary["reader"], "counters": summary["counters"]}));
        }
    }
    Ok(())
}

pub fn inventory(record_dir: PathBuf, journal: Option<PathBuf>, out: PathBuf) -> anyhow::Result<()> {
    let rows = pk_research::inventory::inventory(&record_dir, journal.as_deref())?;
    if let Some(dir) = out.parent() {
        std::fs::create_dir_all(dir)?;
    }
    std::fs::write(&out, serde_json::to_vec_pretty(&rows)?)?;
    for r in &rows {
        println!(
            "{} {:7} k={} p={} {:>9} ooo={} trunc={} fills={} {}",
            r["hour"].as_str().unwrap_or_default(),
            r["host"].as_str().unwrap_or_default(),
            r["feed_ms_k"]["p50"],
            r["feed_ms_p"]["p50"],
            r["lines"],
            r["out_of_order"],
            r["truncated"],
            if r["usable_for_fills"] == true { "Y" } else { "n" },
            r["reasons"]
                .as_array()
                .map(|a| a.iter().filter_map(Value::as_str).collect::<Vec<_>>().join("; "))
                .unwrap_or_default()
        );
    }
    Ok(())
}

pub async fn acceptance(hours: f64, every: f64, url: String, out: PathBuf) -> anyhow::Result<()> {
    let key = crate::env::require("API_KEY")?;
    let start = chrono::Utc::now().format("%Y%m%dT%H%M%SZ").to_string();
    std::fs::create_dir_all(&out)?;
    let log = out.join(format!("acceptance_{start}.jsonl"));
    let http = reqwest::Client::builder().timeout(Duration::from_secs(10)).no_proxy().build()?;
    let end = Instant::now() + Duration::from_secs_f64(hours * 3600.0);
    let (mut polls, mut failures) = (Vec::new(), 0u64);
    loop {
        let poll = match http.get(format!("{url}/v1/status")).header("X-API-Key", &key).send().await {
            Ok(r) if r.status().is_success() => match r.json::<Value>().await {
                Ok(body) => {
                    let p = pk_research::acceptance::poll_of(&body);
                    polls.push(p.clone());
                    p
                }
                Err(e) => {
                    failures += 1;
                    json!({"error": e.to_string()})
                }
            },
            other => {
                failures += 1;
                json!({"error": format!("{:?}", other.map(|r| r.status().as_u16()).map_err(|e| e.to_string()))})
            }
        };
        use std::io::Write as _;
        let mut f = std::fs::OpenOptions::new().create(true).append(true).open(&log)?;
        writeln!(f, "{poll}")?;
        if Instant::now() >= end {
            break;
        }
        tokio::time::sleep(Duration::from_secs_f64(every)).await;
    }
    let mut result = pk_research::acceptance::verdict(&polls, failures);
    result["start"] = start.clone().into();
    result["hours"] = hours.into();
    std::fs::write(out.join(format!("acceptance_{start}.json")), serde_json::to_vec_pretty(&result)?)?;
    print(&result);
    Ok(())
}
