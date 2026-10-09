//! `pk`: the poly-kalshi command line.

use std::path::PathBuf;

use clap::{Parser, Subcommand};

mod env;
mod netprobe;
mod probe_cmd;
mod research_cmd;

#[derive(Parser, Debug)]
#[command(name = "pk", about = "poly-kalshi: order probe, network probe and research")]
struct Cli {
    /// Load `.env.<profile>` from --dir before running.
    #[arg(long, global = true)]
    profile: Option<String>,
    /// The service directory holding `.env.<profile>`.
    #[arg(long, global = true, default_value = ".")]
    dir: PathBuf,
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand, Debug)]
enum Cmd {
    /// Time decision → acknowledgement on the real CLOB with orders that cannot fill.
    ProbeOrders(probe_cmd::Args),
    /// Warm HTTP/2 round trip to both venues' APIs from this host.
    Netprobe(netprobe::Args),
    /// Replay our tape through the live engine, or compare two journals.
    #[command(subcommand)]
    Replay(research_cmd::Replay),
    /// The null battery on a replay run.
    Nulls {
        #[arg(long)]
        run: PathBuf,
        #[arg(long, default_value_t = 0.60)]
        cap: f64,
        #[arg(long, default_value_t = 20)]
        early: i64,
        #[arg(long, default_value_t = 60)]
        late: i64,
        #[arg(long, default_value_t = 7)]
        seed: u64,
    },
    /// The gate read-out from a journal.
    Readout {
        #[arg(long)]
        journal: PathBuf,
        #[arg(long, default_value_t = 0.60)]
        cap: f64,
        #[arg(long)]
        epoch: Option<String>,
        /// e.g. BTC,SOL: read as if they were not traded.
        #[arg(long, default_value = "")]
        exclude_assets: String,
    },
    /// Decide once, fill many (latency x limit offset) on our tape.
    #[command(subcommand)]
    Fills(research_cmd::Fills),
    /// Our tape, one row per hour file, usable or excluded and why.
    Inventory {
        #[arg(long)]
        record_dir: PathBuf,
        #[arg(long)]
        journal: Option<PathBuf>,
        #[arg(long, default_value = "research/data/inventory.json")]
        out: PathBuf,
    },
    /// Phase-2 acceptance: poll the live service's metrics for a day.
    Acceptance {
        #[arg(long, default_value_t = 24.0)]
        hours: f64,
        #[arg(long, default_value_t = 300.0)]
        every: f64,
        #[arg(long, default_value = "http://127.0.0.1:8050")]
        url: String,
        #[arg(long, default_value = "research/data/acceptance")]
        out: PathBuf,
    },
}

fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();
    env::load_profile(cli.profile.as_deref(), &cli.dir)?;
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .init();
    let rt = tokio::runtime::Builder::new_multi_thread().enable_all().build()?;
    rt.block_on(async {
        match cli.cmd {
            Cmd::ProbeOrders(a) => probe_cmd::run(a).await,
            Cmd::Netprobe(a) => netprobe::run(a).await,
            Cmd::Replay(c) => research_cmd::replay(c),
            Cmd::Nulls { run, cap, early, late, seed } => research_cmd::nulls(run, cap, early, late, seed),
            Cmd::Readout { journal, cap, epoch, exclude_assets } => {
                research_cmd::readout(journal, cap, epoch, exclude_assets)
            }
            Cmd::Fills(c) => research_cmd::fills(c),
            Cmd::Inventory { record_dir, journal, out } => research_cmd::inventory(record_dir, journal, out),
            Cmd::Acceptance { hours, every, url, out } => {
                research_cmd::acceptance(hours, every, url, out).await
            }
        }
    })
}
