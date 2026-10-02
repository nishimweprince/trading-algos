"""What the read-only status page shows, built from files and the scalper's status.

Every section is assembled field by field from an explicit list: nothing from
the scalper's ``/v1/status``, the settings or a file is passed through whole,
so no key, balance, account alias or path can reach a viewer by accident.

Sections: ``roadmap`` (where the project stands), ``collection`` (what has been
recorded and how close the research milestones are), ``health`` (the live
services), ``research`` (runs, candidates, selection, gates) and ``trading``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .trades import JsonlDaily, summarise

__all__ = [
    "MILESTONES",
    "UNITS",
    "History",
    "collection",
    "eligibility",
    "health",
    "research",
    "roadmap",
    "trading",
    "unit_states",
]

ROADMAP_PATH = Path(__file__).resolve().parents[2] / "status_roadmap.json"
UNITS = (
    "ofi-scalper-service",
    "execution-service-binance",
    "notification-service",
    "ofi-daily-check.timer",
    "ofi-status",
)
# Eligible days each research run needs (research/cv.py: 3 walk-forward periods + 1 held out).
MILESTONES = {
    "provisional": {"days": 28, "periods": 4, "period": "week", "label": "Provisional run"},
    "binding": {"days": 120, "periods": 4, "period": "month", "label": "Binding run"},
}
REPORT_LIMIT = 20_000


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _pick(source: Any, *keys: str) -> Any:
    """A nested value, or None anywhere along the way."""
    for key in keys:
        if not isinstance(source, dict):
            return None
        source = source.get(key)
    return source


# --- roadmap ---------------------------------------------------------------------------


def roadmap(path: Path = ROADMAP_PATH) -> list[dict[str, str]]:
    raw = _read_json(path) or []
    return [
        {k: str(item.get(k, "")) for k in ("phase", "status", "note", "date")}
        for item in raw
        if isinstance(item, dict)
    ]


# --- data collection -----------------------------------------------------------------------


def eligibility(summary: dict[str, Any], host_tag: str) -> tuple[bool, str]:
    """The research pipeline's rule: clean, recorded on this host, diff mode."""
    if not summary.get("ok"):
        return False, "; ".join(summary.get("problems") or ["problems"])
    hosts = summary.get("hosts")
    if hosts is not None and hosts != [host_tag]:
        return False, f"recorded on {', '.join(hosts) or '?'}, need {host_tag}"
    mode = summary.get("book_mode")
    if mode is not None and mode != "diff":
        return False, f"book mode {mode}, need diff"
    return True, ""


def _day_row(summary: dict[str, Any], host_tag: str) -> dict[str, Any]:
    eligible, reason = eligibility(summary, host_tag)
    symbols = {}
    for symbol, entry in (summary.get("symbols") or {}).items():
        symbols[str(symbol)] = {
            "hours": 24 - len(entry.get("missing_hours") or []),
            "mb": entry.get("mb"),
            "lines": entry.get("lines"),
            "depth_breaks": entry.get("depth_breaks"),
            "unrecovered_breaks": entry.get("unrecovered_breaks"),
            "trade_gaps": entry.get("agg_trade_gaps"),
            "max_silence_s": round((entry.get("max_silence_ms") or 0) / 1000, 1),
        }
    return {
        "date": summary.get("date"),
        "eligible": eligible,
        "reason": reason,
        "book_mode": summary.get("book_mode"),
        "mb": round(sum((v["mb"] or 0) for v in symbols.values()), 1),
        "symbols": symbols,
        "complete": True,
    }


def _today_row(record_dir: Path, today: date) -> dict[str, Any] | None:
    stamp = today.strftime("%Y%m%d")
    symbols: dict[str, dict[str, Any]] = {}
    for manifest in record_dir.glob(f"*/{stamp}/*.gz.json"):
        symbol = manifest.parent.parent.name
        if symbol.startswith("_"):
            continue
        body = _read_json(manifest) or {}
        data = manifest.with_name(manifest.name.removesuffix(".json"))
        entry = symbols.setdefault(symbol, {"hours": 0, "mb": 0.0, "lines": 0, "gaps": 0})
        entry["hours"] += 1
        entry["lines"] += int(body.get("lines") or 0)
        entry["gaps"] += int(body.get("gaps") or 0)
        entry["mb"] = round(entry["mb"] + (data.stat().st_size / 1e6 if data.exists() else 0), 1)
    # The open hour has no manifest until it closes: count its file too.
    for data in record_dir.glob(f"*/{stamp}/*.gz"):
        symbol = data.parent.parent.name
        if symbol.startswith("_") or Path(f"{data}.json").exists():
            continue
        entry = symbols.setdefault(symbol, {"hours": 0, "mb": 0.0, "lines": 0, "gaps": 0})
        entry["hours"] += 1
        entry["mb"] = round(entry["mb"] + data.stat().st_size / 1e6, 1)
    if not symbols:
        return None
    return {
        "date": today.isoformat(),
        "eligible": None,
        "reason": "in progress",
        "mb": round(sum(v["mb"] for v in symbols.values()), 1),
        "symbols": symbols,
        "complete": False,
    }


