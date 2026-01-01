/**
 * Portfolio Tracker for Tinga Tinga trading strategy
 * Tracks account balance, equity, and performance metrics
 */

const EventEmitter = require('events');
const logger = require('../utils/Logger');

class PortfolioTracker extends EventEmitter {
  constructor(initialBalance = 10000) {
    super();
    
    // Account state
    this.initialBalance = initialBalance;
    this.balance = initialBalance;
    this.equity = initialBalance;
    this.margin = 0;
    this.freeMargin = initialBalance;
    
    // Performance tracking
    this.balanceHistory = [{
      timestamp: Date.now(),
      balance: initialBalance,
      equity: initialBalance,
      margin: 0,
      freeMargin: initialBalance
    }];
    
    // Trade tracking
    this.trades = [];
    this.openPositions = new Map();
    
    // Performance metrics
    this.peakBalance = initialBalance;
    this.maxDrawdown = 0;
    this.maxDrawdownPercent = 0;
    this.totalProfit = 0;
    this.totalLoss = 0;
    this.totalCommission = 0;
    
    // Daily tracking
    this.dailyStats = new Map();
    this.lastDailyReset = this.getDayKey(new Date());
  }

  /**
   * Get day key for daily statistics
   * @param {Date} date - Date object
   * @returns {string} Day key (YYYY-MM-DD)
   */
  getDayKey(date) {
    return date.toISOString().split('T')[0];
  }

  /**
   * Update balance after trade execution
   * @param {Object} order - Executed order
   */
  onOrderFilled(order) {
    // Deduct commission
    this.balance -= order.commission;
    this.totalCommission += order.commission;
    
    // Log balance update
    logger.debug('Balance updated after order', {
      orderId: order.id,
      commission: order.commission,
      newBalance: this.balance
    });
    
    this.updateEquity();
  }

  /**
   * Add position to portfolio
   * @param {Object} position - Position object
   */
  onPositionOpened(position) {
    this.openPositions.set(position.id, {
      ...position,
      originalPosition: { ...position }
    });
    
    // Calculate margin requirement (simplified)
    const margin = position.quantity * position.entryPrice * 0.1; // 10:1 leverage
    this.margin += margin;
    
    this.updateEquity();
    
    logger.debug('Position added to portfolio', {
      positionId: position.id,
      margin,
      totalMargin: this.margin
    });
  }

  /**
   * Update position in portfolio
   * @param {Object} position - Updated position
   */
  updatePosition(position) {
    const portfolioPosition = this.openPositions.get(position.id);
    if (!portfolioPosition) return;
    
    // Update position data
    Object.assign(portfolioPosition, position);
    
    this.updateEquity();
  }

  /**
   * Remove position from portfolio
   * @param {Object} position - Closed position
   */
  onPositionClosed(position) {
    const portfolioPosition = this.openPositions.get(position.id);
    if (!portfolioPosition) return;
    
    // Update balance with P&L
    this.balance += position.profit;
    
    // Update profit/loss tracking
    if (position.profit > 0) {
      this.totalProfit += position.profit;
    } else {
      this.totalLoss += Math.abs(position.profit);
    }
    
    // Release margin
    const margin = portfolioPosition.quantity * portfolioPosition.entryPrice * 0.1;
    this.margin = Math.max(0, this.margin - margin);
    
    // Add to trade history
    this.trades.push({
      ...position,
      closedAt: Date.now()
    });
    
    // Remove from open positions
    this.openPositions.delete(position.id);
    
    // Update daily stats
    this.updateDailyStats(position);
    
    // Update equity and check drawdown
    this.updateEquity();
    this.updateDrawdown();
    
    logger.info('Position closed in portfolio', {
      positionId: position.id,
      profit: position.profit,
      newBalance: this.balance,
      totalTrades: this.trades.length
    });
  }

  /**
   * Update account equity
   */
  updateEquity() {
    // Calculate floating P&L
    let floatingPnL = 0;
    
    for (const [id, position] of this.openPositions) {
      floatingPnL += position.profit || 0;
    }
    
    this.equity = this.balance + floatingPnL;
    this.freeMargin = this.equity - this.margin;
    
    // Record history point
    const historyPoint = {
      timestamp: Date.now(),
      balance: this.balance,
      equity: this.equity,
      margin: this.margin,
      freeMargin: this.freeMargin,
      floatingPnL,
      openPositions: this.openPositions.size
    };
    
    this.balanceHistory.push(historyPoint);
    
    // Emit equity update event
    this.emit('equityUpdate', historyPoint);
  }

