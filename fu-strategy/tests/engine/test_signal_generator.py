from datetime import datetime, timezone

from app.config import Settings
from app.core.types import Bias
from app.engine.event_log import EventLog
from app.engine.indicator_pipeline import IndicatorPipeline
from app.engine.signal_generator import SignalGenerator
from app.indicators.fvg import FVGZone


def test_signal_generator_emits_on_fu_zone_bias_confluence(tmp_path):
    settings = Settings(
        _env_file=None,
        htf_timeframes=['1H'],
        ltf_timeframes=['15M'],
        indicator_event_log_path=str(tmp_path / 'indicator_events.jsonl'),
        fu_use_doji_filter=False,
        fu_use_ma_filter=False,
    )
    event_log = EventLog(settings.indicator_event_log_path)
    generator = SignalGenerator(settings, IndicatorPipeline(settings, event_log), event_log)

    htf = generator.pipeline.state_for('EUR_USD', '1H')
    htf.structure.state.isCocUp = True
    htf.structure.state.isCocDn = False
    htf.last_bias = Bias.BULLISH
    htf.fvg.zones.append(FVGZone(
        id='fvg-1', is_bull=True, top=1.12, bottom=1.08,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    ))

    generator.on_new_candle('EUR_USD', '15M', {
        'timestamp': '2024-01-01T00:00:00Z',
        'open': 1.10, 'high': 1.105, 'low': 1.09, 'close': 1.10, 'volume': 1,
    })
    signal = generator.on_new_candle('EUR_USD', '15M', {
        'timestamp': '2024-01-01T00:15:00Z',
        'open': 1.10, 'high': 1.13, 'low': 1.085, 'close': 1.11, 'volume': 1,
    })

    assert signal is not None
    assert signal.zone_id == 'fvg-1'
    assert signal.structure_bias == Bias.BULLISH
    lines = (tmp_path / 'indicator_events.jsonl').read_text().splitlines()
    assert any('"kind":"fu"' in line for line in lines)
    assert any('"status":"ACTIVE"' in line or '"kind":"signal"' in line for line in lines)


def test_signal_generator_skips_backlog_fu_not_on_current_candle(tmp_path):
    settings = Settings(
        _env_file=None,
        htf_timeframes=['1H'],
        ltf_timeframes=['15M'],
        indicator_event_log_path=str(tmp_path / 'indicator_events.jsonl'),
        fu_use_doji_filter=False,
        fu_use_ma_filter=False,
    )
    event_log = EventLog(settings.indicator_event_log_path)
    generator = SignalGenerator(settings, IndicatorPipeline(settings, event_log), event_log)

    htf = generator.pipeline.state_for('EUR_USD', '1H')
    htf.structure.state.isCocUp = True
    htf.structure.state.isCocDn = False
    htf.last_bias = Bias.BULLISH
    htf.fvg.zones.append(FVGZone(
        id='fvg-1', is_bull=True, top=1.12, bottom=1.08,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    ))

    generator.on_new_candle('EUR_USD', '15M', {
        'timestamp': '2024-01-01T00:00:00Z',
        'open': 1.10, 'high': 1.105, 'low': 1.09, 'close': 1.10, 'volume': 1,
    })
    generator.on_new_candle('EUR_USD', '15M', {
        'timestamp': '2024-01-01T00:15:00Z',
        'open': 1.10, 'high': 1.13, 'low': 1.085, 'close': 1.11, 'volume': 1,
    })

    # Simulate backlog/reprocessing: the next on_new_candle call processes the FU candle
    # again together with a newer non-FU candle in a single batch.
    ltf_state = generator.pipeline.state_for('EUR_USD', '15M')
    ltf_state.processed = 1

    signal = generator.on_new_candle('EUR_USD', '15M', {
        'timestamp': '2024-01-01T00:30:00Z',
        'open': 1.11, 'high': 1.115, 'low': 1.10, 'close': 1.112, 'volume': 1,
    })

    assert signal is None
