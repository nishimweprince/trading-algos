"""Append-only HistData-to-Capital.com closed-candle synchronization."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from app.providers.capital import CapitalMarketDataClient
from app.providers.instruments import capital_epic_for
from app.services.candle_quality import CANDLE_COLUMNS, unexpected_gaps, validate_candles
from app.services.h4_resample import rebuild_h4
from app.services.pipeline_lock import pipeline_lock
from app.utils.parquet import month_partition_path, write_month_partition

PRICE_COLUMNS = ["open", "high", "low", "close"]


class CapitalCandleConflict(RuntimeError):
    def __init__(self, quarantine_path: Path) -> None:
        self.quarantine_path = quarantine_path
        super().__init__(
            "Changed Capital.com candle quarantined at "
            f"{quarantine_path}. Review it with scripts/sync_capital.py "
            "--review-conflicts before explicitly accepting it."
        )


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
    spread_fallbacks: int
    spread_unavailable: int
    latest_complete_candle: datetime | None
    histdata_cutoff: datetime
    capital_server_time: datetime
    dry_run: bool
    generation: str | None


@dataclass(frozen=True)
class CapitalConflictResolution:
    symbol: str
    epic: str
    quarantine_path: str
    corrected: int
    already_applied: int
    archived_files: int
    first_changed: datetime
    generation: str


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


def _same_prefixed_prices(existing: pd.Series, candidate: pd.Series, prefix: str) -> bool:
    return all(
        math.isclose(
            float(existing[name]),
            float(candidate[f"{prefix}_{name}"]),
            rel_tol=1e-9,
            abs_tol=1e-9,
        )
        for name in PRICE_COLUMNS
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _conflict_payload(frame: pd.DataFrame) -> list[dict]:
    normalized: list[dict] = []
    for _, row in frame.sort_values("ts").iterrows():
        item = {"ts": pd.Timestamp(row["ts"]).isoformat(), "epic": str(row["epic"])}
        for prefix in ("existing", "incoming"):
            for name in PRICE_COLUMNS:
                item[f"{prefix}_{name}"] = float(row[f"{prefix}_{name}"])
        normalized.append(item)
    return normalized


def _conflict_digest(frame: pd.DataFrame) -> str:
    raw = json.dumps(_conflict_payload(frame), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def review_capital_conflicts(data_dir: Path) -> dict:
    """Return a semantic summary without changing quarantined files."""
    root = data_dir / "quarantine" / "capital-conflicts"
    files = sorted(root.glob("*.parquet"))
    groups: dict[str, dict] = {}
    invalid: list[dict[str, str]] = []
    for path in files:
        try:
            frame = pd.read_parquet(path)
            digest = _conflict_digest(frame)
        except (OSError, ValueError, KeyError) as exc:
            invalid.append({"path": str(path), "error": type(exc).__name__})
            continue
        group = groups.setdefault(
            digest,
            {
                "digest": digest,
                "copies": 0,
                "example_path": str(path),
                "rows": _conflict_payload(frame),
            },
        )
        group["copies"] += 1
    return {
        "pending_files": len(files),
        "unique_conflicts": len(groups),
        "conflicts": list(groups.values()),
        "invalid_files": invalid,
    }


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
    if delta <= pd.Timedelta(2, unit="h") or _has_weekend(left, right):
        return
    raise ValueError(
        f"Unexplained HistData-to-Capital gap: {left.isoformat()} -> {right.isoformat()} ({delta})"
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
                raise ValueError(
                    "Configured Capital epic does not match the frozen source boundary"
                )
            return pd.Timestamp(payload["histdata_cutoff"])
        if existing.empty:
            raise ValueError("HistData H1 history is required before Capital sync")
        return pd.Timestamp(existing["ts"].max())

    def accept_conflict(
        self,
        quarantine_path: Path,
        *,
        symbol: str,
        epic: str | None = None,
    ) -> CapitalConflictResolution:
        """Apply an explicitly reviewed provider correction and refresh derivatives."""
        symbol = symbol.upper()
        if symbol != "XAUUSD":
            raise ValueError("Capital v1 supports XAUUSD only")
        epic = epic or capital_epic_for(symbol)
        quarantine_root = self.data_dir / "quarantine" / "capital-conflicts"
        try:
            resolved_path = quarantine_path.expanduser().resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValueError(f"Conflict file does not exist: {quarantine_path}") from exc
        if resolved_path.parent != quarantine_root.resolve():
            raise ValueError(f"Conflict file must be directly under {quarantine_root}")

        conflict = pd.read_parquet(resolved_path)
        required = {
            "ts",
            "epic",
            *(f"existing_{name}" for name in PRICE_COLUMNS),
            *(f"incoming_{name}" for name in PRICE_COLUMNS),
        }
        missing = sorted(required - set(conflict.columns))
        if missing:
            raise ValueError(f"Conflict file is missing columns: {missing}")
        if conflict.empty or conflict["ts"].duplicated().any():
            raise ValueError("Conflict file must contain unique candle timestamps")
        if set(conflict["epic"].astype(str)) != {epic}:
            raise ValueError("Conflict epic does not match the configured Capital epic")
        conflict["ts"] = pd.to_datetime(conflict["ts"], utc=True)

        generation = uuid.uuid4().hex
        corrected = already_applied = 0
        first_changed = pd.Timestamp(conflict["ts"].min())
        target_sha256 = _file_sha256(resolved_path)
        with pipeline_lock(self.data_dir / ".market-data.lock"):
            existing = _load_partitions(self.candle_root, symbol, "H1")
            boundary = self._boundary(symbol, epic, existing)
            by_ts = existing.set_index("ts", drop=False)
            updates: list[dict] = []
            for _, row in conflict.sort_values("ts").iterrows():
                ts = row["ts"]
                if ts <= boundary:
                    raise ValueError("HistData boundary candles cannot be corrected by Capital")
                if ts not in by_ts.index:
                    raise ValueError(f"Stored candle no longer exists at {ts.isoformat()}")
                stored = by_ts.loc[ts]
                if _same_prefixed_prices(stored, row, "incoming"):
                    already_applied += 1
                    continue
                if not _same_prefixed_prices(stored, row, "existing"):
                    raise ValueError(
                        f"Stored candle changed after quarantine at {ts.isoformat()}; "
                        "review the newest conflict instead"
                    )
                updated = stored[CANDLE_COLUMNS].to_dict()
                updated.update({name: float(row[f"incoming_{name}"]) for name in PRICE_COLUMNS})
                if "incoming_volume" in row and pd.notna(row["incoming_volume"]):
                    updated["volume"] = float(row["incoming_volume"])
                updates.append(updated)

            updates_frame = pd.DataFrame(updates, columns=CANDLE_COLUMNS)
            if not updates_frame.empty:
                validate_candles(updates_frame)
                _atomic_json(
                    self.feature_refresh_path,
                    {
                        "generation": generation,
                        "first_changed": first_changed.isoformat(),
                        "reason": "accepted_capital_correction",
                    },
                )
                updates_frame["year"] = pd.to_datetime(updates_frame["ts"], utc=True).dt.year
                updates_frame["month"] = pd.to_datetime(updates_frame["ts"], utc=True).dt.month
                for (year, month), group in updates_frame.groupby(["year", "month"]):
                    candle_path = month_partition_path(
                        self.candle_root, symbol, "H1", int(year), int(month)
                    )
                    write_month_partition(candle_path, group, CANDLE_COLUMNS)
                    provenance_path = month_partition_path(
                        self.provenance_root, symbol, "H1", int(year), int(month)
                    )
                    if provenance_path.exists():
                        provenance = pd.read_parquet(provenance_path)
                        timestamps = set(pd.to_datetime(group["ts"], utc=True))
                        provenance["ts"] = pd.to_datetime(provenance["ts"], utc=True)
                        provenance = provenance[provenance["ts"].isin(timestamps)].copy()
                    else:
                        provenance = pd.DataFrame({"ts": pd.to_datetime(group["ts"], utc=True)})
                        provenance["provider"] = "capital"
                        provenance["source_instrument"] = epic
                        provenance["epic"] = epic
                        provenance["price_side"] = "bid"
                        provenance["environment"] = self.client.environment
                    provenance["correction_generation"] = generation
                    provenance["correction_accepted_at"] = datetime.now(UTC)
                    provenance["correction_source"] = resolved_path.name
                    write_month_partition(provenance_path, provenance)
                corrected = len(updates_frame)

            if self.feature_refresh_path.exists():
                self._run_pending_refresh(symbol)

            archived_root = quarantine_root / "resolved" / generation
            archived = 0
            for candidate in sorted(quarantine_root.glob("*.parquet")):
                if _file_sha256(candidate) != target_sha256:
                    continue
                archived_root.mkdir(parents=True, exist_ok=True)
                os.replace(candidate, archived_root / candidate.name)
                archived += 1

            _atomic_json(
                self.data_dir / "reports" / "capital-corrections" / f"{generation}.json",
                {
                    "version": 1,
                    "generation": generation,
                    "symbol": symbol,
                    "epic": epic,
                    "accepted_at": datetime.now(UTC).isoformat(),
                    "quarantine_path": str(resolved_path),
                    "quarantine_sha256": target_sha256,
                    "corrected": corrected,
                    "already_applied": already_applied,
                    "archived_files": archived,
                    "first_changed": first_changed.isoformat(),
                },
            )

        return CapitalConflictResolution(
            symbol=symbol,
            epic=epic,
            quarantine_path=str(resolved_path),
            corrected=corrected,
            already_applied=already_applied,
            archived_files=archived,
            first_changed=first_changed.to_pydatetime(),
            generation=generation,
        )

    def sync(
        self,
        *,
        symbol: str,
        epic: str | None = None,
        end: datetime | None = None,
        dry_run: bool = False,
    ) -> CapitalSyncResult:
        symbol = symbol.upper()
        if symbol != "XAUUSD":
            raise ValueError("Capital v1 supports XAUUSD only")
        epic = epic or capital_epic_for(symbol)
        self.client.validate_market(epic)
        existing = _load_partitions(self.candle_root, symbol, "H1")
        boundary = self._boundary(symbol, epic, existing)
        latest = pd.Timestamp(existing["ts"].max())
        server_time = self.client.server_time()
        requested_end = (end or server_time).astimezone(UTC)
        start = (latest - pd.Timedelta(self.overlap_bars, unit="h")).to_pydatetime()
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
                symbol,
                epic,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                latest.to_pydatetime(),
                boundary.to_pydatetime(),
                server_time,
                dry_run,
                None,
            )
        incoming["ts"] = pd.to_datetime(incoming["ts"], utc=True)
        spread_fallbacks = int((incoming["spread_source"] == "intrabar_median_fallback").sum())
        spread_unavailable = int((incoming["spread_source"] == "unavailable").sum())
        if incoming["ts"].duplicated().any():
            raise ValueError("Capital.com pagination returned duplicate candle timestamps")
        gaps = unexpected_gaps(incoming)
        if gaps:
            first_gap = gaps[0]
            raise ValueError(
                "Capital.com response contains an unexplained market-open gap: "
                f"{first_gap['after']} -> {first_gap['before']}"
            )
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
                    histdata_audit.append({"ts": ts.isoformat(), "same_ohlc": same, "epic": epic})
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
                            "incoming_volume": row["volume"],
                            "provider": row["provider"],
                            "source_instrument": row["source_instrument"],
                            "spread": row["spread"],
                            "spread_source": row["spread_source"],
                            "epic": epic,
                        }
                    )
                continue
            additions.append(row.to_dict())

        if additions:
            _validate_boundary(latest, pd.Timestamp(additions[0]["ts"]))
        if dry_run:
            if conflicts:
                raise ValueError(
                    "Capital.com overlap changed OHLC; a committed run would quarantine "
                    "the conflict and abort publication"
                )
            return CapitalSyncResult(
                symbol,
                epic,
                len(incoming),
                len(additions),
                identical,
                histdata_overlaps,
                histdata_mismatches,
                len(gaps),
                spread_fallbacks,
                spread_unavailable,
                pd.Timestamp(incoming["ts"].max()).to_pydatetime(),
                boundary.to_pydatetime(),
                server_time,
                True,
                None,
            )

        generation = uuid.uuid4().hex
        with pipeline_lock(self.data_dir / ".market-data.lock"):
            if conflicts:
                conflict_frame = pd.DataFrame(conflicts)
                digest = _conflict_digest(conflict_frame)
                quarantine = (
                    self.data_dir
                    / "quarantine"
                    / "capital-conflicts"
                    / f"{digest}.parquet"
                )
                quarantine.parent.mkdir(parents=True, exist_ok=True)
                if not quarantine.exists():
                    conflict_frame.to_parquet(quarantine, index=False)
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
                        ["ts", "provider", "source_instrument", "spread", "spread_source"]
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
                        "first_changed": pd.Timestamp(additions_frame["ts"].min()).isoformat(),
                    },
                )

            self._run_pending_refresh(symbol)

            audit_path = self.data_dir / "reports" / "capital-histdata-overlap.json"
            checked_at = datetime.now(UTC).isoformat()
            overlap_audit = {
                "generation": generation,
                "symbol": symbol,
                "epic": epic,
                "checked_at": checked_at,
                "overlaps": histdata_audit,
            }
            publish_audit = {
                "generation": generation,
                "published_at": checked_at,
                "latest_complete_candle": pd.Timestamp(incoming["ts"].max()).isoformat(),
                "capital_server_time": server_time.isoformat(),
                "request_status": "ok",
                "unexpected_gaps": len(gaps),
                "spread_fallbacks": spread_fallbacks,
                "spread_unavailable": spread_unavailable,
            }
            _atomic_json(audit_path, overlap_audit)
            _atomic_json(self.provenance_root / "capital_publish.json", publish_audit)
            _atomic_json(
                self.data_dir / "reports" / "capital-sync" / f"{generation}.json",
                {
                    "version": 1,
                    "generation": generation,
                    "symbol": symbol,
                    "epic": epic,
                    "fetched": len(incoming),
                    "published": len(additions),
                    "identical_overlaps": identical,
                    "histdata_audit": overlap_audit,
                    "publication": publish_audit,
                },
            )

        return CapitalSyncResult(
            symbol,
            epic,
            len(incoming),
            len(additions),
            identical,
            histdata_overlaps,
            histdata_mismatches,
            len(gaps),
            spread_fallbacks,
            spread_unavailable,
            pd.Timestamp(incoming["ts"].max()).to_pydatetime(),
            boundary.to_pydatetime(),
            server_time,
            False,
            generation,
        )
