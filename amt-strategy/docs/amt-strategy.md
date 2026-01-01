# Jesse Trade Implementation Specification for AMT Trend Continuation Strategy

Jesse Trade supports **cryptocurrency futures only**—ES and NQ futures are not available. However, this complete implementation specification applies to crypto perpetual futures (BTC-USDT, ETH-USDT) where Auction Market Theory principles function identically. The framework's architecture makes it ideal for implementing Fabio Valentini's methodology on 24/7 crypto markets.

## Core framework architecture enables systematic AMT trading

Jesse's Strategy class provides a well-defined lifecycle that maps directly to the AMT Trend Continuation workflow. Each candle triggers methods in sequence: `before()` → `should_long()`/`should_short()` → `go_long()`/`go_short()` → `update_position()` → `after()`. This deterministic flow ensures consistent execution of the imbalance-detection, LVN-identification, and entry-confirmation logic.

```python
from jesse.strategies import Strategy, cached
from jesse import utils
import jesse.indicators as ta
import numpy as np
from datetime import datetime
from typing import NamedTuple

class VolumeProfileData(NamedTuple):
    poc: float
    vah: float
    val: float
    profile: np.ndarray
    price_levels: np.ndarray
    hvn: np.ndarray
    lvn: np.ndarray
```

The **filter system** prevents trades outside the NY session by returning filter methods (without parentheses) in a list. Filters execute before `go_long()`/`go_short()`, cleanly separating session logic from entry conditions:

```python
def filters(self):
    return [
        self.filter_ny_session,
        self.filter_min_risk_reward
    ]

def filter_ny_session(self):
    """Trade only during NY session (13:00-21:00 UTC / 8AM-4PM EST)"""
    hour = datetime.utcfromtimestamp(self.time / 1000).hour
    return 13 <= hour < 21
```

## Volume Profile implementation from OHLCV data

Jesse lacks a built-in Volume Profile indicator, requiring custom implementation. Place this in `custom_indicators/volume_profile.py`:

```python
import numpy as np
from typing import NamedTuple

class VolumeProfileResult(NamedTuple):
    poc: float
    vah: float
    val: float
    profile: np.ndarray
    price_levels: np.ndarray
    hvn: np.ndarray
    lvn: np.ndarray

def volume_profile(candles: np.ndarray, num_bins: int = 50,
                   value_area_pct: float = 0.70,
                   lvn_threshold: float = 0.25) -> VolumeProfileResult:
    """
    Calculate Volume Profile with POC, Value Area, HVN, and LVN.
    
    :param candles: Jesse candle array [timestamp, open, close, high, low, volume]
    :param num_bins: Number of price levels for distribution
    :param value_area_pct: Percentage of volume for Value Area (default 70%)
    :param lvn_threshold: Volume threshold for LVN detection (bottom 25%)
    """
    highs = candles[:, 3]
    lows = candles[:, 4]
    volumes = candles[:, 5]
    
    price_min, price_max = np.min(lows), np.max(highs)
    bin_size = (price_max - price_min) / num_bins
    price_levels = np.linspace(price_min + bin_size/2, price_max - bin_size/2, num_bins)
    volume_at_price = np.zeros(num_bins)
    
    # Distribute each candle's volume across price bins it touched
    for i in range(len(candles)):
        low_bin = max(0, min(int((lows[i] - price_min) / bin_size), num_bins - 1))
        high_bin = max(0, min(int((highs[i] - price_min) / bin_size), num_bins - 1))
        bins_touched = high_bin - low_bin + 1
        volume_per_bin = volumes[i] / bins_touched
        for b in range(low_bin, high_bin + 1):
            volume_at_price[b] += volume_per_bin
    
    # POC: highest volume price level
    poc_idx = np.argmax(volume_at_price)
    poc = price_levels[poc_idx]
    
    # Value Area: expand from POC until 70% of volume captured
    total_vol = np.sum(volume_at_price)
    target_vol = total_vol * value_area_pct
    va_vol = volume_at_price[poc_idx]
    low_idx, high_idx = poc_idx, poc_idx
    
    while va_vol < target_vol and (low_idx > 0 or high_idx < num_bins - 1):
        low_vol = volume_at_price[low_idx - 1] if low_idx > 0 else 0
        high_vol = volume_at_price[high_idx + 1] if high_idx < num_bins - 1 else 0
        if low_vol >= high_vol and low_idx > 0:
            low_idx -= 1
            va_vol += volume_at_price[low_idx]
        elif high_idx < num_bins - 1:
            high_idx += 1
            va_vol += volume_at_price[high_idx]
        else:
            break
    
    val, vah = price_levels[low_idx], price_levels[high_idx]
    
    # Identify HVN and LVN
    max_vol = np.max(volume_at_price)
    hvn_mask = volume_at_price >= max_vol * (1 - lvn_threshold)
    lvn_mask = volume_at_price <= max_vol * lvn_threshold
    
    return VolumeProfileResult(
        poc=poc, vah=vah, val=val,
        profile=volume_at_price,
        price_levels=price_levels,
        hvn=price_levels[hvn_mask],
        lvn=price_levels[lvn_mask]
    )
```

