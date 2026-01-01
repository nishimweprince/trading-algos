from jesse.strategies import Strategy, cached
from jesse import utils
import jesse.indicators as ta
import custom_indicators as cta
import numpy as np
from datetime import datetime

class AMTMeanReversion(Strategy):
    """
    Auction Market Theory Mean Reversion Strategy
    Based on Fabio Valentini's methodology:
    1. Identify balance area (prior day's value area)
    2. Detect failed breakout attempts
    3. Wait for reclaim inside balance
    4. Enter on pullback to LVN with aggression
    5. Target: prior balance POC
    """

    def __init__(self):
        super().__init__()
        self.vars['breakout_attempt_detected'] = False
        self.vars['reclaim_start_idx'] = None

    # ========== FILTERS ==========
    def filters(self):
        return [
            self.filter_min_risk_reward,
            self.filter_volatility_regime
        ]

    def filter_min_risk_reward(self):
        """Ensure minimum 2:1 reward-to-risk ratio"""
        if self.average_entry_price and self.average_stop_loss:
            risk = abs(self.average_entry_price - self.average_stop_loss)
            reward = abs(self.average_take_profit - self.average_entry_price)
            if risk == 0:
                return True
            return reward >= risk * 2
        return True

    def filter_volatility_regime(self):
        """Avoid extremely low volatility"""
        if len(self.candles) < 50:
            return True

        current_atr = self.atr
        atr_values = [ta.atr(self.candles[-i:], 14) for i in range(30, 51)]
        avg_atr = np.mean(atr_values)

        return current_atr > avg_atr * 0.5

    # ========== INDICATORS ==========
    @property
    def atr(self):
        return ta.atr(self.candles, period=14)

    @property
    @cached
    def prior_balance_profile(self):
        """Volume Profile of previous trading day (balance reference)"""
        if self.timeframe == '5m':
            candles_per_day = 288
        elif self.timeframe == '15m':
            candles_per_day = 96
        elif self.timeframe == '1h':
            candles_per_day = 24
        else:
            candles_per_day = 50

        lookback_start = candles_per_day * 2
        lookback_end = candles_per_day

        if len(self.candles) < lookback_start:
            return None

        return cta.volume_profile(
            self.candles[-lookback_start:-lookback_end],
            num_bins=40,
            value_area_pct=0.70
        )

    @property
    def prior_balance_poc(self):
        profile = self.prior_balance_profile
        return profile.poc if profile else None

    @property
    def prior_balance_vah(self):
        profile = self.prior_balance_profile
        return profile.vah if profile else None

    @property
    def prior_balance_val(self):
        profile = self.prior_balance_profile
        return profile.val if profile else None

    @property
    @cached
    def reclaim_profile(self):
        """Volume Profile on the reclaim leg (NOT first move back)"""
        if self.vars['reclaim_start_idx'] is None:
            return None

        start_idx = self.vars['reclaim_start_idx']
        reclaim_candles = self.candles[start_idx:]

        if len(reclaim_candles) < 5:
            return None

        return cta.volume_profile(
            reclaim_candles,
            num_bins=min(len(reclaim_candles), 20),
            lvn_threshold=0.20
        )

    @property
    def reclaim_lvns(self):
        profile = self.reclaim_profile
        return profile.lvn if profile else np.array([])

    @property
    def nearest_lvn(self):
        """Find LVN closest to current price"""
        lvns = self.reclaim_lvns
        if len(lvns) == 0:
            return None
        distances = np.abs(lvns - self.close)
        return lvns[np.argmin(distances)]

    # ========== MARKET STATE DETECTION ==========
    @property
    def is_failed_upside_breakout(self):
        """Price attempted to break above VAH but failed"""
        vah = self.prior_balance_vah
        if vah is None:
            return False

        # Check recent 10 candles for breakout attempt
        recent = self.candles[-10:]
        if len(recent) < 10:
            return False

        recent_high = np.max(recent[:, 3])

        # Failed if: touched above VAH but closed back below
        return recent_high > vah and self.close < vah

    @property
    def is_failed_downside_breakout(self):
        """Price attempted to break below VAL but failed"""
        val = self.prior_balance_val
        if val is None:
            return False

        recent = self.candles[-10:]
        if len(recent) < 10:
            return False

        recent_low = np.min(recent[:, 4])

        return recent_low < val and self.close > val

    @property
    def reclaimed_balance(self):
        """Price is firmly back inside value area"""
        val = self.prior_balance_val
        vah = self.prior_balance_vah

        if val is None or vah is None:
            return False

        # Must be inside value area
        return val <= self.close <= vah

    @property
    def bullish_aggression(self):
        """Buying pressure at support (rejection candle)"""
        if len(self.candles) < 10:
            return False

        current = self.candles[-1]
        body = abs(current[2] - current[1])
        lower_wick = min(current[1], current[2]) - current[4]
        upper_wick = current[3] - max(current[1], current[2])

        is_rejection = (lower_wick > body * 2 and
                       current[2] > current[1] and
                       upper_wick < body)

        vol_spike = current[5] > np.mean(self.candles[-10:-1, 5]) * 1.3

        return is_rejection and vol_spike

    @property
    def bearish_aggression(self):
        """Selling pressure at resistance"""
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
        """Price near LVN on reclaim leg"""
        lvn = self.nearest_lvn
        if lvn is None:
            return False
        tolerance = self.atr * 0.5
        return abs(self.close - lvn) <= tolerance

    # ========== ENTRY CONDITIONS ==========
    def should_long(self):
        """
        Long setup:
        1. Failed downside breakout
        2. Reclaimed balance
        3. Pullback to LVN
        4. Bullish aggression
        """
        # Track reclaim start for profile calculation
        if self.is_failed_downside_breakout and self.reclaimed_balance:
            if self.vars['reclaim_start_idx'] is None:
                self.vars['reclaim_start_idx'] = len(self.candles) - 1

        return (self.is_failed_downside_breakout and
                self.reclaimed_balance and
                self.at_lvn_zone and
                self.bullish_aggression)

    def should_short(self):
        """
        Short setup:
        1. Failed upside breakout
        2. Reclaimed balance
        3. Pullback to LVN
        4. Bearish aggression
        """
        if self.is_failed_upside_breakout and self.reclaimed_balance:
            if self.vars['reclaim_start_idx'] is None:
                self.vars['reclaim_start_idx'] = len(self.candles) - 1

        return (self.is_failed_upside_breakout and
                self.reclaimed_balance and
                self.at_lvn_zone and
                self.bearish_aggression)

    def go_long(self):
        """Execute long: stop below LVN, target POC"""
        entry = self.close
        lvn = self.nearest_lvn
        if lvn is None:
            return

        stop = lvn - self.atr * 0.75
        target = self.prior_balance_poc
        if target is None:
            return

        # Position sizing
        risk_pct = 0.5
        qty = utils.risk_to_qty(
            self.balance, risk_pct, entry, stop, fee_rate=self.fee_rate
        )
        max_qty = utils.size_to_qty(self.balance * 0.10, entry, fee_rate=self.fee_rate)
        qty = min(qty, max_qty)

        self.buy = qty, entry
        self.stop_loss = qty, stop
        self.take_profit = qty, target  # Full position at POC

        # Reset reclaim tracking
        self.vars['reclaim_start_idx'] = None

    def go_short(self):
        """Execute short: stop above LVN, target POC"""
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
        self.take_profit = qty, target

        self.vars['reclaim_start_idx'] = None

    def should_cancel_entry(self):
        """Cancel if structure changes"""
        return True

    # ========== POSITION MANAGEMENT ==========
    def update_position(self):
        """Move to break-even after 1 ATR profit"""
        if self.position.pnl_percentage >= 0:
            profit_distance = abs(self.close - self.position.entry_price)

            if profit_distance >= self.atr:
                if self.is_long:
                    new_stop = self.position.entry_price + self.atr * 0.1
                    self.stop_loss = self.position.qty, new_stop
                else:
                    new_stop = self.position.entry_price - self.atr * 0.1
                    self.stop_loss = self.position.qty, new_stop

    # ========== DEBUGGING ==========
    def watch_list(self):
        return [
            ('POC', round(self.prior_balance_poc, 2) if self.prior_balance_poc else 'N/A'),
            ('VAH', round(self.prior_balance_vah, 2) if self.prior_balance_vah else 'N/A'),
            ('VAL', round(self.prior_balance_val, 2) if self.prior_balance_val else 'N/A'),
            ('Nearest LVN', round(self.nearest_lvn, 2) if self.nearest_lvn else 'N/A'),
            ('Failed Up Break', self.is_failed_upside_breakout),
            ('Failed Down Break', self.is_failed_downside_breakout),
            ('Reclaimed', self.reclaimed_balance),
        ]
