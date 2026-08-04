from __future__ import annotations

import json
import math
import uuid
from datetime import datetime

import duckdb

from app.config import settings
from app.services.candles import fetch_labeling_window
from app.services.context import (
    compute_context,
    compute_htf_trend_state,
    context_fingerprint,
    rr_bucket,
    sl_atr_bucket,
)
from app.services.labeler import label_triple_barrier
from app.services.pips import net_r as compute_net_r
from app.services.pips import pip_size as compute_pip_size
from app.services.pips import spread_pips
from app.services.signals import get_signal, link_signal_to_occurrence
from app.utils.time import to_utc, to_utc_iso

# Allowlist for the dynamic INSERT below. Anything not named here is dropped
# rather than reaching SQL.
OCCURRENCE_COLUMNS = (
    "id, source, session_id, symbol, timeframe, ts, setup_id, side, "
    "entry, sl, tp, max_bars, atr_period, atr_at_signal, "
    "result, realized_r, bars_to_resolution, observed_result, "
    "trend_state, atr_bucket, session, rsi_band, "
    "calendar_flag, calendar_tags, notes, labeler_version, "
    "pips_captured, observed_trend, confluence_tags, "
    "screenshot_entry, screenshot_exit, metadata, "
    "exit_ts, exit_price, r_at_horizon, net_r, ambiguous_bar, entry_feasible, "
    "mfe_r, mae_r, mfe_pips, mae_pips, bars_to_mfe, bars_to_mae, r_grid, "
    "market_structure, htf_alignment, entry_quality, confidence, "
    "rr_bucket, sl_atr_bucket, "
    "outcome_kind, skip_reason, blinded, peeked, context_reliable, "
    "excluded, exclude_reason, feature_version, features, "
    "signal_id, lifecycle, compare_at_signal, base_rate_at_signal, assisted, context_fingerprint, "
    "entry_convention, day_of_week, ema_slope_bucket, atr_change_bucket, htf_trend_state, at_key_level, "
    "level_type, consolidation_before, tagger_confidence, payload_hash, "
    "tagger_model_version"
).split(", ")

JSON_COLUMNS = ("metadata", "features", "r_grid", "compare_at_signal", "base_rate_at_signal")
TS_COLUMNS = {"ts", "exit_ts", "started_at", "ended_at", "date_from", "date_to", "created_at"}


def validate_setup(con: duckdb.DuckDBPyConnection, setup_id: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM setups WHERE setup_id = ? AND active = TRUE",
        [setup_id],
    ).fetchone()
    return row is not None


def insert_occurrence(con: duckdb.DuckDBPyConnection, data: dict) -> dict:
    occ_id = str(uuid.uuid4())
    row = {"id": occ_id}
    for column in OCCURRENCE_COLUMNS:
        if column == "id" or column not in data:
            continue
        value = data[column]
        if column in JSON_COLUMNS and value is not None and not isinstance(value, str):
            value = json.dumps(value)
        row[column] = value

    columns = ", ".join(row)
    placeholders = ", ".join("?" * len(row))
    con.execute(
        f"INSERT INTO occurrences ({columns}) VALUES ({placeholders})",
        list(row.values()),
    )
    written = con.execute("SELECT * FROM occurrences WHERE id = ?", [occ_id]).fetchdf()
    return _row_to_dict(written.iloc[0])


def _row_to_dict(row) -> dict:
    d = row.to_dict()
    for k, v in d.items():
        if k in JSON_COLUMNS and isinstance(v, str):
            try:
                d[k] = json.loads(v)
            except json.JSONDecodeError:
                d[k] = v
        elif hasattr(v, "isoformat") and k in TS_COLUMNS:
            d[k] = to_utc_iso(v.to_pydatetime() if hasattr(v, "to_pydatetime") else v)
        elif hasattr(v, "isoformat"):
            d[k] = v.isoformat()
        elif hasattr(v, "item"):
            v = v.item()
            d[k] = None if isinstance(v, float) and math.isnan(v) else v
        elif isinstance(v, float) and math.isnan(v):
            d[k] = None
    if "id" in d:
        d["id"] = str(d["id"])
    if "session_id" in d and d["session_id"] is not None:
        d["session_id"] = str(d["session_id"])
    if "signal_id" in d and d["signal_id"] is not None:
        d["signal_id"] = str(d["signal_id"])
    return d


