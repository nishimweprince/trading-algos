"""Faithful Python ports of the Pine Script primitives used by the IPDA entry.

Every function returns a full series (list with ``None`` for not-yet-defined bars) so
that history references (Pine ``[1]``) and ``crossover``/``crossunder`` work the same
way they do on a TradingView chart. Ported from file.txt Section 5 (supertrend_f,
SMA13, bull_sig / bear_sig).
"""

from __future__ import annotations

from .candles import Candle


def true_range(candles: list[Candle]) -> list[float]:
    """Pine ``ta.tr(true)``: first bar is high-low; otherwise uses the previous close."""
    out: list[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            out.append(c.high - c.low)
        else:
            prev_close = candles[i - 1].close
            out.append(
                max(
                    c.high - c.low,
                    abs(c.high - prev_close),
                    abs(c.low - prev_close),
                )
            )
    return out


def sma(values: list[float], length: int) -> list[float | None]:
    """Simple moving average; ``None`` until ``length`` samples are available."""
    out: list[float | None] = []
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= length:
            running -= values[i - length]
        out.append(running / length if i >= length - 1 else None)
    return out


def rma(values: list[float], length: int) -> list[float | None]:
    """Pine ``ta.rma``: Wilder EMA (alpha = 1/length) seeded with the SMA of the
    first ``length`` values. ``None`` until the seed bar (index length-1)."""
    alpha = 1.0 / length
    out: list[float | None] = []
    prev: float | None = None
    seed = sma(values, length)
    for i, v in enumerate(values):
        if prev is None:
            out.append(seed[i])
            prev = seed[i]
        else:
            prev = alpha * v + (1 - alpha) * prev
            out.append(prev)
    return out


def atr(candles: list[Candle], length: int) -> list[float | None]:
    """Pine ``ta.atr`` = rma(tr(true), length)."""
    return rma(true_range(candles), length)


def supertrend(
    candles: list[Candle], factor: float, atr_len: int
) -> tuple[list[float | None], list[int | None]]:
    """Port of ``supertrend_f`` in file.txt.

    ``_src`` is ``close`` in the IPDA call. Returns (supertrend_line, direction)
    where direction -1 = up-trend (line below price), 1 = down-trend (line above price).
    """
    atr_vals = atr(candles, atr_len)
    n = len(candles)
    st: list[float | None] = [None] * n
    direction: list[int | None] = [None] * n

    prev_lower = 0.0
    prev_upper = 0.0
    prev_st: float | None = None

    for i in range(n):
        atrat = atr_vals[i]
        if atrat is None:
            st[i] = None
            direction[i] = None
            prev_st = None
            continue

        src = candles[i].close
        upper = src + factor * atrat
        lower = src - factor * atrat
        prev_close = candles[i - 1].close if i > 0 else None

        if lower > prev_lower or (prev_close is not None and prev_close < prev_lower):
            lower_band = lower
        else:
            lower_band = prev_lower
        if upper < prev_upper or (prev_close is not None and prev_close > prev_upper):
            upper_band = upper
        else:
            upper_band = prev_upper

        prev_atr = atr_vals[i - 1] if i > 0 else None
        if prev_atr is None or prev_st is None:
            dir_i = 1
        elif prev_st == prev_upper:
            dir_i = -1 if src > upper_band else 1
        else:
            dir_i = 1 if src < lower_band else -1

        st_i = lower_band if dir_i == -1 else upper_band
        st[i] = st_i
        direction[i] = dir_i

        prev_lower = lower_band
        prev_upper = upper_band
        prev_st = st_i

    return st, direction


def rsi(values: list[float], length: int) -> list[float | None]:
    """Port of the ``rev_src`` oscillator in file.txt Section 11 — Wilder's RSI.

    The Pine builds it out of ``ta.rma`` of the up and down changes rather than
    calling ``ta.rsi``, and keeps two explicit guards that a bare RSI formula does
    not have: an all-gains window reports 100 and an all-losses window reports 0.
    Both are reproduced here so a flat or one-sided series cannot divide by zero.
    """
    n = len(values)
    out: list[float | None] = [None] * n
    if n < 2:
        return out

    # ta.change(close) is na on bar 0, so every derived series starts at bar 1.
    gains = [max(values[i] - values[i - 1], 0.0) for i in range(1, n)]
    losses = [max(values[i - 1] - values[i], 0.0) for i in range(1, n)]
    upward = rma(gains, length)
    downward = rma(losses, length)

    for j in range(n - 1):
        up, down = upward[j], downward[j]
        if up is None or down is None:
            continue
        if down == 0:
            out[j + 1] = 100.0
        elif up == 0:
            out[j + 1] = 0.0
        else:
            out[j + 1] = 100.0 - (100.0 / (1.0 + up / down))
    return out


def constant_series(value: float, length: int) -> list[float | None]:
    """A flat series, so ``crossover``/``crossunder`` can compare against a level."""
    return [value] * length


def crossover(a: list[float | None], b: list[float | None], i: int) -> bool:
    """Pine ``ta.crossover(a, b)`` at bar ``i``: a[i] > b[i] and a[i-1] <= b[i-1]."""
    if i < 1:
        return False
    a0, b0, a1, b1 = a[i], b[i], a[i - 1], b[i - 1]
    if a0 is None or b0 is None or a1 is None or b1 is None:
        return False
    return a0 > b0 and a1 <= b1


def crossunder(a: list[float | None], b: list[float | None], i: int) -> bool:
    """Pine ``ta.crossunder(a, b)`` at bar ``i``: a[i] < b[i] and a[i-1] >= b[i-1]."""
    if i < 1:
        return False
    a0, b0, a1, b1 = a[i], b[i], a[i - 1], b[i - 1]
    if a0 is None or b0 is None or a1 is None or b1 is None:
        return False
    return a0 < b0 and a1 >= b1
