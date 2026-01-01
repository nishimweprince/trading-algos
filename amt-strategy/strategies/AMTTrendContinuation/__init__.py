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
            self.filter_min_risk_reward
        ]
    
    # Removed NY Session filter as per user request
    
    def filter_min_risk_reward(self):
        """Ensure minimum 2:1 reward-to-risk ratio"""
        if self.average_entry_price and self.average_stop_loss:
            risk = abs(self.average_entry_price - self.average_stop_loss)
            reward = abs(self.average_take_profit - self.average_entry_price)
            # Prevent division by zero if risk is zero (unlikely but safe)
            if risk == 0:
                return True
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
        # Ensure we have enough candles
        if len(self.candles) < 100:
             return None
        return cta.volume_profile(self.candles[-100:-50], num_bins=40)
    
    @property
    def prior_balance_poc(self):
        profile = self.prior_balance_profile
        return profile.poc if profile else None
    
    @property
    @cached
    def impulse_profile(self):
        """Volume Profile of recent impulse move (last 30 candles)"""
        if len(self.candles) < 30:
            return None
        return cta.volume_profile(self.candles[-30:], num_bins=30, lvn_threshold=0.20)
    
    @property
    def impulse_lvns(self):
        """Low Volume Nodes on the impulse leg - potential retracement targets"""
        profile = self.impulse_profile
        return profile.lvn if profile else np.array([])
    
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
        if len(recent) < 5:
            return False
            
        bullish_candles = sum(1 for c in recent if c[2] > c[1])
        avg_range = np.mean(recent[:, 3] - recent[:, 4])
        
        if len(self.candles) < 15:
            return False
            
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
        if len(recent) < 5:
            return False

        bearish_candles = sum(1 for c in recent if c[2] < c[1])
        avg_range = np.mean(recent[:, 3] - recent[:, 4])
        
        if len(self.candles) < 15:
            return False
            
        prior_range = np.mean(self.candles[-15:-10, 3] - self.candles[-15:-10, 4])
        
        return (displacement > self.atr * 2.5 and 
                bearish_candles >= 3 and 
                avg_range > prior_range * 1.4)
    
    @property
    def bullish_aggression(self):
        """Confirm buying aggression at LVN (rejection candle)"""
        if len(self.candles) < 10:
            return False
        
        current = self.candles[-1]
        body = abs(current[2] - current[1])
        lower_wick = min(current[1], current[2]) - current[4]
        upper_wick = current[3] - max(current[1], current[2])
        
        # Bullish rejection: long lower wick, small body, closes in upper half
        # Adjusted logic: if body is very small, we treat it as doji-like, require strong wick
        is_rejection = (lower_wick > body * 2 and 
                       current[2] > current[1] and
                       upper_wick < body)
        
        # Volume confirmation
        vol_spike = current[5] > np.mean(self.candles[-10:-1, 5]) * 1.3
        
        return is_rejection and vol_spike
    
    @property
    def bearish_aggression(self):
        """Confirm selling aggression at LVN"""
        if len(self.candles) < 10:
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
        # Fallback if lvn is None (though should_long checked it, race conditions unlikely but possible)
        if lvn is None:
            return
            
        stop = lvn - self.atr * 0.75  # Stop beyond LVN
        target = self.prior_balance_poc
        if target is None:
            return
        
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
        if lvn is None:
            return
            
        stop = lvn + self.atr * 0.75
        target = self.prior_balance_poc
        if target is None:
            return
        
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
        ]
