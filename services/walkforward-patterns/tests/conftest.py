import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

DATA = os.environ.get("WF_DATA")
REFERENCE = os.environ.get("WF_REFERENCE")


def synthetic_m1(days: int = 90, seed: int = 7, start: str = "2024-01-07 18:00") -> pd.DataFrame:
    """Random-walk M1 bid bars on the research clock: Sun 18:00 -> Fri 17:00, daily 17:00-18:00 break, random gaps."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=days * 1440, freq="1min")
    m = idx.hour * 60 + idx.minute
    dow = idx.dayofweek
    live = ~((m >= 17 * 60) & (m < 18 * 60))
    live &= ~(dow == 5) & ~((dow == 4) & (m >= 17 * 60)) & ~((dow == 6) & (m < 18 * 60))
    live &= rng.random(len(idx)) > 0.02  # missing minutes
    idx = idx[live]
    ret = rng.standard_t(4, len(idx)) * 0.15
    c = 2000 + np.cumsum(ret)
    o = np.r_[c[0], c[:-1]] + rng.normal(0, 0.02, len(idx))
    h = np.maximum(o, c) + np.abs(rng.normal(0, 0.12, len(idx)))
    lo = np.minimum(o, c) - np.abs(rng.normal(0, 0.12, len(idx)))
    return pd.DataFrame({"o": o, "h": h, "l": lo, "c": c}, index=idx)


@pytest.fixture(scope="session")
def m1() -> pd.DataFrame:
    return synthetic_m1()


@pytest.fixture(scope="session")
def real_m1():
    if not DATA:
        pytest.skip("WF_DATA not set")
    from lib import load_m1

    return load_m1(Path(DATA) / "XAUUSD_m1.npz").loc["2023-01-01":"2023-12-31"]


def research_minutes(idx: pd.DatetimeIndex) -> np.ndarray:
    return idx.values.astype("datetime64[m]").astype(np.int64)
