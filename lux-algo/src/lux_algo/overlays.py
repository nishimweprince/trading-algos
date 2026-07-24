"""Faithful ports of the LuxAlgo overlay components used as confluence filters.

Each ``*_dir`` function returns a per-bar direction series: ``1`` bull, ``-1`` bear,
``0`` neutral. ``tp_points`` and ``reversals`` return the counter-trend markers used as
entry vetoes. Internal parameters are hard-coded to the values in file.txt so the ports
match the indicator exactly.
"""

from __future__ import annotations

import math

from .candles import Candle
from .indicators import (
    atr,
    change,
    cross,
    crossover,
    crossunder,
    ema,
    macd,
    psar,
    rma,
    sma,
    wilder_ma,
)


def _closes(candles: list[Candle]) -> list[float]:
    return [c.close for c in candles]


def _sign_series(a: list[float], b: list[float]) -> list[int]:
    """Per-bar direction: 1 where a>b, -1 where a<b, 0 where equal."""
    return [1 if a[i] > b[i] else -1 if a[i] < b[i] else 0 for i in range(len(a))]


# --- Range Filter (file.txt:202-325); mov_src='Close', Type 2, qty=2.618, per=14 ---


def range_filter(
    candles: list[Candle], rng_per: int = 14, rng_qty: float = 2.618, smooth_per: int = 27
) -> tuple[list[float], list[int]]:
    mid = _closes(candles)  # h_val = l_val = close, so (h_val + l_val) / 2 = close
    n = len(mid)
    ac_src = [0.0] + [abs(mid[i] - mid[i - 1]) for i in range(1, n)]
    ac = ema(ac_src, rng_per)  # Cond_EMA(|Δmid|, 1, n) with cond always true
    rng = [rng_qty * ac[i] for i in range(n)]
    r = ema(rng, smooth_per)  # rng_smooth (smooth_range = true)

    filt = [0.0] * n
    prev_filt = mid[0]
    for i in range(n):
        ri = r[i]
        cur = prev_filt
        if ri > 0:
            if mid[i] >= prev_filt + ri:  # Type 2, h = mid
                cur = prev_filt + math.floor(abs(mid[i] - prev_filt) / ri) * ri
            if mid[i] <= prev_filt - ri:  # Type 2, l = mid
                cur = prev_filt - math.floor(abs(mid[i] - prev_filt) / ri) * ri
        filt[i] = cur
        prev_filt = cur

    fdir = [0] * n
    cur_dir = 0
    for i in range(1, n):
        if filt[i] > filt[i - 1]:
            cur_dir = 1
        elif filt[i] < filt[i - 1]:
            cur_dir = -1
        fdir[i] = cur_dir
    return filt, fdir


def range_filter_dir(candles: list[Candle]) -> list[int]:
    return range_filter(candles)[1]


# --- SuperIchi (file.txt:328-380) ---


def _ichi_avg(candles: list[Candle], srcc: list[float], length: int, mult: float) -> list[float]:
    atr_v = atr(candles, length)
    n = len(candles)
    hl2 = [(candles[i].high + candles[i].low) / 2 for i in range(n)]
    upper = [0.0] * n
    lower = [0.0] * n
    os = [0] * n
    spt = [0.0] * n
    mx = [0.0] * n
    mn = [0.0] * n
    out = [0.0] * n
    for i in range(n):
        a = (atr_v[i] * mult) if atr_v[i] is not None else 0.0
        up = hl2[i] + a
        dn = hl2[i] - a
        if i == 0:
            upper[i] = up
            lower[i] = dn
            os[i] = 0
            spt[i] = upper[i]
            mx[i] = srcc[i]
            mn[i] = srcc[i]
            out[i] = srcc[i]
            continue
        upper[i] = min(up, upper[i - 1]) if srcc[i - 1] < upper[i - 1] else up
        lower[i] = max(dn, lower[i - 1]) if srcc[i - 1] > lower[i - 1] else dn
        if srcc[i] > upper[i]:
            os[i] = 1
        elif srcc[i] < lower[i]:
            os[i] = 0
        else:
            os[i] = os[i - 1]
        spt[i] = lower[i] if os[i] == 1 else upper[i]
        crossed = cross(srcc, spt, i)
        if crossed:
            mx[i] = max(srcc[i], mx[i - 1])
            mn[i] = min(srcc[i], mn[i - 1])
        else:
            mx[i] = max(srcc[i], mx[i - 1]) if os[i] == 1 else spt[i]
            mn[i] = min(srcc[i], mn[i - 1]) if os[i] == 0 else spt[i]
        out[i] = (mx[i] + mn[i]) / 2
    return out


def superichi_dir(candles: list[Candle]) -> list[int]:
    closes = _closes(candles)
    tenkan = _ichi_avg(candles, closes, 6, 2.0)
    kijun = _ichi_avg(candles, closes, 5, 3.0)
    return _sign_series(tenkan, kijun)


# --- TBO (file.txt:382-429); default (non-custom) lengths 20/40 for fast/medium ---


def tbo_dir(candles: list[Candle]) -> list[int]:
    closes = _closes(candles)
    fast = ema(closes, 20)
    medium = ema(closes, 40)
    return _sign_series(fast, medium)


# --- Smart Trail (file.txt:431-501); modified TR, ATRPeriod=13, ATRFactor=4 ---