The `@cached` decorator (placed after `@property`) prevents recalculation on every method call within the same candle—critical for the computationally intensive Volume Profile:

```python
@property
@cached
def impulse_vp(self):
    """Volume Profile of the impulse leg (last 20 candles)"""
    return cta.volume_profile(self.candles[-20:], num_bins=30)
```

## Market state detection identifies imbalance conditions

The AMT model requires detecting when price trades **out-of-balance**—displaying momentum/displacement away from prior value. This implementation measures displacement using ATR multiples and confirms imbalance through consecutive directional candles:

```python
@property
def is_bullish_imbalance(self):
    """Detect bullish out-of-balance: strong momentum away from prior value"""
    # Displacement: price moved > 2 ATR from prior session's POC
    prior_poc = self.prior_balance_poc
    displacement = self.close - prior_poc
    atr = ta.atr(self.candles, period=14)
    
    # Momentum: 3+ consecutive bullish candles with expanding range
    recent = self.candles[-5:]
    bullish_count = sum(1 for c in recent if c[2] > c[1])  # close > open
    avg_range = np.mean(recent[:, 3] - recent[:, 4])  # high - low
    prior_avg_range = np.mean(self.candles[-10:-5, 3] - self.candles[-10:-5, 4])
    
    return (displacement > atr * 2 and 
            bullish_count >= 3 and 
            avg_range > prior_avg_range * 1.3)

@property
def is_bearish_imbalance(self):
    """Detect bearish out-of-balance condition"""
    prior_poc = self.prior_balance_poc
    displacement = prior_poc - self.close
    atr = ta.atr(self.candles, period=14)
    
    recent = self.candles[-5:]
    bearish_count = sum(1 for c in recent if c[2] < c[1])
    avg_range = np.mean(recent[:, 3] - recent[:, 4])
    prior_avg_range = np.mean(self.candles[-10:-5, 3] - self.candles[-10:-5, 4])
    
    return (displacement > atr * 2 and 
            bearish_count >= 3 and 
            avg_range > prior_avg_range * 1.3)
```

## Complete strategy implementation

