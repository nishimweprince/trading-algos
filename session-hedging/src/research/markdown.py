"""Shared Markdown helpers for the research studies."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from models import M1CoverageReport, Timeframe


class _Identified(Protocol):
    symbol: str
    timeframe: Timeframe
    source: str
    bar_count: int
    first_bar_ts: datetime
    last_bar_ts: datetime
    candle_set_sha256: str


def identity_section(
    report: _Identified, *, extra: list[tuple[str, str]] | None = None
) -> list[str]:
    rows = [
        ("Symbol", report.symbol),
        ("Timeframe", report.timeframe.value),
        ("Source", str(report.source)),
        ("Bars", str(report.bar_count)),
        ("First bar (UTC)", report.first_bar_ts.isoformat()),
        ("Last bar (UTC)", report.last_bar_ts.isoformat()),
        ("Candle fingerprint (sha256)", f"`{report.candle_set_sha256}`"),
    ]
    rows.extend(extra or [])
    return ["## Run identity", "", "| Field | Value |", "|---|---|"] + [
        f"| {name} | {value} |" for name, value in rows
    ] + [""]


def m1_section(coverage: M1CoverageReport) -> list[str]:
    return [
        "## M1 coverage and subpath fallback",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| `INTRABAR_MODE` | {coverage.intrabar_mode.value} |",
        f"| M1 coverage status | **{coverage.status}** |",
        f"| M1 bars loaded | {coverage.m1_bars_loaded} |",
        f"| Parent bars with covering M1 | {coverage.covered_parent_bars} / "
        f"{coverage.total_parent_bars} ({coverage.covered_parent_fraction:.2%}) |",
        f"| M1 subpath chronology used | {'yes' if coverage.subpath_used else 'no'} |",
        f"| Fallback | {coverage.subpath_fallback or 'none (M1 subpath used)'} |",
        "",
        coverage.fallback_description,
        "",
    ]


def table(header: list[str], rows: list[list[str]], *, align_right_from: int = 1) -> list[str]:
    separator = [
        "---" if index < align_right_from else "---:" for index in range(len(header))
    ]
    return (
        ["| " + " | ".join(header) + " |", "|" + "|".join(separator) + "|"]
        + ["| " + " | ".join(row) + " |" for row in rows]
        + [""]
    )


def num(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def pct(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def ts(value: datetime | None) -> str:
    return "—" if value is None else value.isoformat()
