from app.core.types import ZoneStatus
from app.indicators.fvg import detect_fvg


def test_fvg_detects_and_mitigates_bull_gap(df_from_rows):
    df = df_from_rows([
        {'open': 9.5, 'high': 10.0, 'low': 9.0, 'close': 9.8, 'volume': 1},
        {'open': 10.1, 'high': 10.8, 'low': 9.9, 'close': 10.5, 'volume': 1},
        {'open': 11.2, 'high': 12.0, 'low': 11.0, 'close': 11.5, 'volume': 1},
        {'open': 10.2, 'high': 10.4, 'low': 9.2, 'close': 9.5, 'volume': 1},
    ])

    state, events = detect_fvg(df)

    assert events[0].status == ZoneStatus.ACTIVE
    assert events[0].is_bull is True
    assert events[-1].status == ZoneStatus.MITIGATED
    assert not state.active_zones()
