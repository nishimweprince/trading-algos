"""The entry timeframe is configurable: 15m bars drive the core end to end.

The default config stays on 1H; ``proto-15m.yaml`` steps the same core on
15m bars with 4H/1D attached at matching closes. Entry-bar windows (retest,
time stop, FSM cooldown) scale to the entry TF in code, so the YAML keeps
the 1H values.
"""

from __future__ import annotations

import numpy as np
import pytest
from conftest import DAY, HOUR, klines, put

from zone_trend.backtest.tape import zone_tape
from zone_trend.config import DEFAULT_PATH, Config, Timeframes, load_config
from zone_trend.core.symbol import SymbolCore
from zone_trend.types import TF, Bar


def ladder_cfg() -> Config:
    return load_config().model_copy(
        update={"timeframes": Timeframes(zones=["4h", "1h"], trend="1h", entry="15m")}
    )


QUARTER = 900_000


@pytest.fixture(scope="module")
def cfg15() -> Config:
    return load_config(DEFAULT_PATH.parent / "proto-15m.yaml")


def test_proto_config_is_15m_with_1h_window_values(cfg15):
    assert cfg15.timeframes.entry == "15m"
    assert cfg15.entries.retest_window_bars == 48
    assert cfg15.entries.limit_valid_bars == 3
    assert cfg15.exits.time_stop_bars == 72
    assert SymbolCore("T", cfg15).entry_scale == 4


def test_core_ladder_follows_configured_timeframes():
    core = SymbolCore("T", ladder_cfg())
    assert core.slow_tf is TF.H4 and core.fast_tf is TF.H1
    assert core.entry_tf is TF.M15 and core.entry_scale == 4
    assert set(core.structure) == {TF.H4, TF.H1}
    assert core.trend is core.structure[TF.H1].trend
    assert core.zones.slow_tf is TF.H4 and core.zones.fast_tf is TF.H1


def test_fast_and_slow_bars_attach_on_the_ladder():
    core = SymbolCore("T", ladder_cfg())
    close = 1_700_000_000_000 // HOUR * HOUR + HOUR - 1
    m15 = Bar(close - QUARTER + 1, close, 100.0, 101.0, 99.0, 100.5, 10.0)
    h1 = Bar(close - HOUR + 1, close, 99.0, 102.0, 98.0, 100.5, 40.0)
    core.on_close(m15, h1, None)
    assert len(core.series[TF.M15]) == 1
    assert len(core.series[TF.H1]) == 1
    assert len(core.series[TF.H4]) == 0
    assert len(core.vol_history) == 0  # no slow close yet


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
    assert len(tape.slow_trend) == len(tape.close_time)


# --- 5m entries on the 1H/15m ladder -----------------------------------------

FIVE = 300_000


@pytest.fixture(scope="module")
def cfg5() -> Config:
    return load_config(DEFAULT_PATH.parent / "proto-5m.yaml")


def test_proto_5m_config_scales_windows_x12(cfg5):
    assert cfg5.timeframes.zones == ["1h", "15m"]
    assert cfg5.timeframes.trend == "15m"
    assert cfg5.timeframes.entry == "5m"
    assert cfg5.entries.retest_window_bars == 48
    assert cfg5.entries.limit_valid_bars == 3
    assert cfg5.exits.time_stop_bars == 72
    core = SymbolCore("T", cfg5)
    assert core.entry_scale == 12
    assert core.reset_bars == 24 * 12


def test_5m_ladder_is_1h_15m_5m(cfg5):
    core = SymbolCore("T", cfg5)
    assert core.slow_tf is TF.H1 and core.fast_tf is TF.M15
    assert core.entry_tf is TF.M5
    assert set(core.structure) == {TF.H1, TF.M15}
    assert core.trend is core.structure[TF.M15].trend
    assert core.zones.slow_tf is TF.H1 and core.zones.fast_tf is TF.M15


def test_5m_tape_replays_5m_closes_with_15m_1h_attached(paths, cfg5):
    put(paths, "klines", "TESTUSDT", "5m", klines("2021-01-01", 30 * 288, FIVE))
    put(paths, "klines", "TESTUSDT", "15m", klines("2021-01-01", 30 * 96, QUARTER))
    put(paths, "klines", "TESTUSDT", "1h", klines("2021-01-01", 30 * 24, HOUR))
    tape = zone_tape("TESTUSDT", paths, cfg5)
    assert len(tape.close_time) == 30 * 288
    assert set(np.diff(tape.close_time)) == {FIVE}
    assert np.isfinite(tape.atr_entry[100:]).all()
    assert len(tape.slow_trend) == len(tape.close_time)
