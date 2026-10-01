# Tinga Tinga Trading Strategy - JavaScript Implementation

A modern JavaScript implementation of the Tinga Tinga forex/cryptocurrency trading strategy, originally written in MQL4. This implementation uses RSI-based crossover signals with percentage-based profit/loss targets and intelligent risk management.

## Overview

The Tinga Tinga strategy is an automated trading system that:
- Uses RSI (Relative Strength Index) crossover signals for entry
- Implements percentage-based profit targets and stop losses
- Manages risk through balance-based position sizing
- Reads crypto market data from market-data-service (Binance-backed `crypto` market)
- Provides comprehensive logging and performance tracking

## Features

- **Modular Architecture**: Clean separation of concerns with dedicated modules for strategy logic, risk management, market data, and trade execution
- **Real-time Trading**: Live quotes and closed candles from market-data-service
- **Risk Management**: Sophisticated position sizing, maximum drawdown controls, and portfolio tracking
- **Backtesting**: Built-in backtesting engine to test strategy on historical data
- **Performance Metrics**: Comprehensive tracking including win rate, profit factor, Sharpe ratio, and drawdown
- **Configurable Parameters**: Easy customization of strategy parameters through JSON configuration
- **Event-Driven Design**: Real-time notifications for trades, positions, and portfolio updates

## Project Structure

```
tinga-tinga/
├── src/
│   ├── strategy/
│   │   ├── TingaTingaStrategy.js    # Main strategy implementation
│   │   ├── RiskManager.js           # Position sizing and risk calculations
│   │   └── TechnicalIndicators.js   # RSI and other technical indicators
│   ├── market/
│   │   ├── BinanceDataFeed.js       # Crypto data via market-data-service
│   │   └── MarketDataProcessor.js   # Market data analysis
│   ├── trading/
│   │   ├── OrderManager.js          # Simulated trade execution
│   │   └── PortfolioTracker.js     # Balance and performance tracking
│   └── utils/
│       ├── Logger.js                # Logging utilities
│       └── Config.js                # Configuration management
├── examples/
│   ├── demo.js                      # Demo script with custom parameters
│   └── backtest.js                  # Backtesting multiple strategy variations
├── config.json                      # Default configuration
├── index.js                         # Main entry point
└── package.json                     # Node.js dependencies
```

## Installation

1. Clone the repository:
```bash
git clone https://github.com/yourusername/tinga-tinga-js.git
cd tinga-tinga-js
```

2. Install dependencies:
```bash
npm install
```

3. Configure your strategy parameters in `config.json` or create a custom configuration file.

## Configuration

The strategy can be configured through the `config.json` file:

```json
{
  "strategy": {
    "rsiPeriod": 14,              // RSI calculation period
    "rsiBuyThreshold": 50,        // RSI level to trigger buy
    "rsiSellThreshold": 50,       // RSI level to trigger sell
    "profitPercentage": 2.0,      // Take profit at 2%
    "lossPercentage": 1.0,        // Stop loss at 1%
    "balancePercentage": 2.0,     // Risk 2% of balance per trade
    "symbol": "BTCUSDT",          // Trading pair
    "timeframe": "1h"             // Candle timeframe
  }
}
```

## Usage

### Live Trading Simulation

Run the strategy with default configuration:
```bash
npm start
```

Or with a custom configuration file:
```bash
node index.js custom-config.json
```

### Demo Mode

Run a 5-minute demo with custom parameters:
```bash
npm run demo
```

### Backtesting

Test the strategy on historical data:
```bash
npm run backtest
```

## Strategy Logic

### Entry Signals
- **Buy Signal**: RSI crosses above the threshold (default: 50)
- **Sell Signal**: RSI crosses below the threshold (default: 50)

### Exit Conditions
- **Take Profit**: Position closed when profit reaches target percentage
- **Stop Loss**: Position closed when loss reaches maximum percentage
- **Risk Management**: Automatic position closure if drawdown limits exceeded

### Position Sizing
- Calculates position size based on:
  - Account balance
  - Risk percentage per trade
  - Distance to stop loss
  - Symbol constraints (min/max lot size)

## Market Data

Prices come from [market-data-service](../services/market-data-service/README.md)'s
`crypto` market through the
[`@trading-algos/market-data`](../packages/ta-market-data-js/README.md) client,
never from Binance directly: the service's Binance plugin owns the rate-limit
budget and the WebSocket for every consumer. Set `MARKET_DATA_URL`,
`MARKET_DATA_API_KEY` and optionally `MARKET_DATA_MARKET` (default `crypto`).

- `getKlines` returns **closed** bars only. Indicators settle on the bar close
  rather than moving inside the forming bar, as they did when this read
  Binance directly.
- `getCurrentPrice` is the book mid, not the last trade.
- `streamPrices` uses the service's SSE stream instead of polling.

Install from this directory with `npm install`; the client is a `file:`
dependency on `../packages/ta-market-data-js`.

**Note**: This implementation simulates trades through detailed logging. Actual trade execution would require authenticated API access.

## Performance Tracking

The strategy tracks and reports:
- **Trade Metrics**: Win rate, average win/loss, profit factor
- **Risk Metrics**: Maximum drawdown, Sharpe ratio, risk/reward ratio
- **Portfolio Metrics**: Balance, equity, floating P&L, margin usage
- **Daily Statistics**: Daily returns, trades per day, daily P&L

## Example Output

```
[TRADE EXECUTION] BUY Order:
  Symbol: BTCUSDT
  Volume: 0.015
  Entry Price: 45000
  Stop Loss: 44550
  Take Profit: 45900
  Risk Amount: $6.75

[INFO] Position closed
  positionId: POS-ORD-1234567890-1
  closePrice: 45900
  profit: 13.50
  profitPercent: 2.00%
  reason: TAKE_PROFIT
  duration: 180 minutes

PERFORMANCE METRICS:
  Balance: $10135.50
  Equity: $10135.50
  Total Trades: 15
  Win Rate: 66.67%
  Profit Factor: 2.10
  Max Drawdown: 3.45%
```

## Development

### Running Tests
```bash
npm test
```

### Adding New Indicators

To add new technical indicators, extend the `TechnicalIndicators` class:

```javascript
// In src/strategy/TechnicalIndicators.js
static myIndicator(prices, period) {
  // Your indicator logic here
}
```

### Custom Data Sources

To use another source, implement a data feed class with the `BinanceDataFeed` interface
(or point `MARKET_DATA_MARKET` at another market-data-service market):

```javascript
class MyExchangeDataFeed {
  async getKlines(symbol, interval, limit) {
    // Fetch candlestick data
  }
  
  async getCurrentPrice(symbol) {
    // Get current price
  }
}
```

## Risk Disclaimer

**IMPORTANT**: This software is for educational and research purposes only. 

- Trading cryptocurrencies and forex carries significant risk
- Past performance does not guarantee future results
- Never trade with money you cannot afford to lose
- Always test thoroughly on demo accounts before live trading
- The authors are not responsible for any financial losses

## Contributing

Contributions are welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Acknowledgments

- Original Tinga Tinga strategy concept from MQL4 implementation
- Binance for providing public market data API
- The Node.js and JavaScript community for excellent libraries

## Support

For questions, issues, or suggestions:
- Open an issue on GitHub
- Contact: princeelysee@gmail.com

---

**Remember**: Always practice responsible trading and proper risk management!