def list_occurrences(con: duckdb.DuckDBPyConnection, session_id: str | None = None) -> list[dict]:
    if session_id:
        df = con.execute(
            "SELECT * FROM occurrences WHERE session_id = ? ORDER BY created_at DESC",
            [session_id],
        ).fetchdf()
    else:
        df = con.execute("SELECT * FROM occurrences ORDER BY created_at DESC").fetchdf()
    return [_row_to_dict(row) for _, row in df.iterrows()]


PATCHABLE_COLUMNS = (
    "notes",
    "observed_result",
    "observed_trend",
    "confluence_tags",
    "calendar_flag",
    "calendar_tags",
    "metadata",
    "excluded",
    "exclude_reason",
)


def get_occurrence(con: duckdb.DuckDBPyConnection, occurrence_id: str) -> dict | None:
    df = con.execute("SELECT * FROM occurrences WHERE id = ?", [occurrence_id]).fetchdf()
    if df.empty:
        return None
    return _row_to_dict(df.iloc[0])


def patch_occurrence(
    con: duckdb.DuckDBPyConnection, occurrence_id: str, changes: dict
) -> dict | None:
    """Update operator labels. The labeler's verdict is not patchable, by design."""
    updates = {k: v for k, v in changes.items() if k in PATCHABLE_COLUMNS}
    if not updates:
        return get_occurrence(con, occurrence_id)

    if get_occurrence(con, occurrence_id) is None:
        return None

    # Editing the blob has to move the columns derived from it, or a corrected
    # label would keep matching the old value in /compare.
    if "metadata" in updates:
        updates.update(dict.fromkeys(PROMOTED_LABELS))
        updates.update(_promoted_labels(updates["metadata"]))

    for key in JSON_COLUMNS:
        if key in updates and updates[key] is not None and not isinstance(updates[key], str):
            updates[key] = json.dumps(updates[key])

    assignments = ", ".join(f"{column} = ?" for column in updates)
    con.execute(
        f"UPDATE occurrences SET {assignments} WHERE id = ?",
        [*updates.values(), occurrence_id],
    )
    return get_occurrence(con, occurrence_id)


def exclude_occurrence(
    con: duckdb.DuckDBPyConnection, occurrence_id: str, reason: str | None = None
) -> dict | None:
    return patch_occurrence(con, occurrence_id, {"excluded": True, "exclude_reason": reason})


