"""Spot, index and quarterly contract prices on the hourly grid."""

from __future__ import annotations

import pandas as pd

from .time import to_utc


def klines_hourly(
    raw: pd.DataFrame, cols: tuple[str, ...] = ("open", "high", "low", "close")
) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame(columns=list(cols), index=pd.DatetimeIndex([], tz="UTC", name="ts"))
    df = raw.copy()
    df["ts"] = to_utc(df["open_time"]).dt.floor("h")
    df = df.dropna(subset=["ts"]).drop_duplicates(subset=["ts"], keep="last").set_index("ts")
    out: pd.DataFrame = df[list(cols)].apply(pd.to_numeric, errors="coerce").sort_index()
    out.index.name = "ts"
    return out


def quarterly_long(
    per_symbol: dict[str, pd.DataFrame], expiries: dict[str, pd.Timestamp]
) -> pd.DataFrame:
    frames = []
    for sym, raw in per_symbol.items():
        k = klines_hourly(raw, ("close",))
        if k.empty:
            continue
        exp = expiries[sym]
        # Bars at/after the assumed expiry are kept (dte <= 0) so validation can detect a
        # wrong expiry hour; analytics drop them.
        f = pd.DataFrame(
            {
                "symbol": sym,
                "expiry_ts": exp,
                "close": k["close"].to_numpy(),
                "dte_days": -(pd.DatetimeIndex(k.index) - exp).total_seconds().to_numpy() / 86400.0,
            },
            index=k.index,
        )
        frames.append(f)
    if not frames:
        return pd.DataFrame(columns=["ts", "symbol", "expiry_ts", "close", "dte_days"])
    out = pd.concat(frames).reset_index().rename(columns={"index": "ts"})
    return out.sort_values(["symbol", "ts"]).reset_index(drop=True)
