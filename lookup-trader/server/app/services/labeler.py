from __future__ import annotations

import pandas as pd

AMBIGUOUS_RESULTS = {"conservative": "loss", "drop": "ambiguous", "optimistic": "win"}


def label_triple_barrier(
    candles: pd.DataFrame,
    signal_idx: int,
    side: int,
    entry: float,
    sl: float,
    tp: float,
    max_bars: int,
    ambiguous: str = "conservative",
    r_grid_targets: list[float] | None = None,
    pip_size: float | None = None,
) -> dict:
    """Score a marked trade over the forward bars.

    `result` / `realized_r` / `exit_idx` / `bars_to_resolution` keep their original
    semantics exactly. Everything else is path detail recorded alongside them so a
    later question ("was the target too greedy?", "would 2R have paid?") can be
    answered without replaying the trade again.
    """
    highs = candles["high"].to_numpy()
    lows = candles["low"].to_numpy()
    closes = candles["close"].to_numpy()

    end = min(signal_idx + 1 + max_bars, len(candles))
    risk = abs(entry - sl) or 1e-9

    exit_idx = end - 1
    result = "timeout"
    ambiguous_bar = False

    for i in range(signal_idx + 1, end):
        hit_tp = highs[i] >= tp if side == 1 else lows[i] <= tp
        hit_sl = lows[i] <= sl if side == 1 else highs[i] >= sl
        if hit_tp and hit_sl:
            ambiguous_bar = True
            result = AMBIGUOUS_RESULTS[ambiguous]
            exit_idx = i
            break
        if hit_tp:
            result, exit_idx = "win", i
            break
        if hit_sl:
            result, exit_idx = "loss", i
            break

    out = _out(result, exit_idx, signal_idx, entry, sl, tp, side)

    if result == "win":
        exit_price = tp
    elif result == "loss":
        exit_price = sl
    else:
        exit_price = float(closes[exit_idx]) if exit_idx >= 0 else None

    out.update(
        {
            "ambiguous_bar": ambiguous_bar,
            "exit_price": exit_price,
            "exit_ts": _ts_at(candles, exit_idx),
            # Mark-to-close at the exit bar. Populated for every result, which is
            # what makes timeouts usable in expectancy — realized_r stays None there.
            "r_at_horizon": (float(closes[exit_idx]) - entry) / risk * side
            if exit_idx > signal_idx
            else None,
        }
    )
    out.update(_excursions(highs, lows, signal_idx, exit_idx, side, entry, risk, pip_size))
    out.update(_entry_feasibility(highs, lows, signal_idx, exit_idx, entry))

    grid = _r_grid(highs, lows, signal_idx, end, side, entry, risk, r_grid_targets, ambiguous)
    out["r_grid"] = grid
    one_r = grid.get("1.0")
    out["touched_1r_before_sl"] = one_r["result"] == "win" if one_r else None

    return out


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


def _ts_at(candles: pd.DataFrame, idx: int):
    if "ts" not in candles.columns or idx < 0 or idx >= len(candles):
        return None
    ts = candles.iloc[idx]["ts"]
    return ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts


def _excursions(highs, lows, signal_idx, exit_idx, side, entry, risk, pip_size) -> dict:
    """Best and worst the trade ever looked, between the signal and the exit."""
    empty = {
        "mfe_r": None,
        "mae_r": None,
        "mfe_pips": None,
        "mae_pips": None,
        "bars_to_mfe": None,
        "bars_to_mae": None,
    }
    if exit_idx <= signal_idx:
        return empty

    favourable, adverse = (highs, lows) if side == 1 else (lows, highs)

    best_r = best_idx = None
    worst_r = worst_idx = None
    for i in range(signal_idx + 1, exit_idx + 1):
        fav = (float(favourable[i]) - entry) / risk * side
        adv = (float(adverse[i]) - entry) / risk * side
        if best_r is None or fav > best_r:
            best_r, best_idx = fav, i
        if worst_r is None or adv < worst_r:
            worst_r, worst_idx = adv, i

    return {
        "mfe_r": best_r,
        "mae_r": worst_r,
        "mfe_pips": best_r * risk / pip_size if pip_size else None,
        "mae_pips": worst_r * risk / pip_size if pip_size else None,
        "bars_to_mfe": best_idx - signal_idx,
        "bars_to_mae": worst_idx - signal_idx,
    }


def _entry_feasibility(highs, lows, signal_idx, exit_idx, entry) -> dict:
    """Was the marked entry a price the market actually traded through?

    The operator places entry by clicking the chart, so it can sit outside every
    bar's range — a trade that could never have been filled.
    """
    for i in range(signal_idx + 1, max(exit_idx + 1, signal_idx + 2)):
        if i >= len(highs):
            break
        if lows[i] <= entry <= highs[i]:
            return {"entry_feasible": True, "entry_fill_bars": i - signal_idx}
    return {"entry_feasible": False, "entry_fill_bars": None}


def _r_grid(highs, lows, signal_idx, end, side, entry, risk, targets, ambiguous) -> dict:
    """Score a ladder of target multiples against the same stop.

    One pass: the stop is fixed, so the first bar that touches it decides every
    target that had not already been reached.
    """
    targets = targets or []
    favourable, adverse = (highs, lows) if side == 1 else (lows, highs)

    first_sl_idx = None
    first_touch: dict[float, int] = {}
    for i in range(signal_idx + 1, end):
        reach = (float(favourable[i]) - entry) / risk * side
        for m in targets:
            if m not in first_touch and reach >= m:
                first_touch[m] = i
        if first_sl_idx is None and (float(adverse[i]) - entry) / risk * side <= -1.0:
            first_sl_idx = i
        if first_sl_idx is not None and len(first_touch) == len(targets):
            break

    grid: dict[str, dict] = {}
    for m in targets:
        tp_idx = first_touch.get(m)
        if tp_idx is not None and (first_sl_idx is None or tp_idx < first_sl_idx):
            grid[f"{m:.1f}"] = {"result": "win", "bars": tp_idx - signal_idx, "ambiguous": False}
        elif tp_idx is not None and tp_idx == first_sl_idx:
            grid[f"{m:.1f}"] = {
                "result": AMBIGUOUS_RESULTS[ambiguous],
                "bars": tp_idx - signal_idx,
                "ambiguous": True,
            }
        elif first_sl_idx is not None:
            grid[f"{m:.1f}"] = {
                "result": "loss",
                "bars": first_sl_idx - signal_idx,
                "ambiguous": False,
            }
        else:
            grid[f"{m:.1f}"] = {"result": "timeout", "bars": None, "ambiguous": False}
    return grid
