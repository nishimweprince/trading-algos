"""Public signal-generation entrypoints."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd
from loguru import logger

from app.config import Settings, get_settings
from app.core.types import Bias, Direction, Signal
from app.data.feed import CapitalDataFeed
from app.engine.confluence import build_signal
from app.engine.event_log import EventLog, get_event_log
from app.engine.indicator_pipeline import IndicatorPipeline
from app.execution.trade_executor import TradeExecutor
from app.indicators.luxalgo import LuxAlgoReversalEvent
from app.notifications.dispatcher import NotificationDispatcher


@dataclass
class SignalGenerator:
    settings: Settings
    pipeline: IndicatorPipeline
    event_log: EventLog
    executor: Optional[TradeExecutor] = None
    _fired_forming: Dict[Tuple[str, str], Tuple[pd.Timestamp, Set[Direction]]] = field(default_factory=dict)

    @classmethod
    def create(cls, settings: Optional[Settings] = None,
               executor: Optional[TradeExecutor] = None) -> 'SignalGenerator':
        settings = settings or get_settings()
        event_log = get_event_log(settings.indicator_event_log_path)
        return cls(settings, IndicatorPipeline(settings, event_log), event_log,
                   executor=executor)

    def seed(self, symbol: str, timeframe: str, df: pd.DataFrame,
             log_events: bool = True) -> None:
        self.pipeline.seed(symbol, timeframe, df, log_events=log_events)

    def on_new_candle(self, symbol: str, timeframe: str, candle,
                      forming: bool = False) -> List[Signal]:
        result = self.pipeline.on_candle(symbol, timeframe, candle, forming=forming)
        signals: List[Signal] = []

        if timeframe not in self.settings.ltf_timeframes:
            return signals

        current_candle_time = self._candle_time(candle)
        state = self.pipeline.state_for(symbol, timeframe)
        fu_only = self.settings.fu_only_mode

        # ── FU Candle signals ─────────────────────────────────────────────
        if not forming:
            for fu in result.fu_events:
                self.event_log.log(
                    symbol, timeframe, 'signal',
                    {'status': 'SKIPPED', 'reason': 'closed FU signals disabled',
                     'fu_time': fu.time.isoformat(), 'direction': fu.direction.value,
                     'current_candle_time': current_candle_time.isoformat()
                     if current_candle_time is not None else None,
                     'forming': forming},
                    ts=fu.time,
                )
        else:
            for fu in result.fu_events:
                if current_candle_time is not None and fu.time != current_candle_time:
                    self.event_log.log(
                        symbol, timeframe, 'signal',
                        {'status': 'SKIPPED', 'reason': 'FU is not on current candle',
                         'fu_time': fu.time.isoformat(), 'direction': fu.direction.value,
                         'current_candle_time': current_candle_time.isoformat(),
                         'forming': forming},
                        ts=fu.time,
                    )
                    continue

                if forming and self._already_fired_forming(symbol, timeframe, fu.time, fu.direction):
                    continue

                if fu_only:
                    htf_biases: Dict = {}
                    htf_zones: Dict = {}
                else:
                    htf_biases = {
                        htf: self.pipeline.bias(symbol, htf)
                        for htf in self.settings.htf_timeframes
                    }
                    htf_zones = {
                        htf: self.pipeline.active_zones(symbol, htf)
                        for htf in self.settings.htf_timeframes
                    }
                decision = build_signal(
                    symbol=symbol,
                    ltf_timeframe=timeframe,
                    fu=fu,
                    ltf_df=state.dataframe,
                    htf_biases=htf_biases,
                    htf_zones=htf_zones,
                    rr_target=self.settings.rr_target,
                    fu_only=fu_only,
                )
                if decision.signal is not None:
                    generated = decision.signal
                    self.event_log.log_signal(generated)
                    if forming:
                        self.event_log.log(
                            symbol, timeframe, 'signal',
                            {'status': 'FORMING', 'signal_id': generated.id,
                             'fu_time': fu.time.isoformat(),
                             'direction': fu.direction.value},
                            ts=fu.time,
                        )
                        self._record_fired_forming(symbol, timeframe, fu.time, fu.direction)

                    if self._should_auto_execute(timeframe):
                        self.executor.execute(generated)
                    else:
                        signals.append(generated)
                else:
                    self.event_log.log(
                        symbol, timeframe, 'signal',
                        {'status': 'SKIPPED', 'reason': decision.reason,
                         'fu_time': fu.time.isoformat(), 'direction': fu.direction.value,
                         'forming': forming},
                        ts=fu.time,
                    )

        # ── LuxAlgo Reversal signals (1m only) ────────────────────────────
        if (self.settings.luxalgo_reversal_enabled
                and timeframe.lower() in ('1', '1m', '1min')):
            for rev in result.luxalgo_reversal_events:
                if forming and self._already_fired_forming(
                        symbol, timeframe, rev.time, rev.direction):
                    continue
                sig = self._build_luxalgo_signal(symbol, timeframe, rev)
                logger.info(
                    f"LuxAlgo reversal: {sig.id} {symbol} {timeframe} "
                    f"{sig.direction.value} price={sig.entry_price:.5f} "
                    f"rsi={rev.rsi_value:.2f}"
                )
                self.event_log.log_signal(sig)
                if forming:
                    self._record_fired_forming(symbol, timeframe, rev.time, rev.direction)
                signals.append(sig)

        return signals

    @staticmethod
    def _build_luxalgo_signal(symbol: str, timeframe: str,
                              rev: LuxAlgoReversalEvent) -> Signal:
        """Package a LuxAlgoReversalEvent as a Signal for dispatch."""
        return Signal(
            id=str(uuid.uuid4()),
            symbol=symbol,
            timeframe=timeframe,
            direction=rev.direction,
            entry_price=rev.price,
            sl=0.0,
            tp=0.0,
            structure_bias=Bias.NEUTRAL,
            fu_candle_time=rev.time,
            confidence=['LuxAlgo Reversal'],
        )

    def _should_auto_execute(self, timeframe: str) -> bool:
        if self.executor is None:
            return False
        return timeframe.upper() == self.settings.capital_execution_timeframe.upper()

    def _already_fired_forming(self, symbol: str, timeframe: str,
                               fu_time: pd.Timestamp, direction: Direction) -> bool:
        entry = self._fired_forming.get((symbol, timeframe))
        if entry is None:
            return False
        bar_time, dirs = entry
        if bar_time != fu_time:
            return False
        return direction in dirs

    def _record_fired_forming(self, symbol: str, timeframe: str,
                              fu_time: pd.Timestamp, direction: Direction) -> None:
        key = (symbol, timeframe)
        entry = self._fired_forming.get(key)
        if entry is None or entry[0] != fu_time:
            self._fired_forming[key] = (fu_time, {direction})
        else:
            entry[1].add(direction)

    @staticmethod
    def _candle_time(candle) -> Optional[pd.Timestamp]:
        if isinstance(candle, pd.Series):
            raw = candle.get('timestamp') or candle.get('time') or candle.name
        else:
            raw = dict(candle).get('timestamp') or dict(candle).get('time')
        if raw is None:
            return None
        return pd.to_datetime(raw, utc=True)

    def replay(self, symbol: str, frames: Dict[str, pd.DataFrame]) -> List[Signal]:
        """Replay already-loaded candles in chronological order."""
        signals: List[Signal] = []
        rows = []
        for tf, df in frames.items():
            norm = df.copy()
            if 'timestamp' in norm.columns:
                norm = norm.set_index('timestamp')
            norm.index = pd.to_datetime(norm.index)
            for ts, row in norm.sort_index().iterrows():
                payload = row.to_dict()
                payload['timestamp'] = ts
                rows.append((ts, tf, payload))
        for _, tf, payload in sorted(rows, key=lambda x: x[0]):
            signals.extend(self.on_new_candle(symbol, tf, payload))
        return signals

    async def run_backfill_once(self, dispatcher: Optional[NotificationDispatcher] = None) -> List[Signal]:
        """Fetch configured markets once, replay candles, optionally notify signals.

        This is intentionally non-invasive: it produces and dispatches signals but
        does not place broker orders or alter execution state.
        """
        if not (self.settings.capital_api_key
                and self.settings.capital_password
                and self.settings.capital_identifier):
            logger.warning("Capital.com credentials missing; skipping backfill run")
            return []

        feed = CapitalDataFeed(
            self.settings.capital_api_key,
            self.settings.capital_password,
            self.settings.capital_identifier,
            self.settings.capital_environment,
        )
        if not feed.authenticate():
            return []

        signals: List[Signal] = []
        timeframes = list(dict.fromkeys([*self.settings.htf_timeframes,
                                         *self.settings.ltf_timeframes]))
        for symbol in self.settings.symbols:
            frames = {
                tf: feed.get_candles(symbol, tf, self.settings.backfill_candles)
                for tf in timeframes
            }
            symbol_signals = self.replay(symbol, frames)
            signals.extend(symbol_signals)
            if dispatcher is not None:
                for signal in symbol_signals:
                    await dispatcher.notify_signal(signal)
        return signals


_default_generator: Optional[SignalGenerator] = None


def get_signal_generator(settings: Optional[Settings] = None) -> SignalGenerator:
    global _default_generator
    if _default_generator is None:
        _default_generator = SignalGenerator.create(settings)
    return _default_generator


def on_new_candle(symbol: str, timeframe: str, candle) -> List[Signal]:
    return get_signal_generator().on_new_candle(symbol, timeframe, candle)


def replay(symbol: str, frames: Dict[str, pd.DataFrame]) -> List[Signal]:
    return get_signal_generator().replay(symbol, frames)
