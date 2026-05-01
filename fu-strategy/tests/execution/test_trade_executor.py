from datetime import datetime, timezone
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

from app.config import Settings
from app.core.types import Bias, Direction, Signal
from app.engine.event_log import EventLog
from app.execution.trade_executor import TradeExecutor


def _make_signal(symbol: str = 'EUR_USD', tf: str = '1M',
                 direction: Direction = Direction.BUY,
                 entry: float = 1.1000, sl: float = 1.0950,
                 tp: float = 1.1100) -> Signal:
    return Signal(
        id='SIG-1',
        symbol=symbol,
        timeframe=tf,
        direction=direction,
        entry_price=entry,
        sl=sl,
        tp=tp,
        structure_bias=Bias.BULLISH if direction == Direction.BUY else Bias.BEARISH,
        fu_candle_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _settings(**overrides) -> Settings:
    base: Dict[str, Any] = dict(
        _env_file=None,
        capital_execution_enabled=True,
        capital_execution_size=2.0,
        capital_execution_guaranteed_stop=False,
        capital_execution_use_market_distance=True,
        capital_execution_sl_pct=0.5,
        rr_target=2.0,
    )
    base.update(overrides)
    return Settings(**base)


def _market(bid: float, offer: float, *,
            scaling_factor: int = 10000,
            min_dist: Optional[float] = 0.0001,
            min_dist_unit: str = 'POINTS',
            min_deal: float = 1.0,
            guaranteed: bool = False) -> Dict:
    rules: Dict[str, Any] = {'minDealSize': {'value': min_deal}}
    if min_dist is not None:
        rule_key = ('minControlledRiskStopDistance' if guaranteed
                    else 'minNormalStopOrLimitDistance')
        rules[rule_key] = {'unit': min_dist_unit, 'value': min_dist}
    return {
        'snapshot': {'scalingFactor': scaling_factor, 'bid': bid, 'offer': offer},
        'dealingRules': rules,
    }


def test_buy_levels_anchored_to_offer_with_pct_and_rr(tmp_path):
    settings = _settings(capital_execution_sl_pct=0.5, rr_target=2.0)
    event_log = EventLog(str(tmp_path / 'events.jsonl'))
    client = MagicMock()
    client.get_market_details.return_value = _market(bid=1.0998, offer=1.1002)
    client.create_position.return_value = {'dealReference': 'OK'}

    TradeExecutor(client, settings, event_log).execute(
        _make_signal(direction=Direction.BUY, sl=1.05, tp=1.20),
    )

    kwargs = client.create_position.call_args.kwargs
    assert kwargs['epic'] == 'EURUSD'
    assert kwargs['direction'] == 'BUY'
    # entry = offer = 1.1002; sl = 1.1002 * (1 - 0.005) = 1.0947
    assert abs(kwargs['stop_loss'] - 1.0947) <= 5e-4
    # tp = 1.1002 * (1 + 0.01) = 1.11120
    assert abs(kwargs['take_profit'] - 1.1112) <= 5e-4
    lines = (tmp_path / 'events.jsonl').read_text().splitlines()
    assert any('"anchored_to_market":true' in line for line in lines)


def test_sell_levels_anchored_to_bid(tmp_path):
    settings = _settings(capital_execution_sl_pct=0.5, rr_target=2.0)
    event_log = EventLog(str(tmp_path / 'events.jsonl'))
    client = MagicMock()
    client.get_market_details.return_value = _market(bid=4612.50, offer=4612.80,
                                                     scaling_factor=100,
                                                     min_dist=0.5,
                                                     guaranteed=True)
    client.create_position.return_value = {'dealReference': 'GOLD-1'}

    settings_g = _settings(capital_execution_sl_pct=0.5, rr_target=2.0,
                           capital_execution_guaranteed_stop=True)
    client.get_market_details.return_value = _market(bid=4612.50, offer=4612.80,
                                                     scaling_factor=100,
                                                     min_dist=0.5,
                                                     guaranteed=True)
    TradeExecutor(client, settings_g, event_log).execute(
        _make_signal(symbol='XAU_USD', direction=Direction.SELL,
                     entry=4564, sl=4568.86, tp=4560.00),
    )

    kwargs = client.create_position.call_args.kwargs
    assert kwargs['direction'] == 'SELL'
    # entry = bid = 4612.50; sl = 4612.50 * 1.005 = 4635.5625
    assert kwargs['stop_loss'] > 4612.50  # above market
    assert abs(kwargs['stop_loss'] - 4635.5625) <= 1.0
    # tp = 4612.50 * (1 - 0.01) = 4566.375
    assert kwargs['take_profit'] < 4612.50  # below market
    assert abs(kwargs['take_profit'] - 4566.375) <= 1.0


def test_btc_buy_with_stale_signal_above_market_is_anchored_correctly(tmp_path):
    """Reproduces the BTC failure: BUY signal had sl=77266 / tp=77417 (both
    above current market ~76930). With market anchoring, signal levels are
    ignored and SL goes below market while TP goes above."""
    settings = _settings(capital_execution_sl_pct=0.5, rr_target=2.0,
                         capital_execution_guaranteed_stop=True)
    event_log = EventLog(str(tmp_path / 'events.jsonl'))
    client = MagicMock()
    client.get_market_details.return_value = _market(
        bid=76925, offer=76935, scaling_factor=100,
        min_dist=0.1, min_dist_unit='PERCENTAGE', guaranteed=True,
    )
    client.create_position.return_value = {'dealReference': 'BTC-1'}

    TradeExecutor(client, settings, event_log).execute(
        _make_signal(symbol='BTC_USD', direction=Direction.BUY,
                     entry=77400, sl=77266.975, tp=77417.125),
    )

    kwargs = client.create_position.call_args.kwargs
    assert kwargs['epic'] == 'BTCUSD'
    assert kwargs['direction'] == 'BUY'
    # SL must be below current market (offer = 76935)
    assert kwargs['stop_loss'] < kwargs['take_profit']
    assert kwargs['stop_loss'] < 76935
    # TP must be above market
    assert kwargs['take_profit'] > 76935
    # Approx: entry=offer=76935; sl≈76935*0.995=76550; tp≈76935*1.01=77704
    assert abs(kwargs['stop_loss'] - 76550) <= 200
    assert abs(kwargs['take_profit'] - 77704) <= 200


def test_percentage_min_distance_resolves_with_reference_price(tmp_path):
    """Crypto often quotes minControlledRiskStopDistance as PERCENTAGE; the
    executor must convert to absolute price via the live market."""
    settings = _settings(capital_execution_sl_pct=0.1, rr_target=2.0,
                         capital_execution_guaranteed_stop=True,
                         capital_execution_safety_multiplier=2.0)
    event_log = EventLog(str(tmp_path / 'events.jsonl'))
    client = MagicMock()
    # min_dist = 0.5% of price = 0.5% of 76935 ≈ 384.7
    client.get_market_details.return_value = _market(
        bid=76925, offer=76935, scaling_factor=100,
        min_dist=0.5, min_dist_unit='PERCENTAGE', guaranteed=True,
    )
    client.create_position.return_value = {'dealReference': 'D'}

    TradeExecutor(client, settings, event_log).execute(
        _make_signal(symbol='BTC_USD', direction=Direction.BUY,
                     entry=77000, sl=76950, tp=77100),
    )

    kwargs = client.create_position.call_args.kwargs
    # 0.1% sl on 76935 = ~76.94 — too tight against 0.5% PERCENTAGE min_dist
    # safety = 384.7 * 2 + 10 (spread) = 779.4; max_sl = 76925 - 779.4 = 76145.6
    assert kwargs['stop_loss'] <= 76145.6 + 1.0


def test_skipped_when_no_live_prices(tmp_path):
    settings = _settings()
    event_log = EventLog(str(tmp_path / 'events.jsonl'))
    client = MagicMock()
    # No bid/offer in snapshot
    client.get_market_details.return_value = {
        'snapshot': {'scalingFactor': 10000},
        'dealingRules': {'minDealSize': {'value': 1.0}},
    }

    response = TradeExecutor(client, settings, event_log).execute(_make_signal())

    assert response is None
    client.create_position.assert_not_called()
    lines = (tmp_path / 'events.jsonl').read_text().splitlines()
    assert any('"status":"SKIPPED"' in line and 'no live market prices' in line
               for line in lines)


def test_skipped_when_market_details_fails(tmp_path):
    settings = _settings()
    event_log = EventLog(str(tmp_path / 'events.jsonl'))
    client = MagicMock()
    client.get_market_details.side_effect = RuntimeError('not found')

    response = TradeExecutor(client, settings, event_log).execute(_make_signal())

    assert response is None
    client.create_position.assert_not_called()


def test_signal_levels_used_when_market_anchoring_disabled(tmp_path):
    settings = _settings(capital_execution_use_market_distance=False)
    event_log = EventLog(str(tmp_path / 'events.jsonl'))
    client = MagicMock()
    client.get_market_details.return_value = _market(bid=1.0998, offer=1.1002)
    client.create_position.return_value = {'dealReference': 'OK'}

    TradeExecutor(client, settings, event_log).execute(
        _make_signal(direction=Direction.BUY, sl=1.05, tp=1.20),
    )

    kwargs = client.create_position.call_args.kwargs
    # With anchoring off and signal levels far enough from market, they pass through.
    assert abs(kwargs['stop_loss'] - 1.05) <= 1e-4
    assert abs(kwargs['take_profit'] - 1.20) <= 1e-4
    lines = (tmp_path / 'events.jsonl').read_text().splitlines()
    assert any('"anchored_to_market":false' in line for line in lines)


def test_uses_min_deal_size_when_configured_size_is_lower(tmp_path):
    settings = _settings(capital_execution_size=0.1)
    event_log = EventLog(str(tmp_path / 'events.jsonl'))
    client = MagicMock()
    client.get_market_details.return_value = _market(bid=1.0998, offer=1.1002,
                                                     min_deal=0.5)
    client.create_position.return_value = {'dealReference': 'D'}

    TradeExecutor(client, settings, event_log).execute(_make_signal())

    assert client.create_position.call_args.kwargs['size'] == 0.5


def test_returns_none_and_logs_failure_when_create_position_raises(tmp_path):
    settings = _settings()
    event_log = EventLog(str(tmp_path / 'events.jsonl'))
    client = MagicMock()
    client.get_market_details.return_value = _market(bid=1.0998, offer=1.1002)
    client.create_position.side_effect = RuntimeError('broker rejected')

    response = TradeExecutor(client, settings, event_log).execute(_make_signal())

    assert response is None
    lines = (tmp_path / 'events.jsonl').read_text().splitlines()
    assert any('"status":"FAILED"' in line and 'broker rejected' in line for line in lines)
