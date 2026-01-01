/**
 * Tinga Tinga Trading Strategy - Main Entry Point
 * 
 * This file demonstrates how to use the Tinga Tinga trading strategy
 * with live market data from Binance.
 */

const TingaTingaStrategy = require('./src/strategy/TingaTingaStrategy');
const config = require('./src/utils/Config');
const logger = require('./src/utils/Logger');

// Load environment variables if available
require('dotenv').config();

/**
 * Main function to run the trading strategy
 */
async function main() {
  console.log('='.repeat(60));
  console.log('TINGA TINGA TRADING STRATEGY');
  console.log('JavaScript Implementation v1.0.0');
  console.log('='.repeat(60));
  console.log();

  // Load configuration from file if specified
  const configFile = process.argv[2];
  if (configFile) {
    try {
      await config.loadFromFile(configFile);
      console.log(`Configuration loaded from: ${configFile}`);
    } catch (error) {
      console.error(`Failed to load configuration: ${error.message}`);
      process.exit(1);
    }
  }

  // Create strategy instance
  const strategy = new TingaTingaStrategy();

  // Setup event listeners
  strategy.on('started', () => {
    console.log('\n✅ Strategy started successfully\n');
  });

  strategy.on('stopped', (stats) => {
    console.log('\n📊 Strategy stopped. Final statistics:');
    console.log(JSON.stringify(stats, null, 2));
  });

  strategy.on('orderFilled', (order) => {
    console.log(`\n📈 Order filled: ${order.side} ${order.quantity} ${order.symbol} @ ${order.executedPrice}`);
  });

  strategy.on('positionClosed', (position) => {
    const emoji = position.profit > 0 ? '✅' : '❌';
    console.log(`\n${emoji} Position closed: ${position.profit > 0 ? 'PROFIT' : 'LOSS'} $${position.profit.toFixed(2)}`);
  });

  // Handle graceful shutdown
  process.on('SIGINT', async () => {
    console.log('\n\nReceived SIGINT, stopping strategy...');
    await strategy.stop();
    process.exit(0);
  });

  process.on('SIGTERM', async () => {
    console.log('\n\nReceived SIGTERM, stopping strategy...');
    await strategy.stop();
    process.exit(0);
  });

  try {
    // Initialize strategy
    console.log('Initializing strategy...');
    await strategy.initialize();

    // Display strategy parameters
    console.log('\nStrategy Parameters:');
    console.log(`- Symbol: ${config.strategy.symbol}`);
    console.log(`- Timeframe: ${config.strategy.timeframe}`);
    console.log(`- RSI Period: ${config.strategy.rsiPeriod}`);
    console.log(`- Profit Target: ${config.strategy.profitPercentage}%`);
    console.log(`- Stop Loss: ${config.strategy.lossPercentage}%`);
    console.log(`- Risk per Trade: ${config.strategy.balancePercentage}%`);
    console.log(`- Initial Balance: $${config.simulation.initialBalance}`);

    // Start strategy
    console.log('\nStarting strategy...');
    await strategy.start();

    // Keep the process running
    console.log('\nStrategy is now running. Press Ctrl+C to stop.\n');

    // Periodically log status
    setInterval(() => {
      const status = strategy.getStatus();
      if (status.isRunning) {
        const runtime = Math.floor(status.runTime / 1000);
        console.log(`\n⏱️  Runtime: ${runtime}s | 📊 Ticks: ${status.tickCount} | 📈 Balance: $${status.portfolio.balance.toFixed(2)} | 💰 Equity: $${status.portfolio.equity.toFixed(2)}`);
      }
    }, 30000); // Every 30 seconds

  } catch (error) {
    console.error('\n❌ Error:', error.message);
    logger.error('Fatal error', { error: error.stack });
    process.exit(1);
  }
}

// Run the main function
if (require.main === module) {
  main().catch(error => {
    console.error('Unhandled error:', error);
    process.exit(1);
  });
}

module.exports = { TingaTingaStrategy };