"""Stateful indicator orchestrator per (symbol, timeframe)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

from app.config import Settings, get_settings
from app.core.types import ActiveZone, Bias, FUEvent, StructureEvent, ZoneEvent
from app.engine.event_log import EventLog, get_event_log
from app.indicators.bars import detect_scob
from app.indicators.fu_candle import detect_latest_fu
from app.indicators.fvg import FVGState, step_fvg
from app.indicators.master_pattern import MasterPatternState, step_master_pattern
from app.indicators.pivot_swing import PivotSwingState, step_pivot_swing
from app.indicators.structure import SMCStructure
from app.indicators.zones_sd import SupplyDemandState, step_supply_demand


OHLCV_COLUMNS = ['open', 'high', 'low', 'close', 'volume']


@dataclass
class IndicatorStepResult:
    fu_events: List[FUEvent] = field(default_factory=list)
    structure_events: List[StructureEvent] = field(default_factory=list)
    zone_events: List[ZoneEvent] = field(default_factory=list)
    old_bias: Optional[Bias] = None
    new_bias: Optional[Bias] = None


@dataclass
class TimeframeIndicatorState:
    dataframe: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=OHLCV_COLUMNS))
    fvg: FVGState = field(default_factory=FVGState)
    structure: Optional[SMCStructure] = None
    supply_demand: SupplyDemandState = field(default_factory=SupplyDemandState)
    pivot_swing: PivotSwingState = field(default_factory=PivotSwingState)
    master_pattern: MasterPatternState = field(default_factory=MasterPatternState)
    processed: int = 0
    last_bias: Bias = Bias.NEUTRAL


class IndicatorPipeline:
    def __init__(self, settings: Optional[Settings] = None,
                 event_log: Optional[EventLog] = None):
        self.settings = settings or get_settings()
        self.event_log = event_log or get_event_log(self.settings.indicator_event_log_path)
        self._states: Dict[Tuple[str, str], TimeframeIndicatorState] = {}

    def state_for(self, symbol: str, timeframe: str) -> TimeframeIndicatorState:
        key = (symbol, timeframe)
        if key not in self._states:
            state = TimeframeIndicatorState()
            state.structure = SMCStructure(self.settings.smc_structure_type)
            self._states[key] = state
        return self._states[key]

    def seed(self, symbol: str, timeframe: str, df: pd.DataFrame,
             log_events: bool = True) -> IndicatorStepResult:
        state = TimeframeIndicatorState(dataframe=self._normalise_df(df.copy()))
        state.structure = SMCStructure(self.settings.smc_structure_type)
        self._states[(symbol, timeframe)] = state
        result = IndicatorStepResult()
        while state.processed < len(state.dataframe):
            result = self._process_next(symbol, timeframe, state, log_events=log_events)
        return result

    def on_candle(self, symbol: str, timeframe: str, candle,
                  forming: bool = False) -> IndicatorStepResult:
        state = self.state_for(symbol, timeframe)
        row = self._row_to_df(candle)
        if state.dataframe.empty:
            state.dataframe = self._normalise_df(row)
        else:
            state.dataframe = self._normalise_df(pd.concat([state.dataframe, row]))
        state.dataframe = state.dataframe[~state.dataframe.index.duplicated(keep='last')]
        state.dataframe = state.dataframe.sort_index()
        if state.processed > len(state.dataframe):
            state.processed = 0

        if forming:
            return self._preview_forming(state)

        combined = IndicatorStepResult()
        while state.processed < len(state.dataframe):
            step = self._process_next(symbol, timeframe, state, log_events=True)
            combined.fu_events.extend(step.fu_events)
            combined.structure_events.extend(step.structure_events)
            combined.zone_events.extend(step.zone_events)
            combined.old_bias = step.old_bias if step.old_bias is not None else combined.old_bias
            combined.new_bias = step.new_bias if step.new_bias is not None else combined.new_bias
        return combined

    def _preview_forming(self, state: TimeframeIndicatorState) -> IndicatorStepResult:
        """Read-only FU detection on the forming bar.

        Does not advance state.processed and does not touch zone/structure state,
        so subsequent reticks of the same bar can be re-evaluated and the bar's
        eventual close still goes through the normal _process_next path.
        """
        result = IndicatorStepResult()
        df = state.dataframe
        if len(df) < 2:
            return result
        fu = detect_latest_fu(
            df,
            use_doji_filter=self.settings.fu_use_doji_filter,
            use_ma_filter=self.settings.fu_use_ma_filter,
            sma_length=self.settings.fu_sma_length,
            doji_body_ratio=self.settings.fu_doji_body_ratio,
        )
        if fu is not None:
            result.fu_events.append(fu)
        return result

    def bias(self, symbol: str, timeframe: str) -> Bias:
        return self.state_for(symbol, timeframe).structure.state.bias  # type: ignore[union-attr]

    def active_zones(self, symbol: str, timeframe: str) -> List[ActiveZone]:
        state = self.state_for(symbol, timeframe)
        zones: List[ActiveZone] = []
        zones.extend(state.fvg.active_zone_events())
        zones.extend(state.supply_demand.active_zones())
        zones.extend(state.master_pattern.active_zones())
        zones.extend(state.pivot_swing.active_zones())
        return zones

    def active_zones_for(self, symbol: str, timeframes: Iterable[str]) -> List[ActiveZone]:
        zones: List[ActiveZone] = []
        for tf in timeframes:
            zones.extend(self.active_zones(symbol, tf))
        return zones

    def _process_next(self, symbol: str, timeframe: str, state: TimeframeIndicatorState,
                      log_events: bool) -> IndicatorStepResult:
        i = state.processed
        df = state.dataframe
        result = IndicatorStepResult()

        fu = detect_latest_fu(
            df.iloc[:i + 1],
            use_doji_filter=self.settings.fu_use_doji_filter,
            use_ma_filter=self.settings.fu_use_ma_filter,
            sma_length=self.settings.fu_sma_length,
            doji_body_ratio=self.settings.fu_doji_body_ratio,
        )
        if fu is not None:
            result.fu_events.append(fu)

        result.zone_events.extend(step_fvg(
            state.fvg, df, i, self.settings.fvg_threshold_pct,
            self.settings.fvg_auto_threshold,
        ))

        assert state.structure is not None
        result.old_bias = state.last_bias
        result.structure_events.extend(state.structure.step(df, i))
        new_bias = state.structure.state.bias
        if new_bias != state.last_bias:
            result.new_bias = new_bias
            state.last_bias = new_bias

        result.zone_events.extend(step_supply_demand(
            state.supply_demand, df, i, self.settings.smc_poi_type,
            self.settings.smc_merge_ratio, self.settings.smc_max_bar_history,
        ))

        result.zone_events.extend(step_pivot_swing(
            state.pivot_swing, df, i,
            swing_size_l=self.settings.swing_size_l,
            swing_size_r=self.settings.swing_size_r,
            hide_filled=self.settings.swing_hide_filled,
            extend_til_filled=self.settings.swing_extend_til_filled,
        ))

        mp_zones, mp_structures = step_master_pattern(
            state.master_pattern, df, i,
            indi_type=self.settings.mp_indi_type,
            max_bars=self.settings.mp_max_bars,
        )
        result.zone_events.extend(mp_zones)
        result.structure_events.extend(mp_structures)

        sd_zones = state.supply_demand.active_zones()
        scob = detect_scob(
            df, i,
            supply_zones=[z for z in sd_zones if not z.is_bull],
            demand_zones=[z for z in sd_zones if z.is_bull],
        )
        if scob is not None:
            result.structure_events.append(scob)

        if log_events:
            for event in result.fu_events:
                self.event_log.log_fu(symbol, timeframe, event)
            for event in result.structure_events:
                self.event_log.log_structure(symbol, timeframe, event)
            for event in result.zone_events:
                self.event_log.log_zone(symbol, timeframe, event)
            if result.new_bias is not None:
                self.event_log.log_bias_change(
                    symbol, timeframe, result.old_bias.value if result.old_bias else Bias.NEUTRAL.value,
                    result.new_bias.value, ts=df.index[i].to_pydatetime()
                    if hasattr(df.index[i], 'to_pydatetime') else df.index[i],
                )

        state.processed += 1
        return result

    @staticmethod
    def _normalise_df(df: pd.DataFrame) -> pd.DataFrame:
        if 'timestamp' in df.columns:
            df = df.set_index('timestamp')
        for col in OHLCV_COLUMNS:
            if col not in df.columns:
                df[col] = 0.0 if col == 'volume' else pd.NA
        df = df[OHLCV_COLUMNS].dropna(subset=['open', 'high', 'low', 'close'])
        df.index = pd.to_datetime(df.index)
        return df.sort_index()

    @staticmethod
    def _row_to_df(candle) -> pd.DataFrame:
        if isinstance(candle, pd.Series):
            data = candle.to_dict()
        else:
            data = dict(candle)
        ts = data.pop('timestamp', None) or data.pop('time', None)
        if ts is None:
            raise ValueError('candle must include timestamp or time')
        return pd.DataFrame([data], index=[pd.to_datetime(ts)])
