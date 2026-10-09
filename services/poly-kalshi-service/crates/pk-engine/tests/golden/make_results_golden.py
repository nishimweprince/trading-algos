"""Provenance of results_<config>.json: the Python status page's and read-out's
summaries (gates.evaluate, results.paper_results, results.open_trade_etas,
shadow.shadow_table) of each golden journal, at build 1286ad5. The Rust
pk-results crate must reproduce them (crates/pk-results/tests/parity.rs).
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[3] / "src")]

from poly_kalshi_service.gates import evaluate  # noqa: E402
from poly_kalshi_service.results import open_trade_etas, paper_results  # noqa: E402
from poly_kalshi_service.shadow import shadow_table  # noqa: E402

for name in ("base", "offset_depth_lag"):
    records = [json.loads(line) for line in (HERE / f"{name}.jsonl").read_text().splitlines()]
    records = [r for r in records if r["type"] != "final"]
    out = {
        "gates": evaluate(records, 0.60),
        "results": paper_results(records),
        "open_trades": open_trade_etas(records),
        "shadow": shadow_table(records),
    }
    (HERE / f"results_{name}.json").write_text(json.dumps(out, indent=1, sort_keys=True))
    print(name, {k: len(json.dumps(v)) for k, v in out.items()})
