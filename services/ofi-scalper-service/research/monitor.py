"""A read-only status page for a research VM: conversion, pipeline, host.

    python -m research.monitor --port 8077          # http://127.0.0.1:8077 (JSON at /api)

Binds to loopback only; reach it through an SSH or private tunnel. It reads
files and logs and never starts or stops work:

- **Conversion**: symbol-days with all 24 hour files under the record dir,
  against the requested range, plus errors and the newest lines of the log;
- **Pipeline**: the last ``{"step": ...}`` line of each kind in the training
  log (eligible days, split, features, candidates, selection, gates), feature
  files written, candidates trained, and any ``gates.json`` verdicts;
- **Host**: load, memory, disk and the tmux sessions running.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

STEPS = ("eligible_days", "split", "features", "candidate", "selection", "gates")


def _tail(path: Path, lines: int) -> list[str]:
    if not path.is_file():
        return []
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        handle.seek(max(handle.tell() - 256_000, 0))
        text = handle.read().decode("utf-8", errors="replace")
    return text.splitlines()[-lines:]


def conversion(record_dir: Path, start: date, end: date, symbols: list[str], log: Path) -> dict:
    days = (end - start).days + 1
    done = {
        s: sum(
            1
            for n in range(days)
            if len(list((record_dir / s / (start + timedelta(n)).strftime("%Y%m%d")).glob("*.gz")))
            == 24
        )
        for s in symbols
    }
    results, errors = [], []
    for line in _tail(log, 5000):
        if not line.startswith("{"):
            continue
        row = json.loads(line)
        if "error" in row:
            errors.append(row)
        elif "lines" in row:
            results.append(row)
    missing = [f"{r['date']} {r['symbol']}: {r['missing']}" for r in results if r.get("missing")]
    seconds = [r["seconds"] for r in results[-30:]]
    return {
        "range": f"{start} → {end}",
        "done": sum(done.values()),
        "total": days * len(symbols),
        "by_symbol": done,
        "mean_seconds": round(sum(seconds) / len(seconds)) if seconds else None,
        "errors": errors[-10:],
        "missing_hours": missing[-10:],
        "partial": sorted(p.name for p in (record_dir / ".partial").glob("*"))
        if (record_dir / ".partial").is_dir()
        else [],
        "log": [line for line in _tail(log, 8) if "retry" not in line],
    }


def pipeline(research_dir: Path, models_dir: Path, run: str, log: Path) -> dict:
    steps: dict[str, Any] = {}
    candidates_seen = []
    for line in _tail(log, 2000):
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        step = row.get("step")
        if step == "candidate":
            candidates_seen.append(row)
        elif step:
            steps[step] = row
    features = sorted((research_dir / "features").glob("*/*.parquet"))
    trained = sorted(
        p.parent.name for p in (research_dir / "candidates" / run).glob("*/candidate.json")
    )
    selection = research_dir / "candidates" / run / "selection.json"
    verdicts = []
    for gates in sorted(models_dir.glob("*/gates.json")):
        body = json.loads(gates.read_text())
        verdicts.append({"model": gates.parent.name, "passed": body.get("passed")})
    chosen = json.loads(selection.read_text()).get("chosen") if selection.is_file() else None
    return {
        "run": run,
        "stage": next(
            (s for s in reversed(STEPS) if s in steps or (s == "candidate" and candidates_seen)),
            None,
        ),
        "steps": steps,
        "features_written": len(features),
        "latest_feature_day": features[-1].stem if features else None,
        "candidates_trained": trained,
        "selection_chosen": chosen,
        "gates": verdicts,
        "log": _tail(log, 12),
    }


def host(data_dir: Path) -> dict:
    meminfo = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        name, _, value = line.partition(":")
        meminfo[name] = int(value.split()[0]) // 1024
    disk = shutil.disk_usage(data_dir)
    try:
        sessions = subprocess.run(
            ["tmux", "ls", "-F", "#{session_name}"], capture_output=True, text=True, timeout=5
        ).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        sessions = []
    return {
        "load": [round(x, 2) for x in os.getloadavg()],
        "cpus": os.cpu_count(),
        "mem_used_mb": meminfo["MemTotal"] - meminfo["MemAvailable"],
        "mem_total_mb": meminfo["MemTotal"],
        "disk_used_gb": round(disk.used / 1e9),
        "disk_total_gb": round(disk.total / 1e9),
        "tmux": sessions,
    }


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30"><title>OFI research</title>
<style>
body{{font:14px/1.45 system-ui,sans-serif;margin:16px;background:#fafafa;color:#222}}
h1{{font-size:18px;margin:0 0 4px}} h2{{font-size:15px;margin:18px 0 6px}}
.card{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px;margin:10px 0}}
.bar{{height:10px;background:#eee;border-radius:5px;overflow:hidden}}
.bar>div{{height:100%;background:#2f7d4f}}
pre{{background:#111;color:#ddd;padding:8px;border-radius:6px;overflow-x:auto;font-size:12px}}
.muted{{color:#777}} .bad{{color:#b3261e}} .good{{color:#2f7d4f}} td{{padding:2px 10px 2px 0}}
@media (prefers-color-scheme:dark){{body{{background:#121212;color:#ddd}}
.card{{background:#1c1c1c;border-color:#333}} .bar{{background:#333}}}}
</style></head><body>
<h1>OFI research VM</h1><div class="muted">updated {now} ·
refreshes every 30 s ·
<a href="/api">JSON</a></div>
{body}</body></html>"""


