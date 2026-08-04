from __future__ import annotations

import math

import duckdb

from app.config import settings
from app.services.pips import pip_size, spread_pips


def wilson_from_rate(p: float, n: float, z: float = 1.96) -> tuple[float | None, float | None]:
    """Wilson bounds around an observed rate at a stated sample size.

    Split out from `wilson_interval` because bar-level base rates have to widen
    the interval to an *effective* sample size — overlapping forward windows mean
    the row count badly overstates how many independent draws there were.
    """
    if n <= 0:
        return None, None
    denom = 1 + z**2 / n
    centre = p + z**2 / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z**2 / (4 * n)) / n)
    low = (centre - margin) / denom
    high = (centre + margin) / denom
    return max(0.0, low), min(1.0, high)


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n == 0:
        return None, None
    return wilson_from_rate(wins / n, n, z)


# Dropped first to last when the sample is too thin. Narrowest and most
# subjective dimensions go first; the structural ones survive longest. `side` is
# last because pooling longs and shorts is the most misleading thing this query
# can do — nine of the seeded setups have no default side at all.
RELAX_ORDER = [
    "confidence_min",
    "entry_quality",
    "confluence_tags",
    "consolidation_before",
    "at_key_level",
    "level_type",
    "market_structure",
    "htf_alignment",
    "observed_trend",
    "calendar_flag",
    "entry_convention",
    "day_of_week",
    "htf_trend_state",
    "ema_slope_bucket",
    "atr_change_bucket",
    "rsi_band",
    "sl_atr_bucket",
    "rr_bucket",
    "atr_bucket",
    "session",
    "trend_state",
    "side",
]

# Dimension -> SQL predicate. Everything is a plain column comparison except the
# two that are not equality tests.
_SPECIAL_CLAUSES = {
    "confidence_min": "confidence >= ?",
    # Comma-separated on the row; the query means "all of these are present", so
    # each requested tag contributes its own clause.
    "confluence_tags": "list_contains(str_split(confluence_tags, ','), ?)",
}


def _clause(dimension: str, value) -> tuple[list[str], list]:
    if dimension == "confluence_tags":
        tags = [t for t in (value or []) if t]
        return [_SPECIAL_CLAUSES[dimension]] * len(tags), list(tags)
    if dimension in _SPECIAL_CLAUSES:
        return [_SPECIAL_CLAUSES[dimension]], [value]
    return [f"{dimension} = ?"], [value]


def _base_filters(
    setup_id: str,
    symbol: str,
    timeframe: str,
    source: str,
    exclude_peeked: bool,
    blinded_only: bool,
    exclude_assisted: bool = False,
) -> tuple[list[str], list]:
    conditions = [
        "setup_id = ?",
        "symbol = ?",
        "timeframe = ?",
        "source = ?",
        "excluded IS NOT TRUE",
        # Rows whose indicators never had enough warmup history.
        "context_reliable IS NOT FALSE",
        # Pending live signals have no outcome yet — never match them.
        "(lifecycle IS NULL OR lifecycle = 'resolved')",
    ]
    params: list = [setup_id, symbol, timeframe, source]
    if exclude_peeked:
        conditions.append("peeked IS NOT TRUE")
    if exclude_assisted:
        # Decisions made with this query's own output on screen. Including them
        # makes the sample partly evidence about the model rather than the market.
        conditions.append("assisted IS NOT TRUE")
    if blinded_only:
        conditions.append("blinded IS TRUE")
    return conditions, params