  /**
   * Update maximum drawdown
   */
  updateDrawdown() {
    // Update peak balance
    if (this.balance > this.peakBalance) {
      this.peakBalance = this.balance;
    }
    
    // Calculate current drawdown
    const drawdown = this.peakBalance - this.balance;
    const drawdownPercent = (drawdown / this.peakBalance) * 100;
    
    // Update maximum drawdown
    if (drawdown > this.maxDrawdown) {
      this.maxDrawdown = drawdown;
      this.maxDrawdownPercent = drawdownPercent;
      
      logger.warn('New maximum drawdown', {
        drawdown: drawdown.toFixed(2),
        drawdownPercent: drawdownPercent.toFixed(2) + '%',
        peakBalance: this.peakBalance,
        currentBalance: this.balance
      });
    }
  }

  /**
   * Update daily statistics
   * @param {Object} position - Closed position
   */
  updateDailyStats(position) {
    const dayKey = this.getDayKey(new Date());
    
    // Reset daily stats if new day
    if (dayKey !== this.lastDailyReset) {
      this.lastDailyReset = dayKey;
    }
    
    // Get or create daily stats
    let dailyStats = this.dailyStats.get(dayKey) || {
      trades: 0,
      profit: 0,
      loss: 0,
      commission: 0,
      winningTrades: 0,
      losingTrades: 0,
      startBalance: this.balance - position.profit,
      endBalance: this.balance
    };
    
    // Update stats
    dailyStats.trades++;
    dailyStats.commission += position.commission;
    dailyStats.endBalance = this.balance;
    
    if (position.profit > 0) {
      dailyStats.profit += position.profit;
      dailyStats.winningTrades++;
    } else {
      dailyStats.loss += Math.abs(position.profit);
      dailyStats.losingTrades++;
    }
    
    this.dailyStats.set(dayKey, dailyStats);
  }

  /**
   * Get current portfolio state
   * @returns {Object} Portfolio state
   */
  getState() {
    return {
      balance: this.balance,
      equity: this.equity,
      margin: this.margin,
      freeMargin: this.freeMargin,
      marginLevel: this.margin > 0 ? (this.equity / this.margin) * 100 : Infinity,
      floatingPnL: this.equity - this.balance,
      openPositions: this.openPositions.size,
      totalTrades: this.trades.length
    };
  }

  /**
   * Get performance metrics
   * @returns {Object} Performance metrics
   */
  getPerformanceMetrics() {
    const totalReturn = ((this.balance - this.initialBalance) / this.initialBalance) * 100;
    const winningTrades = this.trades.filter(t => t.profit > 0);
    const losingTrades = this.trades.filter(t => t.profit <= 0);
    
    // Calculate average trade metrics
    const avgWin = winningTrades.length > 0 
      ? winningTrades.reduce((sum, t) => sum + t.profit, 0) / winningTrades.length 
      : 0;
    
    const avgLoss = losingTrades.length > 0
      ? losingTrades.reduce((sum, t) => sum + Math.abs(t.profit), 0) / losingTrades.length
      : 0;
    
    // Calculate profit factor
    const profitFactor = this.totalLoss > 0 ? this.totalProfit / this.totalLoss : 
                        this.totalProfit > 0 ? Infinity : 0;
    
    // Calculate Sharpe ratio (simplified)
    const returns = this.calculateReturns();
    const sharpeRatio = this.calculateSharpeRatio(returns);
    
    return {
      balance: this.balance,
      equity: this.equity,
      totalReturn,
      totalProfit: this.totalProfit,
      totalLoss: this.totalLoss,
      netProfit: this.totalProfit - this.totalLoss,
      totalCommission: this.totalCommission,
      totalTrades: this.trades.length,
      winningTrades: winningTrades.length,
      losingTrades: losingTrades.length,
      winRate: this.trades.length > 0 ? (winningTrades.length / this.trades.length) * 100 : 0,
      avgWin,
      avgLoss,
      avgRRR: avgLoss > 0 ? avgWin / avgLoss : avgWin > 0 ? Infinity : 0,
      profitFactor,
      maxDrawdown: this.maxDrawdown,
      maxDrawdownPercent: this.maxDrawdownPercent,
      sharpeRatio
    };
  }