def _disk_free_gb(path: Path) -> float | None:
    existing = path.expanduser().absolute()
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    try:
        return round(shutil.disk_usage(existing).free / 1e9, 1)
    except OSError:
        return None


def _milestone(name: str, eligible_days: list[str], today: date) -> dict[str, Any]:
    spec = MILESTONES[name]
    periods = set()
    for day in eligible_days:
        d = date.fromisoformat(day)
        if spec["period"] == "week":
            year, week, _ = d.isocalendar()
            periods.add(f"{year}-W{week:02d}")
        else:
            periods.add(d.strftime("%Y-%m"))
    remaining = max(spec["days"] - len(eligible_days), 0)
    return {
        "label": spec["label"],
        "eligible_days": len(eligible_days),
        "target_days": spec["days"],
        "periods": len(periods),
        "target_periods": spec["periods"],
        "period": spec["period"],
        "progress": round(min(len(eligible_days) / spec["days"], 1.0), 4),
        # Assumes one clean day per day from now: a lower bound, not a promise.
        "earliest": (today + timedelta(days=remaining)).isoformat() if remaining else "reached",
    }


def collection(
    *,
    record_dir: Path,
    state_dir: Path,
    host_tag: str,
    min_free_disk_gb: float,
    today: date | None = None,
) -> dict[str, Any]:
    today = today or datetime.now(UTC).date()
    rows = []
    for path in sorted((state_dir / "daily").glob("*.json")):
        summary = _read_json(path)
        if isinstance(summary, dict) and summary.get("date"):
            rows.append(_day_row(summary, host_tag))
    partial = _today_row(record_dir, today)
    eligible = [r["date"] for r in rows if r["eligible"]]
    recent = [r["mb"] for r in rows[-7:]]
    gb_per_day = round(sum(recent) / len(recent) / 1000, 2) if recent else None
    free = _disk_free_gb(record_dir)
    runway = (
        round(max(free - min_free_disk_gb, 0.0) / gb_per_day, 1)
        if free is not None and gb_per_day
        else None
    )
    return {
        "days": rows[::-1] if partial is None else [partial, *rows[::-1]],
        "recorded_days": len(rows),
        "eligible_days": len(eligible),
        "excluded": [{"date": r["date"], "reason": r["reason"]} for r in rows if not r["eligible"]],
        "total_gb": round((sum(r["mb"] for r in rows) + (partial or {}).get("mb", 0)) / 1000, 2),
        "gb_per_day": gb_per_day,
        "disk_free_gb": free,
        "disk_floor_gb": min_free_disk_gb,
        "disk_runway_days": runway,
        "milestones": {name: _milestone(name, eligible, today) for name in MILESTONES},
    }


# --- live health ---------------------------------------------------------------------------


def unit_states(units: tuple[str, ...] = UNITS) -> dict[str, str]:
    states = {}
    for unit in units:
        try:
            done = subprocess.run(
                ["systemctl", "is-active", unit], capture_output=True, text=True, timeout=5
            )
            states[unit] = done.stdout.strip() or "unknown"
        except (OSError, subprocess.SubprocessError):
            states[unit] = "unknown"
    return states


