from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from solcarry.analytics.runups import q_alpha, runup, runup_table, runups


def _spot(highs, closes):
    idx = pd.date_range("2025-01-01", periods=len(highs), freq="h", tz="UTC")
    return pd.DataFrame({"high": highs, "close": closes}, index=idx)


def test_known_spike():
    s = _spot([100, 100, 150, 100, 100, 100], [100] * 6)
    u = runup(s["high"], s["close"], 2)
    assert u.iloc[0] == pytest.approx(0.5)
    assert u.iloc[1] == pytest.approx(0.5)
    assert u.iloc[2] == pytest.approx(0.0)  # bar t itself excluded


def test_excludes_bar_t():
    s = _spot([200, 100, 100], [100, 100, 100])
    assert runup(s["high"], s["close"], 1).iloc[0] == pytest.approx(0.0)


def test_end_windows_nan():
    s = _spot([100, 110, 120, 130], [100] * 4)
    u = runup(s["high"], s["close"], 2)
    assert np.isnan(u.iloc[-1]) and np.isnan(u.iloc[-2])
    assert u.iloc[1] == pytest.approx(0.3)


def test_table_and_q():
    s = _spot(list(np.linspace(100, 200, 50)), list(np.linspace(100, 200, 50)))
    U = runups(s, [1, 5])
    t = runup_table(U, [0.95, 0.99])
    assert set(t["window_h"]) == {1, 5}
    assert q_alpha(t, 5, 0.99) >= q_alpha(t, 1, 0.99)
    with pytest.raises(KeyError):
        q_alpha(t, 24, 0.99)