def render(status: dict) -> str:
    c, p, h = status["conversion"], status["pipeline"], status["host"]
    e = html.escape
    pct = 100 * c["done"] / max(c["total"], 1)
    rows = "".join(f"<tr><td>{e(k)}</td><td>{v}</td></tr>" for k, v in c["by_symbol"].items())
    errors = "".join(f"<div class='bad'>{e(json.dumps(x))}</div>" for x in c["errors"])
    missing = "".join(f"<div class='bad'>{e(x)}</div>" for x in c["missing_hours"])
    steps = "".join(
        f"<tr><td>{e(k)}</td><td><code>{e(json.dumps(v)[:400])}</code></td></tr>"
        for k, v in p["steps"].items()
    )
    gates = "".join(
        f"<div class='{'good' if g['passed'] else 'bad'}'>{e(g['model'])}: "
        f"{'PASSED' if g['passed'] else 'failed'}</div>"
        for g in p["gates"]
    )
    body = f"""
<div class="card"><h2>Conversion {c["done"]} / {c["total"]} symbol-days ({pct:.0f}%)</h2>
<div class="bar"><div style="width:{pct:.1f}%"></div></div>
<p class="muted">{e(c["range"])} ·
mean {c["mean_seconds"] or "–"} s per symbol-day ·
in progress: {e(", ".join(c["partial"]) or "–")}</p>
<table>{rows}</table>{errors}{missing}<pre>{e(chr(10).join(c["log"]))}</pre></div>
<div class="card"><h2>Pipeline {e(p["run"])}: {e(p["stage"] or "waiting for conversion")}</h2>
<p>Feature days written: <b>{p["features_written"]}</b> (latest {e(str(p["latest_feature_day"]))}) ·
candidates trained: <b>{len(p["candidates_trained"])}</b>
{e(", ".join(p["candidates_trained"]))}</p>
<p>Selection: <code>
{e(json.dumps(p["selection_chosen"])[:300]) if p["selection_chosen"] else "–"}</code></p>
{gates or '<p class="muted">No gate verdict yet.</p>'}<table>{steps}</table>
<pre>{e(chr(10).join(p["log"]))}</pre></div>
<div class="card"><h2>Host</h2>
<p>load {h["load"]} on {h["cpus"]} vCPU · memory {h["mem_used_mb"]:,} / {h["mem_total_mb"]:,} MB ·
disk {h["disk_used_gb"]} / {h["disk_total_gb"]} GB ·
tmux: {e(", ".join(h["tmux"]) or "–")}</p></div>"""
    return PAGE.format(now=status["now"], body=body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Research VM status page")
    parser.add_argument("--port", type=int, default=8077)
    parser.add_argument("--record-dir", type=Path, default=Path("/data/ofi/raw/cryptohftdata"))
    parser.add_argument("--research-dir", type=Path, default=Path("/data/ofi/research"))
    parser.add_argument("--models-dir", type=Path, default=Path("/data/ofi/models"))
    parser.add_argument("--logs", type=Path, default=Path.home() / "logs")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 6, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 10, 28))
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--run", default="chd-binding-1")
    args = parser.parse_args(argv)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    def status() -> dict:
        return {
            "now": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "conversion": conversion(
                args.record_dir, args.start, args.end, symbols, args.logs / "convert.log"
            ),
            "pipeline": pipeline(
                args.research_dir, args.models_dir, args.run, args.logs / "train.log"
            ),
            "host": host(args.record_dir),
        }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's name
            try:
                data = status()
                if self.path.startswith("/api"):
                    body, kind = json.dumps(data, default=str).encode(), "application/json"
                else:
                    body, kind = render(data).encode(), "text/html; charset=utf-8"
                self.send_response(200)
            except Exception as exc:  # the page must say what broke, not hang
                body, kind = f"monitor error: {exc!r}".encode(), "text/plain"
                self.send_response(500)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: Any) -> None:
            pass

    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
