#!/usr/bin/env python3
"""Ingest HistData CSV exports into hive-partitioned Parquet candles."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

# Allow running from repo root without installing server package.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))

from app.utils.parquet import month_partition_path, write_month_partition  # noqa: E402

CANDLE_COLUMNS = ["ts", "open", "high", "low", "close", "volume"]

TIMEFRAME_MINUTES = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
}


def _parse_tick_timestamp(dt_part: str) -> pd.Timestamp:
    """Parse HistData tick stamp: 'YYYYMMDD HHMMSSmmm'."""
    date_str, time_str = dt_part.strip().split(maxsplit=1)
    time_str = time_str.zfill(9)
    return pd.Timestamp(
        year=int(date_str[0:4]),
        month=int(date_str[4:6]),
        day=int(date_str[6:8]),
        hour=int(time_str[0:2]),
        minute=int(time_str[2:4]),
        second=int(time_str[4:6]),
        microsecond=int(time_str[6:9]) * 1000,
        tz="UTC",
    )


def _parse_minute_timestamp(date_str: str, time_str: str) -> pd.Timestamp:
    return pd.to_datetime(date_str + time_str.zfill(6), format="%Y%m%d%H%M%S", utc=True)


def _parse_combined_minute_timestamp(dt_part: str) -> pd.Timestamp:
    """Parse 'YYYYMMDD HHMMSS' combined stamp used in semicolon minute files."""
    date_str, time_str = dt_part.strip().split(maxsplit=1)
    return _parse_minute_timestamp(date_str, time_str)


def _line_separator(line: str) -> str:
    return ";" if line.count(";") > line.count(",") else ","


def _detect_format(path: Path) -> str:
    """Return 'tick' or 'minute' based on filename and first data row."""
    name = path.name.upper()
    if "_T_" in name or name.endswith("_T.CSV"):
        return "tick"

    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("HistData"):
                continue
            if "Status Report" in line or line.startswith("Gap of"):
                continue
            parts = line.split(_line_separator(line))
            if len(parts) == 4 and " " in parts[0]:
                return "tick"
            if len(parts) >= 6:
                return "minute"
            break

    raise ValueError(f"Could not detect HistData format for {path}")


def _parse_histdata_tick_file(path: Path) -> pd.DataFrame:
    """Parse HistData tick ASCII: 'YYYYMMDD HHMMSSmmm,bid,ask,volume'."""
    rows: list[dict] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("HistData") or line.startswith("Gap of"):
                continue
            if "Average tick interval" in line or "Maximum tick interval" in line:
                continue

            parts = line.split(",")
            if len(parts) != 4:
                continue

            ts = _parse_tick_timestamp(parts[0])
            bid = float(parts[1])
            ask = float(parts[2])
            volume = float(parts[3]) if parts[3] else 0.0
            mid = (bid + ask) / 2.0
            rows.append({"ts": ts, "price": mid, "volume": volume})

    if not rows:
        raise ValueError(f"No tick rows parsed from {path}")

    df = pd.DataFrame(rows)
    return df.sort_values("ts").reset_index(drop=True)


def _parse_histdata_minute_file(path: Path) -> pd.DataFrame:
    """Parse HistData minute ASCII.

    Supports both layouts:
    - date,time,open,high,low,close[,volume]  (comma-separated)
    - YYYYMMDD HHMMSS;open;high;low;close;volume  (semicolon-separated)
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    sep = ";" if text.count(";") > text.count(",") else ","
    df = pd.read_csv(path, sep=sep, header=None, engine="python")

    if df.shape[1] < 6:
        raise ValueError(f"Unexpected minute column count in {path}: {df.shape[1]}")

    first = str(df.iloc[0, 0]).strip()
    if " " in first:
        df = df.iloc[:, :6]
        cols = ["datetime", "open", "high", "low", "close"]
        if df.shape[1] == 6:
            cols.append("volume")
        df.columns = cols
        df["ts"] = df["datetime"].astype(str).map(_parse_combined_minute_timestamp)
    else:
        df = df.iloc[:, :7] if df.shape[1] >= 7 else df.iloc[:, :6]
        cols = ["date", "time", "open", "high", "low", "close"]
        if df.shape[1] == 7:
            cols.append("volume")
        df.columns = cols

        df["date"] = df["date"].astype(str).str.strip()
        df["time"] = df["time"].astype(str).str.strip().str.zfill(6)
        df["ts"] = [_parse_minute_timestamp(d, t) for d, t in zip(df["date"], df["time"])]

    if "volume" not in df.columns:
        df["volume"] = 0.0

    out = df[["ts", "open", "high", "low", "close", "volume"]].copy()
    for col in ["open", "high", "low", "close", "volume"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.dropna(subset=["open", "high", "low", "close"])


def _parse_histdata_file(path: Path, source_format: str = "auto") -> pd.DataFrame:
    fmt = _detect_format(path) if source_format == "auto" else source_format
    if fmt == "tick":
        return _parse_histdata_tick_file(path)
    if fmt == "minute":
        return _parse_histdata_minute_file(path)
    raise ValueError(f"Unknown source format: {fmt}")


def _collect_data_files(input_dir: Path) -> list[Path]:
    """Collect HistData candle/tick CSV files; skip status-report TXT files."""
    files = sorted(input_dir.rglob("*.csv"))
    return [f for f in files if f.is_file()]


def _ticks_to_ohlc(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    minutes = TIMEFRAME_MINUTES.get(timeframe)
    if minutes is None:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    indexed = df.set_index("ts").sort_index()
    rule = f"{minutes}min"
    ohlc = indexed["price"].resample(rule, label="right", closed="right").ohlc()
    vol = indexed["volume"].resample(rule, label="right", closed="right").sum()
    out = ohlc.join(vol).dropna(subset=["open"]).reset_index()
    out.columns = ["ts", "open", "high", "low", "close", "volume"]
    return out


def _resample_minute_bars(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    minutes = TIMEFRAME_MINUTES.get(timeframe)
    if minutes is None:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    df = df.set_index("ts").sort_index()
    rule = f"{minutes}min"
    resampled = df.resample(rule, label="right", closed="right").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    return resampled.dropna(subset=["open"]).reset_index()


def _month_partition_path(output_dir: Path, symbol: str, timeframe: str, year: int, month: int) -> Path:
    """Hive path for one calendar month of bars."""
    return month_partition_path(output_dir, symbol, timeframe, year, month)


def _write_month_partition(out_path: Path, group: pd.DataFrame) -> int:
    """Write one month partition, merging with any existing file on disk."""
    return write_month_partition(out_path, group, CANDLE_COLUMNS)


def ingest(
    input_dir: Path,
    symbol: str,
    timeframe: str,
    output_dir: Path,
    dry_run: bool = False,
    source_format: str = "auto",
) -> None:
    files = _collect_data_files(input_dir)
    if not files:
        raise SystemExit(f"No CSV files found in {input_dir}")

    fmt = source_format if source_format != "auto" else _detect_format(files[0])
    print(f"Detected HistData format: {fmt} ({files[0].name})")

    if fmt == "tick":
        frames = [_parse_histdata_tick_file(f) for f in files]
        df = pd.concat(frames, ignore_index=True).sort_values("ts").drop_duplicates(subset=["ts"], keep="last")
        df = _ticks_to_ohlc(df, timeframe)
    else:
        frames = [_parse_histdata_minute_file(f) for f in files]
        df = pd.concat(frames, ignore_index=True).sort_values("ts").drop_duplicates(subset=["ts"], keep="last")
        if timeframe == "M1":
            df["ts"] = df["ts"] + pd.Timedelta(minutes=1)
        elif timeframe != "M1":
            df = _resample_minute_bars(df, timeframe)

    df["symbol"] = symbol.upper()
    df["timeframe"] = timeframe
    df["year"] = df["ts"].dt.year
    df["month"] = df["ts"].dt.month

    if len(df) > 1:
        expected = pd.Timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
        gaps = df["ts"].diff().gt(expected * 1.5).sum()
        if gaps:
            print(f"Warning: {gaps} potential gaps detected in {symbol} {timeframe}")

    print(f"Parsed {len(df)} bars for {symbol} {timeframe} ({df['ts'].min()} → {df['ts'].max()})")

    if dry_run:
        print(df.head())
        return

    for (year, month), group in df.groupby(["year", "month"]):
        out_path = _month_partition_path(output_dir, symbol, timeframe, int(year), int(month))
        written = _write_month_partition(out_path, group)
        print(f"Wrote {out_path} ({written} rows)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest HistData CSV into Parquet candles")
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--symbol", type=str, required=True)
    parser.add_argument("--timeframe", type=str, default="H1", choices=list(TIMEFRAME_MINUTES))
    parser.add_argument("--output-dir", type=Path, default=_REPO_ROOT / "data" / "candles")
    parser.add_argument("--source-format", choices=["auto", "minute", "tick"], default="auto")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    ingest(
        input_dir=args.input_dir,
        symbol=args.symbol.upper(),
        timeframe=args.timeframe.upper(),
        output_dir=args.output_dir,
        dry_run=args.dry_run,
        source_format=args.source_format,
    )


if __name__ == "__main__":
    main()
