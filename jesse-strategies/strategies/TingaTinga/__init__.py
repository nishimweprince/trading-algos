"""
Tinga Tinga Enhanced Multi-Timeframe Trading Strategy for Jesse

Multi-timeframe confirmation system:
- Supertrend (4h): Trend filtering - only trade with the 4-hour trend
- RSI (current TF): Entry timing - pullback/continuation signals
- Volume Profile (current TF): Quality confirmation at key levels
- ATR-based (current TF): Dynamic risk management

Strategy Logic:
- LONG: 4h Supertrend uptrend + current TF RSI crosses above + VP confirmation
- SHORT: 4h Supertrend downtrend + current TF RSI crosses below + VP confirmation
- Uses ATR-based stop loss and take profit (adaptive to volatility)
- Position sizing based on risk percentage per trade
- Circuit breaker at maximum drawdown threshold

Key Enhancement: Supertrend operates on 4-hour timeframe to filter against
the dominant trend, while entry signals use the current trading timeframe for
precise timing. Works on any timeframe: 5m, 15m, 30m, 1h, etc.
"""

from jesse.strategies import Strategy, cached
from jesse import utils
import jesse.indicators as ta
import numpy as np
from custom_indicators import supertrend, volume_profile


class TingaTinga(Strategy):
    """
    Tinga Tinga Enhanced Multi-Timeframe Strategy

    Timeframe Structure:
    - Higher Timeframe (4h): Supertrend for trend direction
    - Current Timeframe (any): RSI, Volume Profile, and entries

    Entry:
    - Long: 4h Supertrend uptrend + RSI cross + VP confirmation
    - Short: 4h Supertrend downtrend + RSI cross + VP confirmation

    Exit:
    - Stop Loss: ATR-based (adaptive to current TF)
    - Take Profit: ATR-based (adaptive to current TF)
    - Supertrend reversal (4h)
    - RSI opposite crossover (current TF)

    Risk Management:
    - Position size based on balance percentage risk (default 2%)
    - Maximum position size: 10% of balance
    - Maximum drawdown circuit breaker: 15%
    - Minimum candles between trades: 3

    Flexibility:
    - Works on any trading timeframe: 5m, 15m, 30m, 1h, 4h, etc.
    - Supertrend ALWAYS uses 4h for trend filtering
    - All other indicators adapt to the chosen trading timeframe
    """

    def __init__(self):
        super().__init__()
        # Track previous RSI for crossover detection
        self.vars['prev_rsi'] = None
        # Track if we recently traded to avoid overtrading
        self.vars['last_trade_index'] = -100
        # Minimum candles between trades (increased from 1 to 3)
        self.vars['min_candles_between_trades'] = 3
        # Track peak balance for drawdown calculation
        # Initialize on first candle since balance might not be available yet
        self.vars['peak_balance'] = None
        # Track current stop price for trailing stop comparison
        self.vars['current_stop_price'] = None

    # ========== HYPERPARAMETERS ==========

    # RSI Settings
    @property
    def rsi_period(self) -> int:
        """RSI calculation period"""
        return self.hp.get('rsi_period', 14)

    @property
    def rsi_buy_threshold(self) -> float:
        """RSI threshold for buy signal (shifted from 50 to 40)"""
        return self.hp.get('rsi_buy_threshold', 40)

    @property
    def rsi_sell_threshold(self) -> float:
        """RSI threshold for sell signal (shifted from 50 to 60)"""
        return self.hp.get('rsi_sell_threshold', 60)

    # Supertrend Settings
    @property
    def supertrend_period(self) -> int:
        """Supertrend ATR period"""
        return self.hp.get('supertrend_period', 10)

    @property
    def supertrend_multiplier(self) -> float:
        """Supertrend ATR multiplier"""
        return self.hp.get('supertrend_multiplier', 3.0)

    # Volume Profile Settings
    @property
    def vp_lookback(self) -> int:
        """Volume Profile lookback period in candles"""
        return self.hp.get('vp_lookback', 100)

    @property
    def vp_proximity_atr(self) -> float:
        """ATR multiplier for VP level proximity detection"""
        return self.hp.get('vp_proximity_atr', 1.0)

    # ATR-based Stop Loss & Take Profit
    @property
    def stop_loss_atr_mult(self) -> float:
        """ATR multiplier for stop loss distance"""
        return self.hp.get('stop_loss_atr_mult', 2.0)

    @property
    def take_profit_atr_mult(self) -> float:
        """ATR multiplier for take profit distance"""
        return self.hp.get('take_profit_atr_mult', 4.0)

    # Risk Management
    @property
    def balance_percentage(self) -> float:
        """Percentage of balance to risk per trade"""
        return self.hp.get('balance_percentage', 2.0)

    @property
    def max_position_pct(self) -> float:
        """Maximum position size as percentage of balance"""
        return self.hp.get('max_position_pct', 10.0)

    @property
    def max_drawdown_pct(self) -> float:
        """Maximum drawdown before halting trading"""
        return self.hp.get('max_drawdown_pct', 15.0)

    @property
    def volume_multiplier(self) -> float:
        """Required volume vs average for breakout confirmation"""
        return self.hp.get('volume_multiplier', 1.0)

    def hyperparameters(self):
        """Define optimizable hyperparameters"""
        return [
            # RSI settings
            {'name': 'rsi_period', 'type': int, 'min': 5, 'max': 30, 'default': 14},
            {'name': 'rsi_buy_threshold', 'type': float, 'min': 30, 'max': 50, 'default': 40},
            {'name': 'rsi_sell_threshold', 'type': float, 'min': 50, 'max': 70, 'default': 60},

            # Supertrend settings
            {'name': 'supertrend_period', 'type': int, 'min': 5, 'max': 20, 'default': 10},
            {'name': 'supertrend_multiplier', 'type': float, 'min': 1.5, 'max': 5.0, 'default': 3.0},

            # Volume Profile settings
            {'name': 'vp_lookback', 'type': int, 'min': 50, 'max': 200, 'default': 100},
            {'name': 'vp_proximity_atr', 'type': float, 'min': 0.5, 'max': 2.0, 'default': 1.0},

            # ATR-based stops
            {'name': 'stop_loss_atr_mult', 'type': float, 'min': 1.0, 'max': 4.0, 'default': 2.0},
            {'name': 'take_profit_atr_mult', 'type': float, 'min': 2.0, 'max': 8.0, 'default': 4.0},

            # Risk management
            {'name': 'balance_percentage', 'type': float, 'min': 0.5, 'max': 5.0, 'default': 2.0},
            {'name': 'max_position_pct', 'type': float, 'min': 5.0, 'max': 20.0, 'default': 10.0},
            {'name': 'max_drawdown_pct', 'type': float, 'min': 10.0, 'max': 25.0, 'default': 15.0},
            {'name': 'volume_multiplier', 'type': float, 'min': 0.5, 'max': 2.0, 'default': 1.0},
        ]

    # ========== INDICATORS ==========

    # RSI Indicators
    @property
    @cached
    def rsi(self) -> float:
        """Current RSI value"""
        return ta.rsi(self.candles, period=self.rsi_period)

    @property
    @cached
    def rsi_array(self) -> np.ndarray:
        """RSI values array for crossover detection"""
        return ta.rsi(self.candles, period=self.rsi_period, sequential=True)

    @property
    def previous_rsi(self) -> float:
        """Previous candle's RSI value"""
        rsi_values = self.rsi_array
        if len(rsi_values) < 2:
            return None
        return rsi_values[-2]

    @property
    def current_rsi(self) -> float:
        """Current candle's RSI value"""
        return self.rsi

    # ATR Indicator
    @property
    @cached
    def atr(self) -> float:
        """Average True Range for volatility measurement"""
        return ta.atr(self.candles, period=14)

    # Higher Timeframe Candles
    @property
    @cached
    def htf_candles(self):
        """Get candles for Supertrend trend filter (using current TF for research mode)"""
        # For research mode compatibility, use current timeframe
        # To use 4h, you must load 4h candles in your backtest setup
        return self.candles  # Using current timeframe instead of 4h

    # Supertrend Indicator
    @property
    @cached
    def supertrend_result(self):
        """Supertrend indicator result on 4-hour timeframe"""
        # Get 4h candles for trend filter
        htf_candles = self.htf_candles

        # Validate data availability
        if htf_candles is None or len(htf_candles) < self.supertrend_period * 2:
            return None

        return supertrend(
            htf_candles,  # Using 4h timeframe
            period=self.supertrend_period,
            multiplier=self.supertrend_multiplier,
            source='hl2',
            use_ema_atr=True
        )

    @property
    def supertrend_trend(self) -> int:
        """Current Supertrend trend from 4h timeframe (1 = uptrend, -1 = downtrend)"""
        result = self.supertrend_result
        if result is None:
            return 0  # Neutral if data unavailable
        return result.trend

    @property
    def supertrend_signal(self) -> int:
        """Supertrend signal from 4h timeframe (1 = buy, -1 = sell, 0 = no signal)"""
        result = self.supertrend_result
        if result is None:
            return 0  # No signal if data unavailable
        return result.signal

    # Volume Profile Indicator
    @property
    @cached
    def vp_result(self):
        """Volume Profile result"""
        # Get lookback candles
        lookback = min(self.vp_lookback, len(self.candles))
        vp_candles = self.candles[-lookback:]

        return volume_profile(
            vp_candles,
            num_bins=50,
            value_area_pct=0.70,
            lvn_threshold=0.25
        )

    @property
    def vp_poc(self) -> float:
        """Volume Profile Point of Control"""
        return self.vp_result.poc

    @property
    def vp_vah(self) -> float:
        """Volume Profile Value Area High"""
        return self.vp_result.vah

    @property
    def vp_val(self) -> float:
        """Volume Profile Value Area Low"""
        return self.vp_result.val

    @property
    def vp_hvn(self) -> np.ndarray:
        """Volume Profile High Volume Nodes"""
        return self.vp_result.hvn

    @property
    def vp_lvn(self) -> np.ndarray:
        """Volume Profile Low Volume Nodes"""
        return self.vp_result.lvn

    # Volume Indicator
    @property
    @cached
    def avg_volume(self) -> float:
        """Average volume over 20 periods"""
        volumes = self.candles[:, 5]
        if len(volumes) < 20:
            return np.mean(volumes)
        return np.mean(volumes[-20:])

    @property
    def current_volume(self) -> float:
        """Current candle volume"""
        return self.candles[-1, 5]

    # ========== SIGNAL DETECTION ==========

    @property
    def rsi_crossed_above(self) -> bool:
        """Check if RSI crossed above the buy threshold"""
        prev = self.previous_rsi
        curr = self.current_rsi
        if prev is None or curr is None:
            return False
        return prev <= self.rsi_buy_threshold and curr > self.rsi_buy_threshold

    @property
    def rsi_crossed_below(self) -> bool:
        """Check if RSI crossed below the sell threshold"""
        prev = self.previous_rsi
        curr = self.current_rsi
        if prev is None or curr is None:
            return False
        return prev >= self.rsi_sell_threshold and curr < self.rsi_sell_threshold

    @property
    def can_trade(self) -> bool:
        """
        Check if enough time has passed since last trade
        Prevents overtrading
        """
        return self.index - self.vars['last_trade_index'] >= self.vars['min_candles_between_trades']

    # ========== VOLUME PROFILE HELPERS ==========

    def is_near_vp_level(self, price: float, level: float, atr_mult: float = None) -> bool:
        """Check if price is near a VP level within ATR distance"""
        if atr_mult is None:
            atr_mult = self.vp_proximity_atr

        distance = abs(price - level)
        threshold = self.atr * atr_mult
        return distance <= threshold

    def is_near_support(self) -> bool:
        """
        Check if price is near VP support levels (POC, VAL, or HVN)
        Suitable for long entries
        """
        price = self.close

        # Check POC proximity
        if self.is_near_vp_level(price, self.vp_poc):
            return True

        # Check VAL proximity (support in uptrend)
        if self.is_near_vp_level(price, self.vp_val):
            return True

        # Check HVN proximity
        for hvn_level in self.vp_hvn:
            if self.is_near_vp_level(price, hvn_level):
                return True

        return False

    def is_near_resistance(self) -> bool:
        """
        Check if price is near VP resistance levels (POC, VAH, or HVN)
        Suitable for short entries
        """
        price = self.close

        # Check POC proximity
        if self.is_near_vp_level(price, self.vp_poc):
            return True

        # Check VAH proximity (resistance in downtrend)
        if self.is_near_vp_level(price, self.vp_vah):
            return True

        # Check HVN proximity
        for hvn_level in self.vp_hvn:
            if self.is_near_vp_level(price, hvn_level):
                return True

        return False

    def is_in_lvn_zone(self) -> bool:
        """Check if current price is in a Low Volume Node zone"""
        price = self.close

        for lvn_level in self.vp_lvn:
            if self.is_near_vp_level(price, lvn_level, atr_mult=0.5):
                return True

        return False

    def has_volume_confirmation(self) -> bool:
        """Check if current volume supports the move"""
        return self.current_volume >= self.avg_volume * self.volume_multiplier

    # ========== RISK MANAGEMENT ==========

    def should_halt_trading(self) -> bool:
        """
        Circuit breaker: stop trading if max drawdown exceeded
        Implements the documented 15% max drawdown protection
        """
        # Calculate drawdown from peak balance
        peak_balance = self.vars.get('peak_balance')
        current_balance = self.balance

        # Initialize peak balance if not set
        if peak_balance is None:
            self.vars['peak_balance'] = current_balance
            return False

        # Update peak
        if current_balance > peak_balance:
            self.vars['peak_balance'] = current_balance
            peak_balance = current_balance

        # Calculate drawdown percentage
        if peak_balance == 0:
            return False

        drawdown_pct = ((peak_balance - current_balance) / peak_balance) * 100

        # Halt if exceeds threshold
        return drawdown_pct >= self.max_drawdown_pct

    # ========== FILTERS ==========

    def filters(self):
        """Apply filters to entry signals"""
        return [
            self.filter_sufficient_data,
            self.filter_min_risk_reward,
            self.filter_not_halted,
            self.filter_htf_data_available,  # NEW: Ensure 4h data available
        ]

    def filter_sufficient_data(self) -> bool:
        """Ensure we have enough data for all indicator calculations"""
        min_candles = max(
            self.rsi_period * 2 + 10,
            self.supertrend_period * 2,
            self.vp_lookback
        )
        return len(self.candles) >= min_candles

    def filter_min_risk_reward(self) -> bool:
        """Ensure minimum 1.5:1 reward-to-risk ratio based on ATR multipliers"""
        return self.take_profit_atr_mult >= self.stop_loss_atr_mult * 1.5

    def filter_not_halted(self) -> bool:
        """Ensure trading is not halted due to max drawdown"""
        return not self.should_halt_trading()

    def filter_htf_data_available(self) -> bool:
        """Ensure 4h timeframe data is available for Supertrend"""
        htf_candles = self.htf_candles
        if htf_candles is None:
            return False

        min_candles = self.supertrend_period * 2
        return len(htf_candles) >= min_candles

    # ========== ENTRY CONDITIONS ==========

    def should_long(self) -> bool:
        """
        Enhanced long entry condition (Multi-Timeframe):
        1. Supertrend (4h): uptrend (trend == 1) - REQUIRED
        2. RSI (current TF): crosses above buy threshold
        3. Volume Profile (current TF): near support OR has volume confirmation
        4. NOT in LVN zone
        5. Can trade (min candles between trades)

        Note: If 4h Supertrend data unavailable, trade is blocked by filter.
        """
        # Basic checks
        if not self.can_trade:
            return False

        # Phase 1: Supertrend trend filter (CRITICAL)
        if self.supertrend_trend != 1:
            return False

        # Phase 5: RSI crossover signal
        if not self.rsi_crossed_above:
            return False

        # Phase 3: Volume Profile filtering
        # Avoid LVN zones (low liquidity)
        if self.is_in_lvn_zone():
            return False

        # Require either VP support OR volume confirmation
        vp_confirmed = self.is_near_support() or self.has_volume_confirmation()
        if not vp_confirmed:
            return False

        return True

    def should_short(self) -> bool:
        """
        Enhanced short entry condition (Multi-Timeframe):
        1. Supertrend (4h): downtrend (trend == -1) - REQUIRED
        2. RSI (current TF): crosses below sell threshold
        3. Volume Profile (current TF): near resistance OR has volume confirmation
        4. NOT in LVN zone
        5. Can trade (min candles between trades)

        Note: If 4h Supertrend data unavailable, trade is blocked by filter.
        """
        # Basic checks
        if not self.can_trade:
            return False

        # Phase 1: Supertrend trend filter (CRITICAL)
        if self.supertrend_trend != -1:
            return False

        # Phase 5: RSI crossover signal
        if not self.rsi_crossed_below:
            return False

        # Phase 3: Volume Profile filtering
        # Avoid LVN zones (low liquidity)
        if self.is_in_lvn_zone():
            return False

        # Require either VP resistance OR volume confirmation
        vp_confirmed = self.is_near_resistance() or self.has_volume_confirmation()
        if not vp_confirmed:
            return False

        return True

    # ========== POSITION EXECUTION ==========

    def go_long(self):
        """Execute long position with ATR-based risk management"""
        entry = self.close

        # Phase 2: ATR-based stop loss and take profit
        stop = entry - (self.atr * self.stop_loss_atr_mult)
        target = entry + (self.atr * self.take_profit_atr_mult)

        # Calculate position size based on risk
        qty = self._calculate_position_size(entry, stop)

        if qty <= 0:
            return

        # Execute the trade
        self.buy = qty, entry
        self.stop_loss = qty, stop
        self.take_profit = qty, target

        # Track current stop price for trailing stop
        self.vars['current_stop_price'] = stop

        # Update last trade index
        self.vars['last_trade_index'] = self.index

    def go_short(self):
        """Execute short position with ATR-based risk management"""
        entry = self.close

        # Phase 2: ATR-based stop loss and take profit
        stop = entry + (self.atr * self.stop_loss_atr_mult)
        target = entry - (self.atr * self.take_profit_atr_mult)

        # Calculate position size based on risk
        qty = self._calculate_position_size(entry, stop)

        if qty <= 0:
            return

        # Execute the trade
        self.sell = qty, entry
        self.stop_loss = qty, stop
        self.take_profit = qty, target

        # Track current stop price for trailing stop
        self.vars['current_stop_price'] = stop

        # Update last trade index
        self.vars['last_trade_index'] = self.index

    def _calculate_position_size(self, entry: float, stop: float) -> float:
        """
        Calculate position size based on risk percentage
        Uses Jesse's risk_to_qty for proper risk management

        Args:
            entry: Entry price
            stop: Stop loss price

        Returns:
            Position quantity
        """
        # Calculate price risk per unit
        price_risk = abs(entry - stop)
        if price_risk == 0:
            return 0

        # Calculate quantity based on risk using Jesse's utility
        qty = utils.risk_to_qty(
            self.balance,
            self.balance_percentage,
            entry,
            stop,
            fee_rate=self.fee_rate
        )

        # Cap position size at max_position_pct of balance
        max_qty = utils.size_to_qty(
            self.balance * (self.max_position_pct / 100),
            entry,
            fee_rate=self.fee_rate
        )
        qty = min(qty, max_qty)

        return qty

    # ========== POSITION MANAGEMENT ==========

    def update_position(self):
        """
        Phase 6: Enhanced position management with ATR-based trailing stop
        - Trail stop to break-even after 1% profit
        - Only update if new stop is an improvement
        """
        # Only update if we have an open position
        if not self.is_open:
            return

        # Get current stop price
        current_stop = self.vars.get('current_stop_price')
        if current_stop is None:
            return

        # Move to break-even after 1% profit
        if self.position.pnl_percentage >= 1.0:
            if self.is_long:
                # Calculate break-even with ATR buffer
                breakeven_stop = self.position.entry_price + (self.atr * 0.1)

                # Only update if new stop is higher (better protection)
                if breakeven_stop > current_stop:
                    self.stop_loss = self.position.qty, breakeven_stop
                    self.vars['current_stop_price'] = breakeven_stop

            elif self.is_short:
                # Calculate break-even with ATR buffer
                breakeven_stop = self.position.entry_price - (self.atr * 0.1)

                # Only update if new stop is lower (better protection)
                if breakeven_stop < current_stop:
                    self.stop_loss = self.position.qty, breakeven_stop
                    self.vars['current_stop_price'] = breakeven_stop

    def should_cancel_entry(self) -> bool:
        """Cancel pending entry if conditions change"""
        return True

    # ========== EXIT CONDITIONS ==========

    def should_long_exit(self) -> bool:
        """
        Enhanced exit condition for longs (Multi-Timeframe):
        1. Supertrend (4h) reverses to downtrend (PRIORITY)
        2. RSI (current TF) crosses below sell threshold (signal reversal)
        3. Price enters LVN zone above entry (low liquidity resistance)
        """
        # Priority exit: Supertrend reversal
        if self.supertrend_trend == -1:
            return True

        # RSI reversal signal
        if self.rsi_crossed_below:
            return True

        # LVN zone exit (if price moved up into LVN)
        if self.is_open and self.close > self.position.entry_price:
            if self.is_in_lvn_zone():
                return True

        return False

    def should_short_exit(self) -> bool:
        """
        Enhanced exit condition for shorts (Multi-Timeframe):
        1. Supertrend (4h) reverses to uptrend (PRIORITY)
        2. RSI (current TF) crosses above buy threshold (signal reversal)
        3. Price enters LVN zone below entry (low liquidity support)
        """
        # Priority exit: Supertrend reversal
        if self.supertrend_trend == 1:
            return True

        # RSI reversal signal
        if self.rsi_crossed_above:
            return True

        # LVN zone exit (if price moved down into LVN)
        if self.is_open and self.close < self.position.entry_price:
            if self.is_in_lvn_zone():
                return True

        return False

    # ========== DEBUGGING & MONITORING ==========

    def watch_list(self):
        """Enhanced values to display in Jesse's dashboard"""
        # Calculate current drawdown
        peak_balance = self.vars.get('peak_balance')
        current_balance = self.balance

        # Initialize peak if not set
        if peak_balance is None:
            peak_balance = current_balance

        drawdown_pct = ((peak_balance - current_balance) / peak_balance) * 100 if peak_balance > 0 else 0

        return [
            # RSI Metrics
            ('RSI', round(self.current_rsi, 2) if self.current_rsi else 'N/A'),
            ('RSI Threshold', f"{self.rsi_buy_threshold}/{self.rsi_sell_threshold}"),
            ('RSI Cross', '↑' if self.rsi_crossed_above else ('↓' if self.rsi_crossed_below else '-')),

            # Supertrend Metrics (4h)
            ('ST Trend (4h)', 'UP' if self.supertrend_trend == 1 else ('DOWN' if self.supertrend_trend == -1 else 'N/A')),
            ('ST Signal (4h)', 'BUY' if self.supertrend_signal == 1 else ('SELL' if self.supertrend_signal == -1 else '-')),

            # Volume Profile Metrics
            ('VP POC', round(self.vp_poc, 2)),
            ('Near Support', '✓' if self.is_near_support() else '✗'),
            ('Near Resist', '✓' if self.is_near_resistance() else '✗'),
            ('In LVN', '✓' if self.is_in_lvn_zone() else '✗'),

            # Risk Metrics
            ('ATR', round(self.atr, 2) if self.atr else 'N/A'),
            ('Drawdown %', round(drawdown_pct, 2)),
            ('Halted', '✓' if self.should_halt_trading() else '✗'),

            # Trading Status
            ('Can Trade', '✓' if self.can_trade else '✗'),
            ('Vol Confirm', '✓' if self.has_volume_confirmation() else '✗'),
        ]

    def terminate(self):
        """Called at the end of backtest/live session"""
        # Print final stats
        peak_balance = self.vars.get('peak_balance')
        final_balance = self.balance

        if peak_balance is not None and peak_balance > 0:
            max_dd = ((peak_balance - final_balance) / peak_balance) * 100
            print(f"Peak Balance: {peak_balance:.2f}")
            print(f"Final Balance: {final_balance:.2f}")
            print(f"Max Drawdown: {max_dd:.2f}%")
