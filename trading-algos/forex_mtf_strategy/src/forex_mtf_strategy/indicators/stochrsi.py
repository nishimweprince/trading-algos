from __future__ import annotations

import pandas as pd
import pandas_ta as ta


def stochrsi_kd(
    close: pd.Series,
    *,
    length: int = 14,
    rsi_length: int = 14,
    k: int = 3,
    d: int = 3,
) -> tuple[pd.Series, pd.Series]:
    """
    TradingView-like StochRSI K and D series (0-100).
    """
    out = ta.stochrsi(close, length=length, rsi_length=rsi_length, k=k, d=d)
    if out is None or out.empty:
        raise ValueError("pandas-ta stochrsi returned empty result")

    k_name = f"STOCHRSIk_{length}_{rsi_length}_{k}_{d}"
    d_name = f"STOCHRSId_{length}_{rsi_length}_{k}_{d}"
    if k_name not in out.columns or d_name not in out.columns:
        # Fall back to first matching columns
        k_candidates = [c for c in out.columns if c.startswith("STOCHRSIk_")]
        d_candidates = [c for c in out.columns if c.startswith("STOCHRSId_")]
        if not k_candidates or not d_candidates:
            raise ValueError(f"Could not find StochRSI columns in: {list(out.columns)}")
        k_name = k_candidates[0]
        d_name = d_candidates[0]

    k_ser = out[k_name].rename("stochrsi_k")
    d_ser = out[d_name].rename("stochrsi_d")
    return k_ser, d_ser

