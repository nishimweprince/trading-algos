"""Golden tests for the signal-driven strategy plugins (ipda, fu).

Each test replays synthetic closed bars through the real plugin ``on_bar``
and the real engine fill path: a known trigger bar must stage exactly one
intent, filled at the next bar open with the expected stop distance.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backtesting_service import registry
from backtesting_service.config import Settings
from backtesting_service.engine import ClosedBarEngine
from backtesting_service.models import BacktestRequest, Candle, EngineParams
from backtesting_service.sessions import build_windows


def _bars(
    ohlc: list[tuple[float, float, float, float]],
    start: datetime,
    minutes: int = 15,
) -> list[Candle]:
    out = []
    ts = start
    for o, h, low, c in ohlc:
        ts = ts + timedelta(minutes=minutes)
        out.append(
            Candle(
                ts=ts,
                open=o,
                high=h,
                low=low,
                close=c,
                volume=10.0,
                source_instrument="XAUUSD",
            )
        )
    return out


def _run(strategy: str, body: BacktestRequest, bars: list[Candle]) -> ClosedBarEngine:
    plugin = registry.get(strategy)
    params = plugin.build(
        registry.StrategyBuildInputs(
            base=Settings().engine_params(), body=body, timeframe_minutes=15
        )
    )
    engine = ClosedBarEngine(
        build_windows(body.sessions or ["new_york"], {}),
        params,
        strategy=registry.execution_for(plugin),
    )
    engine.run(bars)
    return engine


START = datetime(2025, 6, 2, 12, 0, tzinfo=UTC)  # a Monday


def _decline_then_rally() -> list[Candle]:
    closes = [3000.0 - i * 5.0 for i in range(20)]
    closes += [closes[-1] + 60.0, closes[-1] + 65.0, closes[-1] + 70.0]
    return _bars([(c, c + 0.5, c - 0.5, c) for c in closes], START)


def test_ipda_reversal_cross_stages_one_pair_with_fixed_pip_stop() -> None:
    engine = _run(
        "ipda",
        BacktestRequest(strategy="ipda", sessions=["new_york"]),
        _decline_then_rally(),
    )
    assert len(engine.pairs) == 1
    pair = engine.pairs[0]
    assert pair.primary_side == "long"
    assert pair.sl_dist == pytest.approx(40.0 * 0.1)
    assert pair.long_tp - pair.entry == pytest.approx(50.0 * 0.1)
    entries = [e for e in engine.events if e.kind == "entry"]
    assert len(entries) == 1
    assert entries[0].detail["strategy"] == "ipda"
    assert entries[0].detail["trigger"] == "reversal"


def test_ipda_out_of_session_signal_is_skipped_not_traded() -> None:
    bars = _decline_then_rally()
    engine = _run(
        "ipda",
        BacktestRequest(strategy="ipda", sessions=["tokyo"]),
        bars,
    )
    # 12:00+ UTC Mondays is outside Tokyo 09:00-18:00 JST; nothing may fill.
    assert engine.pairs == []
    skips = [e for e in engine.events if e.kind == "signal_skipped_out_of_session"]
    assert len(skips) == 1
    assert skips[0].detail["strategy"] == "ipda"


def test_ipda_params_flow_into_engine() -> None:
    plugin = registry.get("ipda")
    params = plugin.build(
        registry.StrategyBuildInputs(
            base=Settings().engine_params(),
            body=BacktestRequest(
                strategy="ipda",
                pip_size=0.0001,
                ipda={"rsi_len": 10, "oversold": 30.0, "stop_loss_pips": 20.0},
            ),
            timeframe_minutes=15,
        )
    )
    assert params.strategy == "ipda"
    assert params.pip_size == 0.0001
    assert params.strategy_params["rsi_len"] == 10
    assert params.strategy_params["oversold"] == 30.0
    assert params.one_open_per_session is False


def test_ipda_invalid_levels_rejected() -> None:
    with pytest.raises(Exception, match="oversold must be below overbought"):
        BacktestRequest(strategy="ipda", ipda={"oversold": 80.0, "overbought": 75.0})


def test_fu_sweep_and_close_beyond_stages_one_long() -> None:
    ohlc = [
        (100.0, 101.0, 99.0, 100.0),
        (100.0, 101.5, 99.5, 101.0),
        (101.0, 102.0, 100.0, 101.5),
        (101.5, 104.0, 98.0, 103.5),  # low < prev low, close > prev high
        (103.5, 105.0, 102.5, 104.5),
        (104.5, 106.0, 103.5, 105.5),
    ]
    engine = _run("fu", BacktestRequest(strategy="fu", sessions=["new_york"]), _bars(ohlc, START))
    assert len(engine.pairs) == 1
    pair = engine.pairs[0]
    assert pair.primary_side == "long"
    assert pair.long_open and not pair.short_open
    # Stop is the swept extreme minus the ATR buffer: below the signal low.
    assert pair.long_sl < 98.0
    assert pair.long_tp > pair.entry
    entries = [e for e in engine.events if e.kind == "entry"]
    assert len(entries) == 1
    assert entries[0].detail["strategy"] == "fu"


def test_fu_no_sweep_no_trade() -> None:
    ohlc = [(100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i) for i in range(8)]
    engine = _run("fu", BacktestRequest(strategy="fu", sessions=["new_york"]), _bars(ohlc, START))
    assert engine.pairs == []
    assert [e for e in engine.events if e.kind == "entry"] == []


def test_fu_params_flow_and_confluence_gate() -> None:
    plugin = registry.get("fu")
    params = plugin.build(
        registry.StrategyBuildInputs(
            base=Settings().engine_params(),
            body=BacktestRequest(strategy="fu", fu={"rr_target": 3.0, "use_doji_filter": True}),
            timeframe_minutes=15,
        )
    )
    assert params.strategy == "fu"
    assert params.strategy_params["rr_target"] == 3.0
    assert params.strategy_params["use_doji_filter"] is True
    with pytest.raises(Exception, match="fu_only=false"):
        BacktestRequest(strategy="fu", fu={"fu_only": False})


def test_signal_strategies_do_not_run_session_staging() -> None:
    for name in ("ipda", "fu"):
        execution = registry.execution_for(registry.get(name))
        assert execution.session_driven is False
        assert callable(execution.on_bar)
    assert registry.execution_for(registry.get(None)).session_driven is True


def test_unknown_strategy_still_rejected() -> None:
    with pytest.raises(KeyError, match="unknown strategy"):
        registry.get("no_such_strategy")
    assert {"session_hedge", "ipda", "fu"} <= set(registry.available())


def test_default_params_validate() -> None:
    EngineParams.model_validate({"strategy": "ipda", "strategy_params": {}})
