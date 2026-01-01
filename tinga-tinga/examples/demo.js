/**
 * Demo script for Tinga Tinga Trading Strategy
 * Shows how to use the strategy with custom parameters
 */

const TingaTingaStrategy = require('../src/strategy/TingaTingaStrategy');
const config = require('../src/utils/Config');
const logger = require('../src/utils/Logger');

async function runDemo() {
  console.log('='.repeat(60));
  console.log('TINGA TINGA STRATEGY DEMO');
  console.log('='.repeat(60));
  console.log();

  // Custom configuration for demo
  const demoConfig = {
    rsiPeriod: 14,
    rsiBuyThreshold: 50,
    rsiSellThreshold: 50,
    profitPercentage: 1.5,  // 1.5% profit target
    lossPercentage: 0.75,    // 0.75% stop loss
    balancePercentage: 1.0,  // Risk 1% per trade
    symbol: 'ETHUSDT',       // Trade ETH/USDT
    timeframe: '15m',        // 15-minute candles
    magicNumber: 999999,
    slippage: 3,
    minLotSize: 0.001,
    maxLotSize: 10.0,
    lotStep: 0.001
  };

  // Create strategy with custom config
  const strategy = new TingaTingaStrategy(demoConfig);

  // Track some metrics
  let tradeCount = 0;
  let profitCount = 0;
  let lossCount = 0;

  // Enhanced event logging
  strategy.on('orderFilled', (order) => {
    tradeCount++;
    console.log('\n' + '='.repeat(40));
    console.log('NEW TRADE EXECUTED');
    console.log('='.repeat(40));
    console.log(`Trade #${tradeCount}`);
    console.log(`Type: ${order.side}`);
    console.log(`Symbol: ${order.symbol}`);
    console.log(`Quantity: ${order.quantity}`);
    console.log(`Entry Price: $${order.executedPrice.toFixed(2)}`);
    console.log(`Stop Loss: $${order.stopLoss.toFixed(2)}`);
    console.log(`Take Profit: $${order.takeProfit.toFixed(2)}`);
    console.log(`Risk Amount: $${(Math.abs(order.executedPrice - order.stopLoss) * order.quantity).toFixed(2)}`);
    console.log('='.repeat(40));
  });

  strategy.on('positionClosed', (position) => {
    if (position.profit > 0) {
      profitCount++;
      console.log('\n✅ WINNING TRADE CLOSED');
    } else {
      lossCount++;
      console.log('\n❌ LOSING TRADE CLOSED');
    }
    
    console.log(`Profit/Loss: $${position.profit.toFixed(2)} (${position.profitPercent.toFixed(2)}%)`);
    console.log(`Duration: ${Math.floor(position.duration / 60000)} minutes`);
    console.log(`Close Reason: ${position.closeReason}`);
    
    // Show running statistics
    const winRate = tradeCount > 0 ? (profitCount / tradeCount * 100).toFixed(1) : 0;
    console.log(`\n📊 Stats: ${profitCount} wins, ${lossCount} losses (${winRate}% win rate)`);
  });

  strategy.on('equityUpdate', (update) => {
    if (update.openPositions > 0) {
      console.log(`\n💼 Open P&L: $${update.floatingPnL.toFixed(2)}`);
    }
  });

  try {
    // Initialize with demo settings
    console.log('Initializing demo strategy...');
    console.log('\nDemo Configuration:');
    console.log(JSON.stringify(demoConfig, null, 2));
    
    await strategy.initialize();

    // Run for a limited time in demo mode
    console.log('\n🚀 Starting demo (will run for 5 minutes)...\n');
    await strategy.start();

    // Stop after 5 minutes
    setTimeout(async () => {
      console.log('\n⏰ Demo time limit reached, stopping strategy...');
      await strategy.stop();
      
      // Print final summary
      const stats = strategy.getStatus();
      console.log('\n' + '='.repeat(60));
      console.log('DEMO SUMMARY');
      console.log('='.repeat(60));
      console.log(`Total Trades: ${tradeCount}`);
      console.log(`Winning Trades: ${profitCount}`);
      console.log(`Losing Trades: ${lossCount}`);
      console.log(`Win Rate: ${tradeCount > 0 ? (profitCount / tradeCount * 100).toFixed(1) : 0}%`);
      console.log(`Final Balance: $${stats.portfolio.balance.toFixed(2)}`);
      console.log(`Total Return: ${((stats.portfolio.balance - 10000) / 10000 * 100).toFixed(2)}%`);
      console.log('='.repeat(60));
      
      process.exit(0);
    }, 5 * 60 * 1000); // 5 minutes

  } catch (error) {
    console.error('Demo error:', error.message);
    process.exit(1);
  }
}

// Handle interruption
process.on('SIGINT', () => {
  console.log('\nDemo interrupted by user');
  process.exit(0);
});

// Run demo
runDemo();