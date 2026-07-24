"""Faithful Python ports of the Pine Script primitives used by the LuxAlgo entry.

Every function returns a full series (list with ``None`` for not-yet-defined bars) so
that history references (Pine ``[1]``) and ``crossover``/``crossunder`` work the same
way they do on a TradingView chart. Ported from file.txt lines 25-43 (supertrend),
50-67 (sma/components) and 82-83 (bull/bear).
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
            out.append(seed[i])  # None until index length-1, then the SMA seed
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
    """Port of the ``supertrend`` function in file.txt (lines 25-43).

    ``_src`` is ``close`` in the LuxAlgo call. Returns (supertrend_line, direction)
    where direction -1 = up-trend (line below price), 1 = down-trend (line above price).
    """
    atr_vals = atr(candles, atr_len)
    n = len(candles)
    st: list[float | None] = [None] * n
    direction: list[int | None] = [None] * n

    prev_lower = 0.0  # nz(lowerBand[1]) -> 0 on first bar
    prev_upper = 0.0
    prev_st: float | None = None

    for i in range(n):
        atrat = atr_vals[i]
        if atrat is None:
            # Bands undefined until ATR is seeded; carry nothing, keep None.
            st[i] = None
            direction[i] = None
            prev_st = None
            continue

        src = candles[i].close
        upper = src + factor * atrat
        lower = src - factor * atrat
        prev_close = candles[i - 1].close if i > 0 else None

        # lowerBand := lower > prevLower or close[1] < prevLower ? lower : prevLower
        if lower > prev_lower or (prev_close is not None and prev_close < prev_lower):
            lower_band = lower
        else:
            lower_band = prev_lower
        # upperBand := upper < prevUpper or close[1] > prevUpper ? upper : prevUpper
        if upper < prev_upper or (prev_close is not None and prev_close > prev_upper):
            upper_band = upper
        else:
            upper_band = prev_upper

        # Direction: na(atrat[1]) is true on the first bar with a defined ATR.
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


# --- Additional primitives used by the overlay confluences (overlays.py) ---


def ema(values: list[float], length: int) -> list[float]:
    """Pine ``ta.ema``: alpha = 2/(length+1), seeded with the first value."""
    alpha = 2.0 / (length + 1)
    out: list[float] = []
    prev: float | None = None
    for v in values:
        prev = v if prev is None else alpha * v + (1 - alpha) * prev
        out.append(prev)
    return out


def wilder_ma(values: list[float], length: int) -> list[float]:
    """Pine ``Wild_ma`` (file.txt:446): wild := nz(wild[1]) + (src - nz(wild[1]))/len."""
    out: list[float] = []
    prev = 0.0
    for v in values:
        prev = prev + (v - prev) / length
        out.append(prev)
    return out


def change(values: list[float]) -> list[float]:
    """Pine ``ta.change``: values[i] - values[i-1]; 0 on the first bar."""
    return [0.0] + [values[i] - values[i - 1] for i in range(1, len(values))]


def rsi(values: list[float], length: int) -> list[float | None]:
    """Pine ``ta.rsi``: Wilder-smoothed average gain/loss; ``None`` until warmed."""
    n = len(values)
    out: list[float | None] = [None] * n
    if n < length + 1:
        return out
    gains = [max(values[i] - values[i - 1], 0.0) for i in range(1, n)]
    losses = [max(values[i - 1] - values[i], 0.0) for i in range(1, n)]

    def to_rsi(g: float, ln: float) -> float:
        if ln == 0:
            return 100.0
        if g == 0:
            return 0.0
        return 100.0 - 100.0 / (1.0 + g / ln)

    avg_g = sum(gains[:length]) / length
    avg_l = sum(losses[:length]) / length
    out[length] = to_rsi(avg_g, avg_l)
    for k in range(length, len(gains)):
        avg_g = (avg_g * (length - 1) + gains[k]) / length
        avg_l = (avg_l * (length - 1) + losses[k]) / length
        out[k + 1] = to_rsi(avg_g, avg_l)
    return out


def macd(
    values: list[float], fast: int, slow: int, signal_len: int
) -> tuple[list[float], list[float], list[float]]:
    """Pine ``ta.macd``: (macd_line, signal, hist)."""
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    macd_line = [ema_fast[i] - ema_slow[i] for i in range(len(values))]
    signal = ema(macd_line, signal_len)
    hist = [macd_line[i] - signal[i] for i in range(len(values))]
    return macd_line, signal, hist


def psar(
    candles: list[Candle], af_start: float = 0.02, af_inc: float = 0.02, af_max: float = 0.2
) -> list[float | None]:
    """Parabolic SAR (Pine ``ta.sar(0.02, 0.02, 0.2)``, file.txt:66)."""
    n = len(candles)
    out: list[float | None] = [None] * n
    if n < 2:
        return out
    high = [c.high for c in candles]
    low = [c.low for c in candles]
    bull = candles[1].close > candles[0].close
    af = af_start
    ep = high[0] if bull else low[0]
    sar = low[0] if bull else high[0]
    for i in range(1, n):
        sar = sar + af * (ep - sar)
        if bull:
            sar = min(sar, low[i - 1], low[i - 2] if i >= 2 else low[i - 1])
            if high[i] > ep:
                ep = high[i]
                af = min(af + af_inc, af_max)
            if low[i] < sar:
                bull = False
                sar = ep
                ep = low[i]
                af = af_start
        else:
            sar = max(sar, high[i - 1], high[i - 2] if i >= 2 else high[i - 1])
            if low[i] < ep:
                ep = low[i]
                af = min(af + af_inc, af_max)
            if high[i] > sar:
                bull = True
                sar = ep
                ep = high[i]
                af = af_start
        out[i] = sar
    return out


def cross(a: list[float | None], b: list[float | None], i: int) -> bool:
    """Pine ``ta.cross``: a crossover in either direction at bar ``i``."""
    return crossover(a, b, i) or crossunder(a, b, i)
