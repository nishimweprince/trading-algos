//! The Helix port against the Python modules on the synthetic tape
//! (`tests/golden/helix/make_helix_tape.py`, `make_helix_golden.py`).

use std::collections::{BTreeMap, BTreeSet};
use std::io::Read;
use std::path::{Path, PathBuf};

use pk_research::frame::Frame;
use pk_research::helix;
use serde_json::Value;

const DAYS: [&str; 2] = ["20261001", "20261002"];

fn golden(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/golden/helix").join(name)
}

fn tape() -> PathBuf {
    golden("tape")
}

fn json(name: &str) -> Value {
    let p = golden(name);
    let mut text = String::new();
    if name.ends_with(".gz") {
        flate2::read::GzDecoder::new(std::fs::File::open(p).unwrap()).read_to_string(&mut text).unwrap();
    } else {
        text = std::fs::read_to_string(p).unwrap();
    }
    serde_json::from_str(&text).unwrap()
}

fn diff(path: &str, a: &Value, b: &Value, tol: f64, out: &mut Vec<String>) {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => {
            let (x, y) = (x.as_f64().unwrap(), y.as_f64().unwrap());
            if (x - y).abs() > tol * x.abs().max(1.0) {
                out.push(format!("{path}: rust {x} python {y}"));
            }
        }
        (Value::Object(x), Value::Object(y)) => {
            for k in x.keys().chain(y.keys()).collect::<BTreeSet<_>>() {
                match (x.get(k), y.get(k)) {
                    (Some(p), Some(q)) => diff(&format!("{path}.{k}"), p, q, tol, out),
                    (p, q) => out.push(format!("{path}.{k}: rust {p:?} python {q:?}")),
                }
            }
        }
        (Value::Array(x), Value::Array(y)) if x.len() == y.len() => {
            for (i, (p, q)) in x.iter().zip(y).enumerate() {
                diff(&format!("{path}[{i}]"), p, q, tol, out);
            }
        }
        _ if a == b => {}
        _ => out.push(format!("{path}: rust {a} python {b}")),
    }
}

fn same_tol(what: &str, ours: &Value, theirs: &Value, tol: f64) {
    let mut d = Vec::new();
    diff("", ours, theirs, tol, &mut d);
    assert!(d.is_empty(), "{what}: {} differences\n{}", d.len(), d[..d.len().min(25)].join("\n"));
}

fn same(what: &str, ours: &Value, theirs: &Value) {
    same_tol(what, ours, theirs, 1e-9);
}

/// A parquet output as the golden's {columns, data}.
fn frame_json(f: &Frame) -> Value {
    let cols: Vec<Value> = f.cols.keys().map(|k| Value::from(k.as_str())).collect();
    let data: Vec<Value> =
        (0..f.len()).map(|i| Value::Array(f.cols.values().map(|c| c.value(i)).collect())).collect();
    serde_json::json!({"columns": cols, "data": data})
}

fn resolutions() -> helix::replay::Resolutions {
    json("resolutions.json")["rows"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| {
            (
                (r["asset"].as_str().unwrap().to_owned(), r["open_unix"].as_i64().unwrap()),
                r["poly_outcome"].as_str().map(str::to_owned),
            )
        })
        .collect()
}

fn scratch(name: &str) -> PathBuf {
    let p = std::env::temp_dir().join(format!("pk-helix-{name}-{}", std::process::id()));
    std::fs::remove_dir_all(&p).ok();
    std::fs::create_dir_all(&p).unwrap();
    p
}

/// The Rust replay of both days, with the resolutions and the day roles the
/// studies read, as `make_helix_golden.py` laid them out.
fn study_data() -> &'static PathBuf {
    static DATA: std::sync::OnceLock<PathBuf> = std::sync::OnceLock::new();
    DATA.get_or_init(|| {
        let data = scratch("studies").join("data");
        let helix_dir = data.join("helix");
        std::fs::create_dir_all(&helix_dir).unwrap();
        let rows: Vec<pk_research::frame::Row> = json("resolutions.json")["rows"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| r.as_object().unwrap().iter().map(|(k, v)| (k.clone(), v.clone())).collect())
            .collect();
        Frame::from_records(&rows).write_parquet(&data.join("resolutions.parquet")).unwrap();
        let res = resolutions();
        for day in DAYS {
            helix::replay::run_day(day, &helix_dir, &tape(), &res, &BTreeMap::new()).unwrap();
        }
        let roles: serde_json::Map<String, Value> = DAYS
            .iter()
            .map(|d| {
                (d.to_string(), serde_json::json!({"role": "discovery", "usable": true, "chainlink": true}))
            })
            .collect();
        std::fs::write(data.join("helix_inventory.json"), serde_json::json!({"days": roles}).to_string())
            .unwrap();
        data
    })
}

