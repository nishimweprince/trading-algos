"""Validated, conflict-aware OANDA candle synchronization."""

from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from app.providers.base import CandleProvider
from app.providers.oanda import TIMEFRAME_DELTA
from app.utils.parquet import month_partition_path

CANDLE_COLUMNS = ["ts", "open", "high", "low", "close", "volume"]
PRICE_COLUMNS = ["open", "high", "low", "close"]


class SyncLockedError(RuntimeError):
    pass


class CandleConflictError(RuntimeError):
    def __init__(self, conflicts: pd.DataFrame, quarantine_path: Path) -> None:
        self.conflicts = conflicts
        self.quarantine_path = quarantine_path
        super().__init__(
            f"{len(conflicts)} overlapping candle conflict(s); quarantined at {quarantine_path}"
        )


@dataclass(frozen=True)
class SyncResult:
    symbol: str
    source_instrument: str
    timeframe: str
    fetched: int
    published: int
    identical_overlaps: int
    gaps: int
    stale: bool
    latest_complete_candle: datetime | None
    dry_run: bool


def storage_to_oanda(symbol: str) -> str:
    value = symbol.upper()
    if value == "XAUUSD":
        return "XAU_USD"
    raise ValueError(f"No OANDA instrument mapping configured for {symbol!r}")


def oanda_to_storage(instrument: str) -> str:
    value = instrument.upper()
    if value == "XAU_USD":
        return "XAUUSD"
    raise ValueError(f"No storage symbol mapping configured for {instrument!r}")


def symbol_from_partition(path: Path) -> str:
    for part in path.parts:
        if part.startswith("symbol="):
            return part.split("=", 1)[1]
    return "unknown"


