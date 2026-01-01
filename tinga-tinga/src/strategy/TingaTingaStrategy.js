/**
 * Tinga Tinga Trading Strategy
 * Main strategy implementation with RSI-based entry/exit signals
 */

const EventEmitter = require('events');
const config = require('../utils/Config');
const logger = require('../utils/Logger');
const RiskManager = require('./RiskManager');
const TechnicalIndicators = require('./TechnicalIndicators');
const BinanceDataFeed = require('../market/BinanceDataFeed');
const MarketDataProcessor = require('../market/MarketDataProcessor');
const OrderManager = require('../trading/OrderManager');
const PortfolioTracker = require('../trading/PortfolioTracker');

class TingaTingaStrategy extends EventEmitter {
  constructor(strategyConfig = null) {
    super();
    
    // Strategy configuration
    this.config = strategyConfig || config.strategy;
    
    // Core components
    this.riskManager = new RiskManager();
    this.dataFeed = new BinanceDataFeed();
    this.dataProcessor = new MarketDataProcessor();
    this.orderManager = new OrderManager();
    this.portfolio = new PortfolioTracker(config.simulation.initialBalance);
    
    // Strategy state
    this.isRunning = false;
    this.lastSignal = null;
    this.lastTradeTime = 0;
    this.minTimeBetweenTrades = 60000; // 1 minute minimum between trades
    this.openPositions = new Map(); // Track open positions at strategy level
    
    // Performance tracking
    this.startTime = null;
    this.tickCount = 0;
    this.signalCount = 0;
    
    // Setup event handlers
    this.setupEventHandlers();
    
    // Initialize logger
    logger.init(config.logging);
  }

  /**
   * Setup event handlers for components
   */
  setupEventHandlers() {
    // Order manager events
    this.orderManager.on('orderFilled', (order) => {
      this.portfolio.onOrderFilled(order);
      this.emit('orderFilled', order);
    });
    
    this.orderManager.on('positionOpened', (position) => {
      this.riskManager.addPosition(position);
      this.portfolio.onPositionOpened(position);
      this.openPositions.set(position.id, position);
      this.emit('positionOpened', position);
    });
    
    this.orderManager.on('positionClosed', (position) => {
      this.riskManager.closePosition(position.id, position.closePrice);
      this.portfolio.onPositionClosed(position);
      this.openPositions.delete(position.id);
      this.emit('positionClosed', position);
    });
    
    // Portfolio events
    this.portfolio.on('equityUpdate', (update) => {
      this.emit('equityUpdate', update);
    });
  }

  /**
   * Initialize strategy
   */
  async initialize() {
    logger.info('Initializing Tinga Tinga Strategy', {
      symbol: this.config.symbol,
      rsiPeriod: this.config.rsiPeriod,
      profitTarget: this.config.profitPercentage + '%',
      stopLoss: this.config.lossPercentage + '%',
      riskPerTrade: this.config.balancePercentage + '%'
    });
    
    // Validate configuration
    if (!config.validate()) {
      throw new Error('Invalid configuration');
    }
    
    // Get symbol info from Binance
    try {
      const symbolInfo = await this.dataFeed.getSymbolInfo(this.config.symbol);
      logger.info('Symbol info loaded', {
        symbol: this.config.symbol,
        minQty: symbolInfo.filters.find(f => f.filterType === 'LOT_SIZE')?.minQty,
        tickSize: symbolInfo.filters.find(f => f.filterType === 'PRICE_FILTER')?.tickSize
      });
    } catch (error) {
      logger.error('Failed to load symbol info', { error: error.message });
      throw error;
    }
    
    // Load initial market data
    await this.loadInitialData();
    
    logger.info('Strategy initialized successfully');
  }

