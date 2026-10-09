//! Replay the tape through the live engine, and compare with what the live
//! process did.
//!
//! ```text
//! pk replay run --record-dir data/raw/dev --out research/data/run1
//! pk replay compare --live data/state/dev/journal --replay research/data/run1/journal
//! ```
//!
//! `run` pushes every recorded line through `Engine::handle_line` with a fresh
//! ledger and the paper venue, exactly as live, and writes `journal/`,
//! `grid.parquet` (one row per asset per second of every identified window,
//! all three legs' quotes at the end of that second, for the null battery)
//! and `summary.json`. `compare` matches decisions by (asset, window): on the
//! same tape they must agree exactly.
//!
//! `--shift-ms k=…,p=…` re-times a tape (PLAN 3.1): each venue's lines are
//! delayed by its shift and the streams re-merged by the new time.

use std::collections::{BTreeMap, BinaryHeap};
use std::path::{Path, PathBuf};

use pk_core::journal::{JsonlDaily, Sink};
use pk_core::metrics::Metrics;
use pk_core::shadow::{Shadow, parse_variants};
use pk_core::{Ledger, NS, RiskLimits, RiskState, StrategyParams, WINDOW_S};
use pk_engine::{Engine, EngineConfig, PaperVenue, Role};
use pk_tape::TapeLine;
use serde_json::{Value, json};

use crate::frame::{Col, Frame};

pub const ALL_ASSETS: &str = "BTC,ETH,SOL,XRP,BNB,DOGE,HYPE";

/// Per-second snapshots of each asset's three quotes, taken before the next line.
#[derive(Default)]
pub struct GridSampler {
    pub asset: Vec<String>,
    pub open_unix: Vec<i64>,
    pub sec: Vec<i64>,
    pub legs: [Vec<f64>; 6],
    last_sec: Option<i64>,
}

impl GridSampler {
    pub fn before(&mut self, engine: &Engine, recv_ns: i64) {
        let sec = recv_ns.div_euclid(NS);
        let Some(last) = self.last_sec else {
            self.last_sec = Some(sec);
            return;
        };
        // The book is constant between lines: the state now is the state at
        // the end of every whole second since the last line.
        for s in last..sec.min(last + WINDOW_S) {
            self.sample(engine, s);
        }
        self.last_sec = Some(sec);
    }

    fn sample(&mut self, engine: &Engine, sec: i64) {
        for (idx, st) in engine.state.iter().enumerate() {
            let Some(w) = &st.window else { continue };
            if !(w.open_unix <= sec && sec < w.close_unix()) {
                continue;
            }
            let Some((k, y, n)) = engine.quotes(idx) else { continue };
            if !(k.valid && y.valid && n.valid) {
                continue;
            }
            self.asset.push(engine.assets[idx].clone());
            self.open_unix.push(w.open_unix);
            self.sec.push(sec - w.open_unix);
            for (slot, v) in self.legs.iter_mut().zip([k.bid, k.ask, y.bid, y.ask, n.bid, n.ask]) {
                slot.push(v.unwrap_or(f64::NAN));
            }
        }
    }

    pub fn len(&self) -> usize {
        self.sec.len()
    }

    pub fn is_empty(&self) -> bool {
        self.sec.is_empty()
    }

    pub fn frame(&self) -> Frame {
        let mut f = Frame::default();
        f.push("asset", Col::Str(self.asset.iter().cloned().map(Some).collect()));
        f.push("open_unix", Col::I64(self.open_unix.iter().copied().map(Some).collect()));
        f.push("sec", Col::I64(self.sec.iter().copied().map(Some).collect()));
        for (name, v) in ["k_bid", "k_ask", "y_bid", "y_ask", "n_bid", "n_ask"].iter().zip(&self.legs) {
            f.push(name, Col::F64(v.iter().copied().map(Some).collect()));
        }
        f
    }
}

/// Delay each venue's lines by its shift and re-merge them by the new time.
/// Within a venue the order is kept. `reordered` counts lines released after
/// a line that came later in the file.
pub struct Shifted<I> {
    inner: I,
    shifts: BTreeMap<char, i64>,
    floor: i64,
    heap: BinaryHeap<std::cmp::Reverse<(i64, u64, char, String)>>,
    seq: u64,
    released: Option<u64>,
    horizon: Option<i64>,
    done: bool,
    pub reordered: u64,
}

