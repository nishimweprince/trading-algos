/**
 * Backtest script for Tinga Tinga Trading Strategy
 * Tests the strategy on historical data
 */

const TingaTingaStrategy = require('../src/strategy/TingaTingaStrategy');
const config = require('../src/utils/Config');

async function runBacktest() {
  console.log('='.repeat(60));
  console.log('TINGA TINGA STRATEGY BACKTEST');
  console.log('='.repeat(60));
  console.log();

  // Backtest parameters
  const backtestParams = {
    symbol: 'BTCUSDT',
    startDate: new Date('2024-01-01'),
    endDate: new Date('2024-12-31'),
    variations: [
      {
        name: 'Conservative',
        config: {
          rsiPeriod: 14,
          profitPercentage: 3.0,
          lossPercentage: 1.0,
          balancePercentage: 1.0
        }
      },
      {
        name: 'Moderate',
        config: {
          rsiPeriod: 14,
          profitPercentage: 2.0,
          lossPercentage: 1.0,
          balancePercentage: 2.0
        }
      },
      {
        name: 'Aggressive',
        config: {
          rsiPeriod: 14,
          profitPercentage: 1.5,
          lossPercentage: 0.75,
          balancePercentage: 3.0
        }
      },
      {
        name: 'Fast RSI',
        config: {
          rsiPeriod: 9,
          profitPercentage: 2.0,
          lossPercentage: 1.0,
          balancePercentage: 2.0
        }
      },
      {
        name: 'Slow RSI',
        config: {
          rsiPeriod: 21,
          profitPercentage: 2.0,
          lossPercentage: 1.0,
          balancePercentage: 2.0
        }
      }
    ]
  };

  console.log('Backtest Parameters:');
  console.log(`- Symbol: ${backtestParams.symbol}`);
  console.log(`- Period: ${backtestParams.startDate.toDateString()} to ${backtestParams.endDate.toDateString()}`);
  console.log(`- Variations: ${backtestParams.variations.length}`);
  console.log();

  const results = [];

  // Run backtest for each variation
  for (const variation of backtestParams.variations) {
    console.log(`\n${'='.repeat(40)}`);
    console.log(`Running backtest: ${variation.name}`);
    console.log(`Config: ${JSON.stringify(variation.config)}`);
    console.log(`${'='.repeat(40)}`);

    // Create strategy with variation config
    const strategyConfig = {
      ...config.strategy,
      ...variation.config,
      symbol: backtestParams.symbol
    };

    const strategy = new TingaTingaStrategy(strategyConfig);

    try {
      // Initialize strategy
      await strategy.initialize();

      // Run backtest
      const result = await strategy.backtest(
        backtestParams.startDate,
        backtestParams.endDate
      );

      // Store result
      results.push({
        name: variation.name,
        config: variation.config,
        result
      });

      // Print summary
      console.log(`\n✅ Backtest completed for ${variation.name}`);
      console.log(`- Total Trades: ${result.totalTrades}`);
      console.log(`- Win Rate: ${result.winRate.toFixed(2)}%`);
      console.log(`- Net Profit: $${result.netProfit.toFixed(2)}`);
      console.log(`- Total Return: ${result.totalReturn.toFixed(2)}%`);
      console.log(`- Profit Factor: ${result.profitFactor.toFixed(2)}`);
      console.log(`- Max Drawdown: ${result.maxDrawdownPercent.toFixed(2)}%`);

    } catch (error) {
      console.error(`❌ Backtest failed for ${variation.name}:`, error.message);
    }
  }

  // Compare results
  console.log('\n' + '='.repeat(60));
  console.log('BACKTEST COMPARISON');
  console.log('='.repeat(60));
  console.log();

  // Sort by total return
  results.sort((a, b) => b.result.totalReturn - a.result.totalReturn);

  // Create comparison table
  console.log('Strategy         | Trades | Win Rate | Return  | Profit Factor | Max DD');
  console.log('-'.repeat(75));

  for (const { name, result } of results) {
    console.log(
      `${name.padEnd(16)} | ${result.totalTrades.toString().padStart(6)} | ${
        result.winRate.toFixed(1).padStart(7)
      }% | ${
        result.totalReturn.toFixed(1).padStart(6)
      }% | ${
        result.profitFactor.toFixed(2).padStart(13)
      } | ${
        result.maxDrawdownPercent.toFixed(1).padStart(5)
      }%`
    );
  }

  console.log();
  console.log('Best performing strategy:', results[0].name);
  console.log('Highest win rate:', results.sort((a, b) => b.result.winRate - a.result.winRate)[0].name);
  console.log('Lowest drawdown:', results.sort((a, b) => a.result.maxDrawdownPercent - b.result.maxDrawdownPercent)[0].name);

  // Generate detailed report for best performer
  const best = results[0];
  console.log('\n' + '='.repeat(60));
  console.log(`DETAILED REPORT: ${best.name}`);
  console.log('='.repeat(60));
  console.log();
  console.log('Configuration:');
  console.log(JSON.stringify(best.config, null, 2));
  console.log();
  console.log('Performance Metrics:');
  console.log(`- Total Trades: ${best.result.totalTrades}`);
  console.log(`- Winning Trades: ${best.result.winningTrades}`);
  console.log(`- Losing Trades: ${best.result.losingTrades}`);
  console.log(`- Win Rate: ${best.result.winRate.toFixed(2)}%`);
  console.log(`- Average Win: $${best.result.averageWin.toFixed(2)}`);
  console.log(`- Average Loss: $${best.result.averageLoss.toFixed(2)}`);
  console.log(`- Largest Win: $${best.result.largestWin.toFixed(2)}`);
  console.log(`- Largest Loss: $${best.result.largestLoss.toFixed(2)}`);
  console.log(`- Total Commission: $${best.result.totalCommission.toFixed(2)}`);
  console.log(`- Average Holding Time: ${Math.floor(best.result.averageHoldingTime / 60000)} minutes`);
  
  // Risk metrics
  console.log('\nRisk Metrics:');
  console.log(`- Maximum Drawdown: ${best.result.maxDrawdownPercent.toFixed(2)}%`);
  console.log(`- Profit Factor: ${best.result.profitFactor.toFixed(2)}`);
  console.log(`- Sharpe Ratio: ${best.result.sharpeRatio.toFixed(2)}`);
  
  // Monthly breakdown (if we had monthly data)
  console.log('\nReturn Analysis:');
  console.log(`- Total Return: ${best.result.totalReturn.toFixed(2)}%`);
  console.log(`- Annualized Return: ${(best.result.totalReturn * 365 / best.result.period.days).toFixed(2)}%`);
  console.log(`- Return per Trade: ${(best.result.totalReturn / best.result.totalTrades).toFixed(2)}%`);

  console.log('\n' + '='.repeat(60));
  console.log('Backtest analysis complete!');
  console.log('='.repeat(60));
}

// Run backtest
runBacktest().catch(error => {
  console.error('Backtest error:', error);
  process.exit(1);
});