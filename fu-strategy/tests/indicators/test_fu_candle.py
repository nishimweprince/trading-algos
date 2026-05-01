from app.core.types import Direction
from app.indicators.fu_candle import detect_fu, detect_latest_fu


def test_fu_detects_bull_bear_and_doji_filter(df_from_rows):
    df = df_from_rows([
        {'open': 9.6, 'high': 11.0, 'low': 9.0, 'close': 10.0, 'volume': 1},
        {'open': 9.8, 'high': 12.0, 'low': 8.5, 'close': 11.5, 'volume': 1},
        {'open': 11.5, 'high': 12.5, 'low': 11.0, 'close': 11.6, 'volume': 1},
        {'open': 11.7, 'high': 13.0, 'low': 10.5, 'close': 10.8, 'volume': 1},
    ])

    events = detect_fu(df)
    assert [e.direction for e in events] == [Direction.BUY, Direction.SELL]
    assert events[0].prev_high == 11.0
    assert events[0].prev_low == 9.0
    assert events[0].swept_level == 9.0
    assert events[1].prev_high == 12.5
    assert events[1].prev_low == 11.0
    assert events[1].swept_level == 12.5
    filtered = detect_fu(df, use_doji_filter=True, doji_body_ratio=0.1)
    assert [e.direction for e in filtered] == [Direction.SELL]
    assert filtered[0].leading_doji is True


def test_early_alert_kwarg_is_no_op(df_from_rows):
    df = df_from_rows([
        {'open': 9.6, 'high': 11.0, 'low': 9.0, 'close': 10.0, 'volume': 1},
        {'open': 9.8, 'high': 12.0, 'low': 8.5, 'close': 11.5, 'volume': 1},
        {'open': 11.5, 'high': 12.5, 'low': 11.0, 'close': 11.6, 'volume': 1},
        {'open': 11.7, 'high': 13.0, 'low': 10.5, 'close': 10.8, 'volume': 1},
    ])

    on = detect_fu(df, early_alert=True)
    off = detect_fu(df, early_alert=False)
    assert [(e.direction, e.swept_level) for e in on] == [(e.direction, e.swept_level) for e in off]


def test_detect_latest_fu_uses_full_history_for_sma(df_from_rows):
    rows = [
        {'open': 10, 'high': 10.5, 'low': 9.5, 'close': 10 + i * 0.1, 'volume': 1}
        for i in range(8)
    ]
    rows.append({'open': 10.5, 'high': 11.0, 'low': 10.0, 'close': 10.8, 'volume': 1})
    rows.append({'open': 10.7, 'high': 11.5, 'low': 9.8, 'close': 11.2, 'volume': 1})
    df = df_from_rows(rows)

    latest = detect_latest_fu(df, use_ma_filter=True, sma_length=9)

    assert latest is not None
    assert latest.direction == Direction.BUY
    assert latest.sma_aligned is True
