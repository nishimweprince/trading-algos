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
