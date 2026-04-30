from datetime import datetime, timezone

from app.core.types import Bias, Direction, Signal
from app.notifications.formatters import (
    SMS_MAX_CHARS,
    format_signal_sms_text,
    format_signal_template_params,
    format_signal_text,
)


def test_signal_sms_text_is_concise_and_omits_confluence():
    signal = Signal(
        id='signal-1',
        symbol='EUR_USD',
        timeframe='15M',
        direction=Direction.BUY,
        entry_price=1.169512345678,
        sl=1.170670512345,
        tp=1.167189012345,
        structure_bias=Bias.BEARISH,
        fu_candle_time=datetime(2026, 4, 30, 12, 15, tzinfo=timezone.utc),
        confidence=[
            'FU BEARISH',
            'HTF bias 4H,1H BEARISH',
            'SUPPLY_DEMAND zone very-long-zone-id',
        ],
        fu_open=1.168,
        fu_high=1.171,
        fu_low=1.167,
        fu_close=1.169512345678,
        prev_high=1.171,
        prev_low=1.168,
        swept_level=1.168,
    )

    body = format_signal_sms_text(signal)

    assert len(body) <= SMS_MAX_CHARS
    assert 'Confluence' not in body
    assert 'SUPPLY_DEMAND' not in body
    assert body == (
        'FU BUY EUR_USD 15M E:1.1695123 SL:1.1706705 '
        'TP:1.167189 RR:2.01 BI:BEARISH SW:1.168 @04-30 12:15Z'
    )


def test_signal_text_includes_sweep_and_fu_ohlc():
    signal = Signal(
        id='signal-1',
        symbol='EUR_USD',
        timeframe='5M',
        direction=Direction.SELL,
        entry_price=1.105,
        sl=1.112,
        tp=1.091,
        structure_bias=Bias.BEARISH,
        fu_candle_time=datetime(2026, 4, 30, 12, 15, tzinfo=timezone.utc),
        confidence=['FU BEARISH', 'HTF bias 1H BEARISH'],
        fu_open=1.108,
        fu_high=1.111,
        fu_low=1.104,
        fu_close=1.105,
        prev_high=1.11,
        prev_low=1.106,
        swept_level=1.11,
    )

    body = format_signal_text(signal)

    assert 'SELL EUR_USD (5M)' in body
    assert 'Swept previous high: 1.11' in body
    assert 'FU OHLC: O:1.108 H:1.111 L:1.104 C:1.105' in body
    assert 'Confluence: FU BEARISH, HTF bias 1H BEARISH' in body


def test_signal_template_params_remain_backward_compatible():
    signal = Signal(
        id='signal-1',
        symbol='EUR_USD',
        timeframe='15M',
        direction=Direction.BUY,
        entry_price=1.1,
        sl=1.0,
        tp=1.3,
        structure_bias=Bias.BULLISH,
        fu_candle_time=datetime(2026, 4, 30, 12, 15, tzinfo=timezone.utc),
        swept_level=1.09,
    )

    assert format_signal_template_params(signal) == [
        'BUY', 'EUR_USD', '15M', '1.1', '1.0', '1.3', '2.00', 'BULLISH',
    ]
