from __future__ import annotations

import math

import duckdb

from app.config import settings


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n == 0:
        return None, None
    p = wins / n
    denom = 1 + z**2 / n
    centre = p + z**2 / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z**2 / (4 * n)) / n)
    low = (centre - margin) / denom
    high = (centre + margin) / denom
    return max(0.0, low), min(1.0, high)


FILTER_LADDER = [
    ["trend_state", "session", "atr_bucket", "rsi_band"],
    ["trend_state", "session", "atr_bucket"],
    ["trend_state", "session"],
    ["trend_state"],
    [],
]


def compare_occurrences(
    con: duckdb.DuckDBPyConnection,
    setup_id: str,
    symbol: str,
    timeframe: str,
    context: dict,
    source: str = "manual",
    min_samples: int | None = None,
) -> dict:
    min_n = min_samples or settings.min_samples

    for level_fields in FILTER_LADDER:
        conditions = [
            "setup_id = ?",
            "symbol = ?",
            "timeframe = ?",
            "source = ?",
        ]
        params: list = [setup_id, symbol, timeframe, source]

        for field in level_fields:
            val = context.get(field)
            if val is not None:
                conditions.append(f"{field} = ?")
                params.append(val)

        where = " AND ".join(conditions)
        row = con.execute(
            f"""
            SELECT
              count(*) FILTER (WHERE result = 'win') AS wins,
              count(*) FILTER (WHERE result IN ('win', 'loss')) AS decided,
              count(*) FILTER (WHERE result = 'timeout') AS timeouts,
              avg(realized_r) FILTER (WHERE result IN ('win', 'loss')) AS expectancy_r
            FROM occurrences
            WHERE {where}
            """,
            params,
        ).fetchone()

        wins, decided, timeouts, expectancy_r = row
        if decided is None:
            decided = 0
        if wins is None:
            wins = 0

        if decided >= min_n:
            win_rate = wins / decided if decided else None
            wilson_low, wilson_high = wilson_interval(wins, decided)
            level_used = "+".join(level_fields) if level_fields else "setup_only"
            return {
                "matched_count": int(decided + (timeouts or 0)),
                "wins": int(wins),
                "decided": int(decided),
                "timeouts": int(timeouts or 0),
                "win_rate": win_rate,
                "wilson_low": wilson_low,
                "wilson_high": wilson_high,
                "expectancy_r": float(expectancy_r) if expectancy_r is not None else None,
                "level_used": level_used,
            }

    return {
        "matched_count": 0,
        "wins": 0,
        "decided": 0,
        "timeouts": 0,
        "win_rate": None,
        "wilson_low": None,
        "wilson_high": None,
        "expectancy_r": None,
        "level_used": "no_signal",
    }