def compare_occurrences(
    con: duckdb.DuckDBPyConnection,
    setup_id: str,
    symbol: str,
    timeframe: str,
    context: dict,
    source: str = "manual",
    min_samples: int | None = None,
    pinned: list[str] | None = None,
    exclude_peeked: bool = True,
    blinded_only: bool = False,
    exclude_assisted: bool = False,
) -> dict:
    """Match stored occurrences on as many dimensions as the sample supports.

    Starts from every dimension the caller supplied and drops them one at a time
    in RELAX_ORDER until `min_samples` decided trades are matched. Pinned
    dimensions are never dropped: if the pins alone cannot reach the threshold
    the answer is `no_signal`, not a silently widened sample.
    """
    min_n = min_samples or settings.min_samples
    pinned = pinned or []

    supplied = [d for d in RELAX_ORDER if context.get(d) is not None]
    # Pinning a dimension with no value is meaningless rather than an error.
    pins = [d for d in pinned if d in supplied]
    active = list(supplied)

    # Candidates in the order they will be given up.
    droppable = [d for d in RELAX_ORDER if d in active and d not in pins]

    while True:
        conditions, params = _base_filters(
            setup_id, symbol, timeframe, source, exclude_peeked, blinded_only, exclude_assisted
        )
        for dimension in active:
            clauses, values = _clause(dimension, context[dimension])
            conditions.extend(clauses)
            params.extend(values)

        where = " AND ".join(conditions)
        traded_where = f"{where} AND outcome_kind = 'traded'"
        counts = _outcome_counts(con, traded_where, params, symbol=symbol)

        if counts["decided"] >= min_n or not droppable:
            break
        active.remove(droppable.pop(0))

    level_used = "+".join(active) if active else "setup_only"
    if counts["decided"] < min_n:
        return _empty(
            level_used="no_signal",
            min_samples_required=min_n,
            decided_available=counts["decided"],
        )

    wins, decided = counts["wins"], counts["decided"]
    wilson_low, wilson_high = wilson_interval(wins, decided)
    skipped, skip_reasons = _skip_counts(con, f"{where} AND outcome_kind = 'skipped'", params)
    skip_denom = skipped + decided
    skip_rate = skipped / skip_denom if skip_denom else None

    return {
        "matched_count": decided + counts["timeouts"],
        "wins": wins,
        "decided": decided,
        "timeouts": counts["timeouts"],
        "win_rate": wins / decided,
        "wilson_low": wilson_low,
        "wilson_high": wilson_high,
        "expectancy_r": counts["expectancy_r"],
        "expectancy_r_net": counts["expectancy_r_net"],
        "level_used": level_used,
        "overlap_ratio": overlap_ratio(con, traded_where, params),
        "target_grid": target_grid(con, traded_where, params),
        "median_mfe_r": counts["median_mfe_r"],
        "median_mae_r": counts["median_mae_r"],
        "skipped_count": skipped,
        "skip_rate": skip_rate,
        "skip_reasons": skip_reasons,
        "excluded_peeked": _peeked_count(con, setup_id, symbol, timeframe, source)
        if exclude_peeked
        else 0,
        "min_samples_required": min_n,
        "decided_available": decided,
    }


def _empty(
    level_used: str,
    *,
    min_samples_required: int | None = None,
    decided_available: int | None = None,
) -> dict:
    return {
        "matched_count": 0,
        "wins": 0,
        "decided": 0,
        "timeouts": 0,
        "win_rate": None,
        "wilson_low": None,
        "wilson_high": None,
        "expectancy_r": None,
        "expectancy_r_net": None,
        "level_used": level_used,
        "overlap_ratio": None,
        "target_grid": [],
        "median_mfe_r": None,
        "median_mae_r": None,
        "skipped_count": 0,
        "skip_rate": None,
        "skip_reasons": {},
        "excluded_peeked": 0,
        "min_samples_required": min_samples_required,
        "decided_available": decided_available,
    }


def _outcome_counts(
    con: duckdb.DuckDBPyConnection,
    where: str,
    params: list,
    *,
    symbol: str | None = None,
) -> dict:
    spread = spread_pips(symbol) * pip_size(symbol) if symbol and spread_pips(symbol) else 0.0
    net_expr = (
        f"realized_r - ({spread} / abs(entry - sl))"
        if spread
        else "NULL"
    )
    row = con.execute(
        f"""
        SELECT
          count(*) FILTER (WHERE result = 'win') AS wins,
          count(*) FILTER (WHERE result IN ('win', 'loss')) AS decided,
          count(*) FILTER (WHERE result = 'timeout') AS timeouts,
          avg(realized_r) FILTER (WHERE result IN ('win', 'loss')) AS expectancy_r,
          avg({net_expr}) FILTER (
            WHERE result IN ('win', 'loss') AND entry IS NOT NULL AND sl IS NOT NULL
          ) AS expectancy_r_net,
          median(mfe_r) AS median_mfe_r,
          median(mae_r) AS median_mae_r
        FROM occurrences
        WHERE {where}
        """,
        params,
    ).fetchone()
    wins, decided, timeouts, expectancy_r, expectancy_r_net, median_mfe, median_mae = row
    return {
        "wins": int(wins or 0),
        "decided": int(decided or 0),
        "timeouts": int(timeouts or 0),
        "expectancy_r": float(expectancy_r) if expectancy_r is not None else None,
        "expectancy_r_net": float(expectancy_r_net) if expectancy_r_net is not None else None,
        "median_mfe_r": float(median_mfe) if median_mfe is not None else None,
        "median_mae_r": float(median_mae) if median_mae is not None else None,
    }


