"""
Signal generator combining all indicators for trade signals.

Implements the multi-timeframe strategy:
- 4H Supertrend for trend filtering
- 1H Stochastic RSI for momentum
- FVG zones for price action confluence
- Volume Profile for zone identification
"""

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

import pandas as pd

from config.settings import get_settings
from data.resampler import TimeframeResampler, align_signals
from indicators import (
    FVGDetector,
    FVGType,
    FVGZone,
    StochRSIIndicator,
    SupertrendIndicator,
    VolumeProfileCalculator,
    VolumeProfileResult,
)
from monitoring.logger import get_logger

logger = get_logger(__name__)


class SignalType(Enum):
    """Trading signal type."""
    
    BUY = 1
    SELL = -1
    NONE = 0


@dataclass
class TradeSignal:
    """Represents a trading signal with all relevant information."""
    
    type: SignalType
    timestamp: pd.Timestamp
    price: float
    
    # Component signals
    trend_direction: int  # 4H Supertrend direction
    stochrsi_value: float  # Current StochRSI K value
    momentum_state: str  # from_oversold, from_overbought, etc.
    fvg_zone: Optional[FVGZone]  # FVG zone if applicable
    near_hvn: bool  # Near High Volume Node
    near_poc: bool  # Near Point of Control
    
    # Trade levels (to be filled by risk manager)
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    
    @property
    def strength(self) -> int:
        """Calculate signal strength based on confluence (1-4)."""
        strength = 0
        
        # Trend alignment
        if (self.type == SignalType.BUY and self.trend_direction == 1) or \
           (self.type == SignalType.SELL and self.trend_direction == -1):
            strength += 1
        
        # Momentum confirmation
        if self.momentum_state in ("from_oversold", "from_overbought"):
            strength += 1
        
        # FVG confluence
        if self.fvg_zone is not None:
            strength += 1
        
        # Volume confluence
        if self.near_hvn or self.near_poc:
            strength += 1
        
        return strength
    
    def __str__(self) -> str:
        """String representation of the signal."""
        return (
            f"Signal({self.type.name}, price={self.price:.5f}, "
            f"strength={self.strength}, trend={self.trend_direction}, "
            f"momentum={self.momentum_state})"
        )


