"""One-file analysis export: ``ofi-export --profile dev [--out export.json]``.

Reads everything the service wrote about its own trading and writes it as a
single JSON document for analysis:
- performance overall, by day and by symbol, for demo and the shadow mirror;
- every trade (cycle) record (``pandas.DataFrame(doc["trades"])``);
- signal *counts* by action, symbol, side and day. Signals are events,
  thousands a day, so the raw records are left out unless ``--signals``;
- daily recording reports, bridge state, drills, latency, the pinned model's
  JSON files, and research results (runs, candidates' calibration, full
  selection grids with score diagnostics, gates, report).

The status page serves the same document as a download (``GET /api/export``)
with a live health/execution snapshot added.

Recordings (``OFI_RECORD_DIR``) are never touched: they stay on the data disk.
No secrets leave with the export: only an allowlist of non-secret settings
lands in ``meta`` (host tag, mode, model version, symbols), and absolute
filesystem paths are reduced to their file names.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ta_core import base_parser, load_or_exit

from . import status as builders
from .config import Settings, load_settings
from .latency import default_latency_path
from .trades import summarise

__all__ = ["build_export", "default_latency_path", "export_filename", "run", "scrub_paths"]


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    """A day file's records, skipping torn trailing lines from a live writer."""
    records: list[dict[str, Any]] = []
    unparsed = 0
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return records, unparsed
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            unparsed += 1
            continue
        if isinstance(record, dict):
            records.append(record)
        else:
            unparsed += 1
    return records, unparsed


def _stream(
    state_dir: Path, name: str, days: set[str] | None
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], int]:
    """One stream (``trades`` or ``signals``) as a flat list plus per-day groups."""
    flat: list[dict[str, Any]] = []
    by_day: dict[str, list[dict[str, Any]]] = {}
    unparsed = 0
    directory = state_dir / name
    if not directory.is_dir():
        return flat, by_day, unparsed
    for path in sorted(directory.glob("*.jsonl")):
        if days is not None and path.stem not in days:
            continue
        records, skipped = _read_jsonl(path)
        unparsed += skipped
        by_day[path.stem] = records
        flat.extend(records)
    return flat, by_day, unparsed


def _available_days(state_dir: Path, *, include_shadow: bool) -> list[str]:
    found: set[str] = set()
    for sub in ("trades", "signals", "daily"):
        directory = state_dir / sub
        found.update(path.stem for path in directory.glob("*.json*") if directory.is_dir())
    if include_shadow:
        for sub in ("shadow/trades", "shadow/signals"):
            directory = state_dir / sub
            found.update(path.stem for path in directory.glob("*.jsonl") if directory.is_dir())
    return sorted(found)


