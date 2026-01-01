/**
 * Simple test script for Tinga Tinga Trading Strategy
 * Tests basic functionality of core components
 */

const TechnicalIndicators = require('./src/strategy/TechnicalIndicators');
const RiskManager = require('./src/strategy/RiskManager');
const config = require('./src/utils/Config');
const logger = require('./src/utils/Logger');

async function runTests() {
  console.log('='.repeat(60));
  console.log('TINGA TINGA STRATEGY - COMPONENT TESTS');
  console.log('='.repeat(60));
  console.log();

  let passedTests = 0;
  let totalTests = 0;

  // Test 1: RSI Calculation
  console.log('Test 1: RSI Calculation');
  try {
    totalTests++;
    const prices = [
      44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
      45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00,
      46.03, 46.41, 46.22, 45.64
    ];
    const rsi = TechnicalIndicators.rsi(prices, 14);
    
    if (rsi.length > 0 && rsi[rsi.length - 1] >= 0 && rsi[rsi.length - 1] <= 100) {
      console.log(`✅ PASS - RSI calculated: ${rsi[rsi.length - 1].toFixed(2)}`);
      passedTests++;
    } else {
      console.log('❌ FAIL - RSI calculation failed');
    }
  } catch (error) {
    console.log(`❌ FAIL - Error: ${error.message}`);
  }

  // Test 2: RSI Crossover Detection
  console.log('\nTest 2: RSI Crossover Detection');
  try {
    totalTests++;
    const signal1 = TechnicalIndicators.detectRSICrossover(45, 55, 50);
    const signal2 = TechnicalIndicators.detectRSICrossover(55, 45, 50);
    
    if (signal1 === 'BUY' && signal2 === 'SELL') {
      console.log('✅ PASS - RSI crossover detection working correctly');
      passedTests++;
    } else {
      console.log('❌ FAIL - RSI crossover detection incorrect');
    }
  } catch (error) {
    console.log(`❌ FAIL - Error: ${error.message}`);
  }

  // Test 3: Risk Manager - Position Sizing
  console.log('\nTest 3: Risk Manager Position Sizing');
  try {
    totalTests++;
    const riskManager = new RiskManager();
    const balance = 10000;
    const entryPrice = 50000;
    const stopLoss = 49000;
    
    const position = riskManager.calculatePositionSize(balance, entryPrice, stopLoss);
    
    if (position.size > 0 && position.riskAmount > 0) {
      console.log(`✅ PASS - Position size: ${position.size}, Risk: $${position.riskAmount.toFixed(2)}`);
      passedTests++;
    } else {
      console.log('❌ FAIL - Position sizing failed');
    }
  } catch (error) {
    console.log(`❌ FAIL - Error: ${error.message}`);
  }

  // Test 4: Stop Loss Calculation
  console.log('\nTest 4: Stop Loss Calculation');
  try {
    totalTests++;
    const riskManager = new RiskManager();
    const entryPrice = 100;
    const buyStopLoss = riskManager.calculateStopLoss(entryPrice, 'BUY', 2);
    const sellStopLoss = riskManager.calculateStopLoss(entryPrice, 'SELL', 2);
    
    if (buyStopLoss === 98 && sellStopLoss === 102) {
      console.log('✅ PASS - Stop loss calculations correct');
      passedTests++;
    } else {
      console.log(`❌ FAIL - Stop loss incorrect: Buy SL=${buyStopLoss}, Sell SL=${sellStopLoss}`);
    }
  } catch (error) {
    console.log(`❌ FAIL - Error: ${error.message}`);
  }

  // Test 5: Take Profit Calculation
  console.log('\nTest 5: Take Profit Calculation');
  try {
    totalTests++;
    const riskManager = new RiskManager();
    const entryPrice = 100;
    const buyTP = riskManager.calculateTakeProfit(entryPrice, 'BUY', 3);
    const sellTP = riskManager.calculateTakeProfit(entryPrice, 'SELL', 3);
    
    if (buyTP === 103 && sellTP === 97) {
      console.log('✅ PASS - Take profit calculations correct');
      passedTests++;
    } else {
      console.log(`❌ FAIL - Take profit incorrect: Buy TP=${buyTP}, Sell TP=${sellTP}`);
    }
  } catch (error) {
    console.log(`❌ FAIL - Error: ${error.message}`);
  }

  // Test 6: Configuration Validation
  console.log('\nTest 6: Configuration Validation');
  try {
    totalTests++;
    const isValid = config.validate();
    
    if (isValid) {
      console.log('✅ PASS - Configuration is valid');
      passedTests++;
    } else {
      console.log('❌ FAIL - Configuration validation failed');
    }
  } catch (error) {
    console.log(`❌ FAIL - Error: ${error.message}`);
  }

  // Test 7: SMA Calculation
  console.log('\nTest 7: SMA Calculation');
  try {
    totalTests++;
    const prices = [10, 20, 30, 40, 50];
    const sma = TechnicalIndicators.sma(prices, 3);
    
    if (sma.length === 3 && sma[2] === 40) {
      console.log('✅ PASS - SMA calculation correct');
      passedTests++;
    } else {
      console.log(`❌ FAIL - SMA incorrect: ${sma}`);
    }
  } catch (error) {
    console.log(`❌ FAIL - Error: ${error.message}`);
  }

  // Test 8: Trend Detection
  console.log('\nTest 8: Trend Detection');
  try {
    totalTests++;
    const candles = [
      { high: 100, low: 90, close: 95 },
      { high: 105, low: 95, close: 100 },
      { high: 110, low: 100, close: 105 }
    ];
    const indicators = {
      rsi: [40, 50, 60],
      sma: [95, 100, 105]
    };
    
    const trend = TechnicalIndicators.detectTrend(candles, indicators);
    
    if (trend === 'BULLISH') {
      console.log('✅ PASS - Trend detection working');
      passedTests++;
    } else {
      console.log(`❌ FAIL - Trend detection incorrect: ${trend}`);
    }
  } catch (error) {
    console.log(`❌ FAIL - Error: ${error.message}`);
  }

  // Summary
  console.log('\n' + '='.repeat(60));
  console.log('TEST SUMMARY');
  console.log('='.repeat(60));
  console.log(`Total Tests: ${totalTests}`);
  console.log(`Passed: ${passedTests}`);
  console.log(`Failed: ${totalTests - passedTests}`);
  console.log(`Success Rate: ${((passedTests / totalTests) * 100).toFixed(2)}%`);
  
  if (passedTests === totalTests) {
    console.log('\n✅ All tests passed!');
  } else {
    console.log('\n❌ Some tests failed. Please check the implementation.');
  }
}

// Run tests
runTests().catch(error => {
  console.error('Test runner error:', error);
  process.exit(1);
});