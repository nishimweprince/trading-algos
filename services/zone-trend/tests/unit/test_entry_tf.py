"""The entry timeframe is configurable: 15m bars drive the core end to end.

The default config stays on 1H; ``proto-15m.yaml`` steps the same core on
15m bars with 4H/1D attached at matching closes and time-equivalent bar
windows (the FSM cooldown scales to 96 bars = 24h).
"""

from __future__ import annotations

import numpy as np
import pytest
from conftest import DAY, HOUR, klines, put

from zone_trend.backtest.tape import zone_tape
from zone_trend.config import DEFAULT_PATH, Config, load_config
from zone_trend.core.symbol import SymbolCore
from zone_trend.types import TF, Bar

QUARTER = 900_000


@pytest.fixture(scope="module")
def cfg15() -> Config:
    return load_config(DEFAULT_PATH.parent / "proto-15m.yaml")


def test_proto_config_is_15m_with_time_equivalent_windows(cfg15):
    assert cfg15.timeframes.entry == "15m"
    assert cfg15.entries.retest_window_bars == 192
    assert cfg15.entries.limit_valid_bars == 12
    assert cfg15.exits.time_stop_bars == 288


def test_fsm_cooldown_is_24h_in_entry_bars(cfg15):
    assert SymbolCore("T", load_config()).reset_bars == 24
    assert SymbolCore("T", cfg15).reset_bars == 96


def test_htf_bars_attach_at_the_matching_15m_close(cfg15):
    core = SymbolCore("T", cfg15)
    close = 1_700_000_000_000 // (4 * HOUR) * (4 * HOUR) + 4 * HOUR - 1
    m15 = Bar(close - QUARTER + 1, close, 100.0, 101.0, 99.0, 100.5, 10.0)
    h4 = Bar(close - 4 * HOUR + 1, close, 99.0, 102.0, 98.0, 100.5, 40.0)
    core.on_close(m15, h4, None)
    assert len(core.series[TF.M15]) == 1
    assert len(core.series[TF.H4]) == 1
    assert len(core.series[TF.H1]) == 0
    with pytest.raises(ValueError, match="must close with the entry bar"):
        core.on_close(m15, Bar(h4.open_time, h4.close_time + 1, 0, 0, 0, 0, 0), None)


def test_15m_tape_replays_quarter_close_times(paths, cfg15):
    put(paths, "klines", "TESTUSDT", "15m", klines("2021-01-01", 30 * 96, QUARTER))
    put(paths, "klines", "TESTUSDT", "4h", klines("2021-01-01", 30 * 6, 4 * HOUR))
    put(paths, "klines", "TESTUSDT", "1d", klines("2021-01-01", 30, DAY))
    tape = zone_tape("TESTUSDT", paths, cfg15)
    assert len(tape.close_time) == 30 * 96
    assert set(np.diff(tape.close_time)) == {QUARTER}
    assert np.isfinite(tape.atr_entry[100:]).all()
    assert len(tape.d1_trend) == len(tape.close_time)