def _performance(
    trades: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    trades_by_day: dict[str, list[dict[str, Any]]],
    signals_by_day: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    by_day = {
        day: summarise(trades_by_day.get(day, []), signals_by_day.get(day, []))
        for day in sorted(set(trades_by_day) | set(signals_by_day))
    }
    symbols = sorted({str(r.get("symbol")) for r in [*trades, *signals] if r.get("symbol")})
    by_symbol = {
        symbol: summarise(
            [t for t in trades if t.get("symbol") == symbol],
            [s for s in signals if s.get("symbol") == symbol],
        )
        for symbol in symbols
    }
    return {"overall": summarise(trades, signals), "by_day": by_day, "by_symbol": by_symbol}


def _count(records: list[dict[str, Any]], key: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for record in records:
        k = key(record)
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def _signal_counts(
    signals: list[dict[str, Any]], by_day: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    """What the signal log says, without its records: the counts an analysis needs."""
    action = lambda s: str(s.get("action") or "unknown")  # noqa: E731
    return {
        "total": len(signals),
        "by_action": _count(signals, action),
        "by_symbol": _count(signals, lambda s: str(s.get("symbol") or "unknown")),
        "by_side": _count(signals, lambda s: str(s.get("side") or "unknown")),
        "by_symbol_action": {
            symbol: _count([s for s in signals if s.get("symbol") == symbol], action)
            for symbol in sorted({str(s.get("symbol")) for s in signals if s.get("symbol")})
        },
        "by_day": {day: _count(records, action) for day, records in sorted(by_day.items())},
    }


def _research(settings: Settings) -> dict[str, Any]:
    """The page's research view, plus every run's full selection grid and diagnostics."""
    out = builders.research(settings.research_dir, settings.model_dir)
    full: dict[str, Any] = {}
    candidates_dir = settings.research_dir / "candidates"
    if candidates_dir.is_dir():
        for run_dir in sorted(p for p in candidates_dir.iterdir() if p.is_dir()):
            selection = _read_json(run_dir / "selection.json")
            if isinstance(selection, dict):
                full[run_dir.name] = selection
    out["selection_files"] = full
    return out


def scrub_paths(value: Any) -> Any:
    """Absolute filesystem paths become their file names (``/data/x/h5`` -> ``h5``)."""
    if isinstance(value, dict):
        return {k: scrub_paths(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_paths(v) for v in value]
    if isinstance(value, str) and value.startswith("/") and value.count("/") >= 2:
        return Path(value).name
    return value


def export_filename(profile: str | None, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"ofi-export-{profile or 'default'}-{stamp}.json"


def _model(model_dir: Path, version: str | None) -> dict[str, Any] | None:
    if not version:
        return None
    directory = model_dir / version
    files = {}
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            content = _read_json(path)
            if content is not None:
                files[path.name] = content
    return {"version": version, "found": directory.is_dir(), "files": files}


def build_export(
    settings: Settings,
    profile: str | None,
    *,
    days: int | None = None,
    include_shadow: bool = True,
    include_signals: bool = False,
    latency_path: Path | None | bool = None,
) -> dict[str, Any]:
    """Assemble the document from the profile's state files.

    ``days`` keeps only the last N UTC days; ``include_signals`` adds the raw
    signal records (otherwise only their counts); ``latency_path`` overrides
    the auto-detected ``research/latency.json`` (False disables it).
    """
    state_dir = settings.state_dir
    wanted: set[str] | None = None
    if days is not None:
        wanted = set(_available_days(state_dir, include_shadow=include_shadow)[-days:])
    trades, trades_by_day, bad_trades = _stream(state_dir, "trades", wanted)
    signals, signals_by_day, bad_signals = _stream(state_dir, "signals", wanted)

    shadow: dict[str, Any] | None = None
    bad_shadow = 0
    if include_shadow:
        shadow_dir = state_dir / "shadow"
        if shadow_dir.is_dir():
            s_trades, s_trades_by_day, bad_t = _stream(shadow_dir, "trades", wanted)
            s_signals, s_signals_by_day, bad_s = _stream(shadow_dir, "signals", wanted)
            bad_shadow = bad_t + bad_s
            shadow = {
                "performance": _performance(s_trades, s_signals, s_trades_by_day, s_signals_by_day),
                "signal_counts": _signal_counts(s_signals, s_signals_by_day),
                "trades": s_trades,
            }
            if include_signals:
                shadow["signals"] = s_signals

    daily: dict[str, Any] = {}
    daily_dir = state_dir / "daily"
    if daily_dir.is_dir():
        for path in sorted(daily_dir.glob("*.json")):
            if wanted is not None and path.stem not in wanted:
                continue
            content = _read_json(path)
            if content is not None:
                daily[path.stem] = content

    latency: Any = None
    if latency_path is not False:
        resolved = latency_path if isinstance(latency_path, Path) else default_latency_path()
        latency = _read_json(resolved) if resolved is not None else None

    document: dict[str, Any] = {
        "meta": {
            "exported_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "profile": profile,
            "host_tag": settings.host_tag,
            "execution_mode": settings.execution_mode.value,
            "model_version": settings.model_version,
            "symbols": list(settings.binance_futures_symbols),
            "research_symbols": list(settings.research_symbols),
            "book_mode": settings.binance_futures_book_mode,
            "order_notional_usd": settings.order_notional_usd,
            "days": sorted(set(trades_by_day) | set(signals_by_day) | set(daily)),
            "unparsed_lines": bad_trades + bad_signals + bad_shadow,
            "signals_included": include_signals,
            "recordings_included": False,
        },
        "performance": _performance(trades, signals, trades_by_day, signals_by_day),
        "signal_counts": _signal_counts(signals, signals_by_day),
        "trades": trades,
        "shadow": shadow,
        "daily": daily,
        "bridge": _read_json(state_dir / "bridge.json"),
        "drills": _read_json(state_dir / "drills.json"),
        "latency": latency,
        "model": _model(settings.model_dir, settings.model_version),
        "research": _research(settings),
    }
    if include_signals:
        document["signals"] = signals
    return scrub_paths(document)


def run(argv: list[str] | None = None) -> None:
    parser = base_parser("Export performance, signals and state as one JSON file")
    parser.add_argument("--out", type=Path, default=None, help="Write here instead of stdout")
    parser.add_argument("--days", type=int, default=None, help="Only the last N UTC days")
    parser.add_argument("--no-shadow", action="store_true", help="Skip the shadow bridge")
    parser.add_argument(
        "--signals", action="store_true", help="Include the raw signal records (else counts)"
    )
    parser.add_argument("--compact", action="store_true", help="No indentation (smaller file)")
    args = parser.parse_args(argv)
    if args.days is not None and args.days < 1:
        parser.error("--days must be at least 1")
    settings = load_or_exit(load_settings, args.profile)
    document = build_export(
        settings,
        args.profile,
        days=args.days,
        include_shadow=not args.no_shadow,
        include_signals=args.signals,
    )
    text = json.dumps(document, indent=None if args.compact else 2, default=str)
    if args.out is None:
        print(text)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
        print(
            json.dumps(
                {
                    "wrote": str(args.out),
                    "signals": document["signal_counts"]["total"],
                    "trades": len(document["trades"]),
                    "days": document["meta"]["days"],
                }
            )
        )
    sys.exit(0)


if __name__ == "__main__":
    run()
