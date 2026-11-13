from datetime import date

import numpy as np
import pandas as pd

from fx_fix_flows.core import day_table, holm, legs_for_day, load_m1, strategy_net


def test_window_clock_times_winter_and_summer():
    w = legs_for_day(date(2025, 1, 15), "USDJPY")  # EST, Tokyo UTC+9
    assert w["preT"][0] == pd.Timestamp("2025-01-14 22:00", tz="UTC")  # 17:00 EST
    assert w["preT"][1] == pd.Timestamp("2025-01-15 00:55", tz="UTC")  # 09:55 JST
    assert w["preE"][1] == pd.Timestamp("2025-01-15 13:15", tz="UTC")  # 14:15 CET
    assert w["postL"][0] == pd.Timestamp("2025-01-15 16:00", tz="UTC")  # 16:00 GMT
    s = legs_for_day(date(2025, 7, 15), "USDJPY")  # EDT / CEST / BST
    assert s["preT"][0] == pd.Timestamp("2025-07-14 21:00", tz="UTC")
    assert s["preE"][1] == pd.Timestamp("2025-07-15 12:15", tz="UTC")
    assert s["postL"][0] == pd.Timestamp("2025-07-15 15:00", tz="UTC")


def test_positions_follow_usd_and_local_currency():
    j, a = legs_for_day(date(2025, 3, 5), "USDJPY"), legs_for_day(date(2025, 3, 5), "AUDUSD")
    assert (j["preT"][2], j["postT"][2], a["preT"][2], a["postT"][2]) == (1, -1, -1, 1)
    assert (j["br_local"][2], j["br_us"][2], a["br_local"][2], a["br_us"][2]) == (1, -1, -1, 1)


def test_load_m1_reads_ny_time_with_dst(tmp_path):
    d = tmp_path / "USDJPY"
    d.mkdir()
    (d / "DAT_ASCII_USDJPY_M1_2025.csv").write_text("20250714 170000;1;1;1;150.0;0\n")
    m1 = load_m1("USDJPY", tmp_path)
    assert m1.index[0] == pd.Timestamp("2025-07-14 21:00", tz="UTC")


def test_tokyo_reversal_returns_and_costs():
    d = date(2025, 1, 15)
    idx = pd.date_range("2025-01-14 21:00", "2025-01-15 23:00", freq="1min", tz="UTC")
    g = pd.Series(150.0, index=idx)
    g[idx >= pd.Timestamp("2025-01-15 00:55", tz="UTC")] = 150.30  # USD up into Tokyo fix
    g[idx >= pd.Timestamp("2025-01-15 07:00", tz="UTC")] = 150.15  # gives half back by 02:00 ET
    tbl = day_table(g, "USDJPY", d, d)
    assert np.isclose(tbl.loc[d, "preT"], 0.30 / 150 * 1e4)
    assert np.isclose(tbl.loc[d, "postT"], 0.15 / 150.30 * 1e4)
    gross = strategy_net(tbl, "USDJPY", "T", 0.0).loc[d]
    net = strategy_net(tbl, "USDJPY", "T", 0.5).loc[d]
    assert np.isclose(gross - net, 0.005 / 150 * 1e4 + 0.005 / 150.30 * 1e4)


def test_holm():
    assert holm({"a": 0.01, "b": 0.04}) == {"a": True, "b": True}
    assert holm({"a": 0.03, "b": 0.04}) == {"a": False, "b": False}
