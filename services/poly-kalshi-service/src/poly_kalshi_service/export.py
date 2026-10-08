"""One-file export of everything needed to analyse the paper run: a zip.

| File in the zip | What |
|---|---|
| ``README.txt`` | this table, the profile, the export time |
| ``summary.json`` | gates, paper results and breakdowns, live status and latency at export |
| ``journal.jsonl`` | every decision, fill, settlement, resolution and window summary |
| ``events.jsonl`` | the service's structured log (warnings, reconnects, heartbeats) |
| ``ledger.json`` | the paper cash ledger and every position |
| ``recordings.jsonl`` | one manifest per recorded hour file (lines, venues, gaps) |
| ``series.jsonl`` | the per-second price points the charts show, per asset |

Nothing secret is included: no ``.env``, no keys. The raw tape is not included and stays on the
host (it is GBs); replay it there with ``research.replay``.
"""

from __future__ import annotations

import json
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings

__all__ = ["build_export", "export_name"]


def export_name(settings: Settings) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"pk-export-{settings.host_tag}-{settings.profile or 'default'}-{stamp}.zip"


def _write_jsonl_files(zf: zipfile.ZipFile, name: str, files: list[Path]) -> int:
    lines = 0
    with zf.open(name, "w") as out:
        for path in files:
            with path.open("rb") as handle:
                for line in handle:
                    if line.strip():
                        out.write(line if line.endswith(b"\n") else line + b"\n")
                        lines += 1
    return lines


def build_export(
    settings: Settings,
    *,
    summary: dict[str, Any],
    series: dict[str, list[Any]],
) -> Path:
    """Write the zip to a temporary file and return its path (the caller deletes it)."""
    handle = tempfile.NamedTemporaryFile(prefix="pk-export-", suffix=".zip", delete=False)
    handle.close()
    path = Path(handle.name)
    journal = sorted((settings.state_dir / "journal").glob("*.jsonl"))
    events = [settings.events_log_path] if settings.events_log_path.is_file() else []
    manifests = (
        sorted(settings.record_dir.rglob("*.gz.json")) if settings.record_dir.is_dir() else []
    )
    ledger = settings.state_dir / "ledger.json"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        counts = {
            "journal.jsonl": _write_jsonl_files(zf, "journal.jsonl", journal),
            "events.jsonl": _write_jsonl_files(zf, "events.jsonl", events),
        }
        with zf.open("recordings.jsonl", "w") as out:
            for manifest in manifests:
                body = json.loads(manifest.read_text())
                body["file"] = str(manifest.relative_to(settings.record_dir)).removesuffix(".json")
                out.write((json.dumps(body) + "\n").encode())
        counts["recordings.jsonl"] = len(manifests)
        with zf.open("series.jsonl", "w") as out:
            fields = ["sec", "open_unix", "kalshi_mid", "poly_yes_mid", "yes_bid", "yes_ask",
                      "no_bid", "no_ask"]  # fmt: skip
            for asset, points in series.items():
                for point in points:
                    row = {"asset": asset, **dict(zip(fields, point, strict=False))}
                    out.write((json.dumps(row) + "\n").encode())
        zf.writestr("ledger.json", ledger.read_text() if ledger.is_file() else "{}")
        zf.writestr("summary.json", json.dumps(summary, indent=1, default=str))
        exported = datetime.now(UTC).isoformat(timespec="seconds")
        zf.writestr(
            "README.txt",
            f"poly-kalshi export\nprofile={settings.profile} host={settings.host_tag} "
            f"exported_at={exported}\nlines={json.dumps(counts)}\n\n" + (__doc__ or ""),
        )
    return path
