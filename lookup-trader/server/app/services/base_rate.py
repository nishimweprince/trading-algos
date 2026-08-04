"""Context base rates over the bar feature store.

The mirror image of `compare_occurrences`: same relaxation ladder, same honesty
about what the sample can support, but the population is every closed candle
rather than every trade the operator marked. It answers "in this context, what
did price do next" — the prior that a setup's win rate has to beat before it is
evidence of anything.

Outcomes are derived at query time from the stored first-touch bar numbers, so a
single precomputed row prices any (target, stop, horizon, side) the caller asks
for without ever re-reading a candle.
"""

from __future__ import annotations

import duckdb

from app.config import settings
from app.services.bar_features import level_key
from app.services.compare import wilson_from_rate
from app.services.labeler import AMBIGUOUS_RESULTS

# Dropped first to last as the sample thins. Same principle as RELAX_ORDER: the
# narrow, conditional dimensions go first and the structural ones survive. There
# is no `side` here — the store is direction-free, side is chosen by the caller.
BAR_RELAX_ORDER = [
    "session_overlap",
    "day_of_week",
    "htf_atr_bucket",
    "htf_trend_state",
    "ema_slope_bucket",
    "atr_change_bucket",
    "rsi_band",
    "atr_bucket",
    "session",
    "trend_state",
]


class FeatureStoreEmpty(RuntimeError):
    """Raised when no feature store has been built for the symbol/timeframe."""


def _validate(horizon: int, target_atr: float, stop_atr: float) -> None:
    if horizon not in settings.feature_horizons:
        raise ValueError(f"horizon must be one of {settings.feature_horizons}")
    for name, value in (("target_atr", target_atr), ("stop_atr", stop_atr)):
        if value not in settings.touch_levels:
            raise ValueError(f"{name} must be one of {settings.touch_levels}")


def _touch_expr(level: float, direction: str) -> str:
    """Bars to first touch of one barrier, as a nullable integer."""
    return (
        f"TRY_CAST(json_extract_string(level_touch, "
        f"'$.\"{level_key(level)}\".{direction}') AS INTEGER)"
    )


def outcome_expr(horizon: int, target_atr: float, stop_atr: float, side: int) -> str:
    """SQL that turns two first-touch bar numbers into win / loss / timeout.

    This is the whole reason the store keeps bar numbers instead of a fixed
    outcome grid: whichever barrier carries the lower bar number was reached
    first, and every combination of target, stop and horizon is that comparison.
    """
    tp = _touch_expr(target_atr, "up" if side == 1 else "down")
    sl = _touch_expr(stop_atr, "down" if side == 1 else "up")
    ambiguous = AMBIGUOUS_RESULTS[settings.ambiguous_policy]
    return f"""
        CASE
          WHEN {tp} IS NOT NULL AND {sl} IS NOT NULL AND {tp} = {sl} AND {tp} <= {horizon}
            THEN '{ambiguous}'
          WHEN {tp} IS NOT NULL AND {tp} <= {horizon}
               AND ({sl} IS NULL OR {sl} > {horizon} OR {tp} < {sl})
            THEN 'win'
          WHEN {sl} IS NOT NULL AND {sl} <= {horizon}
               AND ({tp} IS NULL OR {tp} > {horizon} OR {sl} < {tp})
            THEN 'loss'
          ELSE 'timeout'
        END
    """


def _base_filters(symbol: str, timeframe: str, horizon: int) -> tuple[list[str], list]:
    return (
        [
            "symbol = ?",
            "timeframe = ?",
            # Mixed feature versions mean incomparable buckets, not a bigger sample.
            "bar_feature_version = ?",
            "context_reliable IS NOT FALSE",
            # A bar whose forward window has not fully elapsed has no outcome yet
            # — the same guard `_base_filters` in compare.py applies to pending
            # occurrences, and here it covers the tail of every build.
            f"fwd{horizon}_complete IS TRUE",
        ],
        [symbol, timeframe, settings.bar_feature_version],
    )