  /**
   * Load initial historical data
   */
  async loadInitialData() {
    logger.info('Loading initial market data...');
    
    try {
      // Fetch enough candles for indicator calculation
      const requiredCandles = this.config.rsiPeriod * 2 + 10;
      const klines = await this.dataFeed.getKlines(
        this.config.symbol,
        this.config.timeframe,
        requiredCandles
      );
      
      // Process data and calculate indicators
      const processedData = this.dataProcessor.processKlines(
        this.config.symbol,
        klines,
        { rsiPeriod: this.config.rsiPeriod }
      );
      
      logger.info('Initial data loaded', {
        candles: klines.length,
        latestPrice: processedData.latestCandle.close,
        latestRSI: processedData.indicators.rsi?.[processedData.indicators.rsi.length - 1]?.toFixed(2)
      });
      
    } catch (error) {
      logger.error('Failed to load initial data', { error: error.message });
      throw error;
    }
  }

  /**
   * Start the trading strategy
   */
  async start() {
    if (this.isRunning) {
      logger.warn('Strategy is already running');
      return;
    }
    
    logger.info('Starting Tinga Tinga Strategy');
    
    this.isRunning = true;
    this.startTime = Date.now();
    
    // Start main trading loop
    this.tradingLoop();
    
    this.emit('started');
  }

  /**
   * Stop the trading strategy
   */
  async stop() {
    if (!this.isRunning) {
      logger.warn('Strategy is not running');
      return;
    }
    
    logger.info('Stopping Tinga Tinga Strategy');
    
    this.isRunning = false;
    
    // Close all open positions
    const currentPrice = await this.dataFeed.getCurrentPrice(this.config.symbol);
    const closedPositions = this.orderManager.closeAllPositions(currentPrice);
    
    // Log final performance
    this.portfolio.logPerformance();
    
    // Calculate run statistics
    const runTime = Date.now() - this.startTime;
    const stats = {
      runTime: Math.floor(runTime / 1000) + 's',
      ticksProcessed: this.tickCount,
      signalsGenerated: this.signalCount,
      ...this.portfolio.getPerformanceMetrics()
    };
    
    logger.info('Strategy stopped', stats);
    
    this.emit('stopped', stats);
  }

  /**
   * Main trading loop
   */
  async tradingLoop() {
    while (this.isRunning) {
      try {
        await this.processTick();
        
        // Wait before next tick (rate limiting)
        await new Promise(resolve => setTimeout(resolve, 5000)); // 5 second intervals
        
      } catch (error) {
        logger.error('Error in trading loop', { error: error.message });
        
        // Wait longer on error
        await new Promise(resolve => setTimeout(resolve, 30000)); // 30 seconds
      }
    }
  }

  /**
   * Process a single tick
   */
  async processTick() {
    this.tickCount++;
    
    // Fetch latest market data
    const klines = await this.dataFeed.getKlines(
      this.config.symbol,
      this.config.timeframe,
      this.config.rsiPeriod * 2 + 10
    );
    
    // Process data and calculate indicators
    const processedData = this.dataProcessor.processKlines(
      this.config.symbol,
      klines,
      { rsiPeriod: this.config.rsiPeriod }
    );
    
    if (!processedData || !processedData.indicators.rsi) {
      logger.debug('Insufficient data for analysis');
      return;
    }
    
    // Update open positions
    await this.updatePositions(processedData.latestCandle.close);
    
    // Check for trading signals
    const signal = this.dataProcessor.getSignals(processedData, this.config);
    
    if (signal.signal !== 'NONE') {
      this.signalCount++;
      logger.info('Trading signal detected', signal);
      
      // Check if we can trade
      if (this.canTrade(signal)) {
        await this.executeSignal(signal, processedData);
      }
    }
    
    // Log tick summary every 10 ticks
    if (this.tickCount % 10 === 0) {
      const state = this.portfolio.getState();
      logger.debug('Tick summary', {
        tick: this.tickCount,
        price: processedData.latestCandle.close,
        rsi: signal.rsi?.toFixed(2),
        balance: state.balance.toFixed(2),
        equity: state.equity.toFixed(2),
        positions: this.openPositions.size
      });
    }
  }