pub fn shift_lines<I: Iterator<Item = TapeLine>>(inner: I, shift_ms: &BTreeMap<char, f64>) -> Shifted<I> {
    let shifts: BTreeMap<char, i64> =
        shift_ms.iter().map(|(v, ms)| (*v, (ms * 1e6).round() as i64)).collect();
    let floor = shifts.values().copied().chain([0]).min().unwrap_or(0);
    Shifted {
        inner,
        shifts,
        floor,
        heap: BinaryHeap::new(),
        seq: 0,
        released: None,
        horizon: None,
        done: false,
        reordered: 0,
    }
}

impl<I: Iterator<Item = TapeLine>> Shifted<I> {
    fn pop(&mut self) -> Option<TapeLine> {
        let std::cmp::Reverse((t, s, v, x)) = self.heap.pop()?;
        if self.released.is_some_and(|r| s < r) {
            self.reordered += 1;
        }
        self.released = Some(self.released.map_or(s, |r| r.max(s)));
        Some(TapeLine { recv_ns: t, venue: v, text: x })
    }
}

impl<I: Iterator<Item = TapeLine>> Iterator for Shifted<I> {
    type Item = TapeLine;

    fn next(&mut self) -> Option<TapeLine> {
        loop {
            // A line is released once no later input can come before it: input
            // arrives in time order, so nothing can, past `recv + min(shift)`.
            let ready = match self.horizon {
                _ if self.done => true,
                Some(h) => self.heap.peek().is_some_and(|std::cmp::Reverse(t)| t.0 <= h),
                None => false,
            };
            if ready {
                return self.pop();
            }
            match self.inner.next() {
                None => self.done = true,
                Some(l) => {
                    let shifted = l.recv_ns + self.shifts.get(&l.venue).copied().unwrap_or(0);
                    self.heap.push(std::cmp::Reverse((shifted, self.seq, l.venue, l.text)));
                    self.seq += 1;
                    self.horizon = Some(l.recv_ns + self.floor);
                }
            }
        }
    }
}

pub fn parse_shift(text: &str) -> anyhow::Result<BTreeMap<char, f64>> {
    let mut out = BTreeMap::new();
    for part in text.split(',').filter(|p| !p.is_empty()) {
        let (v, ms) =
            part.split_once('=').ok_or_else(|| anyhow::anyhow!("--shift-ms {part}: want venue=ms"))?;
        let v = v.trim().chars().next().ok_or_else(|| anyhow::anyhow!("--shift-ms {part}: no venue"))?;
        out.insert(v, ms.trim().parse()?);
    }
    Ok(out)
}

pub struct ReplayConfig {
    pub params: StrategyParams,
    pub assets: Vec<String>,
    pub latency_ms: i64,
    pub start_cash: f64,
    pub shadow: String,
    pub limit_offset: f64,
}

pub fn build_engine(cfg: &ReplayConfig, journal: Box<dyn Sink + Send>) -> anyhow::Result<Engine> {
    let mut ec = EngineConfig::new(cfg.params.clone(), cfg.assets.clone());
    ec.role = Role::Full;
    ec.venue = Some(PaperVenue::new(cfg.latency_ms));
    ec.shadow = Shadow::new(parse_variants(&cfg.shadow, &cfg.params)?, &cfg.params);
    ec.limit_offset = cfg.limit_offset;
    let risk = RiskState::new(RiskLimits {
        max_open_notional_usd: 1e12,
        daily_loss_limit_usd: 1e12,
        kill_file: None,
    });
    let mut e = Engine::new(ec, Ledger::new(cfg.start_cash), risk, journal);
    // Feed geometry is read over the tape, not a live window of 4,096 samples.
    e.metrics = Metrics::new(2_000_000);
    Ok(e)
}

/// Run `lines` through a fresh engine, sampling the grid when asked; `tap` sees
/// the engine before and after every line (the fills study uses it).
pub fn replay(
    lines: impl Iterator<Item = TapeLine>,
    engine: &mut Engine,
    mut sampler: Option<&mut GridSampler>,
    mut tap: impl FnMut(&Engine, &TapeLine, bool),
) {
    for line in lines {
        if let Some(s) = sampler.as_deref_mut() {
            s.before(engine, line.recv_ns);
        }
        tap(engine, &line, true);
        engine.handle_line(line.recv_ns, line.venue, &line.text);
        tap(engine, &line, false);
    }
}

