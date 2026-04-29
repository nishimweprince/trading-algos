from app.core.types import ZoneStatus
from app.indicators.zones_sd import detect_supply_demand


def test_supply_zone_from_sweep_and_mitigation(df_from_rows):
    df = df_from_rows([
        {'open': 8.5, 'high': 9.0, 'low': 8.0, 'close': 8.6, 'volume': 1},
        {'open': 9.2, 'high': 10.0, 'low': 9.0, 'close': 9.4, 'volume': 1},
        {'open': 7.8, 'high': 8.0, 'low': 7.0, 'close': 7.4, 'volume': 1},
        {'open': 6.8, 'high': 7.0, 'low': 6.0, 'close': 6.5, 'volume': 1},
        {'open': 8.0, 'high': 8.5, 'low': 7.5, 'close': 8.2, 'volume': 1},
        {'open': 8.1, 'high': 8.8, 'low': 7.8, 'close': 8.0, 'volume': 1},
        {'open': 8.9, 'high': 9.5, 'low': 8.8, 'close': 9.1, 'volume': 1},
    ])

    state, events = detect_supply_demand(df)

    assert events[0].status == ZoneStatus.ACTIVE
    assert events[0].is_bull is False
    assert any(event.status == ZoneStatus.MITIGATED for event in events)
    assert state.supply
