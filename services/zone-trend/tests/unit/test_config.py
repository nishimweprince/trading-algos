from __future__ import annotations

import copy

import pytest
import yaml
from pydantic import ValidationError

from zone_trend.cli import main
from zone_trend.config import DEFAULT_PATH, Config, load_config


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


def test_cli_config_check(capsys):
    assert main(["config"]) == 0
    assert "mode=backtest" in capsys.readouterr().out
