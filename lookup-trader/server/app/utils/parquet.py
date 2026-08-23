"""Hive-partitioned parquet writing shared by the candle ingest and the feature builder.

Both datasets are month-partitioned, append-mostly, and rebuilt in place, so both
need the same merge-on-write behaviour: read whatever is already in the partition,
concatenate, and keep the newest row per timestamp.
"""

from __future__ import annotations

from pathlib import Path
import os
import tempfile

import pandas as pd


def month_partition_path(
    root: Path,
    symbol: str,
    timeframe: str,
    year: int,
    month: int,
) -> Path:
    """Hive path for one calendar month of rows."""
    return (
        root
        / f"symbol={symbol}"
        / f"timeframe={timeframe}"
        / f"year={year}"
        / f"month={month:02d}"
        / "part-000.parquet"
    )


def write_month_partition(
    out_path: Path,
    frame: pd.DataFrame,
    columns: list[str] | None = None,
    key: str = "ts",
) -> int:
    """Write one month partition, merging with any existing file on disk.

    Existing rows are kept unless the incoming frame carries the same key, which
    is what makes both a fresh build and a re-run over already-written months
    idempotent.

    The de-duplication runs before the sort, not after. `keep="last"` only means
    "the incoming row" while concat order still holds, and `sort_values` defaults
    to a non-stable quicksort that reorders equal keys arbitrarily — sorting first
    left roughly half of a re-run's rows at their old values, silently. That is
    the difference between a rewrite and a no-op for whichever rows lost the coin
    toss, including bars being rebuilt precisely because their forward window had
    finally elapsed.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    incoming = frame[columns].copy() if columns else frame.copy()
    if out_path.exists():
        existing = pd.read_parquet(out_path)
        incoming = (
            pd.concat([existing, incoming], ignore_index=True)
            .drop_duplicates(subset=[key], keep="last")
            .sort_values(key)
            .reset_index(drop=True)
        )

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{out_path.name}.", suffix=".tmp", dir=out_path.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        incoming.to_parquet(temporary, index=False)
        # Windows can raise OSError (Bad file descriptor) on fsync of a
        # freshly written parquet handle; durability still lands via replace.
        with temporary.open("r+b") as handle:
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                if os.name != "nt":
                    raise
        os.replace(temporary, out_path)
    finally:
        temporary.unlink(missing_ok=True)
    return len(incoming)
