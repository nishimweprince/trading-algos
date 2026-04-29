import pandas as pd

from app.core.types import StructureEventType, ZoneStatus
from app.indicators.master_pattern import MPBox, MasterPatternState, step_master_pattern


def test_minor_box_promotes_to_major_and_liquidity_touches(df_from_rows):
    df = df_from_rows([
        {'open': 10, 'high': 11, 'low': 9, 'close': 10, 'volume': 1},
        {'open': 10, 'high': 13, 'low': 9, 'close': 12.5, 'volume': 1},
        {'open': 12, 'high': 12.5, 'low': 8, 'close': 8.5, 'volume': 1},
        {'open': 9, 'high': 12, 'low': 8.5, 'close': 11.5, 'volume': 1},
        {'open': 11, 'high': 12.5, 'low': 7.5, 'close': 8.0, 'volume': 1},
        {'open': 8, 'high': 13.2, 'low': 7.0, 'close': 8.5, 'volume': 1},
    ])
    state = MasterPatternState()
    state.minor_boxes.append(MPBox('box-1', top=12.0, bottom=8.0,
                                   left_index=0, created_at=pd.Timestamp('2024-01-01').to_pydatetime(),
                                   crossed_high=True))

    zone_events, structure_events = step_master_pattern(state, df, 4, indi_type=2)
    _, touch_events = step_master_pattern(state, df, 5, indi_type=2)

    assert zone_events[0].status == ZoneStatus.ACTIVE
    assert state.major_boxes[0].major is True
    assert any(e.type == StructureEventType.MAJOR_LINE_CONFIRM for e in structure_events)
    assert any(e.type == StructureEventType.LIQUIDITY_TOUCH for e in touch_events)