def smart_trail_dir(
    candles: list[Candle], atr_period: int = 13, atr_factor: float = 4.0
) -> list[int]:
    n = len(candles)
    h = [c.high for c in candles]
    lo = [c.low for c in candles]
    c = [c.close for c in candles]
    hl = [h[i] - lo[i] for i in range(n)]
    sma_hl = sma(hl, atr_period)

    tr: list[float] = []
    for i in range(n):
        if i == 0:
            tr.append(h[i] - lo[i])
            continue
        pc, ph, pl = c[i - 1], h[i - 1], lo[i - 1]
        base = 1.5 * sma_hl[i] if sma_hl[i] is not None else (h[i] - lo[i])
        hilo = min(h[i] - lo[i], base)
        href = (h[i] - pc) if lo[i] <= ph else (h[i] - pc - 0.5 * (lo[i] - ph))
        lref = (pc - lo[i]) if h[i] >= pl else (pc - lo[i] - 0.5 * (pl - h[i]))
        tr.append(max(hilo, href, lref))

    loss = [atr_factor * w for w in wilder_ma(tr, atr_period)]
    up = [c[i] - loss[i] for i in range(n)]
    dn = [c[i] + loss[i] for i in range(n)]

    trend_up = [0.0] * n
    trend_down = [0.0] * n
    trend = [1] * n
    for i in range(n):
        if i == 0:
            trend_up[i] = up[i]
            trend_down[i] = dn[i]
            trend[i] = 1
            continue
        trend_up[i] = max(up[i], trend_up[i - 1]) if c[i - 1] > trend_up[i - 1] else up[i]
        trend_down[i] = min(dn[i], trend_down[i - 1]) if c[i - 1] < trend_down[i - 1] else dn[i]
        if c[i] > trend_down[i - 1]:
            trend[i] = 1
        elif c[i] < trend_up[i - 1]:
            trend[i] = -1
        else:
            trend[i] = trend[i - 1]
    return trend


# --- HA Market Bias (file.txt:148-200); ha_len=ha_len2=100 ---


def ha_bias_dir(candles: list[Candle], ha_len: int = 100, ha_len2: int = 100) -> list[int]:
    n = len(candles)
    o = ema([c.open for c in candles], ha_len)
    cl = ema([c.close for c in candles], ha_len)
    hi = ema([c.high for c in candles], ha_len)
    low = ema([c.low for c in candles], ha_len)

    haclose = [(o[i] + hi[i] + low[i] + cl[i]) / 4 for i in range(n)]
    xhaopen = [(o[i] + cl[i]) / 2 for i in range(n)]
    haopen = [0.0] * n
    for i in range(n):
        haopen[i] = xhaopen[i] if i == 0 else (xhaopen[i - 1] + haclose[i - 1]) / 2

    o2 = ema(haopen, ha_len2)
    c2 = ema(haclose, ha_len2)
    return [1 if c2[i] > o2[i] else -1 if c2[i] < o2[i] else 0 for i in range(n)]


# --- MACD candle-color regime (file.txt:680-738); 12/26/9 ---


def macd_dir(candles: list[Candle]) -> list[int]:
    macd_line, _signal, hist = macd(_closes(candles), 12, 26, 9)
    out = [0] * len(candles)
    for i in range(len(candles)):
        if macd_line[i] > 0 and hist[i] > 0:
            out[i] = 1
        elif macd_line[i] < 0 and hist[i] < 0:
            out[i] = -1
    return out


# --- PSAR (file.txt:66,78); bull if psar < ocAvg ---


def psar_dir(candles: list[Candle]) -> list[int]:
    sar = psar(candles)
    out = [0] * len(candles)
    for i in range(len(candles)):
        if sar[i] is None:
            continue
        oc_avg = (candles[i].open + candles[i].close) / 2
        out[i] = 1 if sar[i] < oc_avg else -1
    return out


# --- Counter-trend markers used as vetoes ---


def tp_points(candles: list[Candle], qual: int = 13, length: int = 40) -> list[int]:
    """Port of ``lele`` (file.txt:98-121) for the major TP points. Returns per bar:
    -1 = major SELL TP (exhaustion top), 1 = major BUY TP (exhaustion bottom), else 0."""
    n = len(candles)
    closes = _closes(candles)
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    opens = [c.open for c in candles]
    out = [0] * n
    bindex = 0
    sindex = 0
    for i in range(n):
        ret = 0
        if i >= 4:
            if closes[i] > closes[i - 4]:
                bindex += 1
            if closes[i] < closes[i - 4]:
                sindex += 1
        if i >= length - 1:
            highest = max(highs[i - length + 1 : i + 1])
            lowest = min(lows[i - length + 1 : i + 1])
            if bindex > qual and closes[i] < opens[i] and highs[i] >= highest:
                bindex = 0
                ret = -1
            if sindex > qual and closes[i] > opens[i] and lows[i] <= lowest:
                sindex = 0
                ret = 1
        out[i] = ret
    return out


def reversals(
    candles: list[Candle], length: int = 14, overbought: float = 75, oversold: float = 25
) -> tuple[list[bool], list[bool]]:
    """Port of the Reversal signals (file.txt:503-517). Returns (revup, revdn) per bar."""
    closes = _closes(candles)
    ch = change(closes)
    upward = rma([max(x, 0.0) for x in ch], length)
    downward = rma([max(-x, 0.0) for x in ch], length)
    n = len(candles)
    source: list[float | None] = [None] * n
    for i in range(n):
        up, dn = upward[i], downward[i]
        if up is None or dn is None:
            continue
        if dn == 0:
            source[i] = 100.0
        elif up == 0:
            source[i] = 0.0
        else:
            source[i] = 100.0 - (100.0 / (1.0 + up / dn))
    ob: list[float | None] = [overbought] * n
    osv: list[float | None] = [oversold] * n
    revup = [crossover(source, osv, i) for i in range(n)]
    revdn = [crossunder(source, ob, i) for i in range(n)]
    return revup, revdn
