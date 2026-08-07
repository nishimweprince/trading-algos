"""Deterministic candle acceptance audit and non-destructive exclusions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from app.config import settings
from app.db.duck import register_candles_view
from app.services.candle_quality import file_sha256, unexpected_gaps

AUDIT_VERSION = 1
COMPLETENESS_THRESHOLD = 0.90
MIN_UNEXPECTED_GAPS = 3
PRE_PADDING_BARS = 48
POST_PADDING_BARS = 600


def exclusion_path(symbol: str, timeframe: str) -> Path:
    return settings.data_dir / "exports" / f"candle-exclusions-{symbol}-{timeframe}-v1.json"


def report_path(symbol: str, timeframe: str) -> Path:
    return settings.data_dir / "reports" / f"candle-audit-{symbol}-{timeframe}-v1.json"


def _sha256_payload(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _source_files(symbol: str, timeframe: str) -> list[dict[str, Any]]:
    root = settings.data_dir / "candles" / f"symbol={symbol}" / f"timeframe={timeframe}"
    return [
        {
            "path": str(path.relative_to(settings.data_dir)),
            "bytes": path.stat().st_size,
            "sha256": file_sha256(path),
        }
        for path in sorted(root.glob("year=*/month=*/part-*.parquet"))
    ]


def _load(symbol: str, timeframe: str) -> pd.DataFrame:
    con = duckdb.connect(":memory:")
    try:
        register_candles_view(con, force=True)
        frame = con.execute(
            """
            SELECT ts, open, high, low, close, volume
            FROM candles WHERE symbol = ? AND timeframe = ? ORDER BY ts
            """,
            [symbol, timeframe],
        ).fetchdf()
    finally:
        con.close()
    if frame.empty:
        raise ValueError(f"No candles found for {symbol} {timeframe}")
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    return frame


def build_audit(symbol: str, timeframe: str) -> tuple[dict[str, Any], dict[str, Any]]:
    symbol, timeframe = symbol.upper(), timeframe.upper()
    frame = _load(symbol, timeframe)
    ts = frame["ts"]
    gaps = unexpected_gaps(frame)

    monthly = frame.assign(year=ts.dt.year, month=ts.dt.month).groupby(
        ["year", "month"], as_index=False
    ).size().rename(columns={"size": "bars"})
    medians = monthly.groupby("month")["bars"].median().to_dict()
    gap_counts: dict[tuple[int, int], int] = {}
    for gap in gaps:
        boundary = pd.Timestamp(gap["before"])
        key = (boundary.year, boundary.month)
        gap_counts[key] = gap_counts.get(key, 0) + 1

    month_rows: list[dict[str, Any]] = []
    anomalous: list[tuple[int, int]] = []
    for row in monthly.itertuples(index=False):
        median = float(medians[int(row.month)])
        ratio = float(row.bars) / median if median else 0.0
        count = gap_counts.get((int(row.year), int(row.month)), 0)
        flagged = ratio < COMPLETENESS_THRESHOLD and count >= MIN_UNEXPECTED_GAPS
        if flagged:
            anomalous.append((int(row.year), int(row.month)))
        month_rows.append(
            {
                "year": int(row.year),
                "month": int(row.month),
                "bars": int(row.bars),
                "reference_median": median,
                "completeness_ratio": round(ratio, 6),
                "unexpected_gaps": count,
                "excluded": flagged,
            }
        )

    raw_intervals: list[dict[str, Any]] = []
    expanded: list[dict[str, Any]] = []
    for year, month in anomalous:
        mask = (ts.dt.year == year) & (ts.dt.month == month)
        positions = frame.index[mask].tolist()
        first, last = positions[0], positions[-1]
        start_idx = max(0, first - PRE_PADDING_BARS)
        end_idx = min(len(frame) - 1, last + POST_PADDING_BARS)
        reason = "monthly_completeness_below_90pct_with_3plus_unexpected_gaps"
        raw_intervals.append(
            {
                "start": ts.iloc[first].isoformat(),
                "end": ts.iloc[last].isoformat(),
                "reason": reason,
            }
        )
        expanded.append(
            {
                "start": ts.iloc[start_idx].isoformat(),
                "end": ts.iloc[end_idx].isoformat(),
                "reason": reason,
                "pre_padding_bars": first - start_idx,
                "post_padding_bars": end_idx - last,
            }
        )

    # Consecutive anomalous months overlap after padding. Merge them so every
    # consumer applies exactly the same interval set.
    merged: list[dict[str, Any]] = []
    for item in sorted(expanded, key=lambda value: value["start"]):
        if not merged or pd.Timestamp(item["start"]) > pd.Timestamp(merged[-1]["end"]):
            merged.append(dict(item))
            continue
        merged[-1]["end"] = max(merged[-1]["end"], item["end"])
        merged[-1]["reason"] = "merged_anomalous_months_with_dependency_padding"

    prices = frame[["open", "high", "low", "close"]].astype(float)
    invalid_ohlc = ~(
        (prices["high"] >= prices["low"])
        & prices["open"].between(prices["low"], prices["high"])
        & prices["close"].between(prices["low"], prices["high"])
    )
    sources = _source_files(symbol, timeframe)
    exclusion = {
        "manifest_version": AUDIT_VERSION,
        "symbol": symbol,
        "timeframe": timeframe,
        "policy": {
            "monthly_completeness_threshold": COMPLETENESS_THRESHOLD,
            "minimum_unexpected_gaps": MIN_UNEXPECTED_GAPS,
            "pre_padding_bars": PRE_PADDING_BARS,
            "post_padding_bars": POST_PADDING_BARS,
        },
        "raw_intervals": raw_intervals,
        "expanded_intervals": merged,
    }
    exclusion["policy_sha256"] = _sha256_payload(exclusion)
    report = {
        "report_version": AUDIT_VERSION,
        "status": "accepted_with_exclusions" if merged else "accepted",
        "symbol": symbol,
        "timeframe": timeframe,
        "bars": len(frame),
        "range": {"min": ts.iloc[0].isoformat(), "max": ts.iloc[-1].isoformat()},
        "validation": {
            "duplicate_timestamps": int(ts.duplicated().sum()),
            "invalid_ohlc_rows": int(invalid_ohlc.sum()),
            "zero_volume_rows": int((frame["volume"].fillna(0) == 0).sum()),
            "unexpected_gaps": len(gaps),
        },
        "monthly_coverage": month_rows,
        "excluded_months": [f"{year:04d}-{month:02d}" for year, month in anomalous],
        "raw_exclusion_intervals": raw_intervals,
        "expanded_exclusion_intervals": merged,
        "source_files": sources,
    }
    return report, exclusion


def write_audit(symbol: str, timeframe: str) -> tuple[Path, Path, dict[str, Any]]:
    report, exclusion = build_audit(symbol, timeframe)
    out_report, out_exclusion = report_path(symbol.upper(), timeframe.upper()), exclusion_path(
        symbol.upper(), timeframe.upper()
    )
    out_report.parent.mkdir(parents=True, exist_ok=True)
    out_exclusion.parent.mkdir(parents=True, exist_ok=True)
    out_report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    out_exclusion.write_text(
        json.dumps(exclusion, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return out_report, out_exclusion, report


def load_exclusions(symbol: str, timeframe: str) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    path = exclusion_path(symbol.upper(), timeframe.upper())
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        (pd.Timestamp(item["start"]), pd.Timestamp(item["end"]))
        for item in payload.get("expanded_intervals", [])
    ]


def is_reliable(ts: Any, intervals: list[tuple[pd.Timestamp, pd.Timestamp]]) -> bool:
    value = pd.Timestamp(ts)
    return not any(start <= value <= end for start, end in intervals)
