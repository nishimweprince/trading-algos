"""Per-bar feature rows for chart overlays, gated at the reveal boundary.

The context half of a row is safe to draw at any time — it is computed from bars
at or before its own anchor. The forward half is not: bar *i*'s 24-bar outcome
summarises bars the replay may still be hiding.

The gate is applied here rather than in the client. `/context` passes
`forward_bars=0` instead of fetching the future and trusting the caller to ignore
it; a response carrying forward values the UI merely declines to render would be
one network tab away from defeating the whole replay.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import duckdb

from app.config import settings
from app.utils.time import to_utc, to_utc_iso

# Drawn on the chart. Deliberately narrow: everything here is causal, so the
# whole set is safe to return for every bar in range.
CONTEXT_FIELDS = [
    "trend_state",
    "atr_bucket",
    "session",
    "rsi_band",
    "atr_at_bar",
    "atr_pct",
    "efficiency_ratio",
    "close_range_pct",
    "realized_vol_atr",
    "context_reliable",
]

# Returned only for bars whose whole forward window is already on screen.
FORWARD_FIELDS = ["max_atr", "min_atr", "bars_to_max", "bars_to_min", "max_first"]

TIMEFRAME_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


def horizon_cutoff(
    timeframe: str,
    revealed_through: datetime,
    horizon: int,
) -> datetime | None:
    """Latest anchor whose forward window is entirely at or before the reveal.

    Bar *i* is safe once the cursor has reached *i + horizon*, so the cutoff is
    the reveal timestamp less `horizon` bars. None when the timeframe has no
    known bar length, which suppresses forward data entirely rather than guessing.
    """
    minutes = TIMEFRAME_MINUTES.get(timeframe.upper())
    if minutes is None:
        return None
    return revealed_through - timedelta(minutes=minutes * horizon)


def fetch_bar_series(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    timeframe: str,
    date_from: datetime,
    date_to: datetime,
    revealed_through: datetime | None = None,
    horizon: int = 24,
) -> list[dict]:
    """Feature rows over a date range, with the forward half nulled past the cutoff."""
    if horizon not in settings.feature_horizons:
        raise ValueError(f"horizon must be one of {settings.feature_horizons}")

    forward = [f"fwd{horizon}_{name}" for name in FORWARD_FIELDS]
    columns = ", ".join(["ts", *CONTEXT_FIELDS, *forward, f"fwd{horizon}_complete"])

    rows = con.execute(
        f"""
        SELECT {columns}
        FROM bar_features
        WHERE symbol = ? AND timeframe = ? AND ts >= ? AND ts <= ?
          AND bar_feature_version = ?
        ORDER BY ts ASC
        """,
        [
            symbol,
            timeframe,
            to_utc(date_from),
            to_utc(date_to),
            settings.bar_feature_version,
        ],
    ).fetchall()

    cutoff = (
        horizon_cutoff(timeframe, to_utc(revealed_through), horizon)
        if revealed_through is not None
        else None
    )

    out: list[dict] = []
    for row in rows:
        ts = row[0]
        record: dict = {"ts": to_utc_iso(ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts)}
        for i, name in enumerate(CONTEXT_FIELDS, start=1):
            record[name] = _clean(row[i])

        base = 1 + len(CONTEXT_FIELDS)
        complete = bool(row[base + len(FORWARD_FIELDS)])
        visible = complete and (cutoff is not None and to_utc(ts) <= cutoff)
        for i, name in enumerate(FORWARD_FIELDS):
            record[name] = _clean(row[base + i]) if visible else None
        record["forward_visible"] = visible
        out.append(record)
    return out


def _clean(value):
    """DuckDB hands back numpy scalars and NaN; JSON wants neither."""
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and value != value:
        return None
    return value