#[test]
fn sweeps_match_python() {
    let data = study_data();
    let ours = helix::sweeps::run(&helix::sweeps::Args {
        split: "discovery".into(),
        data: data.join("helix"),
        inventory: data.join("helix_inventory.json"),
        resolutions: data.join("resolutions.parquet"),
        skip_slices: false,
        prereg: PathBuf::from("/nonexistent"),
    })
    .unwrap();
    let mut py = json("sweeps.json");
    let mut ours = ours;
    // The random null arms draw from another generator than numpy's.
    for v in [&mut ours, &mut py] {
        let n = v["nulls"].as_object_mut().unwrap();
        n.remove("signal_minus_random_same_rule_pp");
        let arms = n["arms"].as_object_mut().unwrap();
        for a in ["random_same_rule", "random_cheap"] {
            let keep = arms[a]["n"].clone();
            arms[a] = serde_json::json!({"n": keep});
        }
        n.remove("bands_signal_vs_random_same_rule");
    }
    same("sweeps", &ours, &py);
}

#[test]
fn replay_matches_python() {
    let out = scratch("replay");
    let res = resolutions();
    for day in DAYS {
        let summary = helix::replay::run_day(day, &out, &tape(), &res, &BTreeMap::new()).unwrap();
        let g = json(&format!("replay_{day}.json.gz"));
        for k in ["day", "windows", "crossings", "slices_rows", "grid_rows", "rows"] {
            same(&format!("{day} summary {k}"), &summary[k], &g["summary"][k]);
        }
        for name in ["crossings", "windows", "grid", "slices"] {
            let f = Frame::read_parquet(&out.join(day).join(format!("{name}.parquet"))).unwrap();
            let ours = frame_json(&f);
            same(&format!("{day} {name} columns"), &ours["columns"], &g[name]["columns"]);
            same(&format!("{day} {name}"), &ours["data"], &g[name]["data"]);
        }
    }
    std::fs::remove_dir_all(out).ok();
}

#[test]
fn fairvalue_matches_python() {
    let data = study_data();
    let ours = helix::fairvalue::run(&helix::fairvalue::Args {
        split: "discovery".into(),
        data: data.join("helix"),
        inventory: data.join("helix_inventory.json"),
        resolutions: data.join("resolutions.parquet"),
    })
    .unwrap();
    same("fairvalue", &ours, &json("fairvalue.json"));
}

#[test]
fn leadlag_matches_python() {
    let out = scratch("leadlag").join("leadlag.json");
    let ours = helix::leadlag::run(&helix::leadlag::Args {
        days: DAYS.iter().map(|d| d.to_string()).collect(),
        root: tape(),
        bin_ms: 50,
        out,
    })
    .unwrap();
    // FFT and least squares differ from numpy's in the last bits: compare at
    // the outputs' own rounding.
    same_tol("leadlag", &ours, &json("leadlag.json"), 1e-9);
}

#[test]
fn validate_matches_python() {
    let tmp = scratch("validate");
    for day in DAYS {
        let mut ours = serde_json::Map::new();
        ours.insert("identity".into(), helix::validate::check_identity(day, &tape(), 500_000).unwrap());
        ours.extend(helix::validate::check_day(day, &tape(), 0, &tmp).unwrap());
        ours.remove("seconds");
        same(&format!("validate {day}"), &Value::Object(ours), &json(&format!("validate_{day}.json")));
    }
}

#[test]
fn resolutions_match_python() {
    let days: Vec<String> = DAYS.iter().map(|d| d.to_string()).collect();
    let rows = helix::resolutions::build(&days, &golden("venue_cache"), &tape()).unwrap();
    let g = json("resolutions.json");
    let ours: Vec<Value> =
        rows.iter().map(|r| Value::Object(r.iter().map(|(k, v)| (k.clone(), v.clone())).collect())).collect();
    same("resolution rows", &Value::Array(ours), &g["rows"]);
    same("resolution summary", &helix::resolutions::summarise(&rows), &g["summary"]);
}

#[test]
fn inventory_matches_python() {
    let data = study_data();
    let manifest: BTreeSet<String> = DAYS
        .iter()
        .map(|d| format!("Polymarket_Window/{d}.hlxz"))
        .chain(["Kalshi_Yes_Trade/20261001.hlxz".to_owned()])
        .collect();
    let failures: BTreeSet<String> = ["Kalshi_Yes_Trade/20261001.hlxz".to_owned()].into();
    let ours = helix::inventory::build(
        &tape(),
        &data.join("helix"),
        &data.join("resolutions.parquet"),
        Some(&failures),
        &manifest,
    )
    .unwrap();
    same("inventory", &ours, &json("inventory.json"));
}

#[test]
fn kalshi_windows_decode_and_join() {
    for day in DAYS {
        let ws = helix::records::iter_kalshi_windows(day, &tape()).unwrap();
        assert!(!ws.is_empty(), "{day}: Kalshi_Window decoded no rows");
        for w in &ws {
            let name = helix::asset_name(w.asset).expect("15-minute asset");
            assert_eq!(w.ticker, format!("KX{name}15M"));
            let at = helix::records::kalshi_ticker_at(&ws, w.asset, w.ts_recorded_ns).unwrap();
            assert!(at.ts_recorded_ns == w.ts_recorded_ns && at.asset == w.asset);
        }
        // Every Polymarket token joins back to a 15-minute window.
        let windows = helix::records::iter_windows(day, &tape()).unwrap();
        let idx = helix::records::token_index(&windows);
        assert_eq!(idx.len(), windows.len() * 2);
        assert!(windows.iter().all(|w| w.open_unix % 900 == 0 && idx[&w.token_yes].open_unix == w.open_unix));
    }
}
