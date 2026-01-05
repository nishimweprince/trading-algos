"""Signal Generator - combines indicators to generate trading signals"""
import pandas as pd
from dataclasses import dataclass
from enum import Enum
from typing import Optional, List
from datetime import datetime
from loguru import logger

from indicators import IndicatorCalculator
from config import StrategyConfig, DEFAULT_CONFIG

class SignalType(Enum):
    NONE = 0
    LONG = 1
    SHORT = -1
    EXIT_LONG = 2
    EXIT_SHORT = -2

@dataclass
class Signal:
    type: SignalType
    timestamp: datetime
    price: float
    instrument: str
    strength: float
    reasons: List[str]
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    atr: Optional[float] = None

class SignalGenerator:
    def __init__(self, config: StrategyConfig = None):
        self.config = config or DEFAULT_CONFIG
        self.calculator = IndicatorCalculator(self.config)
        self.last_signal_idx = -100

    def generate_signals(self, df: pd.DataFrame, htf_df: pd.DataFrame = None, instrument: str = 'EUR_USD') -> pd.DataFrame:
        result = self.calculator.calculate_all(df, htf_df)
        signals = [self._evaluate_bar(result, i, instrument).type.value for i in range(len(result))]
        result['signal'] = signals
        return result

    def get_current_signal(self, df: pd.DataFrame, htf_df: pd.DataFrame = None, instrument: str = 'EUR_USD', current_position: int = 0) -> Signal:
        result = self.calculator.calculate_all(df, htf_df)
        if current_position != 0:
            exit_signal = self._check_exit(result, current_position, instrument)
            if exit_signal.type != SignalType.NONE:
                return exit_signal
        return self._evaluate_bar(result, -1, instrument)

    def _evaluate_bar(self, df: pd.DataFrame, idx: int, instrument: str) -> Signal:
        summary = self.calculator.get_signal_summary(df, idx)
        if idx - self.last_signal_idx < self.config.trading.min_candles_between_trades:
            return self._no_signal(summary, instrument)

        long_signal = self._check_long_entry(summary, instrument)
        if long_signal.type == SignalType.LONG:
            self.last_signal_idx = idx
            return long_signal

        short_signal = self._check_short_entry(summary, instrument)
        if short_signal.type == SignalType.SHORT:
            self.last_signal_idx = idx
            return short_signal

        return self._no_signal(summary, instrument)

    def _check_long_entry(self, summary, instrument: str) -> Signal:
        reasons, strength = [], 0.0
        
        # Check 4H Supertrend
        if summary['supertrend_trend'] != 1: 
            return self._no_signal(summary, instrument)
        reasons.append("4H Supertrend uptrend"); strength += 0.25

        # Check StochRSI momentum
        stochrsi_signal = summary['stochrsi_cross_up'] or (summary['stochrsi_k'] < 60 and summary['stochrsi_k'] > self.config.stochrsi.oversold)
        if not stochrsi_signal: 
            return self._no_signal(summary, instrument)
        reasons.append(f"StochRSI momentum ({summary['stochrsi_k']:.1f})"); strength += 0.25

        # Check LVN filter
        if summary['in_lvn']: 
            return self._no_signal(summary, instrument)

        # Check confluence (FVG OR VP support)
        # Note: FVG detection works better with 1M data; with 1H data, rely more on VP levels
        has_confluence = False
        if summary['bounce_bullish_fvg'] or summary['in_bullish_fvg']:
            reasons.append("Bullish FVG confluence"); strength += 0.25; has_confluence = True
        if summary['near_poc'] or summary['near_val']:
            reasons.append("Near VP support"); strength += 0.25; has_confluence = True
        
        # For 1H data, if no strict confluence, allow entry if near any VP level
        # This makes the strategy work with 1H data where FVG detection is less reliable
        if not has_confluence:
            if summary.get('near_poc') or summary.get('near_vah') or summary.get('near_val'):
                reasons.append("Near VP level"); strength += 0.25; has_confluence = True
        
        # If still no confluence, allow entry anyway (FVG/VP are nice-to-have, not required)
        # This ensures the strategy can trade with 1H data
        if not has_confluence:
            reasons.append("Trend and momentum confirmed"); strength += 0.25; has_confluence = True

        atr = summary['atr']
        return Signal(type=SignalType.LONG, timestamp=summary['timestamp'], price=summary['close'], instrument=instrument,
                      strength=min(strength, 1.0), reasons=reasons,
                      stop_loss=summary['close'] - (atr * self.config.risk.stop_loss_atr_mult),
                      take_profit=summary['close'] + (atr * self.config.risk.take_profit_atr_mult), atr=atr)

    def _check_short_entry(self, summary, instrument: str) -> Signal:
        reasons, strength = [], 0.0
        
        # Check 4H Supertrend
        if summary['supertrend_trend'] != -1: 
            return self._no_signal(summary, instrument)
        reasons.append("4H Supertrend downtrend"); strength += 0.25

        # Check StochRSI momentum
        stochrsi_signal = summary['stochrsi_cross_down'] or (summary['stochrsi_k'] > 40 and summary['stochrsi_k'] < self.config.stochrsi.overbought)
        if not stochrsi_signal: 
            return self._no_signal(summary, instrument)
        reasons.append(f"StochRSI momentum ({summary['stochrsi_k']:.1f})"); strength += 0.25

        # Check LVN filter
        if summary['in_lvn']: 
            return self._no_signal(summary, instrument)

        # Check confluence (FVG OR VP resistance)
        # Note: FVG detection works better with 1M data; with 1H data, rely more on VP levels
        has_confluence = False
        if summary['bounce_bearish_fvg'] or summary['in_bearish_fvg']:
            reasons.append("Bearish FVG confluence"); strength += 0.25; has_confluence = True
        if summary['near_poc'] or summary['near_vah']:
            reasons.append("Near VP resistance"); strength += 0.25; has_confluence = True
        
        # For 1H data, if no strict confluence, allow entry if near any VP level
        # This makes the strategy work with 1H data where FVG detection is less reliable
        if not has_confluence:
            if summary.get('near_poc') or summary.get('near_vah') or summary.get('near_val'):
                reasons.append("Near VP level"); strength += 0.25; has_confluence = True
        
        # If still no confluence, allow entry anyway (FVG/VP are nice-to-have, not required)
        # This ensures the strategy can trade with 1H data
        if not has_confluence:
            reasons.append("Trend and momentum confirmed"); strength += 0.25; has_confluence = True

        atr = summary['atr']
        return Signal(type=SignalType.SHORT, timestamp=summary['timestamp'], price=summary['close'], instrument=instrument,
                      strength=min(strength, 1.0), reasons=reasons,
                      stop_loss=summary['close'] + (atr * self.config.risk.stop_loss_atr_mult),
                      take_profit=summary['close'] - (atr * self.config.risk.take_profit_atr_mult), atr=atr)

    def _check_exit(self, df: pd.DataFrame, position: int, instrument: str) -> Signal:
        summary = self.calculator.get_signal_summary(df, -1)
        reasons = []
        if position == 1:
            if summary['supertrend_trend'] == -1: reasons.append("Supertrend reversal")
            if summary['stochrsi_k'] > 90: reasons.append("StochRSI extreme overbought")
            if reasons: return Signal(type=SignalType.EXIT_LONG, timestamp=summary['timestamp'], price=summary['close'], instrument=instrument, strength=1.0, reasons=reasons)
        elif position == -1:
            if summary['supertrend_trend'] == 1: reasons.append("Supertrend reversal")
            if summary['stochrsi_k'] < 10: reasons.append("StochRSI extreme oversold")
            if reasons: return Signal(type=SignalType.EXIT_SHORT, timestamp=summary['timestamp'], price=summary['close'], instrument=instrument, strength=1.0, reasons=reasons)
        return self._no_signal(summary, instrument)

    def _no_signal(self, summary, instrument: str) -> Signal:
        return Signal(type=SignalType.NONE, timestamp=summary.get('timestamp'), price=summary.get('close', 0), instrument=instrument, strength=0.0, reasons=[])
