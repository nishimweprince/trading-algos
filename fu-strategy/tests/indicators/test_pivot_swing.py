from app.core.types import ZoneStatus
from app.indicators.pivot_swing import detect_pivot_swing


def test_pivot_high_box_and_fill(df_from_rows):
    df = df_from_rows([
        {'open': 1, 'high': 2, 'low': 0.8, 'close': 1.5, 'volume': 1},
        {'open': 2, 'high': 3, 'low': 1.8, 'close': 2.5, 'volume': 1},
        {'open': 3, 'high': 5, 'low': 2.8, 'close': 4.0, 'volume': 1},
        {'open': 4, 'high': 3.5, 'low': 2.7, 'close': 3.0, 'volume': 1},
        {'open': 3, 'high': 5.1, 'low': 2.5, 'close': 3.2, 'volume': 1},
    ])

    _, events = detect_pivot_swing(df, swing_size_l=1, swing_size_r=1)

    assert events[0].status == ZoneStatus.ACTIVE
    assert events[0].is_bull is False
    assert events[1].status == ZoneStatus.MITIGATED
