import pandas as pd
import numpy as np
from indicators.supertrend import calculate_supertrend
from indicators.stochrsi import calculate_stochrsi, detect_momentum_from_oversold
from indicators.fvg import calculate_fvg
from indicators.volume_profile import calculate_volume_profile, is_in_volume_zone
from config import settings

class SignalGenerator:
    def __init__(self, df_1h: pd.DataFrame, df_4h: pd.DataFrame):
        self.df_1h = df_1h.copy()
        self.df_4h = df_4h.copy()
        self._calculate_indicators()
    
    def _calculate_indicators(self):
        # 1. 4H Supertrend
        # Calculate on 4H data
        st_4h = calculate_supertrend(
            self.df_4h, 
            length=settings.SUPERTREND_LENGTH, 
            multiplier=settings.SUPERTREND_MULTIPLIER
        )
        
        # Shift to avoid look-ahead bias (using completed candles)
        self.df_4h['trend'] = st_4h['direction'].shift(1)
        
        # 2. 1H StochRSI
        stoch_1h = calculate_stochrsi(
            self.df_1h,
            length=settings.STOCHRSI_LENGTH,
            rsi_length=settings.STOCHRSI_RSI_LENGTH,
            k=settings.STOCHRSI_K,
            d=settings.STOCHRSI_D
        )
        self.df_1h['stochrsi_k'] = stoch_1h['k']
        self.df_1h['stochrsi_d'] = stoch_1h['d']
        
        # 3. FVG (Fair Value Gaps)
        # Note: SMC library might be slow on large datasets, calculate once
        self.fvg_data = calculate_fvg(self.df_1h)
        # We need to map FVGs to the dataframe or store them to check against
        # For this simplified implementation, we'll assume we check the latest FVG logic 
        # inside generate_signals or pre-calculate a 'in_fvg_zone' column.
        
        # 4. Volume Profile
        # Typically calculated on a rolling window or fixed period (e.g. daily/weekly profile)
        # Here we'll calculate a static profile for the provided data for simplicity,
        # or we could implement a rolling profile.
        # User snippet implies a single profile calculation: self.vp = calculate_volume_profile(self.df_1h)
        self.vp = calculate_volume_profile(self.df_1h)
    
    def _in_volume_zone(self, price):
        return is_in_volume_zone(price, self.vp)
    
    def _bouncing_off_fvg(self, index):
        # This requires checking the specific row against known FVGs
        # Simplified placeholder
        # In a real system, we'd maintain a list of active FVGs
        return True # Placeholder
    
    def generate_signals(self) -> pd.DataFrame:
        # Merge 4H trend to 1H
        # Forward fill the 4H trend onto 1H data
        # Ensure indices are datetimes and sorted
        if not isinstance(self.df_1h.index, pd.DatetimeIndex):
            self.df_1h.index = pd.to_datetime(self.df_1h.index)
        if not isinstance(self.df_4h.index, pd.DatetimeIndex):
            self.df_4h.index = pd.to_datetime(self.df_4h.index)
            
        self.df_1h = self.df_1h.sort_index()
        self.df_4h = self.df_4h.sort_index()
        
        # Reindex 4H trend to 1H index, forward filling
        trend_series = self.df_4h['trend'].reindex(self.df_1h.index, method='ffill')
        self.df_1h['trend_4h'] = trend_series
        
        # StochRSI Signal: Crossing 30 from below
        # Using the helper function
        momentum_signal = detect_momentum_from_oversold(
            self.df_1h['stochrsi_k'],
            oversold_threshold=settings.STOCHRSI_OVERSOLD
        )
        self.df_1h['momentum_signal'] = momentum_signal
        
        # Combine signals
        # Buy Signal
        conditions = [
            (self.df_1h['trend_4h'] == 1),           # 4H Uptrend
            (self.df_1h['momentum_signal']),         # StochRSI Bullish Crossover from Oversold
            # (self.df_1h['close'].apply(self._in_volume_zone)), # Volume Support (expensive to apply row-wise)
            # FVG check would go here
        ]
        
        # aggregate boolean conditions
        buy_signal = conditions[0] & conditions[1]
        
        self.df_1h['buy_signal'] = buy_signal
        
        return self.df_1h
