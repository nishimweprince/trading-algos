//! The Rust summaries against the Python ones on the golden journals
//! (`crates/pk-engine/tests/golden/make_results_golden.py`).

use std::path::Path;

use serde_json::Value;

fn golden(name: &str) -> std::path::PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../pk-engine/tests/golden").join(name)
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
            for k in x.keys().chain(y.keys()).collect::<std::collections::BTreeSet<_>>() {
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

#[test]
fn summaries_match_python() {
    for name in ["base", "offset_depth_lag"] {
        let records: Vec<Value> = std::fs::read_to_string(golden(&format!("{name}.jsonl")))
            .unwrap()
            .lines()
            .map(|l| serde_json::from_str::<Value>(l).unwrap())
            .filter(|r| r["type"] != "final")
            .collect();
        let mut python: Value =
            serde_json::from_str(&std::fs::read_to_string(golden(&format!("results_{name}.json"))).unwrap())
                .unwrap();
        // The shadow table was removed (epoch 14).
        python.as_object_mut().unwrap().remove("shadow");
        python["results"].as_object_mut().unwrap().remove("shadow");
        let ours = serde_json::json!({
            "gates": pk_results::evaluate(&records, 0.60),
            "results": pk_results::paper_results(&records, 50),
            "open_trades": pk_results::open_trade_etas(&records),
        });
        let mut problems = Vec::new();
        diff("", &ours, &python, &mut problems);
        assert!(
            problems.is_empty(),
            "{name}: {} differences\n{}",
            problems.len(),
            problems[..problems.len().min(30)].join("\n")
        );
    }
}
