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

from datetime import datetime

import duckdb

from app.config import settings
from app.services.bar_features import level_key
from app.services.compare import wilson_from_rate
from app.services.labeler import AMBIGUOUS_RESULTS
from app.services.pips import spread_cost_r_atr, spread_pips
from app.services.recommendation import (
    break_even_from_geometry,
    derive_recommendation,
    verdict_rank,
)

# Dropped first to last as the sample thins. Same principle as RELAX_ORDER: the
# narrow, conditional dimensions go first and the structural ones survive. There
# is no `side` here — the store is direction-free, side is chosen by the caller.
BAR_RELAX_ORDER = [
    "tag_setup_id",
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

TAG_STATES = ("complete", "forming", "any")


class FeatureStoreEmpty(RuntimeError):
    """Raised when no feature store has been built for the symbol/timeframe."""


def context_from_bar(ctx: dict, htf: dict, ts: datetime) -> dict:
    """The bar-context dict the ladder filters on, from a computed context.

    Shared by `/base-rate` and `process_signal` so a frozen prior and a live
    lookup can never end up filtering on different dimensions for the same bar.
    `session_overlap` is derived here because `compute_context` predates it.
    """
    return {
        "trend_state": ctx.get("trend_state"),
        "atr_bucket": ctx.get("atr_bucket"),
        "session": ctx.get("session"),
        "rsi_band": ctx.get("rsi_band"),
        "day_of_week": ctx.get("day_of_week"),
        "ema_slope_bucket": ctx.get("ema_slope_bucket"),
        "atr_change_bucket": ctx.get("atr_change_bucket"),
        "htf_trend_state": htf.get("trend_state"),
        "htf_atr_bucket": htf.get("atr_bucket"),
        "session_overlap": settings.session_overlap_start
        <= ts.hour
        < settings.session_overlap_end,
    }


def _median_atr(con: duckdb.DuckDBPyConnection, where: str, params: list) -> float | None:
    try:
        row = con.execute(
            f"SELECT median(atr_at_bar) FROM bar_features WHERE {where}", params
        ).fetchone()
    except duckdb.BinderException:
        return None
    if not row or row[0] is None:
        return None
    return float(row[0])


def _net_expectancy(
    gross: float | None, symbol: str, stop_atr: float, atr: float | None
) -> float | None:
    if gross is None or not spread_pips(symbol):
        return None
    return gross - spread_cost_r_atr(symbol, stop_atr, atr or 0.0)


def _validate(horizon: int, target_atr: float, stop_atr: float) -> None:
    if horizon not in settings.feature_horizons:
        raise ValueError(f"horizon must be one of {settings.feature_horizons}")
    for name, value in (("target_atr", target_atr), ("stop_atr", stop_atr)):
        if value not in settings.touch_levels:
            raise ValueError(f"{name} must be one of {settings.touch_levels}")


def _dimension_clause(dimension: str, context: dict) -> tuple[str, list]:
    """SQL for one context dimension, including state-aware pattern matching.

    `bar_tags` is historical persisted input. Treat malformed or legacy JSON as
    having no tags instead of letting one bad row fail the whole base-rate query.
    """
    if dimension != "tag_setup_id":
        return f"{dimension} = ?", [context[dimension]]

    tag_state = context.get("tag_state", "complete")
    if tag_state not in TAG_STATES:
        raise ValueError(f"tag_state must be one of {TAG_STATES}")
    state_clause = (
        ""
        if tag_state == "any"
        else " AND json_extract_string(tag.value, '$.state') = ?"
    )
    params = [context[dimension]]
    if tag_state != "any":
        params.append(tag_state)
    return (
        "EXISTS ("
        "SELECT 1 FROM json_each("
        "CASE WHEN json_valid(bar_tags) THEN CAST(bar_tags AS JSON) "
        "ELSE CAST('{\"tags\":[]}' AS JSON) END, '$.tags') AS tag "
        "WHERE json_extract_string(tag.value, '$.setup_id') = ?"
        f"{state_clause})",
        params,
    )


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


def _resolve_level(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    timeframe: str,
    context: dict,
    horizon: int,
    outcome: str,
    side: int,
    min_n: int,
    pinned: list[str],
) -> tuple[str, list, list[str], dict]:
    """Drop dimensions in BAR_RELAX_ORDER until the sample is large enough.

    Split out from `base_rate` so a grid can resolve the match set once and then
    score many target/stop pairs against it — the same separation `target_grid`
    relies on in compare.py. Running the ladder per cell would give each cell a
    different population, which is not a grid, it is twenty-five unrelated
    answers.
    """
    supplied = [d for d in BAR_RELAX_ORDER if context.get(d) is not None]
    pins = [d for d in pinned if d in supplied]
    active = list(supplied)
    droppable = [d for d in BAR_RELAX_ORDER if d in active and d not in pins]

    while True:
        conditions, params = _base_filters(symbol, timeframe, horizon)
        for dimension in active:
            clause, values = _dimension_clause(dimension, context)
            conditions.append(clause)
            params.extend(values)
        where = " AND ".join(conditions)

        counts = _outcome_counts(con, where, params, outcome, horizon, side)
        if counts["decided"] >= min_n or not droppable:
            return where, params, active, counts
        active.remove(droppable.pop(0))


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
    apply_cost: bool = True,
) -> dict:
    """Outcome distribution over historical bars matching this context."""
    _validate(horizon, target_atr, stop_atr)
    min_n = min_samples or settings.base_rate_min_samples

    outcome = outcome_expr(horizon, target_atr, stop_atr, side)
    where, params, active, counts = _resolve_level(
        con, symbol, timeframe, context, horizon, outcome, side, min_n, pinned or []
    )

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
    gross_exp = (wins * (target_atr / stop_atr) - (decided - wins)) / decided
    median_atr = _median_atr(con, where, params) if apply_cost else None
    net_exp = _net_expectancy(gross_exp, symbol, stop_atr, median_atr) if apply_cost else None

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
        "expectancy_r": gross_exp,
        "expectancy_r_net": net_exp,
        "effective_n": effective_n,
        "level_used": level_used,
        "dimensions_used": active,
        "median_mfe_atr": counts["median_mfe_atr"],
        "median_mae_atr": counts["median_mae_atr"],
        # Every other target against the same stop and the same matched bars, so
        # "was my target wrong?" is answerable without a second lookup.
        "target_grid": _score_cells(
            con,
            where,
            params,
            horizon,
            side,
            [(t, stop_atr) for t in settings.touch_levels],
            symbol=symbol,
            apply_cost=apply_cost,
            median_atr=median_atr,
        ),
        "horizon": horizon,
        "target_atr": target_atr,
        "stop_atr": stop_atr,
        "side": side,
        "scored_side": None,
        "scored_direction": None,
        "recommendation": None,
        "min_samples_required": min_n,
        "decided_available": decided,
    }