```python
from jesse.strategies import Strategy, cached
from jesse import utils
import jesse.indicators as ta
import custom_indicators as cta
import numpy as np
from datetime import datetime

class AMTTrendContinuation(Strategy):
    """
    Auction Market Theory Trend Continuation Strategy
    Based on Fabio Valentini's methodology:
    1. Identify out-of-balance (imbalance) market conditions
    2. Calculate Volume Profile on impulse leg to find LVNs
    3. Wait for retracement to LVN with aggression confirmation
    4. Enter with stop beyond LVN, target prior balance POC
    """
    
    def __init__(self):
        super().__init__()
        self.vars['prior_balance_poc'] = None
        self.vars['impulse_start_idx'] = None
        self.vars['in_retracement'] = False
        self.vars['lvn_entry_zone'] = None
    
    # ========== FILTERS ==========
    def filters(self):
        return [
            self.filter_ny_session,
            self.filter_min_risk_reward
        ]
    
    def filter_ny_session(self):
        """Trade only during NY session (8 AM - 4 PM EST)"""
        hour = datetime.utcfromtimestamp(self.time / 1000).hour
        return 13 <= hour < 21  # UTC equivalent
    
    def filter_min_risk_reward(self):
        """Ensure minimum 2:1 reward-to-risk ratio"""
        if self.average_entry_price and self.average_stop_loss:
            risk = abs(self.average_entry_price - self.average_stop_loss)
            reward = abs(self.average_take_profit - self.average_entry_price)
            return reward >= risk * 2
        return True
    
    # ========== INDICATORS ==========
    @property
    def atr(self):
        return ta.atr(self.candles, period=14)
    
    @property
    @cached
    def prior_balance_profile(self):
        """Volume Profile of prior balance area (candles 100-50 back)"""
        return cta.volume_profile(self.candles[-100:-50], num_bins=40)
    
    @property
    def prior_balance_poc(self):
        return self.prior_balance_profile.poc
    
    @property
    @cached
    def impulse_profile(self):
        """Volume Profile of recent impulse move (last 30 candles)"""
        return cta.volume_profile(self.candles[-30:], num_bins=30, lvn_threshold=0.20)
    
    @property
    def impulse_lvns(self):
        """Low Volume Nodes on the impulse leg - potential retracement targets"""
        return self.impulse_profile.lvn
    
    @property
    def nearest_lvn(self):
        """Find LVN closest to current price for entry zone"""
        lvns = self.impulse_lvns
        if len(lvns) == 0:
            return None
        distances = np.abs(lvns - self.close)
        return lvns[np.argmin(distances)]
    
    # ========== MARKET STATE DETECTION ==========
    @property
    def is_bullish_imbalance(self):
        """Detect bullish displacement from prior value"""
        poc = self.prior_balance_poc
        if poc is None:
            return False
        
        displacement = self.close - poc
        recent = self.candles[-5:]
        bullish_candles = sum(1 for c in recent if c[2] > c[1])
        avg_range = np.mean(recent[:, 3] - recent[:, 4])
        prior_range = np.mean(self.candles[-15:-10, 3] - self.candles[-15:-10, 4])
        
        return (displacement > self.atr * 2.5 and 
                bullish_candles >= 3 and 
                avg_range > prior_range * 1.4)
    
    @property
    def is_bearish_imbalance(self):
        """Detect bearish displacement from prior value"""
        poc = self.prior_balance_poc
        if poc is None:
            return False
        
        displacement = poc - self.close
        recent = self.candles[-5:]
        bearish_candles = sum(1 for c in recent if c[2] < c[1])
        avg_range = np.mean(recent[:, 3] - recent[:, 4])
        prior_range = np.mean(self.candles[-15:-10, 3] - self.candles[-15:-10, 4])
        
        return (displacement > self.atr * 2.5 and 
                bearish_candles >= 3 and 
                avg_range > prior_range * 1.4)
    
    @property
    def bullish_aggression(self):
        """Confirm buying aggression at LVN (rejection candle)"""
        if len(self.candles) < 3:
            return False
        
        current = self.candles[-1]
        body = abs(current[2] - current[1])
        lower_wick = min(current[1], current[2]) - current[4]
        upper_wick = current[3] - max(current[1], current[2])
        
        # Bullish rejection: long lower wick, small body, closes in upper half
        is_rejection = (lower_wick > body * 2 and 
                       current[2] > current[1] and
                       upper_wick < body)
        
        # Volume confirmation
        vol_spike = current[5] > np.mean(self.candles[-10:-1, 5]) * 1.3
        
        return is_rejection and vol_spike
    
    @property
    def bearish_aggression(self):
        """Confirm selling aggression at LVN"""
        if len(self.candles) < 3:
            return False
        
        current = self.candles[-1]
        body = abs(current[2] - current[1])
        upper_wick = current[3] - max(current[1], current[2])
        lower_wick = min(current[1], current[2]) - current[4]
        
        is_rejection = (upper_wick > body * 2 and 
                       current[2] < current[1] and
                       lower_wick < body)
        
        vol_spike = current[5] > np.mean(self.candles[-10:-1, 5]) * 1.3
        
        return is_rejection and vol_spike
    
    @property
    def at_lvn_zone(self):
        """Check if price has retraced to LVN zone"""
        lvn = self.nearest_lvn
        if lvn is None:
            return False
        tolerance = self.atr * 0.5
        return abs(self.close - lvn) <= tolerance
    
    # ========== ENTRY CONDITIONS ==========
    def should_long(self):
        """Long entry: bullish imbalance + retracement to LVN + bullish aggression"""
        return (self.is_bullish_imbalance and 
                self.at_lvn_zone and 
                self.bullish_aggression)
    
    def should_short(self):
        """Short entry: bearish imbalance + retracement to LVN + bearish aggression"""
        return (self.is_bearish_imbalance and 
                self.at_lvn_zone and 
                self.bearish_aggression)
    
    def go_long(self):
        """Execute long: stop below LVN, target prior POC"""
        entry = self.close
        lvn = self.nearest_lvn
        stop = lvn - self.atr * 0.75  # Stop beyond LVN
        target = self.prior_balance_poc
        
        # Position sizing: 0.25-0.5% account risk
        risk_pct = 0.5
        qty = utils.risk_to_qty(
            self.balance, risk_pct, entry, stop, fee_rate=self.fee_rate
        )
        
        # Cap position at 10% of balance
        max_qty = utils.size_to_qty(self.balance * 0.10, entry, fee_rate=self.fee_rate)
        qty = min(qty, max_qty)
        
        self.buy = qty, entry
        self.stop_loss = qty, stop
        self.take_profit = [
            (qty * 0.5, entry + (target - entry) * 0.5),  # 50% at halfway
            (qty * 0.5, target)                           # 50% at POC
        ]
    
    def go_short(self):
        """Execute short: stop above LVN, target prior POC"""
        entry = self.close
        lvn = self.nearest_lvn
        stop = lvn + self.atr * 0.75
        target = self.prior_balance_poc
        
        risk_pct = 0.5
        qty = utils.risk_to_qty(
            self.balance, risk_pct, entry, stop, fee_rate=self.fee_rate
        )
        max_qty = utils.size_to_qty(self.balance * 0.10, entry, fee_rate=self.fee_rate)
        qty = min(qty, max_qty)
        
        self.sell = qty, entry
        self.stop_loss = qty, stop
        self.take_profit = [
            (qty * 0.5, entry - (entry - target) * 0.5),
            (qty * 0.5, target)
        ]
    
    def should_cancel_entry(self):
        """Cancel if market structure changes"""
        return True  # Re-evaluate each candle
    
    # ========== POSITION MANAGEMENT ==========
    def update_position(self):
        """Trail stop to break-even after 1.5 ATR profit"""
        if self.position.pnl_percentage >= 0:
            profit_distance = abs(self.close - self.position.entry_price)
            
            if profit_distance >= self.atr * 1.5:
                # Move stop to break-even + small buffer
                if self.is_long:
                    new_stop = self.position.entry_price + self.atr * 0.1
                    self.stop_loss = self.position.qty, new_stop
                else:
                    new_stop = self.position.entry_price - self.atr * 0.1
                    self.stop_loss = self.position.qty, new_stop
    
    def on_reduced_position(self, order):
        """After first TP, tighten stop"""
        if self.is_long:
            self.stop_loss = self.position.qty, self.position.entry_price + self.atr * 0.25
        else:
            self.stop_loss = self.position.qty, self.position.entry_price - self.atr * 0.25
    
    # ========== DEBUGGING ==========
    def watch_list(self):
        return [
            ('Prior POC', round(self.prior_balance_poc, 2) if self.prior_balance_poc else 'N/A'),
            ('Nearest LVN', round(self.nearest_lvn, 2) if self.nearest_lvn else 'N/A'),
            ('ATR', round(self.atr, 2)),
            ('Bull Imbalance', self.is_bullish_imbalance),
            ('Bear Imbalance', self.is_bearish_imbalance),
            ('At LVN', self.at_lvn_zone),
            ('NY Session', self.filter_ny_session()),
        ]
```