pub struct RunArgs {
    pub record_dir: PathBuf,
    pub out: PathBuf,
    pub start: Option<String>,
    pub end: Option<String>,
    pub cfg: ReplayConfig,
    pub shift_ms: BTreeMap<char, f64>,
}

pub fn run(args: &RunArgs) -> anyhow::Result<Value> {
    let files = pk_tape::iter_files(&args.record_dir, args.start.as_deref(), args.end.as_deref());
    anyhow::ensure!(!files.is_empty(), "no recordings under {}", args.record_dir.display());
    std::fs::create_dir_all(&args.out)?;
    let mut engine = build_engine(&args.cfg, Box::new(JsonlDaily::new(args.out.join("journal"))))?;
    let mut sampler = GridSampler::default();
    let mut reader = pk_tape::read_lines(files.clone());
    let mut reordered = 0;
    if args.shift_ms.values().any(|v| *v != 0.0) {
        let mut shifted = shift_lines(reader.by_ref(), &args.shift_ms);
        replay(shifted.by_ref(), &mut engine, Some(&mut sampler), |_, _, _| {});
        reordered = shifted.reordered;
    } else {
        replay(reader.by_ref(), &mut engine, Some(&mut sampler), |_, _, _| {});
    }
    sampler.frame().write_parquet(&args.out.join("grid.parquet"))?;
    let latency = engine.metrics.snapshot();
    let mut reader_stats = serde_json::to_value(&reader.stats)?;
    reader_stats["reordered"] = reordered.into();
    let summary = json!({
        "files": files.len(),
        "reader": reader_stats,
        "counters": engine.counters,
        "grid_rows": sampler.len(),
        "shift_ms": args.shift_ms.iter().map(|(k, v)| (k.to_string(), *v)).collect::<BTreeMap<_, _>>(),
        "feed_ms_k": latency.get("feed_ms_k"),
        "feed_ms_p": latency.get("feed_ms_p"),
        "params": {
            "record_dir": args.record_dir.display().to_string(), "out": args.out.display().to_string(),
            "assets": args.cfg.assets.join(","), "gap": args.cfg.params.gap_threshold, "cap": args.cfg.params.max_entry_px,
            "notional": args.cfg.params.notional_usd, "early": args.cfg.params.entry_early_s,
            "late": args.cfg.params.entry_late_s, "latency_ms": args.cfg.latency_ms,
        },
    });
    std::fs::write(args.out.join("summary.json"), serde_json::to_vec_pretty(&summary)?)?;
    Ok(summary)
}

/// Decisions matched by (asset, window): action, side, entry price and size.
pub fn compare(live: &[Value], replayed: &[Value]) -> Value {
    let by_window = |rs: &[Value]| -> BTreeMap<(String, i64), Value> {
        rs.iter()
            .filter(|r| r.get("type").and_then(Value::as_str) == Some("decision"))
            .filter_map(|r| {
                Some(((r.get("asset")?.as_str()?.to_owned(), r.get("open_unix")?.as_i64()?), r.clone()))
            })
            .collect()
    };
    let (a, b) = (by_window(live), by_window(replayed));
    let fields = ["action", "side", "entry_px", "contracts"];
    let common: Vec<&(String, i64)> = a.keys().filter(|k| b.contains_key(*k)).collect();
    let mut mismatches = Vec::new();
    for k in &common {
        let (x, y) = (&a[*k], &b[*k]);
        if fields.iter().any(|f| x.get(*f) != y.get(*f)) {
            let pick = |r: &Value| {
                fields
                    .iter()
                    .map(|f| ((*f).to_owned(), r.get(*f).cloned().unwrap_or(Value::Null)))
                    .collect::<serde_json::Map<_, _>>()
            };
            mismatches.push(json!({"window": [k.0, k.1], "live": pick(x), "replay": pick(y)}));
        }
    }
    json!({
        "live_only": a.keys().filter(|k| !b.contains_key(*k)).map(|k| json!([k.0, k.1])).collect::<Vec<_>>(),
        "replay_only": b.keys().filter(|k| !a.contains_key(*k)).map(|k| json!([k.0, k.1])).collect::<Vec<_>>(),
        "common": common.len(),
        "agreement": (!common.is_empty()).then(|| 1.0 - mismatches.len() as f64 / common.len() as f64),
        "mismatches": mismatches,
    })
}

pub fn read_journal_dir(dir: &Path) -> anyhow::Result<Vec<Value>> {
    pk_core::journal::read_journal(dir)
}
