"""
Tests for strategy components.
"""

import sys
from pathlib import Path
from datetime import datetime

import pandas as pd
import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from data.historical import HistoricalDataLoader


@pytest.fixture
def sample_data():
    """Generate sample OHLCV data for testing."""
    loader = HistoricalDataLoader()
    return loader.generate_sample_data(
        instrument="EUR_USD",
        start=datetime(2023, 1, 1),
        end=datetime(2023, 6, 1),
        granularity="H1",
    )


class TestSignalGenerator:
    """Tests for SignalGenerator class."""
    
    def test_signal_generator_initialization(self):
        """Test SignalGenerator initialization."""
        from strategy.signal_generator import SignalGenerator
        
        gen = SignalGenerator()
        
        assert gen.supertrend is not None
        assert gen.stochrsi is not None
        assert gen.fvg_detector is not None
        assert gen.volume_profile is not None
    
    def test_prepare_data(self, sample_data):
        """Test data preparation."""
        from strategy.signal_generator import SignalGenerator
        
        gen = SignalGenerator()
        gen.prepare_data(sample_data)
        
        # Should have prepared both timeframes
        assert gen._df_1h is not None
        assert gen._df_4h is not None
        
        # 1H should have trend_4h column
        assert "trend_4h" in gen._df_1h.columns
    
    def test_generate_signals(self, sample_data):
        """Test signal generation."""
        from strategy.signal_generator import SignalGenerator
        
        gen = SignalGenerator()
        df_signals = gen.generate_signals(sample_data)
        
        # Should have signal column
        assert "signal" in df_signals.columns
        
        # Signals should be -1, 0, or 1
        assert df_signals["signal"].isin([-1, 0, 1]).all()
    
    def test_get_indicator_values(self, sample_data):
        """Test getting indicator values."""
        from strategy.signal_generator import SignalGenerator
        
        gen = SignalGenerator()
        gen.prepare_data(sample_data)
        
        values = gen.get_indicator_values()
        
        assert "close" in values
        assert "trend_4h" in values
        assert "stochrsi_k" in values


class TestTradingFilters:
    """Tests for trading filters."""
    
    def test_time_filter_weekday(self):
        """Test time filter allows weekdays."""
        from strategy.filters import TimeFilter
        
        filt = TimeFilter(excluded_days=[5, 6])  # Sat, Sun
        
        # Monday should be allowed
        monday = pd.Timestamp("2023-06-05 10:00:00")
        assert filt.is_allowed(monday) is True
        
        # Saturday should be excluded
        saturday = pd.Timestamp("2023-06-03 10:00:00")
        assert filt.is_allowed(saturday) is False
    
    def test_time_filter_hours(self):
        """Test time filter excludes specific hours."""
        from strategy.filters import TimeFilter
        
        filt = TimeFilter(excluded_hours=[22, 23, 0, 1])
        
        # 10:00 should be allowed
        allowed = pd.Timestamp("2023-06-05 10:00:00")
        assert filt.is_allowed(allowed) is True
        
        # 23:00 should be excluded
        excluded = pd.Timestamp("2023-06-05 23:00:00")
        assert filt.is_allowed(excluded) is False
    
    def test_spread_filter(self):
        """Test spread filter."""
        from strategy.filters import SpreadFilter
        
        filt = SpreadFilter(max_spread_pips=3.0)
        
        # Low spread should be allowed
        assert filt.is_allowed(0.0002) is True  # 2 pips
        
        # High spread should be rejected
        assert filt.is_allowed(0.0005) is False  # 5 pips
    
    def test_combined_filters(self):
        """Test combined trading filters."""
        from strategy.filters import TradingFilters
        
        filters = TradingFilters.create_default()
        
        # Valid conditions
        valid_time = pd.Timestamp("2023-06-05 10:00:00")
        is_valid, reasons = filters.validate_signal(valid_time)
        
        assert is_valid is True
        assert len(reasons) == 0


