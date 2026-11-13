import io
import zipfile
from datetime import date

import numpy as np
import pandas as pd

from crypto_intraday.core import day_table, holm, load_5m, r1, r2, summarize
from crypto_intraday.fetch import months


def _px(start: str, end: str, fn) -> pd.Series:
    idx = pd.date_range(start, end, freq="5min", tz="UTC")
    return pd.Series([fn(t) for t in idx], index=idx, dtype=float)


def test_months():
    assert months("2025-11", "2026-02") == ["2025-11", "2025-12", "2026-01", "2026-02"]


def test_load_handles_header_ms_and_us(tmp_path):
    d = tmp_path / "BTCUSDT"
    d.mkdir()
    rows_ms = "1735689600000,100,101,99,100.5,1,0,0,0,0,0,0\n"  # 2025-01-01 00:00 UTC
    rows_us = ("open_time,open,high,low,close,volume,a,b,c,d,e,f\n"
               "1735689900000000,101,1,1,1,1,0,0,0,0,0,0\n")
    for name, body in (("BTCUSDT-5m-2025-01.zip", rows_ms), ("BTCUSDT-5m-2025-02.zip", rows_us)):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(name.replace(".zip", ".csv"), body)
        (d / name).write_bytes(buf.getvalue())
    bars = load_5m("BTCUSDT", tmp_path)
    assert list(bars.index) == [pd.Timestamp("2025-01-01 00:00", tz="UTC"),
                                pd.Timestamp("2025-01-01 00:05", tz="UTC")]
    assert list(bars.open) == [100.0, 101.0]


def test_r1_and_r2_signs():
    def fn(t):
        if t.day == 1 and t.hour == 0 and 0 < t.minute <= 30:
            return 100.5  # rises in the first half-hour of day 1
        return 101.0 if (t.day == 2) else 100.0
    px = _px("2025-01-01 00:00", "2025-01-02 01:00", fn)
    assert np.isclose(r1(px, date(2025, 1, 1)), 100.0)  # 100 -> 101 = +100 bp
    sig, tr = r2(px, date(2025, 1, 1))
    assert sig > 0 and np.isclose(tr, 100.0)  # long 23:30 (100) -> 00:00 (101)


def test_r2_short_when_first_half_hour_falls():
    def fn(t):
        if t.day == 1 and t.hour == 0 and 0 < t.minute <= 30:
            return 99.5
        return 99.0 if t.day == 2 else 100.0
    px = _px("2025-01-01 00:00", "2025-01-02 01:00", fn)
    sig, tr = r2(px, date(2025, 1, 1))
    assert sig < 0 and np.isclose(tr, 100.0)  # short 100 -> 99


def test_day_table_and_costs():
    px = _px("2025-01-06 00:00", "2025-01-08 01:00", lambda t: 100.0)
    t = day_table(px, date(2025, 1, 6), date(2025, 1, 7))
    assert np.allclose(t.r1, 0)
    assert np.isclose(summarize(t.r1, 10.0)["mean_bp"], -10.0)


def test_holm():
    assert holm({"a": 0.01, "b": 0.02}) == {"a": True, "b": True}