  /**
   * Update all open positions with current price
   * @param {number} currentPrice - Current market price
   */
  async updatePositions(currentPrice) {
    if (this.openPositions.size === 0) {
      return;
    }

    for (const [positionId, position] of this.openPositions) {
      if (position.symbol === this.config.symbol) {
        const updatedPosition = this.orderManager.updatePosition(positionId, currentPrice);

        if (updatedPosition) {
          // Check if position was closed (status changed to CLOSED)
          if (updatedPosition.status === 'CLOSED') {
            // Position was closed by SL/TP
            this.openPositions.delete(positionId);
            this.portfolio.onPositionClosed(updatedPosition);

            const closeReason = updatedPosition.closeReason === 'STOP_LOSS' ? 'Stop Loss' : 'Take Profit';
            logger.info('Position closed by ' + closeReason, {
              positionId: positionId,
              closePrice: updatedPosition.closePrice,
              profit: updatedPosition.profit.toFixed(2)
            });
          } else {
            // Position is still open, update our local tracking
            this.openPositions.set(positionId, updatedPosition);
            this.portfolio.updatePosition(updatedPosition);
          }
        }
      }
    }
  }

  /**
   * Check if we can execute a trade
   * @param {Object} signal - Trading signal
   * @returns {boolean} Can trade
   */
  canTrade(signal) {
    // Check if enough time has passed since last trade
    const timeSinceLastTrade = Date.now() - this.lastTradeTime;
    if (timeSinceLastTrade < this.minTimeBetweenTrades) {
      logger.debug('Too soon since last trade', {
        timeSinceLastTrade,
        minRequired: this.minTimeBetweenTrades
      });
      return false;
    }
    
    // Check if we have open positions in the same direction
    const openPositionsArray = Array.from(this.openPositions.values()).filter(p => p.symbol === this.config.symbol);
    const hasSameDirection = openPositionsArray.some(p =>
      (p.side === 'BUY' && signal.signal === 'BUY') ||
      (p.side === 'SELL' && signal.signal === 'SELL')
    );
    
    if (hasSameDirection) {
      logger.debug('Already have position in same direction');
      return false;
    }
    
    // Check trading halt conditions
    const haltCheck = this.riskManager.checkTradingHalt(
      this.portfolio.balance,
      this.portfolio.initialBalance
    );
    
    if (haltCheck.shouldHalt) {
      logger.error('Trading halted', haltCheck);
      this.stop();
      return false;
    }
    
    return true;
  }

  /**
   * Execute a trading signal
   * @param {Object} signal - Trading signal
   * @param {Object} processedData - Processed market data
   */
  async executeSignal(signal, processedData) {
    const currentPrice = processedData.latestCandle.close;
    const direction = signal.signal;
    
    // Calculate stop loss and take profit
    const stopLoss = this.riskManager.calculateStopLoss(
      currentPrice,
      direction,
      this.config.lossPercentage
    );
    
    const takeProfit = this.riskManager.calculateTakeProfit(
      currentPrice,
      direction,
      this.config.profitPercentage
    );
    
    // Calculate position size
    const positionSizing = this.riskManager.calculatePositionSize(
      this.portfolio.balance,
      currentPrice,
      stopLoss,
      this.config
    );
    
    // Validate position risk
    const validation = this.riskManager.validateNewPosition(
      this.portfolio.balance,
      positionSizing.riskAmount
    );
    
    if (!validation.isValid) {
      logger.warn('Position rejected', validation);
      return;
    }
    
    // Calculate risk-reward ratio
    const rrr = this.riskManager.calculateRiskRewardRatio(currentPrice, stopLoss, takeProfit);
    
    logger.info('Executing trade signal', {
      signal: direction,
      price: currentPrice,
      stopLoss,
      takeProfit,
      size: positionSizing.size,
      risk: positionSizing.riskAmount.toFixed(2),
      rrr: rrr.toFixed(2)
    });
    
    // Execute order
    try {
      const order = await this.orderManager.marketOrder({
        symbol: this.config.symbol,
        side: direction,
        quantity: positionSizing.size,
        price: currentPrice,
        stopLoss,
        takeProfit,
        comment: `RSI: ${signal.rsi?.toFixed(2)}, Strength: ${signal.strength.toFixed(2)}`,
        magicNumber: this.config.magicNumber
      });
      
      this.lastTradeTime = Date.now();
      this.lastSignal = signal;
      
      logger.info('Order executed successfully', {
        orderId: order.id,
        executedPrice: order.executedPrice,
        slippage: order.slippage
      });
      
    } catch (error) {
      logger.error('Failed to execute order', { error: error.message });
    }
  }

