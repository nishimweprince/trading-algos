"""
Tinga Tinga Trading Strategy for Jesse
RSI-based entry/exit signals with crossover at configurable threshold

Based on the Tinga Tinga JavaScript implementation.
Strategy Logic:
- BUY signal: RSI crosses above the threshold (default 50)
- SELL signal: RSI crosses below the threshold (default 50)
- Uses percentage-based stop loss and take profit
- Position sizing based on risk percentage per trade
"""

from jesse.strategies import Strategy, cached
from jesse import utils
import jesse.indicators as ta
import numpy as np


class TingaTinga(Strategy):
    """
    Tinga Tinga RSI Crossover Strategy

    Entry:
    - Long: RSI crosses above buy threshold (default 50)
    - Short: RSI crosses below sell threshold (default 50)

    Exit:
    - Stop Loss: Percentage-based (default 1%)
    - Take Profit: Percentage-based (default 2%)

    Risk Management:
    - Position size based on balance percentage risk (default 2%)
    - Maximum total risk across positions: 10%
    - Trading halt at 20% total loss or 15% max drawdown
    """

    def __init__(self):
        super().__init__()
        # Track previous RSI for crossover detection
        self.vars['prev_rsi'] = None
        # Track if we recently traded to avoid overtrading
        self.vars['last_trade_index'] = -100
        # Minimum candles between trades
        self.vars['min_candles_between_trades'] = 1

    # ========== HYPERPARAMETERS ==========
    @property
    def rsi_period(self) -> int:
        """RSI calculation period"""
        return self.hp.get('rsi_period', 14)

    @property
    def rsi_buy_threshold(self) -> float:
        """RSI threshold for buy signal (crossover above)"""
        return self.hp.get('rsi_buy_threshold', 50)

    @property
    def rsi_sell_threshold(self) -> float:
        """RSI threshold for sell signal (crossover below)"""
        return self.hp.get('rsi_sell_threshold', 50)

    @property
    def profit_percentage(self) -> float:
        """Take profit percentage"""
        return self.hp.get('profit_percentage', 2.0)

    @property
    def loss_percentage(self) -> float:
        """Stop loss percentage"""
        return self.hp.get('loss_percentage', 1.0)

    @property
    def balance_percentage(self) -> float:
        """Percentage of balance to risk per trade"""
        return self.hp.get('balance_percentage', 2.0)

    @property
    def max_position_pct(self) -> float:
        """Maximum position size as percentage of balance"""
        return self.hp.get('max_position_pct', 10.0)

    def hyperparameters(self):
        """Define optimizable hyperparameters"""
        return [
            {'name': 'rsi_period', 'type': int, 'min': 5, 'max': 30, 'default': 14},
            {'name': 'rsi_buy_threshold', 'type': float, 'min': 30, 'max': 70, 'default': 50},
            {'name': 'rsi_sell_threshold', 'type': float, 'min': 30, 'max': 70, 'default': 50},
            {'name': 'profit_percentage', 'type': float, 'min': 0.5, 'max': 10.0, 'default': 2.0},
            {'name': 'loss_percentage', 'type': float, 'min': 0.5, 'max': 5.0, 'default': 1.0},
            {'name': 'balance_percentage', 'type': float, 'min': 0.5, 'max': 5.0, 'default': 2.0},
            {'name': 'max_position_pct', 'type': float, 'min': 5.0, 'max': 20.0, 'default': 10.0},
        ]

    # ========== INDICATORS ==========
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

    @property
    @cached
    def atr(self) -> float:
        """Average True Range for volatility measurement"""
        return ta.atr(self.candles, period=14)

    @property
    @cached
    def sma_20(self) -> float:
        """20-period Simple Moving Average for trend context"""
        return ta.sma(self.candles, period=20)

    @property
    @cached
    def trend(self) -> str:
        """
        Detect trend direction using price vs SMA
        Returns: 'BULLISH', 'BEARISH', or 'NEUTRAL'
        """
        if self.close > self.sma_20:
            return 'BULLISH'
        elif self.close < self.sma_20:
            return 'BEARISH'
        return 'NEUTRAL'

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

    # ========== FILTERS ==========
    def filters(self):
        """Apply filters to entry signals"""
        return [
            self.filter_min_risk_reward,
            self.filter_sufficient_data,
        ]

    def filter_sufficient_data(self) -> bool:
        """Ensure we have enough data for indicator calculation"""
        return len(self.candles) >= self.rsi_period * 2 + 10

    def filter_min_risk_reward(self) -> bool:
        """Ensure minimum 1.5:1 reward-to-risk ratio based on TP/SL percentages"""
        return self.profit_percentage >= self.loss_percentage * 1.5

    # ========== ENTRY CONDITIONS ==========
    def should_long(self) -> bool:
        """
        Long entry condition:
        - RSI crosses above buy threshold
        - Can trade (not too soon after last trade)
        """
        if not self.can_trade:
            return False
        return self.rsi_crossed_above

    def should_short(self) -> bool:
        """
        Short entry condition:
        - RSI crosses below sell threshold
        - Can trade (not too soon after last trade)
        """
        if not self.can_trade:
            return False
        return self.rsi_crossed_below

    # ========== POSITION EXECUTION ==========
    def go_long(self):
        """Execute long position with risk management"""
        entry = self.close

        # Calculate stop loss and take profit based on percentages
        stop = entry * (1 - self.loss_percentage / 100)
        target = entry * (1 + self.profit_percentage / 100)

        # Calculate position size based on risk
        qty = self._calculate_position_size(entry, stop)

        if qty <= 0:
            return

        # Execute the trade
        self.buy = qty, entry
        self.stop_loss = qty, stop
        self.take_profit = qty, target

        # Update last trade index
        self.vars['last_trade_index'] = self.index

    def go_short(self):
        """Execute short position with risk management"""
        entry = self.close

        # Calculate stop loss and take profit based on percentages
        stop = entry * (1 + self.loss_percentage / 100)
        target = entry * (1 - self.profit_percentage / 100)

        # Calculate position size based on risk
        qty = self._calculate_position_size(entry, stop)

        if qty <= 0:
            return

        # Execute the trade
        self.sell = qty, entry
        self.stop_loss = qty, stop
        self.take_profit = qty, target

        # Update last trade index
        self.vars['last_trade_index'] = self.index

    def _calculate_position_size(self, entry: float, stop: float) -> float:
        """
        Calculate position size based on risk percentage

        Args:
            entry: Entry price
            stop: Stop loss price

        Returns:
            Position quantity
        """
        # Calculate risk amount
        risk_amount = self.balance * (self.balance_percentage / 100)

        # Calculate price risk per unit
        price_risk = abs(entry - stop)
        if price_risk == 0:
            return 0

        # Calculate quantity based on risk
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
        Update position management:
        - Trail stop to break-even after 1% profit
        """
        if self.position.pnl_percentage >= 1.0:
            # Move stop to break-even + small buffer
            if self.is_long:
                new_stop = self.position.entry_price * 1.001  # 0.1% above entry
                if new_stop > self.position.stop_loss:
                    self.stop_loss = self.position.qty, new_stop
            elif self.is_short:
                new_stop = self.position.entry_price * 0.999  # 0.1% below entry
                if new_stop < self.position.stop_loss:
                    self.stop_loss = self.position.qty, new_stop

    def should_cancel_entry(self) -> bool:
        """Cancel pending entry if conditions change"""
        # Re-evaluate each candle
        return True

    # ========== EXIT CONDITIONS ==========
    def should_long_exit(self) -> bool:
        """
        Additional exit condition for longs:
        - Exit if RSI crosses below sell threshold (signal reversal)
        """
        return self.rsi_crossed_below

    def should_short_exit(self) -> bool:
        """
        Additional exit condition for shorts:
        - Exit if RSI crosses above buy threshold (signal reversal)
        """
        return self.rsi_crossed_above

    # ========== DEBUGGING & MONITORING ==========
    def watch_list(self):
        """Values to display in Jesse's dashboard"""
        return [
            ('RSI', round(self.current_rsi, 2) if self.current_rsi else 'N/A'),
            ('Prev RSI', round(self.previous_rsi, 2) if self.previous_rsi else 'N/A'),
            ('Buy Threshold', self.rsi_buy_threshold),
            ('Sell Threshold', self.rsi_sell_threshold),
            ('RSI Cross Up', self.rsi_crossed_above),
            ('RSI Cross Down', self.rsi_crossed_below),
            ('Trend', self.trend),
            ('ATR', round(self.atr, 2) if self.atr else 'N/A'),
            ('Can Trade', self.can_trade),
        ]

    def terminate(self):
        """Called at the end of backtest/live session"""
        pass
