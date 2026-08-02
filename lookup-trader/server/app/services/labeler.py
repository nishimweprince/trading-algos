from __future__ import annotations

import pandas as pd


def label_triple_barrier(
    candles: pd.DataFrame,
    signal_idx: int,
    side: int,
    entry: float,
    sl: float,
    tp: float,
    max_bars: int,
    ambiguous: str = "conservative",
) -> dict:
    highs = candles["high"].to_numpy()
    lows = candles["low"].to_numpy()
    end = min(signal_idx + 1 + max_bars, len(candles))
    for i in range(signal_idx + 1, end):
        hit_tp = highs[i] >= tp if side == 1 else lows[i] <= tp
        hit_sl = lows[i] <= sl if side == 1 else highs[i] >= sl
        if hit_tp and hit_sl:
            result = {"conservative": "loss", "drop": "ambiguous", "optimistic": "win"}[ambiguous]
            return _out(result, i, signal_idx, entry, sl, tp, side)
        if hit_tp:
            return _out("win", i, signal_idx, entry, sl, tp, side)
        if hit_sl:
            return _out("loss", i, signal_idx, entry, sl, tp, side)
    return _out("timeout", end - 1, signal_idx, entry, sl, tp, side)


def _out(result: str, exit_idx: int, signal_idx: int, entry: float, sl: float, tp: float, side: int) -> dict:
    risk = abs(entry - sl) or 1e-9
    if result == "win":
        realized_r = (tp - entry) / risk * side
    elif result == "loss":
        realized_r = (sl - entry) / risk * side
    else:
        realized_r = None
    return {
        "result": result,
        "exit_idx": int(exit_idx),
        "bars_to_resolution": int(exit_idx - signal_idx),
        "realized_r": realized_r,
    }