def health(
    status: dict[str, Any] | None,
    units: dict[str, str],
    *,
    fetched_at: float | None,
    last_ok_at: float | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "services": {str(k): str(v) for k, v in units.items()},
        "scalper_reachable": status is not None,
        "fetched_at": _iso(fetched_at),
        "last_ok_at": _iso(last_ok_at),
    }
    if status is None:
        return out
    heartbeat = status.get("heartbeat") or {}
    streams = status.get("streams") or {}
    risk = status.get("risk") or {}
    books = {}
    for symbol, book in (streams.get("books") or {}).items():
        books[str(symbol)] = {
            "state": _pick(book, "state"),
            "gaps": _pick(book, "gaps"),
            "resyncs": _pick(book, "resyncs"),
        }
    lag = {}
    for kind, pair in (heartbeat.get("lag_ms") or {}).items():
        if kind in {"depth", "aggTrade"} and isinstance(pair, list | tuple) and len(pair) == 2:
            lag[kind] = {"p50": pair[0], "p99": pair[1]}
    recorder = heartbeat.get("recorder") or {}
    out.update(
        ready=bool(status.get("ready")),
        execution_mode=str(status.get("execution_mode") or ""),
        model_version=_pick(status, "model", "version"),
        uptime_s=heartbeat.get("uptime_s"),
        book_mode=streams.get("book_mode"),
        books=books,
        reconnects={str(k): v for k, v in (streams.get("reconnects") or {}).items()},
        lag_ms=lag,
        events_per_s={
            k: v
            for k, v in (heartbeat.get("events_per_s") or {}).items()
            if k in {"DepthUpdate", "BookSnapshot", "AggTrade", "BookTick", "grid_samples"}
        },
        pauses={str(k): list(v) for k, v in (risk.get("pauses") or {}).items()},
        halted=bool(risk.get("halted")),
        halt_reason=_pick(risk, "halt", "reason"),
        recorder={
            "lines_per_s": recorder.get("lines_per_s"),
            "disk_free_gb": recorder.get("disk_free_gb"),
            "disk_paused": recorder.get("disk_paused"),
            "dropped": recorder.get("dropped"),
            "errors": recorder.get("errors"),
        },
        key_check=_pick(status, "key_check", "status"),
        handler_errors=heartbeat.get("handler_errors"),
    )
    return out


class History:
    """24 h of one-minute points from the scalper's heartbeat, in memory."""

    def __init__(self, points: int = 24 * 60) -> None:
        self.points: deque[dict[str, Any]] = deque(maxlen=points)

    def add(self, status: dict[str, Any] | None, at: float) -> None:
        heartbeat = (status or {}).get("heartbeat") or {}
        lag = heartbeat.get("lag_ms") or {}
        depth = lag.get("depth") if isinstance(lag.get("depth"), list | tuple) else None
        rates = heartbeat.get("events_per_s") or {}
        recorder = heartbeat.get("recorder") or {}
        self.points.append(
            {
                "t": round(at),
                "up": status is not None,
                "depth_lag_p50": depth[0] if depth else None,
                "depth_lag_p99": depth[1] if depth else None,
                "depth_per_s": rates.get("DepthUpdate", rates.get("BookSnapshot")),
                "trades_per_s": rates.get("AggTrade"),
                "lines_per_s": recorder.get("lines_per_s"),
                "disk_free_gb": recorder.get("disk_free_gb"),
            }
        )

    def series(self) -> list[dict[str, Any]]:
        return list(self.points)


def _iso(t: float | None) -> str | None:
    return None if t is None else datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds")


# --- research --------------------------------------------------------------------------------


