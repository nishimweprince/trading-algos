"""On-disk layout, Parquet month files and snapshot manifests.

::

    {root}/raw/data/futures/um/...                      verified Vision zips
    {root}/parquet/{dataset}/{symbol}/{tf}/{YYYY-MM}.parquet
    {root}/snapshots/{snapshot_id}.json                 manifest + quarantine
    {root}/snapshots/LATEST                             id of the newest snapshot
    {root}/exchange_info/{YYYY-MM-DD}.json

``tf`` is the kline interval, or ``event`` for ``fundingRate``. Each month file
records in its Parquet metadata whether it came from the monthly archive or
from daily archives (and how many), so a re-sync rebuilds only months that can
still change.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

__all__ = [
    "DataPaths",
    "load",
    "month_meta",
    "read_snapshot",
    "write_month",
    "write_snapshot",
]

_META_KEY = b"zone_trend"
EVENT_TF = "event"


@dataclass(frozen=True)
class DataPaths:
    root: Path

    @classmethod
    def from_env(cls, root: Path | str | None = None) -> DataPaths:
        default = Path(__file__).resolve().parents[3] / "data"
        return cls(Path(root or os.environ.get("ZT_DATA_DIR") or default))

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def parquet(self) -> Path:
        return self.root / "parquet"

    @property
    def snapshots(self) -> Path:
        return self.root / "snapshots"

    @property
    def exchange_info(self) -> Path:
        return self.root / "exchange_info"

    def month_file(self, dataset: str, symbol: str, tf: str, month: str) -> Path:
        return self.parquet / dataset / symbol / tf / f"{month}.parquet"

    def months(self, dataset: str, symbol: str, tf: str) -> list[Path]:
        return sorted((self.parquet / dataset / symbol / tf).glob("*.parquet"))

    def symbols(self, dataset: str, tf: str) -> list[str]:
        base = self.parquet / dataset
        if not base.exists():
            return []
        return sorted(p.name for p in base.iterdir() if (p / tf).is_dir())


_DERIVED = {"open_time": "open_dt", "settle_time": "settle_dt"}


def write_month(path: Path, df: pd.DataFrame, *, source: str, files: int) -> None:
    """Persist one month. Datetime columns are derived on load, not stored.

    Integer columns (timestamps, counts) are delta-encoded: kline timestamps are
    an arithmetic sequence, and this halves a 1m month file against the default.
    """
    df = df.drop(columns=[c for c in _DERIVED.values() if c in df.columns])
    table = pa.Table.from_pandas(df, preserve_index=False)
    ints = [f.name for f in table.schema if pa.types.is_integer(f.type)]
    meta = dict(table.schema.metadata or {})
    meta[_META_KEY] = json.dumps({"source": source, "files": files}).encode()
    table = table.replace_schema_metadata(meta)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    pq.write_table(
        table,
        tmp,
        compression="zstd",
        compression_level=9,
        use_dictionary=False,
        column_encoding=dict.fromkeys(ints, "DELTA_BINARY_PACKED"),
    )
    tmp.replace(path)


def _month(ts_ms: int) -> str:
    return pd.Timestamp(ts_ms, unit="ms", tz="UTC").strftime("%Y-%m")


def month_meta(path: Path) -> dict | None:
    """``{"source", "files"}`` for an existing month file, else None."""
    if not path.exists():
        return None
    meta = pq.read_schema(path).metadata or {}
    raw = meta.get(_META_KEY)
    return json.loads(raw) if raw else {"source": "unknown", "files": 0}


def load(
    paths: DataPaths,
    dataset: str,
    symbol: str,
    tf: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    columns: list[str] | None = None,
) -> pd.DataFrame:
    """All rows for one series in time order, optionally clipped to [start, end)."""
    files = paths.months(dataset, symbol, tf)
    # Month files are named YYYY-MM by their rows' month, so skip the ones outside the range.
    if start_ms is not None:
        first = _month(start_ms)
        files = [f for f in files if f.stem >= first]
    if end_ms is not None:
        last = _month(end_ms - 1)
        files = [f for f in files if f.stem <= last]
    if not files:
        return pd.DataFrame(columns=columns)
    tables = [pq.read_table(f, columns=columns) for f in files]
    df = pd.concat([t.to_pandas() for t in tables], ignore_index=True)
    key = "settle_time" if tf == EVENT_TF else "open_time"
    if start_ms is not None:
        df = df[df[key] >= start_ms]
    if end_ms is not None:
        df = df[df[key] < end_ms]
    df = df.sort_values(key, kind="stable").reset_index(drop=True)
    for src, derived in _DERIVED.items():
        if src in df.columns:
            df[derived] = pd.to_datetime(df[src], unit="ms", utc=True)
    return df


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_snapshot(
    paths: DataPaths,
    files: Iterable[tuple[Path, int]],
    quarantine: dict[str, dict[str, list[str]]],
    report: dict,
) -> str:
    """Write a manifest for ``(parquet file, row count)`` pairs and return its id.

    The id hashes the file list, row counts, file hashes and the quarantine
    list, so any change to data or to what is excluded gives a new id.
    """
    entries = sorted(
        (
            {"path": str(p.relative_to(paths.root)), "rows": rows, "sha256": _sha256(p)}
            for p, rows in files
        ),
        key=lambda e: e["path"],
    )
    body = {"files": entries, "quarantine": quarantine}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    snapshot_id = hashlib.sha256(canonical).hexdigest()[:16]
    paths.snapshots.mkdir(parents=True, exist_ok=True)
    manifest = {"snapshot_id": snapshot_id, **body, "report": report}
    (paths.snapshots / f"{snapshot_id}.json").write_text(json.dumps(manifest, indent=1))
    (paths.snapshots / "LATEST").write_text(snapshot_id + "\n")
    return snapshot_id


def read_snapshot(paths: DataPaths, snapshot_id: str = "latest") -> dict:
    if snapshot_id == "latest":
        snapshot_id = (paths.snapshots / "LATEST").read_text().strip()
    return json.loads((paths.snapshots / f"{snapshot_id}.json").read_text())
