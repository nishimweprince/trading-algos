"""
Tests for technical indicators.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime

from data.historical import HistoricalDataLoader


@pytest.fixture
def sample_data():
    """Generate sample OHLCV data for testing."""
    loader = HistoricalDataLoader()
    return loader.generate_sample_data(
        instrument="EUR_USD",
        start=datetime(2023, 1, 1),
        end=datetime(2023, 3, 1),
        granularity="H1",
    )


class TestSupertrend:
    """Tests for Supertrend indicator."""
    
    def test_supertrend_calculation(self, sample_data):
        """Test basic Supertrend calculation."""
        from indicators.supertrend import SupertrendIndicator
        
        st = SupertrendIndicator(length=10, multiplier=3.0)
        result = st.calculate(sample_data)
        
        assert result.value is not None
        assert result.direction is not None
        assert len(result.value) == len(sample_data)
        assert len(result.direction) == len(sample_data)
    
    def test_supertrend_direction_values(self, sample_data):
        """Test that direction is only 1 or -1."""
        from indicators.supertrend import SupertrendIndicator
        
        st = SupertrendIndicator()
        result = st.calculate(sample_data)
        
        # Drop NaN values
        direction = result.direction.dropna()
        
        # All values should be 1 or -1
        assert set(direction.unique()).issubset({1, -1})
    
    def test_supertrend_add_to_dataframe(self, sample_data):
        """Test adding Supertrend to DataFrame."""
        from indicators.supertrend import SupertrendIndicator
        
        st = SupertrendIndicator()
        df_with_st = st.add_to_dataframe(sample_data, prefix="st")
        
        assert "st_value" in df_with_st.columns
        assert "st_direction" in df_with_st.columns
        assert "st_changed" in df_with_st.columns


class TestStochRSI:
    """Tests for Stochastic RSI indicator."""
    
    def test_stochrsi_calculation(self, sample_data):
        """Test basic StochRSI calculation."""
        from indicators.stochrsi import StochRSIIndicator
        
        stoch = StochRSIIndicator(length=14, rsi_length=14, k=3, d=3)
        result = stoch.calculate(sample_data)
        
        assert result.k is not None
        assert result.d is not None
        assert len(result.k) == len(sample_data)
    
    def test_stochrsi_range(self, sample_data):
        """Test that StochRSI values are in 0-100 range."""
        from indicators.stochrsi import StochRSIIndicator
        
        stoch = StochRSIIndicator()
        result = stoch.calculate(sample_data)
        
        k_values = result.k.dropna()
        
        assert k_values.min() >= 0
        assert k_values.max() <= 100
    
    def test_stochrsi_oversold_detection(self, sample_data):
        """Test oversold detection."""
        from indicators.stochrsi import StochRSIIndicator
        
        stoch = StochRSIIndicator(oversold=30)
        result = stoch.calculate(sample_data)
        
        # Check that is_oversold matches k < 30
        for i in range(len(result.k)):
            if pd.notna(result.k.iloc[i]) and pd.notna(result.is_oversold.iloc[i]):
                expected = result.k.iloc[i] < 30
                assert result.is_oversold.iloc[i] == expected


class TestFVG:
    """Tests for Fair Value Gap detector."""
    
    def test_fvg_detection(self, sample_data):
        """Test FVG detection."""
        from indicators.fvg import FVGDetector
        
        fvg = FVGDetector(min_gap_pips=1.0)
        zones = fvg.detect(sample_data)
        
        # Should find some FVGs
        assert isinstance(zones, list)
    
    def test_fvg_zone_properties(self, sample_data):
        """Test FVG zone properties."""
        from indicators.fvg import FVGDetector, FVGType
        
        fvg = FVGDetector(min_gap_pips=1.0)
        zones = fvg.detect(sample_data)
        
        for zone in zones:
            # Zone should have valid top/bottom
            assert zone.top > zone.bottom
            
            # Type should be bullish or bearish
            assert zone.type in (FVGType.BULLISH, FVGType.BEARISH)
            
            # Should have valid index
            assert 0 <= zone.index < len(sample_data)


class TestVolumeProfile:
    """Tests for Volume Profile calculator."""
    
    def test_volume_profile_calculation(self, sample_data):
        """Test Volume Profile calculation."""
        from indicators.volume_profile import VolumeProfileCalculator
        
        vp = VolumeProfileCalculator(num_bins=30)
        result = vp.calculate(sample_data)
        
        # Should have POC
        assert result.poc > 0
        
        # POC should be within price range
        assert sample_data["low"].min() <= result.poc <= sample_data["high"].max()
    
    def test_value_area(self, sample_data):
        """Test Value Area calculation."""
        from indicators.volume_profile import VolumeProfileCalculator
        
        vp = VolumeProfileCalculator(value_area_pct=0.70)
        result = vp.calculate(sample_data)
        
        # Value area should be within price range
        assert result.value_area_low <= result.value_area_high
        assert result.value_area_low >= sample_data["low"].min()
        assert result.value_area_high <= sample_data["high"].max()
    
    def test_hvn_detection(self, sample_data):
        """Test High Volume Node detection."""
        from indicators.volume_profile import VolumeProfileCalculator
        
        vp = VolumeProfileCalculator()
        result = vp.calculate(sample_data)
        
        # Should identify some HVN zones
        assert isinstance(result.hvn_zones, list)
        
        for hvn in result.hvn_zones:
            assert hvn.price_low < hvn.price_high
            assert hvn.volume > 0
            assert hvn.is_hvn is True


class TestIntegration:
    """Integration tests for all indicators together."""
    
    def test_all_indicators_compatible(self, sample_data):
        """Test that all indicators work on same data."""
        from indicators import (
            SupertrendIndicator,
            StochRSIIndicator,
            FVGDetector,
            VolumeProfileCalculator,
        )
        
        # All should complete without error
        st = SupertrendIndicator()
        st_result = st.calculate(sample_data)
        
        stoch = StochRSIIndicator()
        stoch_result = stoch.calculate(sample_data)
        
        fvg = FVGDetector()
        fvg_zones = fvg.detect(sample_data)
        
        vp = VolumeProfileCalculator()
        vp_result = vp.calculate(sample_data)
        
        # All results should be valid
        assert st_result.direction is not None
        assert stoch_result.k is not None
        assert isinstance(fvg_zones, list)
        assert vp_result.poc > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
