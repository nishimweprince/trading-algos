"""
Fair Value Gap (FVG) detection and analysis.

Uses smartmoneyconcepts package with additional utilities for FVG-based trading.
"""

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

import numpy as np
import pandas as pd

from config.settings import get_settings
from monitoring.logger import get_logger

logger = get_logger(__name__)

# Try to import smartmoneyconcepts, provide fallback if not available
try:
    from smartmoneyconcepts import smc
    SMC_AVAILABLE = True
except ImportError:
    SMC_AVAILABLE = False
    logger.warning("smartmoneyconcepts not installed. Using custom FVG implementation.")


class FVGType(Enum):
    """Fair Value Gap type."""
    
    BULLISH = 1
    BEARISH = -1
    NONE = 0


@dataclass
class FVGZone:
    """Represents a Fair Value Gap zone."""
    
    type: FVGType
    top: float
    bottom: float
    index: int  # Candle index where FVG was created
    timestamp: pd.Timestamp
    mitigated: bool = False
    mitigated_index: Optional[int] = None
    
    @property
    def midpoint(self) -> float:
        """Get the midpoint of the FVG zone."""
        return (self.top + self.bottom) / 2
    
    @property
    def size(self) -> float:
        """Get the size of the FVG in price units."""
        return abs(self.top - self.bottom)
    
    def contains_price(self, price: float) -> bool:
        """Check if a price is within the FVG zone."""
        return self.bottom <= price <= self.top
    
    def is_tested(self, candle: pd.Series) -> bool:
        """Check if a candle tested (entered) the FVG zone."""
        if self.type == FVGType.BULLISH:
            return candle["low"] <= self.top and candle["low"] >= self.bottom
        else:
            return candle["high"] >= self.bottom and candle["high"] <= self.top


