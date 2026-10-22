//! Tape research against the Python modules' outputs on the same tape
//! (`tests/golden/make_research_golden.py`).

use std::collections::{BTreeMap, BTreeSet};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};

use pk_research::frame::Frame;
use pk_research::replay::{ALL_ASSETS, ReplayConfig, RunArgs};
use serde_json::Value;

fn golden(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/golden").join(name)
}

fn engine_golden(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../pk-engine/tests/golden").join(name)
}

fn json(name: &str) -> Value {
    serde_json::from_str(&std::fs::read_to_string(golden(name)).unwrap()).unwrap()
}

fn diff(path: &str, a: &Value, b: &Value, out: &mut Vec<String>) {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => {
            let (x, y) = (x.as_f64().unwrap(), y.as_f64().unwrap());
            if (x - y).abs() > 1e-9 * x.abs().max(1.0) {
                out.push(format!("{path}: rust {x} python {y}"));
            }
        }
        (Value::Object(x), Value::Object(y)) => {
            for k in x.keys().chain(y.keys()).collect::<BTreeSet<_>>() {
                match (x.get(k), y.get(k)) {
                    (Some(p), Some(q)) => diff(&format!("{path}.{k}"), p, q, out),
                    (p, q) => out.push(format!("{path}.{k}: rust {p:?} python {q:?}")),
                }
            }
        }
        (Value::Array(x), Value::Array(y)) if x.len() == y.len() => {
            for (i, (p, q)) in x.iter().zip(y).enumerate() {
                diff(&format!("{path}[{i}]"), p, q, out);
            }
        }
        _ if a == b => {}
        _ => out.push(format!("{path}: rust {a} python {b}")),
    }
}

fn same(what: &str, ours: &Value, theirs: &Value) {
    let mut d = Vec::new();
    diff("", ours, theirs, &mut d);
    assert!(d.is_empty(), "{what}: {} differences\n{}", d.len(), d[..d.len().min(25)].join("\n"));
}

/// The golden tape as the recorder would have written it: one gzip file per hour.
fn record_dir() -> (PathBuf, PathBuf) {
    let root = std::env::temp_dir().join(format!("pk-research-{}", std::process::id()));
    let raw = root.join("raw");
    let day = raw.join("20261009");
    if day.is_dir() {
        return (root, raw);
    }
    std::fs::create_dir_all(&day).unwrap();
    let mut text = String::new();
    flate2::read::GzDecoder::new(std::fs::File::open(engine_golden("tape.txt.gz")).unwrap())
        .read_to_string(&mut text)
        .unwrap();
    let mut files: BTreeMap<String, flate2::write::GzEncoder<std::fs::File>> = BTreeMap::new();
    for line in text.lines() {
        let ns: i64 = line.split(' ').next().unwrap().parse().unwrap();
        let hour = chrono::DateTime::<chrono::Utc>::from_timestamp_nanos(ns).format("%Y%m%d_%H").to_string();
        let f = files.entry(hour.clone()).or_insert_with(|| {
            let p = day.join(format!("pk_{hour}_{:020}.gz", 1));
            flate2::write::GzEncoder::new(std::fs::File::create(p).unwrap(), flate2::Compression::fast())
        });
        writeln!(f, "{line}").unwrap();
    }
    for (_, f) in files {
        f.finish().unwrap();
    }
    (root, raw)
}

fn cfg() -> ReplayConfig {
    ReplayConfig {
        params: pk_core::StrategyParams {
            gap_threshold: 0.10,
            max_entry_px: 0.60,
            notional_usd: 50.0,
            ..Default::default()
        },
        assets: ALL_ASSETS.split(',').map(String::from).collect(),
        latency_ms: 150,
        start_cash: 1e9,
        limit_offset: 0.0,
        tape_latency: false,
    }
}

fn read_jsonl(dir: &Path) -> Vec<Value> {
    pk_core::journal::read_journal(dir).unwrap()
}

