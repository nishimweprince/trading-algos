from __future__ import annotations

import json
import math
import uuid
from datetime import datetime

import duckdb

from app.config import settings
from app.services.candles import fetch_labeling_window
from app.services.context import compute_context
from app.services.labeler import label_triple_barrier
from app.services.pips import net_r as compute_net_r
from app.services.pips import pip_size as compute_pip_size
from app.services.pips import spread_pips
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
    "outcome_kind, skip_reason, blinded, peeked, context_reliable, "
    "excluded, exclude_reason, feature_version, features"
).split(", ")

JSON_COLUMNS = ("metadata", "features", "r_grid")
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
    provenance: dict | None = None,
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

    ctx = compute_context(candles_df, signal_idx)
    size = compute_pip_size(symbol)

    features: dict = {
        "rsi_value": ctx["rsi_value"],
        "ema_value": ctx["ema_value"],
        "atr_pct": ctx["atr_pct"],
        "dist_ema_atr": ctx["dist_ema_atr"],
        "atr_terciles": ctx["atr_terciles"],
        "warmup_bars_available": ctx["warmup_bars_available"],
        "pip_size": size,
        "spread_pips_assumed": spread_pips(symbol),
        "entry_next_open": _next_open(candles_df, signal_idx),
        **(provenance or {}),
    }

    row: dict = {
        "source": "manual",
        "session_id": session_id,
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
        "outcome_kind": outcome_kind,
        "skip_reason": skip_reason,
        "blinded": blinded,
        "peeked": peeked,
        "context_reliable": ctx["context_reliable"],
        "excluded": False,
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
            }
        )

    row["features"] = features
    return insert_occurrence(con, row)


def _next_open(candles_df, signal_idx: int) -> float | None:
    """Open of the bar after the signal — the entry an auto detector would use.

    Recorded on manual rows too so the two sources stay comparable on a common
    entry convention later.
    """
    if signal_idx + 1 >= len(candles_df):
        return None
    return float(candles_df.iloc[signal_idx + 1]["open"])
