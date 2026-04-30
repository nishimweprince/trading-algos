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
        fu_only_mode=False,
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
    }, forming=True)

    assert signal is not None
    assert signal.zone_id == 'fvg-1'
    assert signal.structure_bias == Bias.BULLISH
    lines = (tmp_path / 'indicator_events.jsonl').read_text().splitlines()
    assert any('"kind":"signal"' in line and '"swept_level":1.09' in line for line in lines)


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


def test_fu_only_mode_emits_without_zones_or_bias(tmp_path):
    settings = Settings(
        _env_file=None,
        htf_timeframes=['1H'],
        ltf_timeframes=['15M'],
        indicator_event_log_path=str(tmp_path / 'indicator_events.jsonl'),
        fu_only_mode=True,
    )
    event_log = EventLog(settings.indicator_event_log_path)
    generator = SignalGenerator(settings, IndicatorPipeline(settings, event_log), event_log)

    # No HTF state seeded (bias NEUTRAL, no zones) — would normally be rejected.
    generator.on_new_candle('EUR_USD', '15M', {
        'timestamp': '2024-01-01T00:00:00Z',
        'open': 1.10, 'high': 1.105, 'low': 1.09, 'close': 1.10, 'volume': 1,
    })
    signal = generator.on_new_candle('EUR_USD', '15M', {
        'timestamp': '2024-01-01T00:15:00Z',
        'open': 1.10, 'high': 1.13, 'low': 1.085, 'close': 1.11, 'volume': 1,
    }, forming=True)

    assert signal is not None
    assert signal.zone_id is None
    assert 'fu_only_mode' in signal.confidence
    # SL below fu.low, TP above entry, RR ~ rr_target
    assert signal.sl < 1.085
    assert signal.tp > signal.entry_price
    assert abs(signal.rr - settings.rr_target) < 0.01


def test_fire_on_forming_emits_then_dedups_same_direction(tmp_path):
    settings = Settings(
        _env_file=None,
        htf_timeframes=['1H'],
        ltf_timeframes=['15M'],
        indicator_event_log_path=str(tmp_path / 'indicator_events.jsonl'),
        fu_only_mode=True,
        fu_fire_on_forming=True,
    )
    event_log = EventLog(settings.indicator_event_log_path)
    generator = SignalGenerator(settings, IndicatorPipeline(settings, event_log), event_log)

    # Seed prior closed bar.
    generator.on_new_candle('EUR_USD', '15M', {
        'timestamp': '2024-01-01T00:00:00Z',
        'open': 1.10, 'high': 1.105, 'low': 1.09, 'close': 1.10, 'volume': 1,
    })

    # Forming bar — first tick already satisfies bull-FU vs prior (low<prevLow, close>prevHigh).
    s1 = generator.on_new_candle('EUR_USD', '15M', {
        'timestamp': '2024-01-01T00:15:00Z',
        'open': 1.10, 'high': 1.115, 'low': 1.085, 'close': 1.11, 'volume': 1,
    }, forming=True)
    assert s1 is not None

    # Same forming bar, same direction — must dedup.
    s2 = generator.on_new_candle('EUR_USD', '15M', {
        'timestamp': '2024-01-01T00:15:00Z',
        'open': 1.10, 'high': 1.118, 'low': 1.084, 'close': 1.112, 'volume': 1,
    }, forming=True)
    assert s2 is None

    # Bar closes (forming=False) — closed-path processing must still advance state.
    s3 = generator.on_new_candle('EUR_USD', '15M', {
        'timestamp': '2024-01-01T00:15:00Z',
        'open': 1.10, 'high': 1.118, 'low': 1.084, 'close': 1.112, 'volume': 1,
    })
    # Closed-path FU processing advances state but no longer emits trade signals.
    assert s3 is None
    state = generator.pipeline.state_for('EUR_USD', '15M')
    assert state.processed == 2