class FVGDetector:
    """
    Detect and track Fair Value Gaps.
    
    A bullish FVG occurs when candle 3's low > candle 1's high (gap up).
    A bearish FVG occurs when candle 3's high < candle 1's low (gap down).
    """
    
    def __init__(self, min_gap_pips: Optional[float] = None):
        """
        Initialize FVG detector.
        
        Args:
            min_gap_pips: Minimum FVG size in pips to consider valid
        """
        settings = get_settings()
        self.min_gap_pips = min_gap_pips or settings.strategy.fvg_min_gap_pips
        self.pip_size = 0.0001  # For EUR/USD, GBP/USD, etc.
    
    def detect(self, df: pd.DataFrame) -> List[FVGZone]:
        """
        Detect all FVGs in the data.
        
        Args:
            df: DataFrame with OHLC data
            
        Returns:
            List of FVGZone objects
        """
        if SMC_AVAILABLE:
            return self._detect_with_smc(df)
        else:
            return self._detect_custom(df)
    
    def _detect_with_smc(self, df: pd.DataFrame) -> List[FVGZone]:
        """Detect FVGs using smartmoneyconcepts package."""
        # Ensure lowercase columns for smc
        df_lower = df.copy()
        df_lower.columns = df_lower.columns.str.lower()
        
        fvg_data = smc.fvg(df_lower)
        zones = []
        
        for i in range(len(fvg_data)):
            fvg_type = fvg_data["FVG"].iloc[i]
            
            if fvg_type != 0 and not pd.isna(fvg_data["Top"].iloc[i]):
                zone = FVGZone(
                    type=FVGType(int(fvg_type)),
                    top=float(fvg_data["Top"].iloc[i]),
                    bottom=float(fvg_data["Bottom"].iloc[i]),
                    index=i,
                    timestamp=df.index[i] if isinstance(df.index, pd.DatetimeIndex) else pd.Timestamp.now(),
                    mitigated=not pd.isna(fvg_data["MitigatedIndex"].iloc[i]),
                    mitigated_index=int(fvg_data["MitigatedIndex"].iloc[i]) if not pd.isna(fvg_data["MitigatedIndex"].iloc[i]) else None,
                )
                
                # Filter by minimum size
                if zone.size >= self.min_gap_pips * self.pip_size:
                    zones.append(zone)
        
        return zones
    
    def _detect_custom(self, df: pd.DataFrame) -> List[FVGZone]:
        """Custom FVG detection implementation."""
        zones = []
        
        for i in range(2, len(df)):
            candle_1 = df.iloc[i - 2]
            candle_3 = df.iloc[i]
            
            # Bullish FVG: candle 3 low > candle 1 high
            if candle_3["low"] > candle_1["high"]:
                gap_size = candle_3["low"] - candle_1["high"]
                
                if gap_size >= self.min_gap_pips * self.pip_size:
                    zone = FVGZone(
                        type=FVGType.BULLISH,
                        top=candle_3["low"],
                        bottom=candle_1["high"],
                        index=i,
                        timestamp=df.index[i] if isinstance(df.index, pd.DatetimeIndex) else pd.Timestamp.now(),
                    )
                    zones.append(zone)
            
            # Bearish FVG: candle 3 high < candle 1 low
            elif candle_3["high"] < candle_1["low"]:
                gap_size = candle_1["low"] - candle_3["high"]
                
                if gap_size >= self.min_gap_pips * self.pip_size:
                    zone = FVGZone(
                        type=FVGType.BEARISH,
                        top=candle_1["low"],
                        bottom=candle_3["high"],
                        index=i,
                        timestamp=df.index[i] if isinstance(df.index, pd.DatetimeIndex) else pd.Timestamp.now(),
                    )
                    zones.append(zone)
        
        # Check mitigation
        self._check_mitigation(df, zones)
        
        return zones
    
    def _check_mitigation(self, df: pd.DataFrame, zones: List[FVGZone]):
        """Check if FVGs have been mitigated by subsequent price action."""
        for zone in zones:
            if zone.mitigated:
                continue
            
            # Check candles after FVG creation
            for i in range(zone.index + 1, len(df)):
                candle = df.iloc[i]
                
                if zone.type == FVGType.BULLISH:
                    # Bullish FVG mitigated when price drops into zone
                    if candle["low"] <= zone.bottom:
                        zone.mitigated = True
                        zone.mitigated_index = i
                        break
                else:
                    # Bearish FVG mitigated when price rises into zone
                    if candle["high"] >= zone.top:
                        zone.mitigated = True
                        zone.mitigated_index = i
                        break
    
    def get_active_zones(
        self,
        df: pd.DataFrame,
        lookback: Optional[int] = None,
    ) -> List[FVGZone]:
        """
        Get active (unmitigated) FVG zones.
        
        Args:
            df: DataFrame with OHLC data
            lookback: Only consider FVGs from last N candles
            
        Returns:
            List of active FVGZone objects
        """
        settings = get_settings()
        lookback = lookback or settings.strategy.lookback_periods
        
        zones = self.detect(df)
        
        # Filter to active zones within lookback
        current_idx = len(df) - 1
        active = [
            z for z in zones
            if not z.mitigated and (current_idx - z.index) <= lookback
        ]
        
        return active
    
    def detect_bounce(
        self,
        df: pd.DataFrame,
        zones: Optional[List[FVGZone]] = None,
    ) -> Optional[FVGZone]:
        """
        Detect if current candle is bouncing off an FVG zone.
        
        A bounce is when price enters the zone and closes back outside.
        
        Args:
            df: DataFrame with OHLC data
            zones: Optional pre-calculated zones
            
        Returns:
            FVGZone that price bounced from, or None
        """
        if zones is None:
            zones = self.get_active_zones(df)
        
        if len(df) < 1:
            return None
        
        current_candle = df.iloc[-1]
        
        for zone in zones:
            if zone.type == FVGType.BULLISH:
                # Bullish bounce: wick into zone, close above
                entered = current_candle["low"] <= zone.top
                bounced = current_candle["close"] > zone.top
                
                if entered and bounced:
                    return zone
            
            elif zone.type == FVGType.BEARISH:
                # Bearish bounce: wick into zone, close below
                entered = current_candle["high"] >= zone.bottom
                bounced = current_candle["close"] < zone.bottom
                
                if entered and bounced:
                    return zone
        
        return None
    
    def add_to_dataframe(
        self,
        df: pd.DataFrame,
        prefix: str = "fvg",
    ) -> pd.DataFrame:
        """
        Add FVG signals to DataFrame.
        
        Args:
            df: Input DataFrame
            prefix: Column name prefix
            
        Returns:
            DataFrame with added FVG columns
        """
        zones = self.detect(df)
        
        df_out = df.copy()
        df_out[f"{prefix}_bullish"] = False
        df_out[f"{prefix}_bearish"] = False
        df_out[f"{prefix}_top"] = np.nan
        df_out[f"{prefix}_bottom"] = np.nan
        
        for zone in zones:
            if zone.type == FVGType.BULLISH:
                df_out.loc[df_out.index[zone.index], f"{prefix}_bullish"] = True
            else:
                df_out.loc[df_out.index[zone.index], f"{prefix}_bearish"] = True
            
            df_out.loc[df_out.index[zone.index], f"{prefix}_top"] = zone.top
            df_out.loc[df_out.index[zone.index], f"{prefix}_bottom"] = zone.bottom
        
        return df_out
    
    def get_nearest_zone(
        self,
        price: float,
        zones: List[FVGZone],
        zone_type: Optional[FVGType] = None,
    ) -> Optional[FVGZone]:
        """
        Get the nearest FVG zone to a price level.
        
        Args:
            price: Current price
            zones: List of FVG zones
            zone_type: Filter by zone type
            
        Returns:
            Nearest FVGZone or None
        """
        if not zones:
            return None
        
        filtered = zones
        if zone_type:
            filtered = [z for z in zones if z.type == zone_type]
        
        if not filtered:
            return None
        
        # Sort by distance to midpoint
        sorted_zones = sorted(filtered, key=lambda z: abs(price - z.midpoint))
        return sorted_zones[0]
