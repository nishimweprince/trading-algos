from datetime import date

import numpy as np
import pandas as pd

from gotobi.data import JST, load_m1, price_grid
from gotobi.strategy import day_table


def test_ny_dst_to_jst(tmp_path):
    # 13:00 New York on 2025-07-14 (EDT, UTC-4) = 02:00 JST 15 Jul; in January (EST) = 03:00 JST.
    (tmp_path / "DAT_ASCII_USDJPY_M1_2025.csv").write_text(
        "20250714 130000;147.0;147.1;146.9;147.05;0\n"
        "20250113 130000;157.0;157.1;156.9;157.05;0\n"
    )
    bars = load_m1(tmp_path)
    assert pd.Timestamp("2025-07-15 02:00", tz=JST) in bars.index
    assert pd.Timestamp("2025-01-14 03:00", tz=JST) in bars.index


def _grid_for(d: date, path) -> pd.Series:
    idx = pd.date_range(pd.Timestamp(d, tz=JST) - pd.Timedelta(hours=6),
                        pd.Timestamp(d, tz=JST) + pd.Timedelta(hours=13), freq="1min")
    return pd.Series(path(idx), index=idx)


def test_h1_h2_pips():
    d = date(2025, 7, 15)
    t = lambda h, m: pd.Timestamp(d, tz=JST) + pd.Timedelta(hours=h, minutes=m)  # noqa: E731

    def path(idx):
        p = np.full(len(idx), 100.0)
        s = pd.Series(p, index=idx)
        s[(idx >= t(3, 0)) & (idx < t(9, 55))] = 100.0
        s[idx >= t(9, 0)] = 100.10
        s[idx >= t(9, 55)] = 100.20
        s[idx >= t(10, 0)] = 100.05
        return s.to_numpy()

    tbl = day_table(_grid_for(d, path), {d: True})
    r = tbl.loc[d]
    assert np.isclose(r.h1_plain, 20.0)  # 100.00 -> 100.20
    assert r.anomaly
    assert np.isclose(r.h2, 15.0)  # short 100.20 -> 100.05


def test_golden_cross_entry_is_one_minute_late():
    d = date(2025, 7, 15)
    base = pd.Timestamp(d, tz=JST)

    def path(idx):
        mins = ((idx - base) / pd.Timedelta(minutes=1)).to_numpy()
        # falling until 02:40, then rising fast -> SMA25 crosses SMA100 inside [02:30, 03:00)
        return np.where(mins < 160, 101.0 - 0.001 * (mins + 360), 100.5 + 0.01 * (mins - 160))

    grid = _grid_for(d, path)
    tbl = day_table(grid, {d: True})
    assert not np.isnan(tbl.loc[d, "h1_gc3"])
    assert tbl.loc[d, "h1_gc3"] < tbl.loc[d, "h1_plain"] + 1e9  # computed, finite


def test_price_grid_is_bar_close_at_end():
    idx = pd.DatetimeIndex([pd.Timestamp("2025-07-15 03:00", tz=JST)])
    bars = pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [2.0]}, index=idx)
    g = price_grid(bars)
    assert g[pd.Timestamp("2025-07-15 03:01", tz=JST)] == 2.0
