/**
 * Configuration management for Tinga Tinga trading strategy
 * Manages all strategy parameters and API settings
 */

class Config {
  constructor() {
    // Strategy Parameters
    this.strategy = {
      // RSI indicator settings
      rsiPeriod: 14,
      rsiBuyThreshold: 50,
      rsiSellThreshold: 50,
      
      // Risk management percentages
      profitPercentage: 2.0,  // 2% profit target
      lossPercentage: 1.0,    // 1% stop loss
      balancePercentage: 2.0, // Risk 2% of balance per trade
      
      // Trading parameters
      symbol: 'BTCUSDT',
      timeframe: '1m',
      magicNumber: 123456,
      slippage: 3,
      
      // Position sizing
      minLotSize: 0.001,
      maxLotSize: 10.0,
      lotStep: 0.001
    };

    // Binance API Configuration
    this.binance = {
      baseUrl: 'https://api.binance.com',
      endpoints: {
        klines: '/api/v3/klines',
        ticker: '/api/v3/ticker/price',
        exchangeInfo: '/api/v3/exchangeInfo'
      },
      rateLimits: {
        weight: 1200, // requests per minute
        orderCount: 10, // orders per second
        rawRequests: 5000 // raw requests per 5 minutes
      },
      defaultLimit: 500 // default klines limit
    };

    // Trading simulation settings
    this.simulation = {
      initialBalance: 10000, // Starting balance in USD
      leverage: 1, // No leverage by default
      commission: 0.001, // 0.1% commission per trade
      spread: 0.0001 // Simulated spread
    };

    // Logging configuration
    this.logging = {
      level: 'info', // 'debug', 'info', 'warn', 'error'
      logToFile: false,
      logFilePath: './logs/tinga-tinga.log',
      includeTimestamp: true
    };
  }

  /**
   * Get a nested configuration value using dot notation
   * @param {string} path - Configuration path (e.g., 'strategy.rsiPeriod')
   * @returns {*} Configuration value
   */
  get(path) {
    return path.split('.').reduce((obj, key) => obj?.[key], this);
  }

  /**
   * Set a nested configuration value using dot notation
   * @param {string} path - Configuration path
   * @param {*} value - New value
   */
  set(path, value) {
    const keys = path.split('.');
    const lastKey = keys.pop();
    const target = keys.reduce((obj, key) => {
      if (!obj[key]) obj[key] = {};
      return obj[key];
    }, this);
    target[lastKey] = value;
  }

  /**
   * Load configuration from JSON file
   * @param {string} filePath - Path to configuration file
   */
  async loadFromFile(filePath) {
    try {
      const fs = require('fs').promises;
      const data = await fs.readFile(filePath, 'utf8');
      const config = JSON.parse(data);
      Object.assign(this, config);
    } catch (error) {
      console.error('Error loading configuration:', error);
      throw error;
    }
  }

  /**
   * Save current configuration to JSON file
   * @param {string} filePath - Path to save configuration
   */
  async saveToFile(filePath) {
    try {
      const fs = require('fs').promises;
      const data = JSON.stringify(this, null, 2);
      await fs.writeFile(filePath, data, 'utf8');
    } catch (error) {
      console.error('Error saving configuration:', error);
      throw error;
    }
  }

  /**
   * Validate configuration parameters
   * @returns {boolean} True if configuration is valid
   */
  validate() {
    const errors = [];

    // Validate strategy parameters
    if (this.strategy.rsiPeriod < 2 || this.strategy.rsiPeriod > 100) {
      errors.push('RSI period must be between 2 and 100');
    }
    if (this.strategy.profitPercentage <= 0) {
      errors.push('Profit percentage must be positive');
    }
    if (this.strategy.lossPercentage <= 0) {
      errors.push('Loss percentage must be positive');
    }
    if (this.strategy.balancePercentage <= 0 || this.strategy.balancePercentage > 100) {
      errors.push('Balance percentage must be between 0 and 100');
    }

    if (errors.length > 0) {
      console.error('Configuration validation errors:', errors);
      return false;
    }

    return true;
  }
}

// Export singleton instance
module.exports = new Config();