class TestPositionManager:
    """Tests for position management."""
    
    def test_create_position(self):
        """Test position creation."""
        from execution.position_manager import PositionManager, PositionSide
        
        pm = PositionManager()
        
        pos = pm.create_position(
            instrument="EUR_USD",
            side=PositionSide.LONG,
            units=10000,
            entry_price=1.0800,
            stop_loss=1.0750,
            take_profit=1.0900,
        )
        
        assert pos.id is not None
        assert pos.instrument == "EUR_USD"
        assert pos.units == 10000
        assert pos.is_open is True
    
    def test_close_position(self):
        """Test position closing."""
        from execution.position_manager import PositionManager, PositionSide
        
        pm = PositionManager()
        
        pos = pm.create_position(
            instrument="EUR_USD",
            side=PositionSide.LONG,
            units=10000,
            entry_price=1.0800,
        )
        
        pm.close_position(pos.id, exit_price=1.0850)
        
        assert pos.is_open is False
        assert pos.exit_price == 1.0850
        assert pos.realized_pnl > 0  # Should be profitable
    
    def test_check_stops(self):
        """Test stop loss checking."""
        from execution.position_manager import PositionManager, PositionSide
        
        pm = PositionManager()
        
        pos = pm.create_position(
            instrument="EUR_USD",
            side=PositionSide.LONG,
            units=10000,
            entry_price=1.0800,
            stop_loss=1.0750,
            take_profit=1.0900,
        )
        
        # Price hits stop loss
        closed = pm.check_stops(
            instrument="EUR_USD",
            current_high=1.0780,
            current_low=1.0740,  # Below stop
            current_time=datetime.now(),
        )
        
        assert len(closed) == 1
        assert closed[0].id == pos.id
        assert pos.is_open is False
    
    def test_statistics(self):
        """Test position statistics."""
        from execution.position_manager import PositionManager, PositionSide
        
        pm = PositionManager()
        
        # Create and close some positions
        pos1 = pm.create_position(
            instrument="EUR_USD",
            side=PositionSide.LONG,
            units=10000,
            entry_price=1.0800,
        )
        pm.close_position(pos1.id, exit_price=1.0850)  # Win
        
        pos2 = pm.create_position(
            instrument="EUR_USD",
            side=PositionSide.LONG,
            units=10000,
            entry_price=1.0800,
        )
        pm.close_position(pos2.id, exit_price=1.0750)  # Loss
        
        stats = pm.get_statistics()
        
        assert stats["total_trades"] == 2
        assert stats["winning_trades"] == 1
        assert stats["losing_trades"] == 1
        assert stats["win_rate"] == 0.5


class TestRiskManagement:
    """Tests for risk management components."""
    
    def test_position_sizing(self):
        """Test position size calculation."""
        from risk.position_sizing import PositionSizer
        
        sizer = PositionSizer(
            account_balance=10000,
            max_risk_pct=0.02,
        )
        
        result = sizer.calculate_fixed_risk(
            entry_price=1.0800,
            stop_loss=1.0750,  # 50 pips
        )
        
        # Risk should be approximately $200 (2% of 10000)
        assert result.risk_amount <= 210  # Some rounding
        assert result.units > 0
    
    def test_stop_loss_calculation(self):
        """Test stop loss calculation."""
        from risk.stop_loss import StopLossCalculator
        from data.historical import HistoricalDataLoader
        
        loader = HistoricalDataLoader()
        df = loader.generate_sample_data(
            instrument="EUR_USD",
            start=datetime(2023, 1, 1),
            end=datetime(2023, 2, 1),
            granularity="H1",
        )
        
        calc = StopLossCalculator(atr_multiplier=1.5)
        
        stops = calc.calculate_atr_stop(
            df=df,
            entry_price=1.0800,
            is_long=True,
        )
        
        # Stop should be below entry for long
        assert stops.stop_loss < 1.0800
        # Take profit should be above entry for long
        assert stops.take_profit > 1.0800
    
    def test_exposure_manager(self):
        """Test exposure management."""
        from risk.exposure import ExposureManager
        from execution.position_manager import PositionManager, PositionSide
        
        pm = PositionManager()
        em = ExposureManager(
            account_balance=10000,
            max_total_exposure=0.06,
        )
        
        # Can open when no positions
        can_open, reason = em.can_open_position(
            instrument="EUR_USD",
            risk_amount=200,
            position_manager=pm,
        )
        assert can_open is True
        
        # Create position with 5% risk
        pm.create_position(
            instrument="EUR_USD",
            side=PositionSide.LONG,
            units=10000,
            entry_price=1.0800,
        )
        pm.open_positions[0].risk_amount = 500  # 5% risk
        
        # Should block opening with 2% more (would exceed 6%)
        can_open, reason = em.can_open_position(
            instrument="EUR_USD",
            risk_amount=200,
            position_manager=pm,
        )
        assert can_open is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
