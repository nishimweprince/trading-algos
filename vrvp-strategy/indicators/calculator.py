"""Indicator Calculator - combines all indicators"""
import pandas as pd
from typing import Dict, Any
from loguru import logger

from .supertrend import calculate_supertrend
from .stochrsi import calculate_stochrsi
from .fvg import detect_fvg
from .volume_profile import calculate_volume_profile
from config import StrategyConfig, DEFAULT_CONFIG

class IndicatorCalculator:
    def __init__(self, config: StrategyConfig = None):
        self.config = config or DEFAULT_CONFIG

    def calculate_all(self, df: pd.DataFrame, htf_df: pd.DataFrame = None) -> pd.DataFrame:
        result = df.copy()
        result = calculate_stochrsi(result, self.config.stochrsi.rsi_period, self.config.stochrsi.stoch_period,
                                     self.config.stochrsi.k_smooth, self.config.stochrsi.d_smooth,
                                     self.config.stochrsi.oversold, self.config.stochrsi.overbought)
        result = detect_fvg(
            result, 
            max_zones=self.config.fvg.max_zones,
            threshold_pct=self.config.fvg.threshold_pct,
            auto_threshold=self.config.fvg.auto_threshold
        )
        result = calculate_volume_profile(result)

        tr = pd.concat([df['high'] - df['low'], abs(df['high'] - df['close'].shift(1)), abs(df['low'] - df['close'].shift(1))], axis=1).max(axis=1)
        result['atr'] = tr.rolling(14).mean()

        if htf_df is not None and len(htf_df) > 0:
            htf_st = calculate_supertrend(htf_df, self.config.supertrend.period, self.config.supertrend.multiplier)
            htf_st['st_trend'] = htf_st['st_trend'].shift(1)
            htf_st['st_value'] = htf_st['st_value'].shift(1)
            htf_st['st_signal'] = htf_st['st_signal'].shift(1)
            for col in ['st_trend', 'st_value', 'st_signal']:
                result[f'{col}_htf'] = htf_st[col].reindex(result.index, method='ffill')
        else:
            st_df = calculate_supertrend(result, self.config.supertrend.period, self.config.supertrend.multiplier)
            result['st_trend_htf'], result['st_value_htf'], result['st_signal_htf'] = st_df['st_trend'], st_df['st_value'], st_df['st_signal']

        return result

    def get_signal_summary(self, df: pd.DataFrame, idx: int = -1) -> Dict[str, Any]:
        row = df.iloc[idx]
        
        # Helper function to safely convert to int, handling NaN
        def safe_int(value, default=0):
            if pd.isna(value):
                return default
            try:
                return int(value)
            except (ValueError, TypeError):
                return default
        
        # Helper function to safely convert to float, handling NaN
        def safe_float(value, default=0.0):
            if pd.isna(value):
                return default
            try:
                return float(value)
            except (ValueError, TypeError):
                return default
        
        # Helper function to safely convert to bool, handling NaN
        def safe_bool(value, default=False):
            if pd.isna(value):
                return default
            try:
                return bool(value)
            except (ValueError, TypeError):
                return default
        
        return {
            'supertrend_trend': safe_int(row.get('st_trend_htf', 0)), 
            'supertrend_signal': safe_int(row.get('st_signal_htf', 0)),
            'stochrsi_k': safe_float(row.get('stochrsi_k', 50)), 
            'stochrsi_d': safe_float(row.get('stochrsi_d', 50)),
            'stochrsi_oversold': safe_bool(row.get('stochrsi_oversold', False)), 
            'stochrsi_overbought': safe_bool(row.get('stochrsi_overbought', False)),
            'stochrsi_cross_up': safe_bool(row.get('stochrsi_cross_up', False)), 
            'stochrsi_cross_down': safe_bool(row.get('stochrsi_cross_down', False)),
            'in_bullish_fvg': safe_bool(row.get('in_bullish_fvg', False)), 
            'in_bearish_fvg': safe_bool(row.get('in_bearish_fvg', False)),
            'bounce_bullish_fvg': safe_bool(row.get('bounce_bullish_fvg', False)), 
            'bounce_bearish_fvg': safe_bool(row.get('bounce_bearish_fvg', False)),
            'near_poc': safe_bool(row.get('vp_near_poc', False)), 
            'near_vah': safe_bool(row.get('vp_near_vah', False)),
            'near_val': safe_bool(row.get('vp_near_val', False)), 
            'in_lvn': safe_bool(row.get('vp_in_lvn', False)),
            'atr': safe_float(row.get('atr', 0)), 
            'close': safe_float(row.get('close', 0)), 
            'timestamp': row.name
        }
