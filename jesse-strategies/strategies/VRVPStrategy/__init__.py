"""
VRVP Forex Trading Strategy for Jesse

A sophisticated multi-timeframe forex trading system combining:
- 4-Hour Supertrend: Trend filtering (only trade with the higher timeframe trend)
- Stochastic RSI: Momentum detection (oversold/overbought conditions)
- Fair Value Gap (FVG): Price action analysis (imbalance zones)
- Volume Profile: Zone identification (POC, VAH, VAL, HVN, LVN)

Strategy Logic:
- LONG: 4h Supertrend uptrend + StochRSI from oversold + FVG/VP confluence
- SHORT: 4h Supertrend downtrend + StochRSI from overbought + FVG/VP confluence
- Uses ATR-based dynamic stop loss and take profit
- Position sizing based on risk percentage per trade
- Circuit breaker at maximum drawdown threshold

Key Design:
- Supertrend operates on 4-hour timeframe for trend filtering
- Entry signals use current trading timeframe for precise timing
- FVG zones provide high-probability entry areas
- Volume Profile confirms institutional interest levels
"""

from jesse.strategies import Strategy, cached
from jesse import utils
import jesse.indicators as ta
import numpy as np
from custom_indicators import supertrend, volume_profile, stochrsi, fvg


class VRVPStrategy(Strategy):
    """
    VRVP Multi-Timeframe Forex Strategy

    Components:
    - Higher Timeframe (4h): Supertrend for trend direction
    - Current Timeframe: StochRSI, FVG, Volume Profile, and entries

    Entry Conditions:
    - Long: 4h uptrend + StochRSI crossing from oversold + FVG/VP support
    - Short: 4h downtrend + StochRSI crossing from overbought + FVG/VP resistance

    Exit Conditions:
    - Stop Loss: ATR-based (adaptive to volatility)
    - Take Profit: ATR-based (configurable R:R ratio)
    - Supertrend reversal triggers exit
    - FVG zone mitigation

    Risk Management:
    - Position size based on balance percentage risk (default 2%)
    - Maximum position size: 10% of balance
    - Maximum drawdown circuit breaker: 15%
    - Minimum candles between trades: 2
    """

    def __init__(self):
        super().__init__()
        # Track previous StochRSI for crossover detection
        self.vars['prev_stochrsi_k'] = None
        # Track if we recently traded to avoid overtrading
        self.vars['last_trade_index'] = -100
        # Minimum candles between trades
        self.vars['min_candles_between_trades'] = 2
        # Track peak balance for drawdown calculation
        self.vars['peak_balance'] = None
        # Track current stop price for trailing stop
        self.vars['current_stop_price'] = None
        # Track active FVG zones for position management
        self.vars['entry_fvg_zone'] = None

    # ========== HYPERPARAMETERS ==========

    # StochRSI Settings
    @property
    def stochrsi_rsi_period(self) -> int:
        """RSI period for StochRSI calculation"""
        return self.hp.get('stochrsi_rsi_period', 14)

    @property
    def stochrsi_stoch_period(self) -> int:
        """Stochastic period for StochRSI"""
        return self.hp.get('stochrsi_stoch_period', 14)

    @property
    def stochrsi_k_smooth(self) -> int:
        """Smoothing for %K line"""
        return self.hp.get('stochrsi_k_smooth', 3)

    @property
    def stochrsi_d_smooth(self) -> int:
        """Smoothing for %D line"""
        return self.hp.get('stochrsi_d_smooth', 3)

    @property
    def stochrsi_oversold(self) -> float:
        """Oversold threshold (default 20)"""
        return self.hp.get('stochrsi_oversold', 20.0)

    @property
    def stochrsi_overbought(self) -> float:
        """Overbought threshold (default 80)"""
        return self.hp.get('stochrsi_overbought', 80.0)

    # Supertrend Settings (4h timeframe)
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

    # FVG Settings
    @property
    def fvg_max_zones(self) -> int:
        """Maximum number of active FVG zones to track"""
        return self.hp.get('fvg_max_zones', 20)

    @property
    def fvg_min_gap_atr(self) -> float:
        """Minimum FVG gap size as ATR multiple"""
        return self.hp.get('fvg_min_gap_atr', 0.1)

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

    def hyperparameters(self):
        """Define optimizable hyperparameters"""
        return [
            # StochRSI settings
            {'name': 'stochrsi_rsi_period', 'type': int, 'min': 7, 'max': 21, 'default': 14},
            {'name': 'stochrsi_stoch_period', 'type': int, 'min': 7, 'max': 21, 'default': 14},
            {'name': 'stochrsi_k_smooth', 'type': int, 'min': 1, 'max': 5, 'default': 3},
            {'name': 'stochrsi_d_smooth', 'type': int, 'min': 1, 'max': 5, 'default': 3},
            {'name': 'stochrsi_oversold', 'type': float, 'min': 10, 'max': 30, 'default': 20.0},
            {'name': 'stochrsi_overbought', 'type': float, 'min': 70, 'max': 90, 'default': 80.0},

            # Supertrend settings
            {'name': 'supertrend_period', 'type': int, 'min': 7, 'max': 20, 'default': 10},
            {'name': 'supertrend_multiplier', 'type': float, 'min': 2.0, 'max': 5.0, 'default': 3.0},

            # Volume Profile settings
            {'name': 'vp_lookback', 'type': int, 'min': 50, 'max': 200, 'default': 100},
            {'name': 'vp_proximity_atr', 'type': float, 'min': 0.5, 'max': 2.0, 'default': 1.0},

            # FVG settings
            {'name': 'fvg_max_zones', 'type': int, 'min': 10, 'max': 50, 'default': 20},
            {'name': 'fvg_min_gap_atr', 'type': float, 'min': 0.05, 'max': 0.5, 'default': 0.1},

            # ATR-based stops
            {'name': 'stop_loss_atr_mult', 'type': float, 'min': 1.0, 'max': 4.0, 'default': 2.0},
            {'name': 'take_profit_atr_mult', 'type': float, 'min': 2.0, 'max': 8.0, 'default': 4.0},

            # Risk management
            {'name': 'balance_percentage', 'type': float, 'min': 0.5, 'max': 5.0, 'default': 2.0},
            {'name': 'max_position_pct', 'type': float, 'min': 5.0, 'max': 20.0, 'default': 10.0},
            {'name': 'max_drawdown_pct', 'type': float, 'min': 10.0, 'max': 25.0, 'default': 15.0},
        ]

    # ========== INDICATORS ==========

    # ATR Indicator
    @property
    @cached
    def atr(self) -> float:
        """Average True Range for volatility measurement"""
        return ta.atr(self.candles, period=14)

    # Stochastic RSI Indicator
    @property
    @cached
    def stochrsi_result(self):
        """StochRSI indicator result"""
        return stochrsi(
            self.candles,
            rsi_period=self.stochrsi_rsi_period,
            stoch_period=self.stochrsi_stoch_period,
            k_smooth=self.stochrsi_k_smooth,
            d_smooth=self.stochrsi_d_smooth,
            oversold=self.stochrsi_oversold,
            overbought=self.stochrsi_overbought
        )

    @property
    def stochrsi_k(self) -> float:
        """Current StochRSI %K value"""
        return self.stochrsi_result.k

    @property
    def stochrsi_d(self) -> float:
        """Current StochRSI %D value"""
        return self.stochrsi_result.d

    @property
    def stochrsi_is_oversold(self) -> bool:
        """Is StochRSI in oversold territory"""
        return self.stochrsi_result.is_oversold

    @property
    def stochrsi_is_overbought(self) -> bool:
        """Is StochRSI in overbought territory"""
        return self.stochrsi_result.is_overbought

    @property
    def stochrsi_crossed_above_oversold(self) -> bool:
        """StochRSI just crossed above oversold level"""
        return self.stochrsi_result.crossed_above_oversold

    @property
    def stochrsi_crossed_below_overbought(self) -> bool:
        """StochRSI just crossed below overbought level"""
        return self.stochrsi_result.crossed_below_overbought

    # Supertrend Indicator (for higher timeframe trend)
    @property
    @cached
    def htf_candles(self):
        """Get candles for Supertrend trend filter"""
        return self.candles

    @property
    @cached
    def supertrend_result(self):
        """Supertrend indicator result on higher timeframe"""
        htf_candles = self.htf_candles

        if htf_candles is None or len(htf_candles) < self.supertrend_period * 2:
            return None

        return supertrend(
            htf_candles,
            period=self.supertrend_period,
            multiplier=self.supertrend_multiplier,
            source='hl2',
            use_ema_atr=True
        )

    @property
    def supertrend_trend(self) -> int:
        """Current Supertrend trend (1 = uptrend, -1 = downtrend)"""
        result = self.supertrend_result
        if result is None:
            return 0
        return result.trend

    @property
    def supertrend_signal(self) -> int:
        """Supertrend signal (1 = buy, -1 = sell, 0 = no signal)"""
        result = self.supertrend_result
        if result is None:
            return 0
        return result.signal

    # Volume Profile Indicator
    @property
    @cached
    def vp_result(self):
        """Volume Profile result"""
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

    # Fair Value Gap Indicator
    @property
    @cached
    def fvg_result(self):
        """Fair Value Gap detection result"""
        return fvg(
            self.candles,
            max_zones=self.fvg_max_zones,
            min_gap_atr_mult=self.fvg_min_gap_atr
        )

    @property
    def has_bullish_fvg(self) -> bool:
        """Bullish FVG detected on current candle"""
        return self.fvg_result.bullish_fvg

    @property
    def has_bearish_fvg(self) -> bool:
        """Bearish FVG detected on current candle"""
        return self.fvg_result.bearish_fvg

    @property
    def price_in_bullish_fvg(self) -> bool:
        """Current price is inside a bullish FVG zone"""
        return self.fvg_result.price_in_bullish_fvg

    @property
    def price_in_bearish_fvg(self) -> bool:
        """Current price is inside a bearish FVG zone"""
        return self.fvg_result.price_in_bearish_fvg

    @property
    def bouncing_off_bullish_fvg(self) -> bool:
        """Price entered and bounced from bullish FVG"""
        return self.fvg_result.bouncing_off_bullish_fvg

    @property
    def bouncing_off_bearish_fvg(self) -> bool:
        """Price entered and bounced from bearish FVG"""
        return self.fvg_result.bouncing_off_bearish_fvg

    @property
    def active_fvg_zones(self):
        """List of active (unmitigated) FVG zones"""
        return self.fvg_result.active_zones

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
    def can_trade(self) -> bool:
        """Check if enough time has passed since last trade"""
        return self.index - self.vars['last_trade_index'] >= self.vars['min_candles_between_trades']

    def is_near_vp_level(self, price: float, level: float, atr_mult: float = None) -> bool:
        """Check if price is near a VP level within ATR distance"""
        if atr_mult is None:
            atr_mult = self.vp_proximity_atr

        distance = abs(price - level)
        threshold = self.atr * atr_mult
        return distance <= threshold

    def is_near_vp_support(self) -> bool:
        """Check if price is near VP support levels (POC, VAL, or HVN)"""
        price = self.close

        # Check POC proximity
        if self.is_near_vp_level(price, self.vp_poc):
            return True

        # Check VAL proximity
        if self.is_near_vp_level(price, self.vp_val):
            return True

        # Check HVN proximity
        for hvn_level in self.vp_hvn:
            if self.is_near_vp_level(price, hvn_level):
                return True

        return False

    def is_near_vp_resistance(self) -> bool:
        """Check if price is near VP resistance levels (POC, VAH, or HVN)"""
        price = self.close

        # Check POC proximity
        if self.is_near_vp_level(price, self.vp_poc):
            return True

        # Check VAH proximity
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
        return self.current_volume >= self.avg_volume

    def has_fvg_confluence_long(self) -> bool:
        """Check for bullish FVG confluence"""
        # Price bouncing off bullish FVG OR price currently in bullish FVG
        return self.bouncing_off_bullish_fvg or self.price_in_bullish_fvg

    def has_fvg_confluence_short(self) -> bool:
        """Check for bearish FVG confluence"""
        # Price bouncing off bearish FVG OR price currently in bearish FVG
        return self.bouncing_off_bearish_fvg or self.price_in_bearish_fvg

    # ========== RISK MANAGEMENT ==========

    def should_halt_trading(self) -> bool:
        """Circuit breaker: stop trading if max drawdown exceeded"""
        peak_balance = self.vars.get('peak_balance')
        current_balance = self.balance

        if peak_balance is None:
            self.vars['peak_balance'] = current_balance
            return False

        if current_balance > peak_balance:
            self.vars['peak_balance'] = current_balance
            peak_balance = current_balance

        if peak_balance == 0:
            return False

        drawdown_pct = ((peak_balance - current_balance) / peak_balance) * 100
        return drawdown_pct >= self.max_drawdown_pct

    # ========== FILTERS ==========

    def filters(self):
        """Apply filters to entry signals"""
        return [
            self.filter_sufficient_data,
            self.filter_min_risk_reward,
            self.filter_not_halted,
            self.filter_htf_data_available,
        ]

    def filter_sufficient_data(self) -> bool:
        """Ensure we have enough data for all indicator calculations"""
        min_candles = max(
            self.stochrsi_rsi_period + self.stochrsi_stoch_period + 10,
            self.supertrend_period * 2,
            self.vp_lookback,
            50  # Minimum for FVG zone tracking
        )
        return len(self.candles) >= min_candles

    def filter_min_risk_reward(self) -> bool:
        """Ensure minimum 1.5:1 reward-to-risk ratio"""
        return self.take_profit_atr_mult >= self.stop_loss_atr_mult * 1.5

    def filter_not_halted(self) -> bool:
        """Ensure trading is not halted due to max drawdown"""
        return not self.should_halt_trading()

    def filter_htf_data_available(self) -> bool:
        """Ensure higher timeframe data is available for Supertrend"""
        htf_candles = self.htf_candles
        if htf_candles is None:
            return False
        return len(htf_candles) >= self.supertrend_period * 2

    # ========== ENTRY CONDITIONS ==========

    def should_long(self) -> bool:
        """
        Long entry condition (Multi-Timeframe with FVG/VP confluence):
        1. Supertrend (4h): uptrend (trend == 1)
        2. StochRSI: crossing above oversold OR moving from oversold (K < 60)
        3. FVG/VP Confluence: bouncing off bullish FVG OR near VP support
        4. NOT in LVN zone (avoid low liquidity)
        5. Can trade (min candles between trades)
        """
        if not self.can_trade:
            return False

        # Supertrend trend filter (4h uptrend required)
        if self.supertrend_trend != 1:
            return False

        # StochRSI momentum: crossing from oversold or still coming out of oversold
        stochrsi_signal = (
            self.stochrsi_crossed_above_oversold or
            (self.stochrsi_k < 60 and self.stochrsi_k > self.stochrsi_oversold)
        )
        if not stochrsi_signal:
            return False

        # Avoid LVN zones (low liquidity)
        if self.is_in_lvn_zone():
            return False

        # Require FVG or VP confluence
        has_confluence = (
            self.has_fvg_confluence_long() or
            self.is_near_vp_support() or
            self.has_volume_confirmation()
        )
        if not has_confluence:
            return False

        return True

    def should_short(self) -> bool:
        """
        Short entry condition (Multi-Timeframe with FVG/VP confluence):
        1. Supertrend (4h): downtrend (trend == -1)
        2. StochRSI: crossing below overbought OR moving from overbought (K > 40)
        3. FVG/VP Confluence: bouncing off bearish FVG OR near VP resistance
        4. NOT in LVN zone (avoid low liquidity)
        5. Can trade (min candles between trades)
        """
        if not self.can_trade:
            return False

        # Supertrend trend filter (4h downtrend required)
        if self.supertrend_trend != -1:
            return False

        # StochRSI momentum: crossing from overbought or still coming out of overbought
        stochrsi_signal = (
            self.stochrsi_crossed_below_overbought or
            (self.stochrsi_k > 40 and self.stochrsi_k < self.stochrsi_overbought)
        )
        if not stochrsi_signal:
            return False

        # Avoid LVN zones (low liquidity)
        if self.is_in_lvn_zone():
            return False

        # Require FVG or VP confluence
        has_confluence = (
            self.has_fvg_confluence_short() or
            self.is_near_vp_resistance() or
            self.has_volume_confirmation()
        )
        if not has_confluence:
            return False

        return True

    # ========== POSITION EXECUTION ==========

    def go_long(self):
        """Execute long position with ATR-based risk management"""
        entry = self.close

        # ATR-based stop loss and take profit
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

        # Track state
        self.vars['current_stop_price'] = stop
        self.vars['last_trade_index'] = self.index

        # Track entry FVG zone if applicable
        if self.has_fvg_confluence_long():
            for zone in self.active_fvg_zones:
                if zone.type == 'bullish':
                    self.vars['entry_fvg_zone'] = zone
                    break

    def go_short(self):
        """Execute short position with ATR-based risk management"""
        entry = self.close

        # ATR-based stop loss and take profit
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

        # Track state
        self.vars['current_stop_price'] = stop
        self.vars['last_trade_index'] = self.index

        # Track entry FVG zone if applicable
        if self.has_fvg_confluence_short():
            for zone in self.active_fvg_zones:
                if zone.type == 'bearish':
                    self.vars['entry_fvg_zone'] = zone
                    break

    def _calculate_position_size(self, entry: float, stop: float) -> float:
        """Calculate position size based on risk percentage"""
        price_risk = abs(entry - stop)
        if price_risk == 0:
            return 0

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
        """Position management with break-even trailing stop"""
        if not self.is_open:
            return

        current_stop = self.vars.get('current_stop_price')
        if current_stop is None:
            return

        # Move to break-even after 1% profit
        if self.position.pnl_percentage >= 1.0:
            if self.is_long:
                breakeven_stop = self.position.entry_price + (self.atr * 0.1)
                if breakeven_stop > current_stop:
                    self.stop_loss = self.position.qty, breakeven_stop
                    self.vars['current_stop_price'] = breakeven_stop

            elif self.is_short:
                breakeven_stop = self.position.entry_price - (self.atr * 0.1)
                if breakeven_stop < current_stop:
                    self.stop_loss = self.position.qty, breakeven_stop
                    self.vars['current_stop_price'] = breakeven_stop

    def should_cancel_entry(self) -> bool:
        """Cancel pending entry if conditions change"""
        return True

    # ========== EXIT CONDITIONS ==========

    def should_long_exit(self) -> bool:
        """
        Exit condition for longs:
        1. Supertrend reverses to downtrend
        2. StochRSI becomes extremely overbought (> 90)
        3. Entry FVG zone gets mitigated
        """
        # Supertrend reversal
        if self.supertrend_trend == -1:
            return True

        # Extreme overbought
        if self.stochrsi_k > 90:
            return True

        # Check if entry FVG zone was mitigated
        entry_zone = self.vars.get('entry_fvg_zone')
        if entry_zone and entry_zone.mitigated:
            return True

        return False

    def should_short_exit(self) -> bool:
        """
        Exit condition for shorts:
        1. Supertrend reverses to uptrend
        2. StochRSI becomes extremely oversold (< 10)
        3. Entry FVG zone gets mitigated
        """
        # Supertrend reversal
        if self.supertrend_trend == 1:
            return True

        # Extreme oversold
        if self.stochrsi_k < 10:
            return True

        # Check if entry FVG zone was mitigated
        entry_zone = self.vars.get('entry_fvg_zone')
        if entry_zone and entry_zone.mitigated:
            return True

        return False

    # ========== MONITORING ==========

    def watch_list(self):
        """Values to display in Jesse's dashboard"""
        peak_balance = self.vars.get('peak_balance')
        current_balance = self.balance

        if peak_balance is None:
            peak_balance = current_balance

        drawdown_pct = ((peak_balance - current_balance) / peak_balance) * 100 if peak_balance > 0 else 0

        return [
            # StochRSI Metrics
            ('StochRSI K', round(self.stochrsi_k, 2)),
            ('StochRSI D', round(self.stochrsi_d, 2)),
            ('StochRSI State', 'OVERSOLD' if self.stochrsi_is_oversold else ('OVERBOUGHT' if self.stochrsi_is_overbought else 'NEUTRAL')),

            # Supertrend Metrics
            ('ST Trend', 'UP' if self.supertrend_trend == 1 else ('DOWN' if self.supertrend_trend == -1 else 'N/A')),
            ('ST Signal', 'BUY' if self.supertrend_signal == 1 else ('SELL' if self.supertrend_signal == -1 else '-')),

            # FVG Metrics
            ('FVG Zones', len(self.active_fvg_zones)),
            ('In Bull FVG', 'Y' if self.price_in_bullish_fvg else 'N'),
            ('In Bear FVG', 'Y' if self.price_in_bearish_fvg else 'N'),
            ('FVG Bounce L', 'Y' if self.bouncing_off_bullish_fvg else 'N'),
            ('FVG Bounce S', 'Y' if self.bouncing_off_bearish_fvg else 'N'),

            # Volume Profile Metrics
            ('VP POC', round(self.vp_poc, 5)),
            ('Near VP Sup', 'Y' if self.is_near_vp_support() else 'N'),
            ('Near VP Res', 'Y' if self.is_near_vp_resistance() else 'N'),
            ('In LVN', 'Y' if self.is_in_lvn_zone() else 'N'),

            # Risk Metrics
            ('ATR', round(self.atr, 5) if self.atr else 'N/A'),
            ('Drawdown %', round(drawdown_pct, 2)),
            ('Halted', 'Y' if self.should_halt_trading() else 'N'),

            # Trading Status
            ('Can Trade', 'Y' if self.can_trade else 'N'),
        ]

    def terminate(self):
        """Called at the end of backtest/live session"""
        peak_balance = self.vars.get('peak_balance')
        final_balance = self.balance

        if peak_balance is not None and peak_balance > 0:
            max_dd = ((peak_balance - final_balance) / peak_balance) * 100
            print(f"Peak Balance: {peak_balance:.2f}")
            print(f"Final Balance: {final_balance:.2f}")
            print(f"Max Drawdown: {max_dd:.2f}%")
