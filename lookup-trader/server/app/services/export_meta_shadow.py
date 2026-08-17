"""Flatten the live meta-event ledger to a CSV for offline analysis."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.services.meta_shadow_store import MetaShadowStore

COLUMNS = [
    "event_id",
    "symbol",
    "timeframe",
    "signal_ts",
    "side",
    "primary_setup_id",
    "confidence",
    "state",
    "forward_evaluation_eligible",
    "entry_ts",
    "entry_price",
    "stop_price",
    "target_price",
    "exit_ts",
    "exit_price",
    "outcome",
    "gross_r",
    "net_r_3",
    "net_r_5",
    "net_r_8",
    "bars_to_resolution",
    "notification_status",
    "notification_attempts",
    "notified_at",
]


def export_meta_shadow(
    store: MetaShadowStore,
    path: Path,
    *,
    symbol: str,
    timeframe: str,
    forward_only: bool,
) -> int:
    with store.connect() as con:
        forward_clause = " AND forward_evaluation_eligible=1" if forward_only else ""
        rows = con.execute(
            f"SELECT {','.join(COLUMNS)} FROM meta_live_events "
            f"WHERE symbol=? AND timeframe=?{forward_clause} ORDER BY signal_ts",
            [symbol, timeframe],
        ).fetchall()
    df = pd.DataFrame(rows, columns=COLUMNS)
    df.to_csv(path, index=False)
    return len(df)
