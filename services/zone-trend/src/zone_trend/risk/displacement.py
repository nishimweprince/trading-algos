"""Displacement multiplier ``k_sym`` from favourable-move statistics.

Per symbol and entry type, with at least ``min_trades`` closed trades::

    k_sym = clamp(symbol median MFE in R / universe median MFE in R, lo, hi)

and 1.0 before that. A table is always built from trades that closed before
a cut-off (the end of a walk-forward in-sample window), never from the
trades it will be applied to.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

__all__ = ["DisplacementTable"]


@dataclass
class DisplacementTable:
    k: dict[tuple[str, str], float] = field(default_factory=dict)
    stats: dict[tuple[str, str], dict] = field(default_factory=dict)

    def get(self, symbol: str, entry_type: str) -> float:
        return self.k.get((symbol, entry_type), 1.0)

    @classmethod
    def from_trades(
        cls,
        trades: pd.DataFrame,
        cutoff_ms: int,
        min_trades: int = 30,
        clamp: tuple[float, float] = (0.5, 1.5),
    ) -> DisplacementTable:
        """``trades`` needs symbol, entry_type, exit_time, mfe_r, mae_r."""
        table = cls()
        done = trades[trades["exit_time"] < cutoff_ms]
        if done.empty:
            return table
        universe = float(np.median(done["mfe_r"]))
        for (symbol, etype), g in done.groupby(["symbol", "entry_type"]):
            stats = {
                "n": len(g),
                "mfe_p50_r": float(g["mfe_r"].median()),
                "mfe_p75_r": float(g["mfe_r"].quantile(0.75)),
                "mae_p50_r": float(g["mae_r"].median()),
            }
            table.stats[(symbol, etype)] = stats
            if len(g) >= min_trades and universe > 0:
                ratio = stats["mfe_p50_r"] / universe
                table.k[(symbol, etype)] = float(min(max(ratio, clamp[0]), clamp[1]))
        return table