def _attach_recommendation(payload: dict, *, side: int, stop_atr: float, target_atr: float) -> dict:
    expectancy = payload.get("expectancy_r_net")
    if expectancy is None:
        expectancy = payload.get("expectancy_r")
    recommendation = derive_recommendation(
        side=side,
        expectancy_r=expectancy,
        wilson_low=payload.get("wilson_low"),
        decided=payload.get("decided") or 0,
        min_samples=payload.get("min_samples_required") or settings.base_rate_min_samples,
        level_used=payload.get("level_used"),
        effective_n=payload.get("effective_n"),
        break_even_win_rate=break_even_from_geometry(stop_atr, target_atr),
    )
    payload["scored_side"] = side
    payload["scored_direction"] = "long" if side == 1 else "short"
    payload["recommendation"] = recommendation
    return payload


def base_rate_with_recommendation(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    timeframe: str,
    context: dict,
    horizon: int = 24,
    target_atr: float = 1.5,
    stop_atr: float = 1.0,
    side: int | None = None,
    min_samples: int | None = None,
    pinned: list[str] | None = None,
    apply_cost: bool = True,
) -> dict:
    if side is not None:
        result = base_rate(
            con,
            symbol=symbol,
            timeframe=timeframe,
            context=context,
            horizon=horizon,
            target_atr=target_atr,
            stop_atr=stop_atr,
            side=side,
            min_samples=min_samples,
            pinned=pinned,
            apply_cost=apply_cost,
        )
        return _attach_recommendation(result, side=side, stop_atr=stop_atr, target_atr=target_atr)

    candidates: list[dict] = []
    for candidate_side in (1, -1):
        result = base_rate(
            con,
            symbol=symbol,
            timeframe=timeframe,
            context=context,
            horizon=horizon,
            target_atr=target_atr,
            stop_atr=stop_atr,
            side=candidate_side,
            min_samples=min_samples,
            pinned=pinned,
            apply_cost=apply_cost,
        )
        candidates.append(
            _attach_recommendation(
                result, side=candidate_side, stop_atr=stop_atr, target_atr=target_atr
            )
        )

    def key_fn(item: dict) -> tuple[int, float, float]:
        rec = item.get("recommendation") or {}
        expectancy = item.get("expectancy_r_net")
        if expectancy is None:
            expectancy = item.get("expectancy_r")
        return (
            verdict_rank(rec.get("verdict", "insufficient_data")),
            expectancy if expectancy is not None else -99.0,
            item.get("wilson_low") or -99.0,
        )

    best = max(candidates, key=key_fn)
    return best


