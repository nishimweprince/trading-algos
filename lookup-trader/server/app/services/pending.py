from __future__ import annotations

from datetime import datetime

import duckdb

from app.config import settings
from app.services.candles import fetch_labeling_window
from app.services.labeler import label_triple_barrier
from app.services.pips import pip_size as compute_pip_size


def resolve_pending_occurrences(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Promote pending rows whose forward window is now available.

    Live signals stay out of the probability base until max_bars have elapsed
    and the labeler can score them.
    """
    rows = con.execute(
        """
        SELECT id, symbol, timeframe, ts, side, entry, sl, tp
        FROM occurrences
        WHERE lifecycle = 'pending'
          AND outcome_kind = 'traded'
          AND entry IS NOT NULL AND sl IS NOT NULL AND tp IS NOT NULL
        """
    ).fetchall()

    resolved: list[dict] = []
    now_cutoff = datetime.utcnow()

    for occ_id, symbol, timeframe, ts, side, entry, sl, tp in rows:
        signal_ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        try:
            candles_df, signal_idx = fetch_labeling_window(
                con,
                symbol,
                timeframe,
                signal_ts,
                warmup_bars=settings.warmup_bars,
                forward_bars=settings.max_bars + 1,
            )
        except ValueError:
            continue

        exit_bar_ts = candles_df.iloc[min(signal_idx + settings.max_bars, len(candles_df) - 1)]["ts"]
        exit_ts = exit_bar_ts.to_pydatetime() if hasattr(exit_bar_ts, "to_pydatetime") else exit_bar_ts
        if exit_ts > now_cutoff:
            continue

        size = compute_pip_size(symbol)
        label = label_triple_barrier(
            candles_df,
            signal_idx=signal_idx,
            side=int(side),
            entry=float(entry),
            sl=float(sl),
            tp=float(tp),
            max_bars=settings.max_bars,
            ambiguous=settings.ambiguous_policy,
            r_grid_targets=settings.r_grid_targets,
            pip_size=size,
        )

        con.execute(
            """
            UPDATE occurrences SET
              lifecycle = 'resolved',
              result = ?,
              realized_r = ?,
              bars_to_resolution = ?,
              exit_ts = ?,
              exit_price = ?,
              r_at_horizon = ?,
              ambiguous_bar = ?,
              entry_feasible = ?,
              mfe_r = ?,
              mae_r = ?,
              mfe_pips = ?,
              mae_pips = ?,
              bars_to_mfe = ?,
              bars_to_mae = ?,
              r_grid = ?
            WHERE id = ?
            """,
            [
                label["result"],
                label["realized_r"],
                label["bars_to_resolution"],
                label["exit_ts"],
                label["exit_price"],
                label["r_at_horizon"],
                label["ambiguous_bar"],
                label["entry_feasible"],
                label["mfe_r"],
                label["mae_r"],
                label["mfe_pips"],
                label["mae_pips"],
                label["bars_to_mfe"],
                label["bars_to_mae"],
                __import__("json").dumps(label["r_grid"]),
                str(occ_id),
            ],
        )
        df = con.execute("SELECT * FROM occurrences WHERE id = ?", [str(occ_id)]).fetchdf()
        if not df.empty:
            from app.services.occurrences import _row_to_dict

            resolved.append(_row_to_dict(df.iloc[0]))

    return resolved