## Risk management enforces 0.25-0.5% per trade

The `utils.risk_to_qty()` function calculates exact position size to risk a specified percentage. Given a **$10,000 account** risking **0.5%** with entry at **$100** and stop at **$95**:

| Parameter | Value |
|-----------|-------|
| Capital at risk | $50 (0.5% × $10,000) |
| Risk per unit | $5 ($100 - $95) |
| Position size | 10 units ($50 ÷ $5) |

The implementation caps maximum position size at 10% of balance to prevent `risk_to_qty` from returning oversized positions when stops are tight:

```python
risk_qty = utils.risk_to_qty(self.balance, 0.5, entry, stop, self.fee_rate)
max_qty = utils.size_to_qty(self.balance * 0.10, entry, fee_rate=self.fee_rate)
qty = min(risk_qty, max_qty)
```

## Configuration for crypto futures backtesting

Since Jesse doesn't support ES/NQ, configure for crypto perpetual futures which exhibit similar AMT dynamics:

```python
# routes.py equivalent (configured via dashboard)
routes = [
    {
        'exchange': 'Binance Perpetual Futures',
        'strategy': 'AMTTrendContinuation',
        'symbol': 'BTC-USDT',
        'timeframe': '15m'
    }
]

extra_routes = [
    {'exchange': 'Binance Perpetual Futures', 'symbol': 'BTC-USDT', 'timeframe': '1h'},
    {'exchange': 'Binance Perpetual Futures', 'symbol': 'BTC-USDT', 'timeframe': '4h'},
]
```