def target_grid(con: duckdb.DuckDBPyConnection, where: str, params: list) -> list[dict]:
    """Win rate at each target multiple across the matched set.

    Every occurrence stores the outcome of the same stop against a ladder of
    targets, so the marked target can be judged against the alternatives without
    re-labelling anything.
    """
    targets = settings.r_grid_targets
    if not targets:
        return []

    selects = []
    for i, multiple in enumerate(targets):
        # Keys are written by the labeler as f"{m:.1f}", not caller input.
        path = f"$.\"{multiple:.1f}\".result"
        selects.append(
            f"count(*) FILTER (WHERE json_extract_string(r_grid, '{path}') = 'win') AS w{i}, "
            f"count(*) FILTER (WHERE json_extract_string(r_grid, '{path}') "
            f"IN ('win', 'loss')) AS d{i}"
        )

    row = con.execute(
        f"SELECT {', '.join(selects)} FROM occurrences WHERE {where}",
        params,
    ).fetchone()

    grid = []
    for i, multiple in enumerate(targets):
        wins, decided = int(row[i * 2] or 0), int(row[i * 2 + 1] or 0)
        losses = decided - wins
        grid.append(
            {
                "target_r": multiple,
                "wins": wins,
                "decided": decided,
                "win_rate": wins / decided if decided else None,
                # A win pays the target multiple, a loss costs exactly 1R.
                "expectancy_r": (wins * multiple - losses) / decided if decided else None,
            }
        )
    return grid


def _skip_counts(con: duckdb.DuckDBPyConnection, where: str, params: list) -> tuple[int, dict]:
    rows = con.execute(
        f"SELECT coalesce(skip_reason, 'unspecified'), count(*) FROM occurrences "
        f"WHERE {where} GROUP BY 1",
        params,
    ).fetchall()
    reasons = {reason: int(count) for reason, count in rows}
    return sum(reasons.values()), reasons


def _peeked_count(
    con: duckdb.DuckDBPyConnection, setup_id: str, symbol: str, timeframe: str, source: str
) -> int:
    """How many rows the honesty gate removed, so a thin sample is explicable."""
    row = con.execute(
        """
        SELECT count(*) FROM occurrences
        WHERE setup_id = ? AND symbol = ? AND timeframe = ? AND source = ?
          AND outcome_kind = 'traded' AND excluded IS NOT TRUE AND peeked IS TRUE
        """,
        [setup_id, symbol, timeframe, source],
    ).fetchone()
    return int(row[0] or 0)


def overlap_ratio(con: duckdb.DuckDBPyConnection, where: str, params: list) -> float | None:
    """Fraction of matched occurrences whose holding window overlaps another's.

    The Wilson interval treats every occurrence as an independent draw. Two trades
    held over the same bars are not independent, so a high ratio means the stated
    confidence is wider than reality. Reported rather than corrected — the caller
    should know, and the correction (uniqueness weighting) is a bigger change.
    """
    row = con.execute(
        f"""
        WITH matched AS (
          SELECT id, ts, coalesce(exit_ts, ts) AS exit_ts
          FROM occurrences
          WHERE {where} AND result IN ('win', 'loss', 'timeout')
        )
        SELECT
          (SELECT count(*) FROM matched) AS total,
          (SELECT count(DISTINCT a.id) FROM matched a JOIN matched b
             ON a.id <> b.id AND a.ts <= b.exit_ts AND b.ts <= a.exit_ts) AS overlapping
        """,
        params,
    ).fetchone()
    total, overlapping = row
    if not total:
        return None
    return (overlapping or 0) / total