def base_rate(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    timeframe: str,
    context: dict,
    horizon: int = 24,
    target_atr: float = 1.5,
    stop_atr: float = 1.0,
    side: int = 1,
    min_samples: int | None = None,
    pinned: list[str] | None = None,
) -> dict:
    """Outcome distribution over historical bars matching this context."""
    _validate(horizon, target_atr, stop_atr)
    min_n = min_samples or settings.base_rate_min_samples
    pinned = pinned or []

    supplied = [d for d in BAR_RELAX_ORDER if context.get(d) is not None]
    pins = [d for d in pinned if d in supplied]
    active = list(supplied)
    droppable = [d for d in BAR_RELAX_ORDER if d in active and d not in pins]

    outcome = outcome_expr(horizon, target_atr, stop_atr, side)

    while True:
        conditions, params = _base_filters(symbol, timeframe, horizon)
        for dimension in active:
            conditions.append(f"{dimension} = ?")
            params.append(context[dimension])
        where = " AND ".join(conditions)

        counts = _outcome_counts(con, where, params, outcome, horizon, side)
        if counts["decided"] >= min_n or not droppable:
            break
        active.remove(droppable.pop(0))

    level_used = "+".join(active) if active else "context_free"
    decided = counts["decided"]
    if decided < min_n:
        return _empty(
            level_used="no_signal",
            horizon=horizon,
            min_samples_required=min_n,
            decided_available=decided,
        )

    wins = counts["wins"]
    win_rate = wins / decided
    # Adjacent bars share all but one of their forward bars, so the row count is
    # not a count of independent observations. Dividing by the horizon is the
    # crude but honest correction; without it the interval is fiction.
    effective_n = max(decided / horizon, 1.0)
    low, high = wilson_from_rate(win_rate, effective_n)

    return {
        "matched_count": counts["matched"],
        "wins": wins,
        "losses": decided - wins,
        "decided": decided,
        "timeouts": counts["timeouts"],
        "win_rate": win_rate,
        "wilson_low": low,
        "wilson_high": high,
        # A win pays target/stop in R against a stop that costs exactly 1R.
        "expectancy_r": (wins * (target_atr / stop_atr) - (decided - wins)) / decided,
        "effective_n": effective_n,
        "level_used": level_used,
        "dimensions_used": active,
        "median_mfe_atr": counts["median_mfe_atr"],
        "median_mae_atr": counts["median_mae_atr"],
        "horizon": horizon,
        "target_atr": target_atr,
        "stop_atr": stop_atr,
        "side": side,
        "min_samples_required": min_n,
        "decided_available": decided,
    }


def _outcome_counts(
    con: duckdb.DuckDBPyConnection,
    where: str,
    params: list,
    outcome: str,
    horizon: int,
    side: int,
) -> dict:
    # Excursions are stored against the anchor close, so a short's favourable
    # move is the negated minimum.
    favourable = f"fwd{horizon}_max_atr" if side == 1 else f"-fwd{horizon}_min_atr"
    adverse = f"fwd{horizon}_min_atr" if side == 1 else f"-fwd{horizon}_max_atr"

    row = con.execute(
        f"""
        WITH scored AS (
          SELECT {outcome} AS result, {favourable} AS fav, {adverse} AS adv
          FROM bar_features WHERE {where}
        )
        SELECT
          count(*) AS matched,
          count(*) FILTER (WHERE result = 'win') AS wins,
          count(*) FILTER (WHERE result IN ('win', 'loss')) AS decided,
          count(*) FILTER (WHERE result = 'timeout') AS timeouts,
          median(fav) AS median_mfe_atr,
          median(adv) AS median_mae_atr
        FROM scored
        """,
        params,
    ).fetchone()

    matched, wins, decided, timeouts, mfe, mae = row
    return {
        "matched": int(matched or 0),
        "wins": int(wins or 0),
        "decided": int(decided or 0),
        "timeouts": int(timeouts or 0),
        "median_mfe_atr": float(mfe) if mfe is not None else None,
        "median_mae_atr": float(mae) if mae is not None else None,
    }


def _empty(
    level_used: str,
    horizon: int,
    min_samples_required: int | None = None,
    decided_available: int | None = None,
) -> dict:
    return {
        "matched_count": 0,
        "wins": 0,
        "losses": 0,
        "decided": 0,
        "timeouts": 0,
        "win_rate": None,
        "wilson_low": None,
        "wilson_high": None,
        "expectancy_r": None,
        "effective_n": None,
        "level_used": level_used,
        "dimensions_used": [],
        "median_mfe_atr": None,
        "median_mae_atr": None,
        "horizon": horizon,
        "target_atr": None,
        "stop_atr": None,
        "side": None,
        "min_samples_required": min_samples_required,
        "decided_available": decided_available,
    }


def store_is_built(con: duckdb.DuckDBPyConnection) -> bool:
    """Whether the view points at real parquet rather than the empty fallback."""
    row = con.execute(
        "SELECT count(*) FROM duckdb_columns() "
        "WHERE table_name = 'bar_features' AND column_name = 'level_touch'"
    ).fetchone()
    return bool(row and row[0])
