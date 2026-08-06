"""Append-only HistData-to-Capital.com closed-candle synchronization."""

from __future__ import annotations

import json
import math
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd

from app.providers.capital import CapitalMarketDataClient
from app.services.candle_quality import CANDLE_COLUMNS, unexpected_gaps
from app.services.h4_resample import rebuild_h4
from app.services.pipeline_lock import pipeline_lock
from app.utils.parquet import month_partition_path, write_month_partition

PRICE_COLUMNS = ["open", "high", "low", "close"]


class CapitalCandleConflict(RuntimeError):
    def __init__(self, quarantine_path: Path) -> None:
        self.quarantine_path = quarantine_path
        super().__init__(f"Changed Capital.com candle quarantined at {quarantine_path}")


@dataclass(frozen=True)
class CapitalSyncResult:
    symbol: str
    epic: str
    fetched: int
    published: int
    identical_overlaps: int
    histdata_overlaps: int
    histdata_mismatches: int
    unexpected_gaps: int
    latest_complete_candle: datetime | None
    histdata_cutoff: datetime
    capital_server_time: datetime
    dry_run: bool
    generation: str | None


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _same_prices(existing: pd.Series, incoming: pd.Series) -> bool:
    return all(
        math.isclose(float(existing[name]), float(incoming[name]), rel_tol=1e-9, abs_tol=1e-9)
        for name in PRICE_COLUMNS
    )


def _load_partitions(root: Path, symbol: str, timeframe: str) -> pd.DataFrame:
    base = root / f"symbol={symbol}" / f"timeframe={timeframe}"
    files = sorted(base.glob("year=*/month=*/part-*.parquet"))
    if not files:
        return pd.DataFrame(columns=CANDLE_COLUMNS)
    frame = pd.concat([pd.read_parquet(path) for path in files], ignore_index=True)
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    return frame[CANDLE_COLUMNS].sort_values("ts").drop_duplicates("ts", keep="last")


def _has_weekend(left: pd.Timestamp, right: pd.Timestamp) -> bool:
    return any(
        value.weekday() == 5
        for value in pd.date_range(left.normalize(), right.normalize(), freq="D")
    )


def _validate_boundary(left: pd.Timestamp, right: pd.Timestamp) -> None:
    delta = right - left
    if delta <= pd.Timedelta(hours=2) or _has_weekend(left, right):
        return
    raise ValueError(
        "Unexplained HistData-to-Capital gap: "
        f"{left.isoformat()} -> {right.isoformat()} ({delta})"
    )