def process_trade(
    con: duckdb.DuckDBPyConnection,
    *,
    session_id: str | None,
    symbol: str,
    timeframe: str,
    signal_ts: datetime,
    setup_id: str,
    side: int,
    entry: float | None = None,
    sl: float | None = None,
    tp: float | None = None,
    outcome_kind: str = "traded",
    skip_reason: str | None = None,
    notes: str | None = None,
    calendar_flag: bool | None = None,
    calendar_tags: str | None = None,
    observed_result: str | None = None,
    observed_trend: str | None = None,
    confluence_tags: str | None = None,
    session_override: str | None = None,
    pips_captured: float | None = None,
    screenshot_entry: str | None = None,
    screenshot_exit: str | None = None,
    metadata: dict | None = None,
    blinded: bool | None = None,
    peeked: bool | None = None,
    assisted: bool | None = None,
    provenance: dict | None = None,
    signal_id: str | None = None,
    entry_convention: str | None = None,
    at_key_level: bool | None = None,
    level_type: str | None = None,
    consolidation_before: bool | None = None,
    lifecycle: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> dict:
    """Compute context at the signal bar, score the trade, and write one occurrence.

    `date_from` / `date_to` are accepted for backwards compatibility and ignored:
    the labeling window is a fixed bar count around the signal so that the same
    bar always produces the same context, whatever range the operator replayed.
    """
    if not validate_setup(con, setup_id):
        raise ValueError(f"Invalid or inactive setup_id: {setup_id}")
    if outcome_kind not in ("traded", "skipped"):
        raise ValueError(f"Unknown outcome_kind: {outcome_kind}")

    signal_ts = to_utc(signal_ts)
    candles_df, signal_idx = fetch_labeling_window(
        con,
        symbol,
        timeframe,
        signal_ts,
        warmup_bars=settings.warmup_bars,
        forward_bars=settings.max_bars + 1,
    )

    ctx = compute_context(
        candles_df,
        signal_idx,
        htf_trend_state=compute_htf_trend_state(con, symbol, timeframe, signal_ts),
    )
    size = compute_pip_size(symbol)

    linked_signal = get_signal(con, signal_id) if signal_id else None
    compare_at_signal = linked_signal.get("compare_at_signal") if linked_signal else None
    # Carried onto the occurrence rather than left on the signal so calibration —
    # predicted vs prior vs outcome — is a read of /trades alone.
    base_rate_at_signal = linked_signal.get("base_rate_at_signal") if linked_signal else None

    if linked_signal:
        if assisted is None:
            assisted = linked_signal.get("assisted")
        if calendar_flag is None:
            calendar_flag = linked_signal.get("calendar_flag")
        if calendar_tags is None:
            calendar_tags = linked_signal.get("calendar_tags")
        if confluence_tags is None:
            confluence_tags = linked_signal.get("confluence_tags")
        if at_key_level is None:
            at_key_level = linked_signal.get("at_key_level")
        if level_type is None:
            level_type = linked_signal.get("level_type")
        if consolidation_before is None:
            consolidation_before = linked_signal.get("consolidation_before")
        if metadata is None and linked_signal.get("confidence"):
            metadata = {"confidence": linked_signal["confidence"]}

    features: dict = {
        "rsi_value": ctx["rsi_value"],
        "ema_value": ctx["ema_value"],
        "atr_pct": ctx["atr_pct"],
        "dist_ema_atr": ctx["dist_ema_atr"],
        "atr_terciles": ctx["atr_terciles"],
        "warmup_bars_available": ctx["warmup_bars_available"],
        "day_of_week": ctx["day_of_week"],
        "dist_day_high_atr": ctx["dist_day_high_atr"],
        "dist_day_low_atr": ctx["dist_day_low_atr"],
        "ema_slope_atr": ctx["ema_slope_atr"],
        "ema_slope_bucket": ctx["ema_slope_bucket"],
        "atr_change_ratio": ctx["atr_change_ratio"],
        "atr_change_bucket": ctx["atr_change_bucket"],
        "signal_body_pct": ctx["signal_body_pct"],
        "signal_upper_wick_pct": ctx["signal_upper_wick_pct"],
        "signal_lower_wick_pct": ctx["signal_lower_wick_pct"],
        "signal_range_atr": ctx["signal_range_atr"],
        "bars_since_swing_high": ctx["bars_since_swing_high"],
        "bars_since_swing_low": ctx["bars_since_swing_low"],
        "gap_atr": ctx["gap_atr"],
        "pip_size": size,
        "spread_pips_assumed": spread_pips(symbol),
        "entry_next_open": _next_open(candles_df, signal_idx),
        **(provenance or {}),
    }

    resolved_lifecycle = lifecycle or ("pending" if outcome_kind == "traded" and entry is None else "resolved")

    row: dict = {
        "source": "manual",
        "session_id": session_id,
        "signal_id": signal_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "ts": signal_ts,
        "setup_id": setup_id,
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "max_bars": settings.max_bars,
        "atr_period": settings.atr_period,
        "atr_at_signal": ctx["atr_at_signal"],
        "observed_result": observed_result,
        "trend_state": ctx["trend_state"],
        "atr_bucket": ctx["atr_bucket"],
        "session": session_override or ctx["session"],
        "rsi_band": ctx["rsi_band"],
        "day_of_week": ctx["day_of_week"],
        "ema_slope_bucket": ctx["ema_slope_bucket"],
        "atr_change_bucket": ctx["atr_change_bucket"],
        "htf_trend_state": ctx["htf_trend_state"],
        "calendar_flag": calendar_flag,
        "calendar_tags": calendar_tags,
        "notes": notes,
        "labeler_version": settings.labeler_version,
        "feature_version": settings.feature_version,
        "pips_captured": pips_captured,
        "observed_trend": observed_trend,
        "confluence_tags": confluence_tags,
        "screenshot_entry": screenshot_entry,
        "screenshot_exit": screenshot_exit,
        "metadata": metadata,
        **_promoted_labels(metadata),
        "at_key_level": at_key_level,
        "level_type": level_type,
        "consolidation_before": consolidation_before,
        "outcome_kind": outcome_kind,
        "skip_reason": skip_reason,
        "blinded": blinded,
        "peeked": peeked,
        "assisted": assisted,
        "base_rate_at_signal": base_rate_at_signal,
        "context_reliable": ctx["context_reliable"],
        "excluded": False,
        "lifecycle": resolved_lifecycle,
        "compare_at_signal": compare_at_signal,
        "context_fingerprint": context_fingerprint(setup_id, symbol, timeframe, ctx),
        "entry_convention": entry_convention or "marked",
    }

    # A skip without marked levels has nothing to score; one with levels gets the
    # counterfactual, which is the whole value of recording a rejected trade.
    if entry is not None and sl is not None and tp is not None:
        label = label_triple_barrier(
            candles_df,
            signal_idx=signal_idx,
            side=side,
            entry=entry,
            sl=sl,
            tp=tp,
            max_bars=settings.max_bars,
            ambiguous=settings.ambiguous_policy,
            r_grid_targets=settings.r_grid_targets,
            pip_size=size,
        )
        risk = abs(entry - sl) or 1e-9
        features.update(
            {
                "sl_pips": risk / size if size else None,
                "sl_atr_mult": risk / ctx["atr_at_signal"] if ctx["atr_at_signal"] else None,
                "rr_planned": abs(tp - entry) / risk,
                "entry_fill_bars": label["entry_fill_bars"],
                "touched_1r_before_sl": label["touched_1r_before_sl"],
            }
        )
        row.update(
            {
                "rr_bucket": rr_bucket(features["rr_planned"]),
                "sl_atr_bucket": sl_atr_bucket(features["sl_atr_mult"]),
                "result": label["result"],
                "realized_r": label["realized_r"],
                "bars_to_resolution": label["bars_to_resolution"],
                "exit_ts": label["exit_ts"],
                "exit_price": label["exit_price"],
                "r_at_horizon": label["r_at_horizon"],
                "net_r": compute_net_r(label["realized_r"], symbol, entry, sl),
                "ambiguous_bar": label["ambiguous_bar"],
                "entry_feasible": label["entry_feasible"],
                "mfe_r": label["mfe_r"],
                "mae_r": label["mae_r"],
                "mfe_pips": label["mfe_pips"],
                "mae_pips": label["mae_pips"],
                "bars_to_mfe": label["bars_to_mfe"],
                "bars_to_mae": label["bars_to_mae"],
                "r_grid": label["r_grid"],
                "lifecycle": "resolved",
            }
        )

        next_open = features.get("entry_next_open")
        if next_open is not None:
            risk = abs(entry - sl)
            tp_offset = abs(tp - entry)
            next_sl = next_open - risk if side == 1 else next_open + risk
            next_tp = next_open + tp_offset if side == 1 else next_open - tp_offset
            next_label = label_triple_barrier(
                candles_df,
                signal_idx=signal_idx,
                side=side,
                entry=next_open,
                sl=next_sl,
                tp=next_tp,
                max_bars=settings.max_bars,
                ambiguous=settings.ambiguous_policy,
                r_grid_targets=settings.r_grid_targets,
                pip_size=size,
            )
            features["next_open_label"] = {
                "entry": next_open,
                "sl": next_sl,
                "tp": next_tp,
                "result": next_label["result"],
                "realized_r": next_label["realized_r"],
                "bars_to_resolution": next_label["bars_to_resolution"],
            }

    row["features"] = features
    occurrence = insert_occurrence(con, row)
    if signal_id:
        link_signal_to_occurrence(con, signal_id, occurrence["id"])
    return occurrence


# Operator labels that get their own column. Anything else the client sends in
# metadata is still stored there, it just is not a comparison dimension.
PROMOTED_LABELS = ("market_structure", "htf_alignment", "entry_quality", "confidence")


def _promoted_labels(metadata: dict | None) -> dict:
    if not metadata:
        return {}
    return {key: metadata.get(key) for key in PROMOTED_LABELS if metadata.get(key) is not None}


def _next_open(candles_df, signal_idx: int) -> float | None:
    """Open of the bar after the signal — the entry an auto detector would use.

    Recorded on manual rows too so the two sources stay comparable on a common
    entry convention later.
    """
    if signal_idx + 1 >= len(candles_df):
        return None
    return float(candles_df.iloc[signal_idx + 1]["open"])