The **project structure** organizes custom indicators and strategies:

```
project/
├── .env                           # API keys, database config
├── strategies/
│   └── AMTTrendContinuation/
│       └── __init__.py            # Main strategy code
├── custom_indicators/
│   ├── __init__.py                # Export: from .volume_profile import *
│   └── volume_profile.py          # Volume Profile implementation
└── storage/
    └── logs/                      # Trade logs and charts
```

## Key adaptations for cryptocurrency markets

The AMT framework translates directly to crypto with these adjustments:

- **Session filter modification**: Crypto trades 24/7, but the NY session filter (13:00-21:00 UTC) captures peak institutional activity when BTC/ETH correlate most strongly with equity index futures
- **Increased ATR multipliers**: Crypto volatility demands wider stops—use **2.5-3× ATR** for displacement detection versus 2× for ES/NQ
- **LVN threshold adjustment**: Set `lvn_threshold=0.20` (20% of max volume) rather than 25% due to crypto's thinner order books
- **Multiple timeframe confirmation**: Add 1h and 4h extra routes to confirm higher-timeframe imbalance direction

The implementation preserves Valentini's core logic: identify displacement from value, locate inefficiencies (LVNs) created during the impulse, enter on aggressive rejection at those levels, and target the magnetic pull of prior value (POC). Jesse's lifecycle methods map cleanly to this workflow, with `should_long()`/`should_short()` handling multi-condition entry logic and `update_position()` managing the break-even and trailing mechanics essential to protecting profits in trending markets.