class CapitalCandleSync:
    def __init__(
        self,
        client: CapitalMarketDataClient,
        *,
        data_dir: Path,
        overlap_bars: int = 3,
        after_publish: Callable[[datetime], None] | None = None,
    ) -> None:
        if overlap_bars < 1:
            raise ValueError("overlap_bars must be positive")
        self.client = client
        self.data_dir = data_dir
        self.candle_root = data_dir / "candles"
        self.provenance_root = data_dir / "candle_sources"
        self.overlap_bars = overlap_bars
        self.after_publish = after_publish

    @property
    def boundary_path(self) -> Path:
        return self.provenance_root / "capital_boundary.json"

    @property
    def feature_refresh_path(self) -> Path:
        return self.provenance_root / "capital_feature_refresh_pending.json"

    def _run_pending_refresh(self, symbol: str) -> None:
        if not self.feature_refresh_path.exists():
            return
        payload = json.loads(self.feature_refresh_path.read_text(encoding="utf-8"))
        first_changed = datetime.fromisoformat(payload["first_changed"])
        rebuild_h4(self.candle_root, symbol)
        if self.after_publish:
            self.after_publish(first_changed)
        self.feature_refresh_path.unlink()

    def _boundary(self, symbol: str, epic: str, existing: pd.DataFrame) -> pd.Timestamp:
        if self.boundary_path.exists():
            payload = json.loads(self.boundary_path.read_text(encoding="utf-8"))
            if payload.get("symbol") != symbol or payload.get("epic") != epic:
                raise ValueError("Configured Capital epic does not match the frozen source boundary")
            return pd.Timestamp(payload["histdata_cutoff"])
        if existing.empty:
            raise ValueError("HistData H1 history is required before Capital sync")
        return pd.Timestamp(existing["ts"].max())

    def sync(
        self,
        *,
        symbol: str,
        epic: str,
        end: datetime | None = None,
        dry_run: bool = False,
    ) -> CapitalSyncResult:
        symbol = symbol.upper()
        if symbol != "XAUUSD":
            raise ValueError("Capital v1 supports XAUUSD only")
        self.client.validate_market(epic)
        existing = _load_partitions(self.candle_root, symbol, "H1")
        boundary = self._boundary(symbol, epic, existing)
        latest = pd.Timestamp(existing["ts"].max())
        server_time = self.client.server_time()
        requested_end = (end or server_time).astimezone(UTC)
        start = (latest - pd.Timedelta(hours=self.overlap_bars)).to_pydatetime()
        candles = []
        cursor = start
        while cursor < requested_end:
            page_end = min(cursor + timedelta(hours=1000), requested_end)
            candles.extend(
                self.client.fetch_closed_hourly(
                    epic,
                    cursor,
                    page_end,
                    server_time=server_time,
                    max_bars=1000,
                )
            )
            cursor = page_end
        incoming = pd.DataFrame([asdict(candle) for candle in candles])
        if incoming.empty:
            if not dry_run and self.feature_refresh_path.exists():
                with pipeline_lock(self.data_dir / ".market-data.lock"):
                    self._run_pending_refresh(symbol)
            return CapitalSyncResult(
                symbol, epic, 0, 0, 0, 0, 0, 0,
                latest.to_pydatetime(), boundary.to_pydatetime(), server_time, dry_run, None,
            )
        incoming["ts"] = pd.to_datetime(incoming["ts"], utc=True)
        if incoming["ts"].duplicated().any():
            raise ValueError("Capital.com pagination returned duplicate candle timestamps")
        gaps = unexpected_gaps(incoming)
        by_ts = existing.set_index("ts", drop=False)
        additions: list[dict] = []
        conflicts: list[dict] = []
        histdata_audit: list[dict] = []
        identical = histdata_overlaps = histdata_mismatches = 0
        for _, row in incoming.iterrows():
            ts = row["ts"]
            if ts <= boundary:
                if ts in by_ts.index:
                    histdata_overlaps += 1
                    same = _same_prices(by_ts.loc[ts], row)
                    histdata_mismatches += int(not same)
                    histdata_audit.append(
                        {"ts": ts.isoformat(), "same_ohlc": same, "epic": epic}
                    )
                continue
            if ts in by_ts.index:
                if _same_prices(by_ts.loc[ts], row):
                    identical += 1
                else:
                    conflicts.append(
                        {
                            "ts": ts,
                            **{f"existing_{name}": by_ts.loc[ts][name] for name in PRICE_COLUMNS},
                            **{f"incoming_{name}": row[name] for name in PRICE_COLUMNS},
                            "epic": epic,
                        }
                    )
                continue
            additions.append(row.to_dict())

        if additions:
            _validate_boundary(latest, pd.Timestamp(additions[0]["ts"]))
        if dry_run:
            return CapitalSyncResult(
                symbol, epic, len(incoming), len(additions), identical,
                histdata_overlaps, histdata_mismatches, len(gaps),
                pd.Timestamp(incoming["ts"].max()).to_pydatetime(),
                boundary.to_pydatetime(), server_time, True, None,
            )

        generation = uuid.uuid4().hex
        with pipeline_lock(self.data_dir / ".market-data.lock"):
            if conflicts:
                quarantine = self.data_dir / "quarantine" / "capital-conflicts" / f"{generation}.parquet"
                quarantine.parent.mkdir(parents=True, exist_ok=True)
                pd.DataFrame(conflicts).to_parquet(quarantine, index=False)
                raise CapitalCandleConflict(quarantine)

            if not self.boundary_path.exists():
                _atomic_json(
                    self.boundary_path,
                    {
                        "version": 1,
                        "symbol": symbol,
                        "epic": epic,
                        "histdata_cutoff": boundary.isoformat(),
                        "created_at": datetime.now(UTC).isoformat(),
                    },
                )

            additions_frame = pd.DataFrame(additions)
            if not additions_frame.empty:
                additions_frame["year"] = additions_frame["ts"].dt.year
                additions_frame["month"] = additions_frame["ts"].dt.month
                for (year, month), group in additions_frame.groupby(["year", "month"]):
                    candle_path = month_partition_path(
                        self.candle_root, symbol, "H1", int(year), int(month)
                    )
                    write_month_partition(candle_path, group, CANDLE_COLUMNS)
                    provenance = group[
                        ["ts", "provider", "source_instrument", "spread"]
                    ].copy()
                    provenance["epic"] = epic
                    provenance["price_side"] = "bid"
                    provenance["environment"] = self.client.environment
                    provenance["retrieved_at"] = datetime.now(UTC)
                    provenance["generation"] = generation
                    provenance["sync_generation"] = generation
                    provenance["source_boundary"] = boundary.isoformat()
                    provenance_path = month_partition_path(
                        self.provenance_root, symbol, "H1", int(year), int(month)
                    )
                    write_month_partition(provenance_path, provenance)

                _atomic_json(
                    self.feature_refresh_path,
                    {
                        "generation": generation,
                        "first_changed": pd.Timestamp(
                            additions_frame["ts"].min()
                        ).isoformat(),
                    },
                )

            self._run_pending_refresh(symbol)

            audit_path = self.data_dir / "reports" / "capital-histdata-overlap.json"
            _atomic_json(
                audit_path,
                {
                    "generation": generation,
                    "symbol": symbol,
                    "epic": epic,
                    "checked_at": datetime.now(UTC).isoformat(),
                    "overlaps": histdata_audit,
                },
            )
            _atomic_json(
                self.provenance_root / "capital_publish.json",
                {
                    "generation": generation,
                    "published_at": datetime.now(UTC).isoformat(),
                    "latest_complete_candle": pd.Timestamp(incoming["ts"].max()).isoformat(),
                    "capital_server_time": server_time.isoformat(),
                    "request_status": "ok",
                    "unexpected_gaps": len(gaps),
                },
            )

        return CapitalSyncResult(
            symbol, epic, len(incoming), len(additions), identical,
            histdata_overlaps, histdata_mismatches, len(gaps),
            pd.Timestamp(incoming["ts"].max()).to_pydatetime(),
            boundary.to_pydatetime(), server_time, False, generation,
        )