  /**
   * Calculate returns for Sharpe ratio
   * @returns {Array} Array of returns
   */
  calculateReturns() {
    const returns = [];
    
    for (let i = 1; i < this.balanceHistory.length; i++) {
      const prevBalance = this.balanceHistory[i - 1].balance;
      const currBalance = this.balanceHistory[i].balance;
      const returnPct = ((currBalance - prevBalance) / prevBalance) * 100;
      returns.push(returnPct);
    }
    
    return returns;
  }

  /**
   * Calculate Sharpe ratio
   * @param {Array} returns - Array of returns
   * @param {number} riskFreeRate - Risk-free rate (annual %)
   * @returns {number} Sharpe ratio
   */
  calculateSharpeRatio(returns, riskFreeRate = 2) {
    if (returns.length === 0) return 0;
    
    // Calculate average return
    const avgReturn = returns.reduce((sum, r) => sum + r, 0) / returns.length;
    
    // Calculate standard deviation
    const variance = returns.reduce((sum, r) => sum + Math.pow(r - avgReturn, 2), 0) / returns.length;
    const stdDev = Math.sqrt(variance);
    
    // Annualize (assuming daily returns)
    const annualizedReturn = avgReturn * 252;
    const annualizedStdDev = stdDev * Math.sqrt(252);
    
    // Calculate Sharpe ratio
    return stdDev > 0 ? (annualizedReturn - riskFreeRate) / annualizedStdDev : 0;
  }

  /**
   * Get daily performance summary
   * @param {string} date - Date string (YYYY-MM-DD)
   * @returns {Object} Daily performance
   */
  getDailyPerformance(date = null) {
    const dayKey = date || this.getDayKey(new Date());
    const stats = this.dailyStats.get(dayKey);
    
    if (!stats) {
      return {
        date: dayKey,
        trades: 0,
        profit: 0,
        loss: 0,
        netProfit: 0,
        commission: 0,
        winRate: 0,
        dailyReturn: 0
      };
    }
    
    const netProfit = stats.profit - stats.loss;
    const dailyReturn = stats.startBalance > 0 
      ? ((stats.endBalance - stats.startBalance) / stats.startBalance) * 100 
      : 0;
    
    return {
      date: dayKey,
      trades: stats.trades,
      profit: stats.profit,
      loss: stats.loss,
      netProfit,
      commission: stats.commission,
      winRate: stats.trades > 0 ? (stats.winningTrades / stats.trades) * 100 : 0,
      dailyReturn,
      startBalance: stats.startBalance,
      endBalance: stats.endBalance
    };
  }

  /**
   * Get balance history for charting
   * @param {number} limit - Number of points to return
   * @returns {Array} Balance history points
   */
  getBalanceHistory(limit = null) {
    const history = limit ? this.balanceHistory.slice(-limit) : this.balanceHistory;
    
    return history.map(point => ({
      timestamp: point.timestamp,
      balance: point.balance,
      equity: point.equity,
      drawdown: this.peakBalance > 0 ? ((this.peakBalance - point.balance) / this.peakBalance) * 100 : 0
    }));
  }

  /**
   * Log current performance metrics
   */
  logPerformance() {
    const metrics = this.getPerformanceMetrics();
    logger.logPerformance(metrics);
  }

  /**
   * Reset portfolio to initial state
   */
  reset() {
    this.balance = this.initialBalance;
    this.equity = this.initialBalance;
    this.margin = 0;
    this.freeMargin = this.initialBalance;
    
    this.balanceHistory = [{
      timestamp: Date.now(),
      balance: this.initialBalance,
      equity: this.initialBalance,
      margin: 0,
      freeMargin: this.initialBalance
    }];
    
    this.trades = [];
    this.openPositions.clear();
    
    this.peakBalance = this.initialBalance;
    this.maxDrawdown = 0;
    this.maxDrawdownPercent = 0;
    this.totalProfit = 0;
    this.totalLoss = 0;
    this.totalCommission = 0;
    
    this.dailyStats.clear();
    this.lastDailyReset = this.getDayKey(new Date());
    
    logger.info('Portfolio reset to initial state', {
      initialBalance: this.initialBalance
    });
  }
}

module.exports = PortfolioTracker;