"""Signal-only replay of one symbol through ``SymbolCore``.

Feeds entry bars in order and attaches a fast- or slow-TF bar exactly at
the entry close where it closes, which is how the backtester and the live
runner deliver them. No positions are opened; this is the harness for
look-ahead tests, golden episodes and quick inspection.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pandas as pd

from zone_trend.config import Config
from zone_trend.core.symbol import CloseResult, SymbolCore
from zone_trend.types import Bar

__all__ = ["ReplayStep", "bars_from_frame", "replay"]

_FIELDS = ("open_time", "close_time", "open", "high", "low", "close", "volume")


def bars_from_frame(df: pd.DataFrame) -> list[Bar]:
    cols = [df[c].to_numpy() for c in _FIELDS]
    return [
        Bar(int(ot), int(ct), float(o), float(h), float(lo), float(c), float(v))
        for ot, ct, o, h, lo, c, v in zip(*cols, strict=True)
    ]


@dataclass
class ReplayStep:
    bar: Bar
    result: CloseResult


def replay(
    symbol: str,
    entry: pd.DataFrame,
    fast: pd.DataFrame,
    slow: pd.DataFrame,
    cfg: Config,
    core: SymbolCore | None = None,
) -> Iterator[tuple[SymbolCore, ReplayStep]]:
    core = core or SymbolCore(symbol, cfg)
    fast_at = {b.close_time: b for b in bars_from_frame(fast)}
    slow_at = {b.close_time: b for b in bars_from_frame(slow)}
    for bar in bars_from_frame(entry):
        res = core.on_close(bar, fast_at.get(bar.close_time), slow_at.get(bar.close_time))
        yield core, ReplayStep(bar, res)
