from __future__ import annotations

import copy

import pytest
import yaml
from pydantic import ValidationError

from zone_trend.cli import main
from zone_trend.config import DEFAULT_PATH, Config, load_config
from zone_trend.types import TF


@pytest.fixture
def raw() -> dict:
    return yaml.safe_load(DEFAULT_PATH.read_text())


def _validate(raw: dict, **patch: dict) -> Config:
    data = copy.deepcopy(raw)
    for section, values in patch.items():
        data[section].update(values)
    return Config.model_validate(data)


def test_default_config_loads():
    cfg = load_config()
    assert cfg.exchange.leverage == 5
    assert cfg.entries.enabled == ["B"]
    assert cfg.sizing.mode == "movement"
    assert cfg.swings.h4.left == 5


def test_unknown_key_is_refused(raw):
    raw["exits"]["tp3_r"] = 5.0
    with pytest.raises(ValidationError):
        Config.model_validate(raw)


@pytest.mark.parametrize(
    "section, values",
    [
        ("portfolio", {"max_positions": 7}),
        ("portfolio", {"daily_loss_pct": 0.10}),
        ("portfolio", {"dd_halve_pct": 0.30}),
        ("exchange", {"margin_type": "CROSSED"}),
        ("entries", {"fib_band": [0.8, 0.3]}),
        ("exits", {"tp2_r": 1.0}),
        ("sizing", {"risk_pct": 0.11}),
    ],
)
def test_out_of_range_is_refused(raw, section, values):
    with pytest.raises(ValidationError):
        _validate(raw, **{section: values})


def test_research_risk_is_backtest_only(raw):
    assert _validate(raw, sizing={"risk_pct": 0.05}).sizing.risk_pct == 0.05
    for mode in ("shadow", "live"):
        data = copy.deepcopy(raw)
        data["mode"] = mode
        data["sizing"]["risk_pct"] = 0.05
        with pytest.raises(ValidationError, match="backtest-only"):
            Config.model_validate(data)


def test_htf_ladder_accepts_slow_fast_entry(raw):
    cfg = _validate(raw, timeframes={"zones": ["4h", "1h"], "trend": "1h", "entry": "15m"})
    assert cfg.timeframes.zones == ["4h", "1h"]
    assert cfg.zones.lookback_days.days(TF.H1) == 30


def test_htf_ladder_accepts_5m_entry(raw):
    cfg = _validate(raw, timeframes={"zones": ["1h", "15m"], "trend": "15m", "entry": "5m"})
    assert cfg.timeframes.entry == "5m"
    assert cfg.zones.lookback_days.days(TF.M5) == 2


@pytest.mark.parametrize(
    "zones, trend, entry",
    [
        (["1h", "4h"], "4h", "15m"),  # fast-first
        (["4h"], "4h", "1h"),  # not a pair
        (["4h", "4h"], "4h", "1h"),  # not distinct
        (["1d", "4h"], "1h", "15m"),  # trend is not the fast TF
        (["1d", "4h"], "4h", "4h"),  # entry not below fast
        (["1d", "4h"], "4h", "1d"),  # entry above fast
    ],
)
def test_htf_ladder_violations_refused(raw, zones, trend, entry):
    with pytest.raises(ValidationError, match="timeframes"):
        _validate(raw, timeframes={"zones": zones, "trend": trend, "entry": entry})


def test_timeframes_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ZT_ZONES_TF", "4h,1h")
    monkeypatch.setenv("ZT_TREND_TF", "1h")
    monkeypatch.setenv("ZT_ENTRY_TF", "15m")
    cfg = load_config()
    assert cfg.timeframes.zones == ["4h", "1h"]
    assert cfg.timeframes.trend == "1h"
    assert cfg.timeframes.entry == "15m"


def test_timeframes_env_override_empty_is_ignored(monkeypatch):
    monkeypatch.setenv("ZT_ZONES_TF", "")
    monkeypatch.setenv("ZT_TREND_TF", "")
    monkeypatch.setenv("ZT_ENTRY_TF", "")
    cfg = load_config()
    assert cfg.timeframes.zones == ["1d", "4h"]
    assert cfg.timeframes.entry == "1h"


def test_timeframes_env_override_invalid_refused(monkeypatch):
    monkeypatch.setenv("ZT_ZONES_TF", "1h,4h")
    monkeypatch.setenv("ZT_TREND_TF", "4h")
    with pytest.raises(ValidationError, match="slow-first"):
        load_config()


def test_cli_config_check(capsys):
    assert main(["config"]) == 0
    assert "mode=backtest" in capsys.readouterr().out