@contextmanager
def single_writer_lock(path: Path) -> Iterator[None]:
    """Hold a non-blocking process lock for the complete preflight/publish cycle."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyncLockedError(f"Candle sync is already running ({path})") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_parquet(frame: pd.DataFrame, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        frame.to_parquet(temporary, index=False)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_frame(frame: pd.DataFrame) -> None:
    if frame.empty:
        return
    if frame["ts"].duplicated().any():
        duplicates = frame.loc[frame["ts"].duplicated(keep=False), "ts"].tolist()
        raise ValueError(f"Duplicate OANDA candles: {duplicates[:5]}")
    prices = frame[PRICE_COLUMNS].astype(float)
    if not np.isfinite(prices.to_numpy()).all() or (prices <= 0).any().any():
        raise ValueError("Candles contain non-finite or non-positive OHLC values")
    valid = (
        (frame["high"] >= frame["low"])
        & (frame["open"].between(frame["low"], frame["high"]))
        & (frame["close"].between(frame["low"], frame["high"]))
        & (frame["volume"] >= 0)
    )
    if not valid.all():
        raise ValueError("Candles contain invalid OHLC ordering or volume")


def _same_prices(existing: pd.Series, incoming: pd.Series) -> bool:
    return all(
        np.isclose(float(existing[column]), float(incoming[column]), rtol=1e-9, atol=1e-9)
        for column in PRICE_COLUMNS
    )


def _quarantine(
    root: Path,
    symbol: str,
    timeframe: str,
    conflicts: pd.DataFrame,
) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = root / "quarantine" / "oanda-conflicts" / f"{symbol}-{timeframe}-{stamp}.parquet"
    _atomic_parquet(conflicts, path)
    return path


class OandaCandleSync:
    def __init__(
        self,
        provider: CandleProvider,
        candle_root: Path,
        *,
        refresh_views: Callable[[], None] | None = None,
        overlap_bars: int = 3,
    ) -> None:
        if overlap_bars < 1:
            raise ValueError("overlap_bars must be at least one")
        self.provider = provider
        self.candle_root = candle_root
        self.data_root = candle_root.parent
        self.provenance_root = self.data_root / "candle_sources"
        self.refresh_views = refresh_views
        self.overlap_bars = overlap_bars

    def _latest(self, symbol: str, timeframe: str) -> datetime | None:
        root = self.candle_root / f"symbol={symbol}" / f"timeframe={timeframe}"
        latest: pd.Timestamp | None = None
        for path in root.glob("year=*/month=*/part-*.parquet"):
            frame = pd.read_parquet(path, columns=["ts"])
            if frame.empty:
                continue
            candidate = pd.to_datetime(frame["ts"], utc=True).max()
            latest = candidate if latest is None or candidate > latest else latest
        return latest.to_pydatetime() if latest is not None else None

    def _fetch(
        self, instrument: str, timeframe: str, start: datetime, end: datetime
    ) -> pd.DataFrame:
        rows: list[dict] = []
        cursor: datetime | None = start
        while cursor is not None and cursor < end:
            page = self.provider.fetch_candles_page(instrument, timeframe, cursor, end)
            rows.extend(asdict(candle) for candle in page.candles)
            if page.next_start is not None and page.next_start <= cursor:
                raise RuntimeError("Provider pagination cursor did not advance")
            cursor = page.next_start
        if not rows:
            return pd.DataFrame(columns=[*CANDLE_COLUMNS, "provider", "source_instrument"])
        frame = pd.DataFrame(rows)
        frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
        frame = frame.sort_values("ts").reset_index(drop=True)
        _validate_frame(frame)
        return frame

    def _preflight_partition(
        self,
        path: Path,
        provenance_path: Path,
        incoming: pd.DataFrame,
        retrieved_at: datetime,
    ) -> tuple[pd.DataFrame, pd.DataFrame, int, int, pd.DataFrame]:
        existing = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=CANDLE_COLUMNS)
        if not existing.empty:
            existing["ts"] = pd.to_datetime(existing["ts"], utc=True)
        provenance = (
            pd.read_parquet(provenance_path)
            if provenance_path.exists()
            else pd.DataFrame(columns=["ts", "provider", "source_instrument", "retrieved_at"])
        )
        if not provenance.empty:
            provenance["ts"] = pd.to_datetime(provenance["ts"], utc=True)
        by_ts = existing.set_index("ts", drop=False)
        conflicts: list[dict] = []
        additions: list[dict] = []
        identical = 0
        for _, row in incoming.iterrows():
            ts = row["ts"]
            if ts in by_ts.index:
                prior = by_ts.loc[ts]
                if isinstance(prior, pd.DataFrame):
                    raise ValueError(f"Existing partition has duplicate timestamp {ts}")
                if _same_prices(prior, row):
                    identical += 1
                    continue
                conflicts.append(
                    {
                        "ts": ts,
                        **{f"existing_{key}": prior[key] for key in CANDLE_COLUMNS[1:]},
                        **{f"incoming_{key}": row[key] for key in CANDLE_COLUMNS[1:]},
                        "existing_provider": ",".join(
                            sorted(
                                provenance.loc[provenance["ts"] == ts, "provider"]
                                .astype(str)
                                .unique()
                            )
                        )
                        or "histdata_or_unknown",
                        "incoming_provider": row["provider"],
                        "source_instrument": row["source_instrument"],
                    }
                )
            else:
                additions.append(row[CANDLE_COLUMNS].to_dict())

        if conflicts:
            return existing, pd.DataFrame(), identical, 0, pd.DataFrame(conflicts)

        additions_frame = pd.DataFrame(additions, columns=CANDLE_COLUMNS)
        if existing.empty:
            merged = additions_frame
        elif additions_frame.empty:
            merged = existing.copy()
        else:
            merged = pd.concat([existing, additions_frame], ignore_index=True)
        merged = merged[CANDLE_COLUMNS].sort_values("ts").reset_index(drop=True)

        if not existing.empty and provenance.empty:
            provenance = pd.DataFrame(
                {
                    "ts": existing["ts"],
                    "provider": "histdata_or_unknown",
                    "source_instrument": symbol_from_partition(path),
                    "retrieved_at": pd.NaT,
                }
            )
        source_rows = incoming[["ts", "provider", "source_instrument"]].copy()
        source_rows["retrieved_at"] = retrieved_at
        provenance = (
            source_rows
            if provenance.empty
            else pd.concat([provenance, source_rows], ignore_index=True)
        )
        provenance["ts"] = pd.to_datetime(provenance["ts"], utc=True)
        provenance = (
            provenance.drop_duplicates(
                subset=["ts", "provider", "source_instrument"], keep="last"
            )
            .sort_values(["ts", "provider"])
            .reset_index(drop=True)
        )
        return merged, provenance, identical, len(additions), pd.DataFrame()

    def sync(
        self,
        *,
        symbol: str,
        timeframe: str,
        start: datetime | None,
        end: datetime,
        dry_run: bool = False,
    ) -> SyncResult:
        symbol, timeframe = symbol.upper(), timeframe.upper()
        instrument = storage_to_oanda(symbol)
        if timeframe not in TIMEFRAME_DELTA:
            raise ValueError("OANDA sync supports H1 and H4 only")
        if end.tzinfo is None or (start is not None and start.tzinfo is None):
            raise ValueError("Sync boundaries must be timezone-aware")
        end = end.astimezone(UTC)
        self.provider.validate_instrument(instrument)

        latest_before = self._latest(symbol, timeframe)
        delta = TIMEFRAME_DELTA[timeframe]
        if latest_before is not None:
            overlap_start = latest_before - delta * self.overlap_bars
            start = (
                max(start.astimezone(UTC), overlap_start) if start else overlap_start
            )
        elif start is None:
            raise ValueError("start is required for the initial backfill")
        else:
            start = start.astimezone(UTC)

        frame = self._fetch(instrument, timeframe, start, end)
        gaps = int(frame["ts"].diff().gt(delta * 1.5).sum()) if len(frame) > 1 else 0
        latest = frame["ts"].max().to_pydatetime() if not frame.empty else latest_before
        stale = latest is None or (end - latest) > delta * 2
        if dry_run or frame.empty:
            return SyncResult(
                symbol,
                instrument,
                timeframe,
                len(frame),
                0,
                0,
                gaps,
                stale,
                latest,
                dry_run,
            )

        lock_path = self.candle_root / ".oanda-sync.lock"
        with single_writer_lock(lock_path):
            prepared: list[tuple[Path, pd.DataFrame, Path, pd.DataFrame]] = []
            all_conflicts: list[pd.DataFrame] = []
            identical = published = 0
            retrieved_at = datetime.now(UTC)
            grouped = frame.assign(
                year=frame["ts"].dt.year, month=frame["ts"].dt.month
            ).groupby(["year", "month"])
            for (year, month), group in grouped:
                path = month_partition_path(
                    self.candle_root, symbol, timeframe, int(year), int(month)
                )
                provenance_path = month_partition_path(
                    self.provenance_root, symbol, timeframe, int(year), int(month)
                )
                merged, provenance, same_count, added_count, conflicts = (
                    self._preflight_partition(
                        path, provenance_path, group, retrieved_at
                    )
                )
                identical += same_count
                if not conflicts.empty:
                    all_conflicts.append(conflicts)
                else:
                    published += added_count
                    prepared.append((path, merged, provenance_path, provenance))

            if all_conflicts:
                conflicts = pd.concat(all_conflicts, ignore_index=True)
                quarantine = _quarantine(self.data_root, symbol, timeframe, conflicts)
                raise CandleConflictError(conflicts, quarantine)

            for path, merged, provenance_path, provenance in prepared:
                _atomic_parquet(merged, path)
                _atomic_parquet(provenance, provenance_path)

        if self.refresh_views and published:
            self.refresh_views()
        return SyncResult(
            symbol,
            instrument,
            timeframe,
            len(frame),
            published,
            identical,
            gaps,
            stale,
            latest,
            False,
        )
