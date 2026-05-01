from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import MagicMock

from app.config import Settings
from app.core.types import Bias, Direction, Signal
from app.engine.event_log import EventLog
from app.execution.trade_executor import TradeExecutor


def _make_signal(symbol: str = 'EUR_USD', tf: str = '1M',
                 direction: Direction = Direction.BUY) -> Signal:
    return Signal(
        id='SIG-1',
        symbol=symbol,
        timeframe=tf,
        direction=direction,
        entry_price=1.1000,
        sl=1.0950,
        tp=1.1100,
        structure_bias=Bias.BULLISH,
        fu_candle_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _settings(**overrides) -> Settings:
    base: Dict[str, Any] = dict(
        _env_file=None,
        capital_execution_enabled=True,
        capital_execution_size=2.0,
        capital_execution_guaranteed_stop=False,
    )
    base.update(overrides)
    return Settings(**base)


def test_execute_calls_create_position_with_mapped_epic_and_rounded_levels(tmp_path):
    settings = _settings()
    event_log = EventLog(str(tmp_path / 'events.jsonl'))
    client = MagicMock()
    client.get_market_details.return_value = {
        'snapshot': {'scalingFactor': 10000},
        'dealingRules': {'minDealSize': {'value': 1.0}},
    }
    client.create_position.return_value = {'dealReference': 'DEAL-1'}

    executor = TradeExecutor(client, settings, event_log)
    response = executor.execute(_make_signal())

    assert response == {'dealReference': 'DEAL-1'}
    client.create_position.assert_called_once()
    kwargs = client.create_position.call_args.kwargs
    assert kwargs['epic'] == 'EURUSD'
    assert kwargs['direction'] == 'BUY'
    assert kwargs['size'] == 2.0
    # BUY: SL floors away from entry, TP ceils away from entry, both at 4dp.
    assert abs(kwargs['stop_loss'] - 1.0950) <= 1e-4
    assert kwargs['stop_loss'] <= 1.0950
    assert abs(kwargs['take_profit'] - 1.1100) <= 1e-4
    assert kwargs['take_profit'] >= 1.1100
    assert kwargs['guaranteed_stop'] is False

    lines = (tmp_path / 'events.jsonl').read_text().splitlines()
    assert any('"status":"PLACED"' in line and '"deal_reference":"DEAL-1"' in line for line in lines)


def test_execute_uses_min_deal_size_when_configured_size_is_lower(tmp_path):
    settings = _settings(capital_execution_size=0.1)
    event_log = EventLog(str(tmp_path / 'events.jsonl'))
    client = MagicMock()
    client.get_market_details.return_value = {
        'snapshot': {'scalingFactor': 10000},
        'dealingRules': {'minDealSize': {'value': 0.5}},
    }
    client.create_position.return_value = {'dealReference': 'D'}

    TradeExecutor(client, settings, event_log).execute(_make_signal())

    assert client.create_position.call_args.kwargs['size'] == 0.5


def test_execute_returns_none_and_logs_failure_when_create_position_raises(tmp_path):
    settings = _settings()
    event_log = EventLog(str(tmp_path / 'events.jsonl'))
    client = MagicMock()
    client.get_market_details.return_value = {
        'snapshot': {'scalingFactor': 10000},
        'dealingRules': {'minDealSize': {'value': 1.0}},
    }
    client.create_position.side_effect = RuntimeError('broker rejected')

    executor = TradeExecutor(client, settings, event_log)
    response = executor.execute(_make_signal())

    assert response is None
    lines = (tmp_path / 'events.jsonl').read_text().splitlines()
    assert any('"status":"FAILED"' in line and 'broker rejected' in line for line in lines)


def test_execute_swallows_market_details_failure_and_uses_fallback_decimals(tmp_path):
    settings = _settings()
    event_log = EventLog(str(tmp_path / 'events.jsonl'))
    client = MagicMock()
    client.get_market_details.side_effect = RuntimeError('not found')
    client.create_position.return_value = {'dealReference': 'D'}

    executor = TradeExecutor(client, settings, event_log)
    response = executor.execute(_make_signal())

    assert response == {'dealReference': 'D'}
    client.create_position.assert_called_once()