  /**
   * Backtest the strategy on historical data
   * @param {Date} startDate - Backtest start date
   * @param {Date} endDate - Backtest end date
   */
  async backtest(startDate, endDate) {
    logger.info('Starting backtest', {
      startDate: startDate.toISOString(),
      endDate: endDate.toISOString(),
      symbol: this.config.symbol
    });
    
    // Reset components
    this.portfolio.reset();
    this.orderManager.reset();
    this.riskManager = new RiskManager();
    this.openPositions.clear();
    
    // Fetch historical data
    const klines = await this.dataFeed.getKlines(
      this.config.symbol,
      this.config.timeframe,
      1000,
      startDate.getTime(),
      endDate.getTime()
    );
    
    logger.info('Historical data loaded', { candles: klines.length });
    
    // Process each candle
    for (let i = this.config.rsiPeriod * 2; i < klines.length; i++) {
      // Get subset of klines up to current point
      const currentKlines = klines.slice(0, i + 1);
      
      // Process data
      const processedData = this.dataProcessor.processKlines(
        this.config.symbol,
        currentKlines.slice(-this.config.rsiPeriod * 2 - 10),
        { rsiPeriod: this.config.rsiPeriod }
      );
      
      if (!processedData || !processedData.indicators.rsi) {
        continue;
      }
      
      // Update positions
      await this.updatePositions(processedData.latestCandle.close);
      
      // Check for signals
      const signal = this.dataProcessor.getSignals(processedData, this.config);
      
      if (signal.signal !== 'NONE' && this.canTrade(signal)) {
        await this.executeSignal(signal, processedData);
      }
    }
    
    // Close remaining positions
    const finalPrice = klines[klines.length - 1].close;
    this.orderManager.closeAllPositions(finalPrice);
    
    // Get backtest results
    const results = {
      period: {
        start: startDate.toISOString(),
        end: endDate.toISOString(),
        days: Math.floor((endDate - startDate) / (1000 * 60 * 60 * 24))
      },
      candlesProcessed: klines.length,
      ...this.portfolio.getPerformanceMetrics(),
      ...this.orderManager.getStatistics()
    };
    
    logger.info('Backtest completed', results);
    
    return results;
  }

  /**
   * Get current strategy status
   * @returns {Object} Strategy status
   */
  getStatus() {
    const portfolio = this.portfolio.getState();
    const riskExposure = this.riskManager.getRiskExposure(portfolio.balance);
    const statistics = this.orderManager.getStatistics();
    
    return {
      isRunning: this.isRunning,
      startTime: this.startTime,
      runTime: this.isRunning ? Date.now() - this.startTime : 0,
      tickCount: this.tickCount,
      signalCount: this.signalCount,
      lastSignal: this.lastSignal,
      openPositions: Array.from(this.openPositions.values()),
      portfolio,
      riskExposure,
      statistics,
      config: this.config
    };
  }

  /**
   * Update strategy configuration
   * @param {Object} newConfig - New configuration values
   */
  updateConfig(newConfig) {
    Object.assign(this.config, newConfig);
    logger.info('Strategy configuration updated', newConfig);
  }
}

module.exports = TingaTingaStrategy;