class SignalGenerator:
    """
    Generate trading signals combining multiple indicators.
    
    Strategy logic:
    - BUY: 4H uptrend + 1H StochRSI from oversold + (FVG or HVN confluence)
    - SELL: 4H downtrend + 1H StochRSI from overbought + (FVG or HVN confluence)
    """
    
    def __init__(
        self,
        supertrend: Optional[SupertrendIndicator] = None,
        stochrsi: Optional[StochRSIIndicator] = None,
        fvg_detector: Optional[FVGDetector] = None,
        volume_profile: Optional[VolumeProfileCalculator] = None,
    ):
        """
        Initialize signal generator with indicators.
        
        Args:
            supertrend: Supertrend indicator instance
            stochrsi: StochRSI indicator instance
            fvg_detector: FVG detector instance
            volume_profile: Volume profile calculator instance
        """
        self.supertrend = supertrend or SupertrendIndicator()
        self.stochrsi = stochrsi or StochRSIIndicator()
        self.fvg_detector = fvg_detector or FVGDetector()
        self.volume_profile = volume_profile or VolumeProfileCalculator()
        
        settings = get_settings()
        self.primary_tf = settings.strategy.primary_timeframe
        self.trend_tf = settings.strategy.trend_timeframe
        self.vp_lookback = 100  # Bars for volume profile calculation
        
        # Cache for calculated values
        self._df_1h: Optional[pd.DataFrame] = None
        self._df_4h: Optional[pd.DataFrame] = None
        self._vp_result: Optional[VolumeProfileResult] = None
        self._active_fvgs: List[FVGZone] = []
    
    def prepare_data(
        self,
        df_1h: pd.DataFrame,
        df_4h: Optional[pd.DataFrame] = None,
    ):
        """
        Prepare data and calculate all indicators.
        
        Args:
            df_1h: Primary (1H) timeframe data
            df_4h: Trend (4H) timeframe data (resampled from 1H if not provided)
        """
        self._df_1h = df_1h.copy()
        
        # Resample to 4H if not provided
        if df_4h is None:
            resampler = TimeframeResampler(df_1h)
            self._df_4h = resampler.resample(self.trend_tf, shift=True)
        else:
            self._df_4h = df_4h.copy()
        
        # Calculate 4H Supertrend
        st_result = self.supertrend.calculate(self._df_4h)
        self._df_4h["st_direction"] = st_result.direction
        
        # Merge 4H trend to 1H with forward-fill
        self._df_1h["trend_4h"] = self._df_4h["st_direction"].reindex(
            self._df_1h.index, method="ffill"
        )
        
        # Calculate 1H StochRSI
        stoch_result = self.stochrsi.calculate(self._df_1h)
        self._df_1h["stochrsi_k"] = stoch_result.k
        self._df_1h["stochrsi_d"] = stoch_result.d
        self._df_1h["from_oversold"] = stoch_result.from_oversold
        self._df_1h["from_overbought"] = stoch_result.from_overbought
        
        # Get active FVG zones
        self._active_fvgs = self.fvg_detector.get_active_zones(self._df_1h)
        
        # Calculate Volume Profile
        if len(self._df_1h) >= self.vp_lookback:
            vp_window = self._df_1h.iloc[-self.vp_lookback:]
            self._vp_result = self.volume_profile.calculate(vp_window)
        else:
            self._vp_result = self.volume_profile.calculate(self._df_1h)
        
        logger.info(
            f"Prepared data: {len(self._df_1h)} 1H bars, {len(self._df_4h)} 4H bars, "
            f"{len(self._active_fvgs)} active FVGs"
        )
    
    def check_buy_conditions(self, idx: int = -1) -> dict:
        """
        Check buy signal conditions at given index.
        
        Args:
            idx: Index to check (-1 for latest)
            
        Returns:
            Dict with condition results
        """
        row = self._df_1h.iloc[idx]
        price = row["close"]
        
        conditions = {
            "trend_bullish": row["trend_4h"] == 1,
            "from_oversold": row["from_oversold"],
            "stochrsi_value": row["stochrsi_k"],
            "fvg_bounce": None,
            "near_hvn": False,
            "near_poc": False,
        }
        
        # Check FVG bounce (bullish FVG)
        bullish_fvgs = [f for f in self._active_fvgs if f.type == FVGType.BULLISH]
        for fvg in bullish_fvgs:
            if fvg.contains_price(row["low"]) and row["close"] > fvg.top:
                conditions["fvg_bounce"] = fvg
                break
        
        # Check volume confluence
        if self._vp_result:
            conditions["near_hvn"] = self.volume_profile.is_near_hvn(
                price, self._vp_result
            )
            conditions["near_poc"] = self.volume_profile.is_near_poc(
                price, self._vp_result
            )
        
        return conditions
    
    def check_sell_conditions(self, idx: int = -1) -> dict:
        """
        Check sell signal conditions at given index.
        
        Args:
            idx: Index to check (-1 for latest)
            
        Returns:
            Dict with condition results
        """
        row = self._df_1h.iloc[idx]
        price = row["close"]
        
        conditions = {
            "trend_bearish": row["trend_4h"] == -1,
            "from_overbought": row["from_overbought"],
            "stochrsi_value": row["stochrsi_k"],
            "fvg_bounce": None,
            "near_hvn": False,
            "near_poc": False,
        }
        
        # Check FVG bounce (bearish FVG)
        bearish_fvgs = [f for f in self._active_fvgs if f.type == FVGType.BEARISH]
        for fvg in bearish_fvgs:
            if fvg.contains_price(row["high"]) and row["close"] < fvg.bottom:
                conditions["fvg_bounce"] = fvg
                break
        
        # Check volume confluence
        if self._vp_result:
            conditions["near_hvn"] = self.volume_profile.is_near_hvn(
                price, self._vp_result
            )
            conditions["near_poc"] = self.volume_profile.is_near_poc(
                price, self._vp_result
            )
        
        return conditions
    
    def generate_signal(self, idx: int = -1) -> Optional[TradeSignal]:
        """
        Generate trading signal for given bar.
        
        Args:
            idx: Index to generate signal for (-1 for latest)
            
        Returns:
            TradeSignal if conditions met, None otherwise
        """
        if self._df_1h is None:
            raise ValueError("Must call prepare_data() first")
        
        row = self._df_1h.iloc[idx]
        price = row["close"]
        timestamp = self._df_1h.index[idx]
        
        # Check buy conditions
        buy_cond = self.check_buy_conditions(idx)
        if buy_cond["trend_bullish"] and buy_cond["from_oversold"]:
            has_confluence = (
                buy_cond["fvg_bounce"] is not None or
                buy_cond["near_hvn"] or
                buy_cond["near_poc"]
            )
            
            if has_confluence:
                signal = TradeSignal(
                    type=SignalType.BUY,
                    timestamp=timestamp,
                    price=price,
                    trend_direction=1,
                    stochrsi_value=buy_cond["stochrsi_value"],
                    momentum_state="from_oversold",
                    fvg_zone=buy_cond["fvg_bounce"],
                    near_hvn=buy_cond["near_hvn"],
                    near_poc=buy_cond["near_poc"],
                )
                logger.info(f"BUY signal generated: {signal}")
                return signal
        
        # Check sell conditions
        sell_cond = self.check_sell_conditions(idx)
        if sell_cond["trend_bearish"] and sell_cond["from_overbought"]:
            has_confluence = (
                sell_cond["fvg_bounce"] is not None or
                sell_cond["near_hvn"] or
                sell_cond["near_poc"]
            )
            
            if has_confluence:
                signal = TradeSignal(
                    type=SignalType.SELL,
                    timestamp=timestamp,
                    price=price,
                    trend_direction=-1,
                    stochrsi_value=sell_cond["stochrsi_value"],
                    momentum_state="from_overbought",
                    fvg_zone=sell_cond["fvg_bounce"],
                    near_hvn=sell_cond["near_hvn"],
                    near_poc=sell_cond["near_poc"],
                )
                logger.info(f"SELL signal generated: {signal}")
                return signal
        
        return None
    
    def generate_signals(
        self,
        df_1h: pd.DataFrame,
        df_4h: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        """
        Generate signals for entire DataFrame.
        
        Args:
            df_1h: Primary timeframe data
            df_4h: Trend timeframe data (optional)
            
        Returns:
            DataFrame with signal column added
        """
        self.prepare_data(df_1h, df_4h)
        
        signals = []
        for i in range(len(self._df_1h)):
            signal = self.generate_signal(i)
            if signal:
                signals.append({
                    "index": self._df_1h.index[i],
                    "signal": signal.type.value,
                    "strength": signal.strength,
                    "price": signal.price,
                })
            else:
                signals.append({
                    "index": self._df_1h.index[i],
                    "signal": 0,
                    "strength": 0,
                    "price": self._df_1h.iloc[i]["close"],
                })
        
        signals_df = pd.DataFrame(signals).set_index("index")
        result = self._df_1h.copy()
        result["signal"] = signals_df["signal"]
        result["signal_strength"] = signals_df["strength"]
        
        buy_count = (result["signal"] == 1).sum()
        sell_count = (result["signal"] == -1).sum()
        logger.info(f"Generated {buy_count} buy signals, {sell_count} sell signals")
        
        return result
    
    def get_current_signal(self) -> Optional[TradeSignal]:
        """Get signal for the most recent bar."""
        return self.generate_signal(-1)
    
    def get_indicator_values(self, idx: int = -1) -> dict:
        """
        Get all indicator values at given index.
        
        Args:
            idx: Index to check
            
        Returns:
            Dict with all indicator values
        """
        if self._df_1h is None:
            raise ValueError("Must call prepare_data() first")
        
        row = self._df_1h.iloc[idx]
        
        return {
            "close": row["close"],
            "trend_4h": row["trend_4h"],
            "stochrsi_k": row["stochrsi_k"],
            "stochrsi_d": row["stochrsi_d"],
            "from_oversold": row["from_oversold"],
            "from_overbought": row["from_overbought"],
            "active_fvgs": len(self._active_fvgs),
            "vp_poc": self._vp_result.poc if self._vp_result else None,
            "vp_va_high": self._vp_result.value_area_high if self._vp_result else None,
            "vp_va_low": self._vp_result.value_area_low if self._vp_result else None,
        }