#[test]
fn research_matches_python() {
    let (root, raw) = record_dir();
    let out = root.join("run");
    let summary = pk_research::replay::run(&RunArgs {
        record_dir: raw.clone(),
        out: out.clone(),
        start: None,
        end: None,
        cfg: cfg(),
        shift_ms: BTreeMap::new(),
    })
    .unwrap();

    // Replay: the journal, the grid, the summary.
    let ours = read_jsonl(&out.join("journal"));
    let theirs: Vec<Value> = std::fs::read_to_string(golden("replay_journal.jsonl"))
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str::<Value>(l).unwrap())
        // The shadow variants were removed (epoch 14); every other record still matches.
        .filter(|r| !matches!(r["type"].as_str(), Some("shadow" | "shadow_settle")))
        .collect();
    same("replay journal", &Value::Array(ours.clone()), &Value::Array(theirs.clone()));
    let mut g = String::new();
    flate2::read::GzDecoder::new(std::fs::File::open(golden("replay_grid.json.gz")).unwrap())
        .read_to_string(&mut g)
        .unwrap();
    let g: Value = serde_json::from_str(&g).unwrap();
    let grid = Frame::read_parquet(&out.join("grid.parquet")).unwrap();
    let cols: Vec<String> =
        g["columns"].as_array().unwrap().iter().map(|c| c.as_str().unwrap().to_owned()).collect();
    assert_eq!(cols, grid.cols.keys().cloned().collect::<Vec<_>>());
    let ours_grid: Vec<Value> =
        (0..grid.len()).map(|i| Value::Array(grid.cols.values().map(|c| c.value(i)).collect())).collect();
    same("replay grid", &Value::Array(ours_grid), &g["data"]);
    let py = json("replay_summary.json");
    for k in ["files", "counters", "grid_rows", "feed_ms_k", "feed_ms_p"] {
        same(&format!("replay summary {k}"), &summary[k], &py[k]);
    }
    for k in ["lines", "bad", "out_of_order", "truncated"] {
        assert_eq!(summary["reader"][k], py["reader"][k], "reader {k}");
    }
    let cmp = pk_research::replay::compare(&theirs, &ours);
    assert_eq!(cmp["agreement"], 1.0);

    // Nulls: the deterministic arms (the random ones draw from another generator).
    let books = pk_research::nulls::arms(&grid, &ours, 0.60, 20, 60, 7);
    let n = pk_research::nulls::summarise(&books);
    let py = json("nulls.json");
    for arm in ["signal", "signal_capped", "fixed_cheap"] {
        same(&format!("nulls {arm}"), &n["arms"][arm], &py["arms"][arm]);
    }
    same("nulls by asset", &n["signal_by_asset"], &py["signal_by_asset"]);
    assert_eq!(
        n["arms"]["random_same_rule"]["n"], py["arms"]["random_same_rule"]["n"],
        "one pick per window"
    );

    // Read-out of the Python journal.
    let ex: BTreeSet<String> = ["XRP".to_owned()].into();
    let mut want = json("readout.json");
    want.as_object_mut().unwrap().remove("shadow");
    for side in ["all", "without"] {
        want["assets"][side].as_object_mut().unwrap().remove("shadow_live");
    }
    let mut ours = pk_research::readout::readout(theirs.clone(), 0.60, None, &ex);
    // Gate readouts added after the Python engine was retired (epoch 16).
    for k in ["fill_by_path", "fill_by_phase"] {
        ours.as_object_mut().unwrap().remove(k);
    }
    same("readout", &ours, &want);

    // Fills: run, then the grid.
    let fills_out = root.join("fills");
    std::fs::create_dir_all(&fills_out).unwrap();
    let fcfg = cfg();
    let mut engine = pk_research::replay::build_engine(
        &fcfg,
        Box::new(pk_core::journal::JsonlDaily::new(fills_out.join("journal"))),
    )
    .unwrap();
    let mut tap = pk_research::fills::LadderTap::default();
    let files = pk_tape::iter_files(&raw, None, None);
    pk_research::replay::replay(pk_tape::read_lines(files), &mut engine, None, |e, line, before| {
        if before { tap.before(e, line.recv_ns) } else { tap.after(e, line.recv_ns) }
    });
    tap.write(&fills_out.join("fills.jsonl.zst")).unwrap();
    let rows = pk_research::fills::read_rows(&fills_out.join("fills.jsonl.zst")).unwrap();
    let fj = read_jsonl(&fills_out.join("journal"));
    same("fills grid", &pk_research::fills::grid(&rows, &fj, 150, 0.60), &json("fills_grid.json"));

    // The reverse summary, on the reverse golden journal.
    let rev: Vec<Value> = std::fs::read_to_string(engine_golden("reverse.jsonl"))
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str::<Value>(l).unwrap())
        .filter(|r| r["type"] != "final")
        .collect();
    same("reverse summary", &pk_research::reverse::summarize(&rev), &json("reverse_summary.json"));

    // Inventory.
    let inv = pk_research::inventory::inventory(&raw, None).unwrap();
    same("inventory", &Value::Array(inv), &json("inventory.json"));
    let _ = BufReader::new(std::io::empty()).lines();
    std::fs::remove_dir_all(root).ok();
}
