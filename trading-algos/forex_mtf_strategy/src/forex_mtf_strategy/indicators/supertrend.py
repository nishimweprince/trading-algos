from __future__ import annotations

import pandas as pd
import pandas_ta as ta


def supertrend_direction(
    df: pd.DataFrame, *, length: int = 10, multiplier: float = 3.0
) -> pd.Series:
    """
    Return Supertrend direction: 1 (bullish) or -1 (bearish).
    """
    st = ta.supertrend(df["high"], df["low"], df["close"], length=length, multiplier=multiplier)
    if st is None or st.empty:
        raise ValueError("pandas-ta supertrend returned empty result")

    # Typical name: SUPERTd_10_3.0
    expected = f"SUPERTd_{length}_{multiplier}"
    if expected in st.columns:
        out = st[expected]
    else:
        candidates = [c for c in st.columns if c.startswith(f"SUPERTd_{length}_")]
        if not candidates:
            raise ValueError(f"Could not find Supertrend direction column in: {list(st.columns)}")
        out = st[candidates[0]]

    out.name = "supertrend_direction"
    return out.astype("float").astype("Int64")

