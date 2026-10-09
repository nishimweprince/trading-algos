"""Provenance of the tape-research goldens: the Python research modules on the
engine's golden tape (crates/pk-engine/tests/golden/tape.txt.gz), at build
1286ad5. Run from services/poly-kalshi-service with the research extra.

Outputs (JSON, next to this file):
- replay_grid.json, replay_summary.json, replay_journal.jsonl (research.replay run)
- nulls.json (research.nulls; random arms are reported but not compared)
- readout.json (research.readout --exclude-assets XRP)
- fills_grid.json (research.fills run + grid)
- reverse_summary.json (research.reverse_replay.summarize of reverse.jsonl)
- inventory.json (research.inventory, without a journal)
"""

import gzip
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVICE = HERE.parents[3]
TAPE = SERVICE / "crates/pk-engine/tests/golden/tape.txt.gz"
PY = sys.executable


def run(*args: str) -> str:
    out = subprocess.run([PY, "-m", *args], cwd=SERVICE, capture_output=True, text=True, check=True)
    return out.stdout


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    rec = tmp / "raw" / "20261009"
    rec.mkdir(parents=True)
    # One hour file per tape hour, as the recorder writes them.
    files: dict[str, object] = {}
    with gzip.open(TAPE, "rt") as f:
        for line in f:
            ns = int(line.split(" ", 1)[0])
            hour = __import__("datetime").datetime.utcfromtimestamp(ns / 1e9).strftime("%Y%m%d_%H")
            if hour not in files:
                files[hour] = gzip.open(rec / f"pk_{hour}_{1:020d}.gz", "wt")
            files[hour].write(line)
    for h in files.values():
        h.close()
    raw = tmp / "raw"
    run("research.replay", "run", "--record-dir", str(raw), "--out", str(tmp / "run"))
    import pandas as pd

    grid = pd.read_parquet(tmp / "run" / "grid.parquet")
    (HERE / "replay_grid.json").write_text(grid.to_json(orient="split", index=False))
    summary = json.loads((tmp / "run" / "summary.json").read_text())
    summary.pop("params", None)
    (HERE / "replay_summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True))
    shutil.copy(sorted((tmp / "run" / "journal").glob("*.jsonl"))[0], HERE / "replay_journal.jsonl")
    run("research.nulls", "--run", str(tmp / "run"))
    shutil.copy(tmp / "run" / "nulls.json", HERE / "nulls.json")
    (HERE / "readout.json").write_text(
        run("research.readout", "--journal", str(tmp / "run" / "journal"), "--exclude-assets", "XRP")
    )
    run("research.fills", "run", "--record-dir", str(raw), "--out", str(tmp / "fills"))
    run("research.fills", "grid", "--run", str(tmp / "fills"))
    shutil.copy(tmp / "fills" / "fill_grid.json", HERE / "fills_grid.json")
    sys.path[:0] = [str(SERVICE / "src"), str(SERVICE)]
    from research.reverse_replay import summarize

    rj = tmp / "rev"
    rj.mkdir()
    lines = [l for l in (SERVICE / "crates/pk-engine/tests/golden/reverse.jsonl").read_text().splitlines()
             if '"type":"final"' not in l]
    (rj / "2026-10-09.jsonl").write_text("\n".join(lines) + "\n")
    (HERE / "reverse_summary.json").write_text(json.dumps(summarize(rj), indent=1, sort_keys=True))
    from research.inventory import inventory

    (HERE / "inventory.json").write_text(json.dumps(inventory(raw, None), indent=1))
print("ok")
