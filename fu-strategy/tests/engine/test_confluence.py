from app.core.types import ActiveZone, Bias, Direction, FUEvent, ZoneSource
from app.engine.confluence import build_signal


def test_confluence_requires_bias_and_zone(df_from_rows):
    df = df_from_rows([
        {'open': 1.0, 'high': 1.1, 'low': 0.9, 'close': 1.0, 'volume': 1},
        {'open': 1.0, 'high': 1.2, 'low': 0.8, 'close': 1.15, 'volume': 1},
    ])
    fu = FUEvent(df.index[-1].to_pydatetime(), Direction.BUY, 1.0, 1.2, 0.8, 1.15)
    zone = ActiveZone('z1', ZoneSource.FVG, top=1.2, bottom=1.1, is_bull=True)

    rejected = build_signal(
        symbol='EUR_USD', ltf_timeframe='15M', fu=fu, ltf_df=df,
        htf_biases={'1H': Bias.BEARISH}, htf_zones={'1H': [zone]},
    )
    accepted = build_signal(
        symbol='EUR_USD', ltf_timeframe='15M', fu=fu, ltf_df=df,
        htf_biases={'1H': Bias.BULLISH}, htf_zones={'1H': [zone]},
    )

    assert rejected.signal is None
    assert accepted.signal is not None
    assert accepted.signal.zone_id == 'z1'
    assert 'FVG zone z1' in accepted.signal.confidence
