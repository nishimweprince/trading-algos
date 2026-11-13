"""Vectorised replay of the live IPDA reversal entry on 1-minute bid data.

What the live service does (src/service.py + src/strategy.py), and how it maps here:

* Every poll it rebuilds 3M buckets from closed 1M candles; the last bucket is the
  *forming* bar. RSI(14) is Wilder's RSI over bucket closes, with the forming bar's close
  equal to the latest closed minute's close. So the forming-bar RSI after minute ``j`` of
  bucket ``g`` is one Wilder step from bucket ``g-1``'s state using ``close_j - C[g-1]``.
* BUY when RSI[g-1] <= 25 and forming RSI > 25; SELL when RSI[g-1] >= 75 and forming
  RSI < 75. The first minute that triggers locks the bucket (SignalGate), so at most one
  entry per 3M bucket.
* The session gate uses wall-clock "now", which is the close of the triggering minute.
* Market order, SL/TP as distances anchored to the fill. Buy fills at ask, closes at bid;
  sell fills at bid, closes at ask. The data is bid, so ask = bid + spread.
* No flip logic, no position cap: every signal is an independent position (hedging).

Intrabar order is unknown on 1M bars. When a bar touches both SL and TP, ``same_bar``
decides ("sl" = pessimistic default, "tp" = optimistic bound).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

import numpy as np
import pandas as pd

MIN_NS = 60_000_000_000


# --------------------------------------------------------------------------- buckets
@dataclass(slots=True)
class Buckets:
    group: np.ndarray  # bucket index per minute
    close: np.ndarray  # bucket close
    high: np.ndarray
    low: np.ndarray
    last_minute: np.ndarray  # minute index of each bucket's last minute
    rsi: np.ndarray
    avg_up: np.ndarray
    avg_dn: np.ndarray
    atr: np.ndarray
    ema_fast: np.ndarray
    ema_slow: np.ndarray


def _wilder(values: np.ndarray, length: int) -> np.ndarray:
    """Pine ta.rma: SMA seed at index length-1, then alpha = 1/length."""
    out = np.full(values.shape, np.nan)
    if len(values) < length:
        return out
    a = 1.0 / length
    prev = values[:length].mean()
    out[length - 1] = prev
    v = values.tolist()
    res = out.tolist()
    for i in range(length, len(v)):
        prev = a * v[i] + (1 - a) * prev
        res[i] = prev
    return np.asarray(res)


def _ema(values: np.ndarray, length: int) -> np.ndarray:
    return pd.Series(values).ewm(span=length, adjust=False).mean().to_numpy()


def build_buckets(
    df: pd.DataFrame,
    tf: int = 3,
    rsi_len: int = 14,
    atr_len: int = 14,
    ema_fast: int = 50,
    ema_slow: int = 200,
) -> Buckets:
    ts_ns = df["ts"].dt.as_unit("ns").astype("int64").to_numpy()
    minute = ts_ns // MIN_NS
    day = minute // 1440
    bucket = day * 1440 + ((minute - day * 1440) // tf) * tf
    change = np.empty(len(bucket), dtype=bool)
    change[0] = True
    change[1:] = bucket[1:] != bucket[:-1]
    group = np.cumsum(change) - 1
    n_b = group[-1] + 1
    last_minute = np.empty(n_b, dtype=np.int64)
    last_minute[group] = np.arange(len(group))  # last write wins = last minute
    hi = np.maximum.reduceat(df["high"].to_numpy(), np.flatnonzero(change))
    lo = np.minimum.reduceat(df["low"].to_numpy(), np.flatnonzero(change))
    close = df["close"].to_numpy()[last_minute]

    diff = np.diff(close)
    up = _wilder(np.maximum(diff, 0.0), rsi_len)
    dn = _wilder(np.maximum(-diff, 0.0), rsi_len)
    avg_up = np.concatenate([[np.nan], up])
    avg_dn = np.concatenate([[np.nan], dn])
    rsi = _rsi_from(avg_up, avg_dn)

    prev_close = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum(hi - lo, np.maximum(abs(hi - prev_close), abs(lo - prev_close)))
    tr[0] = hi[0] - lo[0]
    atr = _wilder(tr, atr_len)
    return Buckets(
        group,
        close,
        hi,
        lo,
        last_minute,
        rsi,
        avg_up,
        avg_dn,
        atr,
        _ema(close, ema_fast),
        _ema(close, ema_slow),
    )


def _rsi_from(up: np.ndarray, dn: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        r = 100.0 - 100.0 / (1.0 + up / dn)
    r = np.where(dn == 0, 100.0, r)
    r = np.where((up == 0) & (dn != 0), 0.0, r)
    return np.where(np.isnan(up) | np.isnan(dn), np.nan, r)


# --------------------------------------------------------------------------- signals
def reversal_signals(
    df: pd.DataFrame,
    b: Buckets,
    rsi_len: int = 14,
    oversold: float = 25.0,
    overbought: float = 75.0,
    mode: str = "forming",
) -> pd.DataFrame:
    """Entries as the live service would emit them. One row per locked bucket.

    mode="forming": first minute inside the bucket whose forming RSI crosses (live).
    mode="close":   only the bucket's final minute (close-confirmed, TradingView alert).
    """
    g = b.group
    c = df["close"].to_numpy()
    valid = g >= 1
    gp = np.where(valid, g - 1, 0)
    d = c - b.close[gp]
    a = 1.0 / rsi_len
    up = a * np.maximum(d, 0) + (1 - a) * b.avg_up[gp]
    dn = a * np.maximum(-d, 0) + (1 - a) * b.avg_dn[gp]
    rsi_f = _rsi_from(up, dn)
    rsi_prev = b.rsi[gp]
    buy = valid & (rsi_prev <= oversold) & (rsi_f > oversold)
    sell = valid & (rsi_prev >= overbought) & (rsi_f < overbought)
    hit = buy | sell
    if mode == "close":
        is_last = np.zeros(len(g), dtype=bool)
        is_last[b.last_minute] = True
        hit &= is_last
    idx = np.flatnonzero(hit)
    # first triggering minute per bucket (SignalGate lock)
    keep = np.concatenate([[True], g[idx][1:] != g[idx][:-1]]) if len(idx) else idx
    idx = idx[keep]
    out = pd.DataFrame(
        {
            "i": idx,
            "group": g[idx],
            "dir": np.where(buy[idx], 1, -1),
            "rsi": rsi_f[idx],
            "atr": b.atr[g[idx] - 1],
            "ema_fast": b.ema_fast[g[idx] - 1],
            "ema_slow": b.ema_slow[g[idx] - 1],
            "minute_in_bucket": idx - np.where(g[idx] >= 1, b.last_minute[g[idx] - 1], -1) - 1,
        }
    )
    out["ts"] = (df["ts"].iloc[idx] + pd.Timedelta(minutes=1)).to_numpy()  # poll time
    out["ts"] = pd.to_datetime(out["ts"], utc=True)
    return out


# --------------------------------------------------------------------------- sessions
SESSIONS = {
    "tokyo": ("Asia/Tokyo", time(9, 0), time(18, 0)),
    "new_york": ("America/New_York", time(8, 0), time(17, 0)),
}


def session_labels(ts: pd.Series, names: list[str]) -> np.ndarray:
    """First matching session name per timestamp ('' = outside). Mirrors sessions.py."""
    out = np.full(len(ts), "", dtype=object)
    for name in names:
        tz, start, end = SESSIONS[name]
        local = ts.dt.tz_convert(tz)
        mins = local.dt.hour * 60 + local.dt.minute
        inside = (
            (local.dt.weekday < 5).to_numpy()
            & (mins >= start.hour * 60 + start.minute).to_numpy()
            & (mins < end.hour * 60 + end.minute).to_numpy()
        )
        out = np.where((out == "") & inside, name, out)
    return out


# --------------------------------------------------------------------------- exits
@dataclass(slots=True)
class ExitParams:
    spread: float = 0.20
    commission: float = 0.0  # $ per trade per 1 oz (0.01 lot)
    same_bar: str = "sl"
    be_trigger: float | None = None  # move SL to entry once MFE >= this distance
    max_hold: int | None = None  # minutes
    extra: dict = field(default_factory=dict)


def simulate(
    df: pd.DataFrame, sig: pd.DataFrame, sl: np.ndarray, tp: np.ndarray, p: ExitParams
) -> pd.DataFrame:
    OP = df["open"].to_numpy()
    H = df["high"].to_numpy()
    L = df["low"].to_numpy()
    C = df["close"].to_numpy()
    n = len(C)
    s = p.spread
    rows = []
    for k, (e, d) in enumerate(zip(sig["i"].to_numpy(), sig["dir"].to_numpy(), strict=True)):
        entry = C[e] + s if d == 1 else C[e]
        res = _walk(OP, H, L, C, n, e, d, entry, sl[k], tp[k], s, p)
        if res is None:
            continue
        x, px, reason = res
        pnl = (px - entry) if d == 1 else (entry - px)
        rows.append((k, x, px, entry, pnl - p.commission, reason))
    r = pd.DataFrame(rows, columns=["k", "exit_i", "exit_px", "entry_px", "pnl", "reason"])
    out = sig.iloc[r["k"].to_numpy()].reset_index(drop=True)
    out = pd.concat([out, r.drop(columns="k")], axis=1)
    out["sl_dist"] = sl[r["k"].to_numpy()]
    out["r"] = out["pnl"] / out["sl_dist"]
    out["exit_ts"] = (df["ts"].iloc[out["exit_i"].to_numpy()] + pd.Timedelta(minutes=1)).to_numpy()
    out["exit_ts"] = pd.to_datetime(out["exit_ts"], utc=True)
    out["hold_min"] = (out["exit_ts"] - out["ts"]).dt.total_seconds() / 60
    return out


def _walk(OP, H, L, C, n, e, d, entry, sl, tp, s, p: ExitParams):
    """Return (exit_index, exit_price_on_close_side, reason) or None if never closed."""
    sl_px = entry - d * sl
    tp_px = entry + d * tp
    be_armed = p.be_trigger is not None
    start = e + 1
    end_cap = n if p.max_hold is None else min(n, e + 1 + p.max_hold)
    chunk = 240
    while start < end_cap:
        stop = min(end_cap, start + chunk)
        h, lo = H[start:stop], L[start:stop]
        if d == 1:  # closes at bid
            hit_sl = lo <= sl_px
            hit_tp = h >= tp_px
            mfe_hit = (h - entry >= p.be_trigger) if be_armed else None
        else:  # closes at ask
            hit_sl = h + s >= sl_px
            hit_tp = lo + s <= tp_px
            mfe_hit = (entry - (lo + s) >= p.be_trigger) if be_armed else None
        any_hit = hit_sl | hit_tp
        first = int(np.argmax(any_hit)) if any_hit.any() else None
        if be_armed and mfe_hit.any():
            fb = int(np.argmax(mfe_hit))
            if first is None or fb < first:
                # BE armed on bar fb without SL/TP that bar; SL -> entry from next bar
                sl_px = entry
                be_armed = False
                start = start + fb + 1
                continue
        if first is not None:
            j = start + first
            close_side_open = OP[j] if d == 1 else OP[j] + s
            # gap through a level at the open fills at the open
            if d * (close_side_open - sl_px) <= 0:
                return j, close_side_open, "sl" if sl_px != entry else "be"
            if d * (close_side_open - tp_px) >= 0:
                return j, close_side_open, "tp"
            if hit_sl[first] and hit_tp[first]:
                take_tp = p.same_bar == "tp"
            else:
                take_tp = bool(hit_tp[first])
            if take_tp:
                return j, tp_px, "tp"
            return j, sl_px, "sl" if sl_px != entry else "be"
        start = stop
        chunk = min(chunk * 4, 20_000)
    if p.max_hold is not None and end_cap < n:
        j = end_cap - 1
        px = C[j] if d == 1 else C[j] + s
        return j, px, "time"
    return None
