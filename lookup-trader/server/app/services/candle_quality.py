"""Candle validation, provenance, and deterministic audit reports."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from app.utils.parquet import month_partition_path, write_month_partition

CANDLE_COLUMNS = ["ts", "open", "high", "low", "close", "volume"]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _spans_weekend(left: pd.Timestamp, right: pd.Timestamp) -> bool:
    days = pd.date_range(left.normalize(), right.normalize(), freq="D")
    return any(day.weekday() == 5 for day in days)


def unexpected_gaps(frame: pd.DataFrame, *, hours: int = 1) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    ts = pd.to_datetime(frame["ts"], utc=True).sort_values().drop_duplicates()
    expected = pd.Timedelta(hours, unit="h")
    gaps = []
    for left, right in zip(ts.iloc[:-1], ts.iloc[1:], strict=True):
        delta = right - left
        # One missing hour is tolerated for the metals maintenance pause, and
        # weekend closures are expected. Longer weekday holes are operational.
        if delta <= expected * 2 or _spans_weekend(left, right):
            continue
        gaps.append(
            {
                "after": left.isoformat(),
                "before": right.isoformat(),
                "hours": delta.total_seconds() / 3600.0,
            }
        )
    return gaps


def validate_candles(frame: pd.DataFrame) -> None:
    missing = sorted(set(CANDLE_COLUMNS) - set(frame.columns))
    if missing:
        raise ValueError(f"Candle columns missing: {missing}")
    if frame["ts"].duplicated().any():
        raise ValueError("Candle timestamps contain duplicates")
    prices = frame[["open", "high", "low", "close"]].astype(float)
    if not prices.map(lambda value: math.isfinite(value) and value > 0).all().all():
        raise ValueError("Candles contain non-finite or non-positive prices")
    valid = (
        (prices["high"] >= prices["low"])
        & prices["open"].between(prices["low"], prices["high"])
        & prices["close"].between(prices["low"], prices["high"])
    )
    if not valid.all():
        raise ValueError("Candles contain invalid OHLC ordering")
    volume = pd.to_numeric(frame["volume"], errors="coerce")
    if volume.isna().any() or (volume < 0).any():
        raise ValueError("Candles contain invalid volume")


def quality_report(
    frame: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    source_files: list[Path] | None = None,
) -> dict[str, Any]:
    missing = sorted(set(CANDLE_COLUMNS) - set(frame.columns))
    if missing:
        raise ValueError(f"Candle columns missing: {missing}")
    prices = frame[["open", "high", "low", "close"]].astype(float)
    invalid_price_rows = ~prices.map(
        lambda value: math.isfinite(value) and value > 0
    ).all(axis=1)
    invalid_ohlc_rows = ~(
        (prices["high"] >= prices["low"])
        & prices["open"].between(prices["low"], prices["high"])
        & prices["close"].between(prices["low"], prices["high"])
    )
    volume = pd.to_numeric(frame["volume"], errors="coerce")
    invalid_volume_rows = volume.isna() | (volume < 0)
    duplicates = int(frame["ts"].duplicated().sum())
    ordered = frame.sort_values("ts")
    minimum = pd.Timestamp(ordered["ts"].min())
    maximum = pd.Timestamp(ordered["ts"].max())
    months = pd.period_range(minimum.tz_localize(None), maximum.tz_localize(None), freq="M")
    present = set(pd.to_datetime(ordered["ts"], utc=True).dt.tz_localize(None).dt.to_period("M"))
    validation = {
        "duplicate_timestamps": duplicates,
        "invalid_price_rows": int(invalid_price_rows.sum()),
        "invalid_ohlc_rows": int(invalid_ohlc_rows.sum()),
        "invalid_volume_rows": int(invalid_volume_rows.sum()),
    }
    return {
        "report_version": 1,
        "provider": "histdata",
        "symbol": symbol.upper(),
        "timeframe": timeframe.upper(),
        "bars": len(ordered),
        "range": {"min": minimum.isoformat(), "max": maximum.isoformat()},
        "coverage_years": (maximum - minimum).total_seconds() / (365.2425 * 86400),
        "valid": not any(validation.values()),
        "validation": validation,
        "duplicates": duplicates,
        "unexpected_gaps": unexpected_gaps(ordered),
        "missing_months": [str(month) for month in months if month not in present],
        "source_files": [
            {"name": path.name, "bytes": path.stat().st_size, "sha256": file_sha256(path)}
            for path in sorted(source_files or [])
        ],
    }


def write_histdata_provenance(
    frame: pd.DataFrame,
    *,
    output_root: Path,
    symbol: str,
    timeframe: str,
    source_files: list[Path],
) -> None:
    digest = hashlib.sha256(
        (
            f"{symbol.upper()}|{timeframe.upper()}|"
            + "".join(file_sha256(path) for path in sorted(source_files))
        ).encode("ascii")
    ).hexdigest()
    batch_path = output_root / "histdata_batches" / f"{digest}.json"
    batch_path.parent.mkdir(parents=True, exist_ok=True)
    if not batch_path.exists():
        batch_path.write_text(
            json.dumps(
                {
                    "provider": "histdata",
                    "symbol": symbol.upper(),
                    "timeframe": timeframe.upper(),
                    "source_batch_sha256": digest,
                    "source_files": [
                        {
                            "name": path.name,
                            "bytes": path.stat().st_size,
                            "sha256": file_sha256(path),
                        }
                        for path in sorted(source_files)
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    provenance = pd.DataFrame(
        {
            "ts": pd.to_datetime(frame["ts"], utc=True),
            "provider": "histdata",
            "source_instrument": symbol.upper(),
            "price_side": "bid",
            "source_batch_sha256": digest,
        }
    )
    provenance["year"] = provenance["ts"].dt.year
    provenance["month"] = provenance["ts"].dt.month
    columns = [
        "ts", "provider", "source_instrument", "price_side", "source_batch_sha256"
    ]
    for (year, month), group in provenance.groupby(["year", "month"]):
        path = month_partition_path(
            output_root, symbol.upper(), timeframe.upper(), int(year), int(month)
        )
        write_month_partition(path, group, columns)


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
