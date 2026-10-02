"""Daily recording check: ``ofi-daily-check --profile dev [--date 2026-10-01]``.

Runs :func:`gapcheck.check_file` over one UTC day of recordings (default:
yesterday), summarises it per symbol (hours present, missing hours, lines,
size, depth breaks, unrecovered breaks, trade-id gaps, truncated files) and
sends the summary through notification-service. Meant for a daily systemd
timer; exits 1 when the day has a problem a researcher must know about:
missing hours, an unrecovered depth break, or a backwards timestamp.

In shadow or testnet mode it adds the day's trading report (plan §5 <output>):
trades, net P&L, fees, win rate, largest loss, maker fill rate, adverse
selection, order round trip and calibration (Brier) on our own fills.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ta_core import base_parser, load_or_exit
from ta_notify import SyncNotifier

from .config import load_settings
from .gapcheck import check_file
from .trades import JsonlDaily
from .trades import summarise as summarise_trades

__all__ = ["run", "summarise"]

HOURS = 24


def summarise(record_dir: Path, day: date) -> dict[str, Any]:
    stamp = day.strftime("%Y%m%d")
    files = sorted(record_dir.glob(f"*/{stamp}/*.gz"))
    symbols: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "hours": set(),
            "lines": 0,
            "bytes": 0,
            "depth_breaks": 0,
            "unrecovered_breaks": 0,
            "agg_trade_gaps": 0,
            "partial_skipped": 0,
            "backwards": 0,
            "truncated": 0,
            "max_silence_ms": 0.0,
        }
    )
    for path in files:
        symbol, _, hour = path.stem.rpartition("_")
        symbol = symbol.removesuffix(f"_{stamp}")
        if symbol.startswith("_"):
            continue  # _CONTROL: session/reset lines, not a market stream
        report = check_file(path)
        entry = symbols[symbol]
        entry["hours"].add(int(hour))
        entry["bytes"] += path.stat().st_size
        entry["truncated"] += int(report["truncated"])
        entry["max_silence_ms"] = max(entry["max_silence_ms"], report["max_silence_ms"])
        for key in (
            "lines",
            "depth_breaks",
            "unrecovered_breaks",
            "agg_trade_gaps",
            "partial_skipped",
            "backwards",
        ):
            entry[key] += report[key]
    out: dict[str, Any] = {"date": day.isoformat(), "files": len(files), "symbols": {}}
    problems: list[str] = []
    if not files:
        problems.append("no recordings at all")
    for symbol, entry in sorted(symbols.items()):
        missing = sorted(set(range(HOURS)) - entry.pop("hours"))
        entry["missing_hours"] = missing
        entry["mb"] = round(entry.pop("bytes") / 1e6, 1)
        out["symbols"][symbol] = entry
        if missing:
            problems.append(f"{symbol}: {len(missing)} missing hour(s) {missing[:6]}")
        if entry["unrecovered_breaks"]:
            problems.append(f"{symbol}: {entry['unrecovered_breaks']} unrecovered depth break(s)")
        if entry["backwards"]:
            problems.append(f"{symbol}: {entry['backwards']} backwards timestamp(s)")
    out["problems"] = problems
    out["ok"] = not problems
    return out


def trading_summary(state_dir: Path, day: date) -> dict[str, Any] | None:
    """The day's trading report, or None if nothing traded or signalled."""
    trades = JsonlDaily(state_dir / "trades").read(day.isoformat())
    signals = JsonlDaily(state_dir / "signals").read(day.isoformat())
    if not trades and not signals:
        return None
    return summarise_trades(trades, signals)


def _trading_lines(report: dict[str, Any]) -> list[str]:
    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value * 100:.1f}%"

    adverse = report["adverse_bp_mean"]
    return [
        f"Trading: {report['signals']} signals, {report['entries']} entries, "
        f"{report['trades']} trades (maker fill {pct(report['maker_fill_rate'])})",
        f"Net ${report['net_usd']:.4f}, fees ${report['fees_usd']:.4f}, "
        f"win rate {pct(report['win_rate'])}, largest loss ${report['largest_loss_usd']:.4f}",
        f"Mid move after fill 1/5/30s (bp): {adverse.get('1s')} / {adverse.get('5s')} / "
        f"{adverse.get('30s')}; Brier {report['brier']}; "
        f"entry RTT p50/p99 {report['entry_rtt_ms']['p50']}/{report['entry_rtt_ms']['p99']} ms",
        f"Exits: {report['exit_reasons']}",
    ]


def _lines(summary: dict[str, Any]) -> list[str]:
    lines = [f"{summary['date']}: {summary['files']} files"]
    for symbol, entry in summary["symbols"].items():
        lines.append(
            f"{symbol}: {HOURS - len(entry['missing_hours'])}/{HOURS}h, {entry['mb']} MB, "
            f"{entry['lines']:,} lines, breaks {entry['depth_breaks']} "
            f"(unrecovered {entry['unrecovered_breaks']}), trade gaps {entry['agg_trade_gaps']}, "
            f"max silence {entry['max_silence_ms'] / 1000:.1f}s"
        )
    lines += [f"PROBLEM: {problem}" for problem in summary["problems"]]
    if summary.get("trading"):
        lines += _trading_lines(summary["trading"])
    return lines


def run(argv: list[str] | None = None) -> None:
    parser = base_parser("Check one UTC day of recordings and send a summary")
    parser.add_argument("--date", type=date.fromisoformat, default=None)
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args(argv)
    settings = load_or_exit(load_settings, args.profile)
    day = args.date or (datetime.now(UTC).date() - timedelta(days=1))
    summary = summarise(settings.record_dir, day)
    summary["trading"] = trading_summary(settings.state_dir, day)
    print(json.dumps(summary, indent=2))
    if not args.no_notify:
        verdict = "OK" if summary["ok"] else "PROBLEMS"
        SyncNotifier(settings, source="ofi-scalper").send(
            f"OFI recordings {summary['date']}: {verdict}",
            _lines(summary),
            idempotency_key=f"ofi:daily:{summary['date']}",
        )
    sys.exit(0 if summary["ok"] else 1)


if __name__ == "__main__":
    run()