def research(research_dir: Path, model_dir: Path) -> dict[str, Any]:
    runs = []
    for path in sorted((research_dir / "splits").glob("*.json")):
        split = _read_json(path) or {}
        runs.append(
            {
                "run": split.get("run"),
                "period": split.get("period"),
                "binding": split.get("binding"),
                "walk_forward": list(split.get("walk_forward") or []),
                "held_out": list(split.get("holdout") or []),
                "days": sum(len(v) for v in (split.get("days") or {}).values()),
                "created_at": split.get("created_at"),
            }
        )
    candidates, selections = [], []
    for run_dir in sorted(p for p in (research_dir / "candidates").glob("*") if p.is_dir()):
        for cand in sorted(run_dir.glob("h*/candidate.json")):
            meta = _read_json(cand) or {}
            cv = _read_json(cand.parent / "cv_report.json") or {}
            candidates.append(
                {
                    "run": run_dir.name,
                    "horizon_s": meta.get("horizon_s"),
                    "barrier_bp": meta.get("barrier_bp"),
                    "features": len(meta.get("features") or []),
                    "train_rows": meta.get("final_train_rows"),
                    "folds": len([f for f in cv.get("folds") or [] if "skipped" not in f]),
                    "brier": {
                        name: _pick(cv, "calibrated", name, "brier")
                        for name in ("down", "none", "up")
                    },
                    "reliability_up": [
                        {
                            "predicted": b.get("predicted"),
                            "observed": b.get("observed"),
                            "n": b.get("n"),
                        }
                        for b in _pick(cv, "calibrated", "up", "reliability") or []
                    ],
                }
            )
        selection = _read_json(run_dir / "selection.json")
        if isinstance(selection, dict):
            row_keys = (
                "name",
                "horizon_s",
                "threshold",
                "trades",
                "net_usd",
                "mean_net_bp",
                "win_rate",
            )
            chosen = selection.get("chosen")
            selections.append(
                {
                    "run": run_dir.name,
                    "rule": selection.get("rule"),
                    "chosen": {k: chosen.get(k) for k in row_keys}
                    if isinstance(chosen, dict)
                    else None,
                    "grid": [
                        {k: row.get(k) for k in row_keys}
                        for row in (selection.get("grid") or [])[:10]
                    ],
                }
            )
    models = []
    for gates_path in sorted(model_dir.glob("*/gates.json")):
        gates = _read_json(gates_path) or {}
        models.append(
            {
                "version": gates.get("model_version"),
                "passed": gates.get("passed") is True,
                "binding": gates.get("binding") is True,
                "evaluated_at": gates.get("evaluated_at"),
                "held_out_days": len(gates.get("held_out_days") or []),
                "gates": {
                    str(name): {
                        "value": g.get("value"),
                        "threshold": g.get("threshold"),
                        "passed": g.get("passed") is True,
                    }
                    for name, g in (gates.get("gates") or {}).items()
                    if isinstance(g, dict)
                },
            }
        )
    report_path = research_dir / "REPORT.md"
    report = report_path.read_text()[:REPORT_LIMIT] if report_path.is_file() else None
    ledger = _read_json(research_dir / "holdout_ledger.json") or {}
    return {
        "runs": runs,
        "candidates": candidates,
        "selections": selections,
        "models": models,
        "report": report,
        "holdout_evaluations": [
            {"held_out": key, "candidate": v.get("candidate"), "at": v.get("at")}
            for key, v in ledger.items()
            if isinstance(v, dict)
        ],
    }


# --- trading ---------------------------------------------------------------------------------

TRADE_FIELDS = (
    "signal_at",
    "symbol",
    "side",
    "p",
    "filled",
    "entry_price",
    "exit_price",
    "exit_reason",
    "net_bp",
    "net_usd",
    "mode",
    "model_version",
)
SIGNAL_FIELDS = ("at", "symbol", "side", "p", "threshold", "edge_bp", "cost_bp", "action")


def trading(state_dir: Path, *, model_loaded: bool, today: date | None = None) -> dict[str, Any]:
    today = today or datetime.now(UTC).date()
    trades_log, signals_log = JsonlDaily(state_dir / "trades"), JsonlDaily(state_dir / "signals")
    days = [(today - timedelta(days=i)).isoformat() for i in range(7)]
    week_trades, week_signals = [], []
    for day in days:
        week_trades += trades_log.read(day)
        week_signals += signals_log.read(day)
    today_trades, today_signals = trades_log.read(days[0]), signals_log.read(days[0])

    def brief(summary: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "signals",
            "entries",
            "trades",
            "maker_fill_rate",
            "net_usd",
            "fees_usd",
            "win_rate",
            "largest_loss_usd",
            "mean_net_bp",
            "brier",
        )
        return {k: summary.get(k) for k in keys}

    empty = not week_trades and not week_signals
    return {
        "note": (
            "No model loaded: the bot trades only after a model passes its research gates."
            if empty and not model_loaded
            else None
        ),
        "today": brief(summarise(today_trades, today_signals)),
        "last_7_days": brief(summarise(week_trades, week_signals)),
        "recent_trades": [{k: t.get(k) for k in TRADE_FIELDS} for t in week_trades[-50:]][::-1],
        "recent_signals": [{k: s.get(k) for k in SIGNAL_FIELDS} for s in week_signals[-50:]][::-1],
    }


# --- caching -----------------------------------------------------------------------------------


class Cached:
    """A builder's result, recomputed at most every ``ttl`` seconds."""

    def __init__(self, build: Callable[[], Any], ttl: float = 60.0) -> None:
        self.build, self.ttl = build, ttl
        self.value: Any = None
        self.at = float("-inf")

    def get(self) -> Any:
        now = time.monotonic()
        if now - self.at >= self.ttl:
            self.value, self.at = self.build(), now
        return self.value
