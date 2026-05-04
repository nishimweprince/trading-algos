"""
Volume Profile (VRVP) indicator for zone identification.

Identifies High Volume Nodes (HVN), Low Volume Nodes (LVN), Point of Control (POC),
and Value Area for support/resistance analysis.
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from config.settings import VolumeProfileParams, get_settings
from monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class VolumeNode:
    """Represents a volume node (high or low volume area)."""
    
    price_low: float
    price_high: float
    volume: float
    is_hvn: bool  # True if High Volume Node, False if Low Volume Node
    
    @property
    def midpoint(self) -> float:
        """Get the midpoint price of the node."""
        return (self.price_low + self.price_high) / 2


@dataclass
class VolumeProfileResult:
    """Container for Volume Profile calculation results."""
    
    poc: float  # Point of Control (highest volume price level)
    value_area_high: float  # Upper bound of value area
    value_area_low: float  # Lower bound of value area
    profile: np.ndarray  # Volume at each price level
    price_levels: np.ndarray  # Price level boundaries
    hvn_zones: List[VolumeNode]  # High Volume Nodes
    lvn_zones: List[VolumeNode]  # Low Volume Nodes


class VolumeProfileCalculator:
    """
    Calculate Volume Profile (VRVP - Volume at Price).
    
    Note: Forex uses tick volume, not true volume. While not as reliable as
    equity volume, tick volume still provides useful distribution information.
    
    Key concepts:
    - POC (Point of Control): Price level with highest volume
    - Value Area: Price range containing 70% of total volume
    - HVN (High Volume Node): Areas of high trading activity (support/resistance)
    - LVN (Low Volume Node): Areas of low activity (price moves quickly through)
    """
    
    def __init__(
        self,
        num_bins: Optional[int] = None,
        value_area_pct: Optional[float] = None,
        hvn_std_multiplier: Optional[float] = None,
    ):
        """
        Initialize Volume Profile calculator.
        
        Args:
            num_bins: Number of price bins for profile
            value_area_pct: Percentage of volume for value area (default 0.70)
            hvn_std_multiplier: Standard deviations above mean for HVN threshold
        """
        settings = get_settings()
        params = settings.strategy.volume_profile
        
        self.num_bins = num_bins or params.num_bins
        self.value_area_pct = value_area_pct or params.value_area_pct
        self.hvn_std_multiplier = hvn_std_multiplier or params.hvn_std_multiplier
    
    def calculate(self, df: pd.DataFrame) -> VolumeProfileResult:
        """
        Calculate Volume Profile for the given data.
        
        Args:
            df: DataFrame with OHLC and volume data
            
        Returns:
            VolumeProfileResult with profile analysis
        """
        price_min = df["low"].min()
        price_max = df["high"].max()
        
        # Create price buckets
        price_levels = np.linspace(price_min, price_max, self.num_bins + 1)
        volume_profile = np.zeros(self.num_bins)
        
        # Distribute volume across price levels
        for _, row in df.iterrows():
            volume = row.get("volume", 1)
            
            for i in range(self.num_bins):
                bucket_low = price_levels[i]
                bucket_high = price_levels[i + 1]
                
                # Check if candle overlaps with this bucket
                if row["high"] >= bucket_low and row["low"] <= bucket_high:
                    # Proportion of candle in this bucket
                    candle_range = row["high"] - row["low"]
                    if candle_range > 0:
                        overlap_low = max(row["low"], bucket_low)
                        overlap_high = min(row["high"], bucket_high)
                        overlap = overlap_high - overlap_low
                        proportion = overlap / candle_range
                    else:
                        proportion = 1.0
                    
                    volume_profile[i] += volume * proportion
        
        # Find Point of Control (highest volume level)
        poc_idx = np.argmax(volume_profile)
        poc = (price_levels[poc_idx] + price_levels[poc_idx + 1]) / 2
        
        # Calculate Value Area (70% of volume)
        total_volume = volume_profile.sum()
        target_volume = total_volume * self.value_area_pct
        
        # Start from POC and expand outward
        va_volume = volume_profile[poc_idx]
        va_low_idx = poc_idx
        va_high_idx = poc_idx
        
        while va_volume < target_volume and (va_low_idx > 0 or va_high_idx < self.num_bins - 1):
            # Check which direction to expand
            low_vol = volume_profile[va_low_idx - 1] if va_low_idx > 0 else 0
            high_vol = volume_profile[va_high_idx + 1] if va_high_idx < self.num_bins - 1 else 0
            
            if low_vol >= high_vol and va_low_idx > 0:
                va_low_idx -= 1
                va_volume += low_vol
            elif va_high_idx < self.num_bins - 1:
                va_high_idx += 1
                va_volume += high_vol
            else:
                break
        
        value_area_low = price_levels[va_low_idx]
        value_area_high = price_levels[va_high_idx + 1]
        
        # Identify HVN and LVN zones
        hvn_zones, lvn_zones = self._identify_nodes(volume_profile, price_levels)
        
        return VolumeProfileResult(
            poc=poc,
            value_area_high=value_area_high,
            value_area_low=value_area_low,
            profile=volume_profile,
            price_levels=price_levels,
            hvn_zones=hvn_zones,
            lvn_zones=lvn_zones,
        )
    
    def _identify_nodes(
        self,
        profile: np.ndarray,
        price_levels: np.ndarray,
    ) -> Tuple[List[VolumeNode], List[VolumeNode]]:
        """Identify High Volume and Low Volume Nodes."""
        mean_vol = profile.mean()
        std_vol = profile.std()
        
        hvn_threshold = mean_vol + std_vol * self.hvn_std_multiplier
        lvn_threshold = mean_vol - std_vol * 0.5  # LVN below mean - 0.5 std
        
        hvn_zones = []
        lvn_zones = []
        
        for i in range(len(profile)):
            node = VolumeNode(
                price_low=price_levels[i],
                price_high=price_levels[i + 1],
                volume=profile[i],
                is_hvn=profile[i] >= hvn_threshold,
            )
            
            if profile[i] >= hvn_threshold:
                hvn_zones.append(node)
            elif profile[i] <= lvn_threshold:
                node.is_hvn = False
                lvn_zones.append(node)
        
        # Merge adjacent HVN zones
        hvn_zones = self._merge_adjacent_zones(hvn_zones)
        
        return hvn_zones, lvn_zones
    
    def _merge_adjacent_zones(self, zones: List[VolumeNode]) -> List[VolumeNode]:
        """Merge adjacent volume nodes into larger zones."""
        if not zones:
            return []
        
        # Sort by price
        sorted_zones = sorted(zones, key=lambda z: z.price_low)
        merged = [sorted_zones[0]]
        
        for zone in sorted_zones[1:]:
            last = merged[-1]
            
            # Check if adjacent (within small gap)
            gap = zone.price_low - last.price_high
            zone_size = last.price_high - last.price_low
            
            if gap <= zone_size * 0.1:  # Within 10% of zone size
                # Merge
                merged[-1] = VolumeNode(
                    price_low=last.price_low,
                    price_high=zone.price_high,
                    volume=last.volume + zone.volume,
                    is_hvn=last.is_hvn,
                )
            else:
                merged.append(zone)
        
        return merged
    
    def is_near_poc(
        self,
        price: float,
        profile: VolumeProfileResult,
        tolerance_pips: float = 10.0,
    ) -> bool:
        """
        Check if price is near the Point of Control.
        
        Args:
            price: Current price
            profile: Volume profile result
            tolerance_pips: Distance tolerance in pips
            
        Returns:
            True if price is within tolerance of POC
        """
        pip_size = 0.0001  # For EUR/USD, etc.
        distance = abs(price - profile.poc)
        return distance <= tolerance_pips * pip_size
    
    def is_in_value_area(
        self,
        price: float,
        profile: VolumeProfileResult,
    ) -> bool:
        """
        Check if price is within the Value Area.
        
        Args:
            price: Current price
            profile: Volume profile result
            
        Returns:
            True if price is within value area
        """
        return profile.value_area_low <= price <= profile.value_area_high
    
    def is_near_hvn(
        self,
        price: float,
        profile: VolumeProfileResult,
        tolerance_pips: float = 5.0,
    ) -> bool:
        """
        Check if price is near a High Volume Node.
        
        Args:
            price: Current price
            profile: Volume profile result
            tolerance_pips: Distance tolerance in pips
            
        Returns:
            True if price is near an HVN
        """
        pip_size = 0.0001
        
        for hvn in profile.hvn_zones:
            if (hvn.price_low - tolerance_pips * pip_size <= price <= 
                hvn.price_high + tolerance_pips * pip_size):
                return True
        
        return False
    
    def get_nearest_hvn(
        self,
        price: float,
        profile: VolumeProfileResult,
        direction: int = 0,
    ) -> Optional[VolumeNode]:
        """
        Get the nearest HVN to the current price.
        
        Args:
            price: Current price
            profile: Volume profile result
            direction: 1 for above, -1 for below, 0 for any
            
        Returns:
            Nearest HVN or None
        """
        if not profile.hvn_zones:
            return None
        
        candidates = profile.hvn_zones
        
        if direction == 1:
            candidates = [h for h in profile.hvn_zones if h.midpoint > price]
        elif direction == -1:
            candidates = [h for h in profile.hvn_zones if h.midpoint < price]
        
        if not candidates:
            return None
        
        return min(candidates, key=lambda h: abs(h.midpoint - price))
    
    def add_to_dataframe(
        self,
        df: pd.DataFrame,
        lookback: int = 100,
        prefix: str = "vp",
    ) -> pd.DataFrame:
        """
        Add rolling Volume Profile analysis to DataFrame.
        
        Args:
            df: Input DataFrame
            lookback: Rolling window size
            prefix: Column name prefix
            
        Returns:
            DataFrame with volume profile columns
        """
        df_out = df.copy()
        df_out[f"{prefix}_poc"] = np.nan
        df_out[f"{prefix}_va_high"] = np.nan
        df_out[f"{prefix}_va_low"] = np.nan
        df_out[f"{prefix}_near_poc"] = False
        df_out[f"{prefix}_near_hvn"] = False
        
        for i in range(lookback, len(df)):
            window = df.iloc[i - lookback:i]
            profile = self.calculate(window)
            
            current_price = df["close"].iloc[i]
            
            df_out.iloc[i, df_out.columns.get_loc(f"{prefix}_poc")] = profile.poc
            df_out.iloc[i, df_out.columns.get_loc(f"{prefix}_va_high")] = profile.value_area_high
            df_out.iloc[i, df_out.columns.get_loc(f"{prefix}_va_low")] = profile.value_area_low
            df_out.iloc[i, df_out.columns.get_loc(f"{prefix}_near_poc")] = self.is_near_poc(current_price, profile)
            df_out.iloc[i, df_out.columns.get_loc(f"{prefix}_near_hvn")] = self.is_near_hvn(current_price, profile)
        
        return df_out


def calculate_volume_profile(
    df: pd.DataFrame,
    num_bins: int = 50,
) -> VolumeProfileResult:
    """
    Convenience function to calculate Volume Profile.
    
    Args:
        df: DataFrame with OHLC and volume data
        num_bins: Number of price bins
        
    Returns:
        VolumeProfileResult
    """
    calculator = VolumeProfileCalculator(num_bins=num_bins)
    return calculator.calculate(df)
