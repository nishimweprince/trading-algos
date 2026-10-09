//! `pk helix …`: research on the Helix kpc tape (`$HELIX_TAPE_DIR`, else `~/kpc/raw`).

use std::path::PathBuf;

use pk_research::helix;

#[derive(clap::Subcommand, Debug)]
pub enum Helix {
    /// Check the decoder on a day before any number depends on it.
    Validate {
        #[arg(long)]
        day: String,
        #[arg(long)]
        root: Option<PathBuf>,
        /// Windows to check against Gamma (cached).
        #[arg(long, default_value_t = 40)]
        gamma: usize,
        #[arg(long, default_value = "research/data")]
        out: PathBuf,
    },
    /// Every window's settlement from Gamma and Kalshi, joined to the tape.
    Resolutions {
        #[arg(long)]
        root: Option<PathBuf>,
        #[arg(long, default_value = "research/data")]
        out: PathBuf,
    },
    /// Every window of every day through the live decide(), per variant.
    Replay {
        #[arg(long)]
        root: Option<PathBuf>,
        /// YYYYMMDD-YYYYMMDD or a comma list (default: all).
        #[arg(long)]
        days: Option<String>,
        #[arg(long, default_value = "research/data/helix")]
        out: PathBuf,
        #[arg(long, default_value = "research/data/resolutions.parquet")]
        resolutions: PathBuf,
        /// Days in parallel (about 3 GB each).
        #[arg(long, default_value_t = 3)]
        jobs: usize,
        /// Per-venue delay, e.g. k=9,p=-4.
        #[arg(long, default_value = "")]
        shift_ms: String,
        /// Redo days already written.
        #[arg(long)]
        force: bool,
        /// Stop before the disk falls under this many GB (PLAN 3.3).
        #[arg(long, default_value_t = helix::replay::MIN_FREE_GB)]
        min_free_gb: f64,
    },
    /// The sweeps, the null battery and the price-side latency curve on a split.
    Sweeps {
        #[arg(long, value_parser = ["discovery", "holdout", "earlier"])]
        split: String,
        #[arg(long, default_value = "research/data/helix")]
        data: PathBuf,
        #[arg(long, default_value = "research/data/helix_inventory.json")]
        inventory: PathBuf,
        #[arg(long, default_value = "research/data/resolutions.parquet")]
        resolutions: PathBuf,
        /// No latency pass (faster).
        #[arg(long)]
        skip_slices: bool,
        #[arg(long, default_value = "research/PREREG.md")]
        prereg: PathBuf,
    },
    /// Who moves first: spot, Chainlink, Kalshi or Polymarket.
    Leadlag {
        /// Comma list of YYYYMMDD.
        #[arg(long)]
        days: String,
        #[arg(long)]
        root: Option<PathBuf>,
        #[arg(long, default_value_t = 20)]
        bin_ms: i64,
        #[arg(long, default_value = "research/data/helix_leadlag.json")]
        out: PathBuf,
    },
    /// A Chainlink fair-value signal against the Kalshi gap.
    Fairvalue {
        #[arg(long, value_parser = ["discovery", "holdout"])]
        split: String,
        #[arg(long, default_value = "research/data/helix")]
        data: PathBuf,
        #[arg(long, default_value = "research/data/helix_inventory.json")]
        inventory: PathBuf,
        #[arg(long, default_value = "research/data/resolutions.parquet")]
        resolutions: PathBuf,
    },
    /// One row per stream per day, and each day usable or excluded.
    Inventory {
        #[arg(long)]
        root: Option<PathBuf>,
        #[arg(long, default_value = "research/data/helix")]
        replay: PathBuf,
        #[arg(long, default_value = "research/data/resolutions.parquet")]
        resolutions: PathBuf,
        /// sha256sum manifest (default ~/kpc/kpc_tier2.sha256).
        #[arg(long)]
        manifest: Option<PathBuf>,
        /// `sha256sum -c` output.
        #[arg(long)]
        checksums: Option<PathBuf>,
        #[arg(long, default_value = "research/data/helix_inventory.json")]
        out: PathBuf,
    },
}

fn print(v: &serde_json::Value) -> anyhow::Result<()> {
    println!("{}", serde_json::to_string_pretty(v)?);
    Ok(())
}

pub fn run(cmd: Helix) -> anyhow::Result<()> {
    match cmd {
        Helix::Validate { day, root, gamma, out } => {
            print(&helix::validate::run(&day, &helix::tape_dir(root.as_deref()), gamma, &out)?)
        }
        Helix::Resolutions { root, out } => {
            let s = helix::resolutions::run(&helix::tape_dir(root.as_deref()), &out)?;
            print(&serde_json::json!({"first_twap_day": s["first_twap_day"]}))
        }
        Helix::Replay { root, days, out, resolutions, jobs, shift_ms, force, min_free_gb } => {
            helix::replay::run(&helix::replay::RunArgs {
                root: helix::tape_dir(root.as_deref()),
                days,
                out,
                resolutions,
                jobs,
                shift_ms: pk_research::replay::parse_shift(&shift_ms)?,
                force,
                min_free_gb,
            })
        }
        Helix::Sweeps { split, data, inventory, resolutions, skip_slices, prereg } => {
            let r = helix::sweeps::run(&helix::sweeps::Args {
                split,
                data,
                inventory,
                resolutions,
                skip_slices,
                prereg,
            })?;
            print(&serde_json::json!({"split": r["split"], "variants": r["variants"]}))
        }
        Helix::Leadlag { days, root, bin_ms, out } => print(&helix::leadlag::run(&helix::leadlag::Args {
            days: days.split(',').filter(|d| !d.is_empty()).map(str::to_owned).collect(),
            root: helix::tape_dir(root.as_deref()),
            bin_ms,
            out,
        })?),
        Helix::Fairvalue { split, data, inventory, resolutions } => {
            let r = helix::fairvalue::run(&helix::fairvalue::Args { split, data, inventory, resolutions })?;
            print(
                &serde_json::json!({"settlement": r["settlement"], "kalshi_gap_per_second": r["kalshi_gap_per_second"]}),
            )
        }
        Helix::Inventory { root, replay, resolutions, manifest, checksums, out } => {
            let root = helix::tape_dir(root.as_deref());
            let manifest = manifest.unwrap_or_else(|| {
                std::env::var("HOME").map(PathBuf::from).unwrap_or_default().join("kpc/kpc_tier2.sha256")
            });
            let failures = helix::inventory::failures(checksums.as_deref());
            let body = helix::inventory::build(
                &root,
                &replay,
                &resolutions,
                failures.as_ref(),
                &helix::inventory::manifest(&manifest),
            )?;
            if let Some(p) = out.parent() {
                std::fs::create_dir_all(p)?;
            }
            std::fs::write(&out, serde_json::to_string_pretty(&body)?)?;
            for (day, d) in body["days"].as_object().into_iter().flatten() {
                let verdict = if d["usable"] == true {
                    "usable".to_owned()
                } else {
                    d["reasons"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .filter_map(|r| r.as_str())
                        .collect::<Vec<_>>()
                        .join("; ")
                };
                println!(
                    "{day} {} {} {verdict}",
                    d["regime"].as_str().unwrap_or("?"),
                    d["role"].as_str().unwrap_or("?")
                );
            }
            Ok(())
        }
    }
}
