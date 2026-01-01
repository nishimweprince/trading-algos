/**
 * Logger utility for Tinga Tinga trading strategy
 * Provides structured logging with different levels and formatting
 */

const fs = require('fs');
const path = require('path');

class Logger {
  constructor() {
    this.levels = {
      debug: 0,
      info: 1,
      warn: 2,
      error: 3
    };
    
    this.colors = {
      debug: '\x1b[36m', // Cyan
      info: '\x1b[37m',  // White
      warn: '\x1b[33m',  // Yellow
      error: '\x1b[31m', // Red
      reset: '\x1b[0m'
    };

    this.config = null;
    this.logStream = null;
  }

  /**
   * Initialize logger with configuration
   * @param {Object} config - Logger configuration
   */
  init(config) {
    this.config = config;
    
    if (config.logToFile && config.logFilePath) {
      const logDir = path.dirname(config.logFilePath);
      if (!fs.existsSync(logDir)) {
        fs.mkdirSync(logDir, { recursive: true });
      }
      
      this.logStream = fs.createWriteStream(config.logFilePath, { flags: 'a' });
    }
  }

  /**
   * Format log message with timestamp and level
   * @param {string} level - Log level
   * @param {string} message - Log message
   * @param {Object} data - Additional data
   * @returns {string} Formatted message
   */
  formatMessage(level, message, data) {
    const timestamp = this.config?.includeTimestamp 
      ? new Date().toISOString() 
      : '';
    
    let formatted = timestamp ? `[${timestamp}] ` : '';
    formatted += `[${level.toUpperCase()}] ${message}`;
    
    if (data && Object.keys(data).length > 0) {
      formatted += ' ' + JSON.stringify(data);
    }
    
    return formatted;
  }

  /**
   * Log message at specified level
   * @param {string} level - Log level
   * @param {string} message - Log message
   * @param {Object} data - Additional data
   */
  log(level, message, data = {}) {
    if (!this.config) {
      console.log(message, data);
      return;
    }

    const configLevel = this.levels[this.config.level] || 1;
    const messageLevel = this.levels[level] || 1;
    
    if (messageLevel < configLevel) {
      return;
    }

    const formatted = this.formatMessage(level, message, data);
    
    // Console output with colors
    const color = this.colors[level] || this.colors.reset;
    console.log(`${color}${formatted}${this.colors.reset}`);
    
    // File output without colors
    if (this.logStream && this.config.logToFile) {
      this.logStream.write(formatted + '\n');
    }
  }

  /**
   * Debug level logging
   * @param {string} message - Log message
   * @param {Object} data - Additional data
   */
  debug(message, data) {
    this.log('debug', message, data);
  }

  /**
   * Info level logging
   * @param {string} message - Log message
   * @param {Object} data - Additional data
   */
  info(message, data) {
    this.log('info', message, data);
  }

  /**
   * Warning level logging
   * @param {string} message - Log message
   * @param {Object} data - Additional data
   */
  warn(message, data) {
    this.log('warn', message, data);
  }

  /**
   * Error level logging
   * @param {string} message - Log message
   * @param {Object} data - Additional data
   */
  error(message, data) {
    this.log('error', message, data);
  }

  /**
   * Log trade execution details
   * @param {string} action - Trade action (BUY/SELL)
   * @param {Object} tradeDetails - Trade details
   */
  logTrade(action, tradeDetails) {
    const message = `[TRADE EXECUTION] ${action} Order`;
    this.info(message, tradeDetails);
    
    // Special formatted output for trades
    console.log('\n' + '='.repeat(60));
    console.log(`[TRADE EXECUTION] ${action} Order:`);
    console.log(`  Symbol: ${tradeDetails.symbol}`);
    console.log(`  Volume: ${tradeDetails.volume}`);
    console.log(`  Entry Price: ${tradeDetails.entryPrice}`);
    console.log(`  Stop Loss: ${tradeDetails.stopLoss}`);
    console.log(`  Take Profit: ${tradeDetails.takeProfit}`);
    console.log(`  Risk Amount: $${tradeDetails.riskAmount}`);
    console.log('='.repeat(60) + '\n');
  }

  /**
   * Log strategy performance metrics
   * @param {Object} metrics - Performance metrics
   */
  logPerformance(metrics) {
    const message = 'Strategy Performance Update';
    this.info(message, metrics);
    
    // Special formatted output for performance
    console.log('\n' + '-'.repeat(40));
    console.log('PERFORMANCE METRICS:');
    console.log(`  Balance: $${metrics.balance?.toFixed(2)}`);
    console.log(`  Equity: $${metrics.equity?.toFixed(2)}`);
    console.log(`  Total Trades: ${metrics.totalTrades}`);
    console.log(`  Win Rate: ${metrics.winRate?.toFixed(2)}%`);
    console.log(`  Profit Factor: ${metrics.profitFactor?.toFixed(2)}`);
    console.log(`  Max Drawdown: ${metrics.maxDrawdown?.toFixed(2)}%`);
    console.log('-'.repeat(40) + '\n');
  }

  /**
   * Close log stream
   */
  close() {
    if (this.logStream) {
      this.logStream.end();
      this.logStream = null;
    }
  }
}

// Export singleton instance
module.exports = new Logger();