def base_rate_grid(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    timeframe: str,
    context: dict,
    horizon: int = 24,
    side: int = 1,
    min_samples: int | None = None,
    pinned: list[str] | None = None,
) -> dict:
    """Every target/stop pair the store can price, over one matched population.

    Frozen at annotation time, where no levels are marked yet: any single
    target/stop would be a guess, and if the operator later marks a different
    ratio the stored prior would describe a trade they did not take.
    """
    min_n = min_samples or settings.base_rate_min_samples
    probe = outcome_expr(horizon, 1.0, 1.0, side)
    where, params, active, counts = _resolve_level(
        con, symbol, timeframe, context, horizon, probe, side, min_n, pinned or []
    )

    cells = [(t, s) for s in settings.touch_levels for t in settings.touch_levels]
    return {
        "level_used": "+".join(active) if active else "context_free",
        "dimensions_used": active,
        "matched_count": counts["matched"],
        "median_mfe_atr": counts["median_mfe_atr"],
        "median_mae_atr": counts["median_mae_atr"],
        "horizon": horizon,
        "side": side,
        "min_samples_required": min_n,
        "cells": _score_cells(con, where, params, horizon, side, cells),
    }


def _score_cells(
    con: duckdb.DuckDBPyConnection,
    where: str,
    params: list,
    horizon: int,
    side: int,
    cells: list[tuple[float, float]],
    *,
    symbol: str | None = None,
    apply_cost: bool = False,
    median_atr: float | None = None,
) -> list[dict]:
    """Score many target/stop pairs in one pass over the matched bars.

    One query with a pair of FILTER aggregates per cell rather than one query per
    cell — the construction `target_grid` uses in compare.py, for the same reason.
    """
    if not cells:
        return []

    selects = []
    for i, (target, stop) in enumerate(cells):
        outcome = outcome_expr(horizon, target, stop, side)
        selects.append(
            f"count(*) FILTER (WHERE ({outcome}) = 'win') AS w{i}, "
            f"count(*) FILTER (WHERE ({outcome}) IN ('win', 'loss')) AS d{i}"
        )

    row = con.execute(
        f"SELECT {', '.join(selects)} FROM bar_features WHERE {where}", params
    ).fetchone()

    grid = []
    for i, (target, stop) in enumerate(cells):
        wins, decided = int(row[i * 2] or 0), int(row[i * 2 + 1] or 0)
        losses = decided - wins
        gross = (wins * (target / stop) - losses) / decided if decided else None
        grid.append(
            {
                "target_atr": target,
                "stop_atr": stop,
                "wins": wins,
                "decided": decided,
                "win_rate": wins / decided if decided else None,
                # A win pays target/stop in R; a loss costs exactly 1R.
                "expectancy_r": gross,
                "expectancy_r_net": (
                    _net_expectancy(gross, symbol, stop, median_atr)
                    if apply_cost and symbol
                    else None
                ),
                "effective_n": max(decided / horizon, 1.0) if decided else None,
            }
        )
    return grid


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
        "expectancy_r_net": None,
        "effective_n": None,
        "level_used": level_used,
        "dimensions_used": [],
        "median_mfe_atr": None,
        "median_mae_atr": None,
        "target_grid": [],
        "horizon": horizon,
        "target_atr": None,
        "stop_atr": None,
        "side": None,
        "scored_side": None,
        "scored_direction": None,
        "recommendation": None,
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
