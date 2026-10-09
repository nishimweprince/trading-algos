//! `pk`: the poly-kalshi command line.

use std::path::PathBuf;

use clap::{Parser, Subcommand};

mod env;
mod netprobe;
mod probe_cmd;

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
        }
    })
}
