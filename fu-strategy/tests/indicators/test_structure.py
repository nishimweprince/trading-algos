from app.core.types import Bias, StructureEventType
from app.indicators.structure import SMCStructure


def test_structure_emits_choch_idm_and_bias(df_from_rows):
    df = df_from_rows([
        {'open': 1.0, 'high': 2.0, 'low': 1.0, 'close': 1.5, 'volume': 1},
        {'open': 1.5, 'high': 3.0, 'low': 1.2, 'close': 2.8, 'volume': 1},
        {'open': 2.8, 'high': 2.9, 'low': 1.4, 'close': 1.6, 'volume': 1},
        {'open': 1.6, 'high': 3.5, 'low': 1.5, 'close': 3.2, 'volume': 1},
        {'open': 3.2, 'high': 3.4, 'low': 1.0, 'close': 1.1, 'volume': 1},
        {'open': 1.1, 'high': 1.2, 'low': 0.8, 'close': 0.85, 'volume': 1},
    ])
    structure = SMCStructure()
    events = []
    for i in range(len(df)):
        events.extend(structure.step(df, i))

    types = [event.type for event in events]
    assert StructureEventType.CHOCH in types
    assert StructureEventType.IDM in types
    assert StructureEventType.HH in types
    assert structure.state.bias == Bias.BEARISH
