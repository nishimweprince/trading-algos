# VRVP Forex Trading Strategy

A sophisticated multi-timeframe forex trading strategy combining **Volume Profile**, **Stochastic RSI**, **Fair Value Gap**, and **Supertrend** indicators for the Jesse trading framework.

## Strategy Overview

VRVP (Volume-RSI-VPG-Profile) is designed for forex trading, combining institutional-grade concepts with momentum and trend analysis:

| Component | Timeframe | Purpose |
|-----------|-----------|---------|
| **Supertrend** | 4-Hour | Trend filtering - only trade with the dominant trend |
| **Stochastic RSI** | Current TF | Momentum detection - identify oversold/overbought reversals |
| **Fair Value Gap** | Current TF | Price action - find institutional imbalance zones |
| **Volume Profile** | Current TF | Zone identification - POC, VAH, VAL, HVN, LVN levels |

## Entry Logic

### Long Entry
All conditions must be met:
1. **Supertrend (4h)**: Uptrend (trend = 1)
2. **Stochastic RSI**: Crossing above oversold level OR moving from oversold (K < 60)
3. **Confluence**: Bouncing off bullish FVG OR near VP support OR volume confirmation
4. **Filter**: NOT in Low Volume Node (LVN) zone

### Short Entry
All conditions must be met:
1. **Supertrend (4h)**: Downtrend (trend = -1)
2. **Stochastic RSI**: Crossing below overbought level OR moving from overbought (K > 40)
3. **Confluence**: Bouncing off bearish FVG OR near VP resistance OR volume confirmation
4. **Filter**: NOT in Low Volume Node (LVN) zone

## Exit Logic

### Stop Loss & Take Profit
- **Stop Loss**: ATR-based (default 2x ATR)
- **Take Profit**: ATR-based (default 4x ATR, 2:1 R:R)
- **Trailing**: Move to break-even after 1% profit

### Signal Exits
- Supertrend trend reversal
- Extreme StochRSI levels (> 90 for longs, < 10 for shorts)
- Entry FVG zone mitigation

## Risk Management

| Parameter | Default | Description |
|-----------|---------|-------------|
| `balance_percentage` | 2% | Risk per trade as % of balance |
| `max_position_pct` | 10% | Maximum position size as % of balance |
| `max_drawdown_pct` | 15% | Circuit breaker - halt trading at this drawdown |
| `min_candles_between_trades` | 2 | Prevent overtrading |

## Hyperparameters

### Stochastic RSI
| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `stochrsi_rsi_period` | 14 | 7-21 | RSI calculation period |
| `stochrsi_stoch_period` | 14 | 7-21 | Stochastic period on RSI |
| `stochrsi_k_smooth` | 3 | 1-5 | %K smoothing |
| `stochrsi_d_smooth` | 3 | 1-5 | %D smoothing |
| `stochrsi_oversold` | 20 | 10-30 | Oversold threshold |
| `stochrsi_overbought` | 80 | 70-90 | Overbought threshold |

### Supertrend
| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `supertrend_period` | 10 | 7-20 | ATR period |
| `supertrend_multiplier` | 3.0 | 2.0-5.0 | ATR multiplier for bands |

### Volume Profile
| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `vp_lookback` | 100 | 50-200 | Candles for VP calculation |
| `vp_proximity_atr` | 1.0 | 0.5-2.0 | ATR multiplier for level proximity |

### Fair Value Gap
| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `fvg_max_zones` | 20 | 10-50 | Maximum active FVG zones to track |
| `fvg_min_gap_atr` | 0.1 | 0.05-0.5 | Minimum gap size as ATR multiple |

### Risk Management
| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `stop_loss_atr_mult` | 2.0 | 1.0-4.0 | ATR multiplier for stop loss |
| `take_profit_atr_mult` | 4.0 | 2.0-8.0 | ATR multiplier for take profit |
| `balance_percentage` | 2.0 | 0.5-5.0 | Risk per trade (%) |
| `max_position_pct` | 10.0 | 5.0-20.0 | Max position size (%) |
| `max_drawdown_pct` | 15.0 | 10.0-25.0 | Circuit breaker threshold (%) |

## Custom Indicators

This strategy uses three custom indicators from `custom_indicators/`:

### 1. Stochastic RSI (`stochrsi.py`)
Applies the Stochastic formula to RSI values for a more sensitive momentum oscillator.

```python
from custom_indicators import stochrsi, StochRSIResult

result = stochrsi(candles, rsi_period=14, stoch_period=14,
                  k_smooth=3, d_smooth=3, oversold=20, overbought=80)
# Returns: k, d, is_oversold, is_overbought, crossed_above_oversold, crossed_below_overbought
```

### 2. Fair Value Gap (`fvg.py`)
Detects 3-candle imbalance patterns (FVGs) and tracks zone mitigation.

```python
from custom_indicators import fvg, FVGResult

result = fvg(candles, max_zones=20, min_gap_atr_mult=0.1)
# Returns: bullish_fvg, bearish_fvg, active_zones, price_in_fvg, bouncing_off_fvg
```

### 3. Volume Profile (existing)
Calculates POC, Value Area, HVN, and LVN levels.

### 4. Supertrend (existing)
Trend-following indicator using ATR-based bands.

## Usage

### Backtesting
```bash
jesse backtest 2023-01-01 2024-01-01
```

### Optimization
```bash
jesse optimize 2023-01-01 2023-06-30 --optimal-total 100
```

### Live Trading
Configure in Jesse's dashboard with your broker credentials.

## Recommended Pairs

This strategy is optimized for major forex pairs with good liquidity:
- EUR/USD
- GBP/USD
- USD/JPY
- AUD/USD
- USD/CHF

## Recommended Timeframes

| Timeframe | Use Case |
|-----------|----------|
| **1H** | Primary trading timeframe (recommended) |
| **30M** | More signals, higher noise |
| **4H** | Fewer signals, higher quality |

Note: Supertrend always uses 4H for trend filtering regardless of trading timeframe.

## Dashboard Metrics

The strategy displays the following in Jesse's watch list:

| Metric | Description |
|--------|-------------|
| StochRSI K/D | Current Stochastic RSI values |
| StochRSI State | OVERSOLD / OVERBOUGHT / NEUTRAL |
| ST Trend | Supertrend direction (UP/DOWN) |
| FVG Zones | Number of active FVG zones |
| In Bull/Bear FVG | Price inside FVG zone |
| VP POC | Volume Profile Point of Control |
| Near VP Sup/Res | Price near VP support/resistance |
| In LVN | Price in Low Volume Node |
| Drawdown % | Current drawdown from peak |
| Halted | Circuit breaker triggered |

## Performance Considerations

1. **Multi-Timeframe**: Strategy requires sufficient 4H data for Supertrend
2. **FVG Tracking**: Maintains up to 20 active zones (configurable)
3. **Volume Profile**: Recalculated each candle over lookback period

## Changelog

### v1.0.0
- Initial release
- Combines Supertrend, StochRSI, FVG, and Volume Profile
- ATR-based risk management
- Break-even trailing stop
- Circuit breaker at max drawdown
