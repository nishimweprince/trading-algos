/**
 * Order Manager for Tinga Tinga trading strategy
 * Simulates order execution and manages trade lifecycle
 */

const { v4: uuidv4 } = require('uuid');
const EventEmitter = require('events');
const logger = require('../utils/Logger');

class OrderManager extends EventEmitter {
  constructor() {
    super();
    
    // Order tracking
    this.orders = new Map();
    this.orderHistory = [];
    this.activeOrders = new Map();
    
    // Position tracking
    this.positions = new Map();
    this.positionHistory = [];
    
    // Order ID counter
    this.orderIdCounter = 1;
    
    // Simulated order execution delay (ms)
    this.executionDelay = 100;
    
    // Slippage simulation
    this.slippageEnabled = true;
    this.maxSlippagePercent = 0.1; // 0.1%
  }

  /**
   * Generate unique order ID
   * @returns {string} Order ID
   */
  generateOrderId() {
    return `ORD-${Date.now()}-${this.orderIdCounter++}`;
  }

  /**
   * Simulate market order execution
   * @param {Object} orderParams - Order parameters
   * @returns {Promise<Object>} Executed order details
   */
  async marketOrder(orderParams) {
    const {
      symbol,
      side,
      quantity,
      price,
      stopLoss,
      takeProfit,
      comment = '',
      magicNumber = 0
    } = orderParams;

    const orderId = this.generateOrderId();
    const timestamp = Date.now();

    // Create order object
    const order = {
      id: orderId,
      type: 'MARKET',
      symbol,
      side: side.toUpperCase(),
      quantity,
      requestedPrice: price,
      stopLoss,
      takeProfit,
      comment,
      magicNumber,
      status: 'PENDING',
      timestamp,
      fills: []
    };

    // Store order
    this.orders.set(orderId, order);
    this.activeOrders.set(orderId, order);

    // Emit order created event
    this.emit('orderCreated', order);

    // Simulate order execution
    await this.simulateExecution(order);

    return order;
  }

  /**
   * Simulate order execution with slippage
   * @param {Object} order - Order object
   */
  async simulateExecution(order) {
    // Simulate network/execution delay
    await new Promise(resolve => setTimeout(resolve, this.executionDelay));

    // Calculate execution price with slippage
    let executionPrice = order.requestedPrice;
    
    if (this.slippageEnabled) {
      const slippage = (Math.random() - 0.5) * 2 * this.maxSlippagePercent / 100;
      executionPrice = order.requestedPrice * (1 + slippage);
    }

    // Create fill
    const fill = {
      price: executionPrice,
      quantity: order.quantity,
      timestamp: Date.now(),
      commission: this.calculateCommission(order.quantity * executionPrice)
    };

    order.fills.push(fill);
    order.executedPrice = executionPrice;
    order.executedQuantity = order.quantity;
    order.status = 'FILLED';
    order.executionTime = Date.now();

    // Calculate trade costs
    order.totalCost = order.quantity * executionPrice;
    order.commission = fill.commission;
    order.slippage = executionPrice - order.requestedPrice;
    order.slippagePercent = (order.slippage / order.requestedPrice) * 100;

    // Create position from filled order
    const position = this.createPosition(order);

    // Log trade execution
    logger.logTrade(order.side, {
      symbol: order.symbol,
      orderId: order.id,
      volume: order.quantity,
      entryPrice: executionPrice,
      requestedPrice: order.requestedPrice,
      slippage: order.slippage.toFixed(4),
      stopLoss: order.stopLoss,
      takeProfit: order.takeProfit,
      riskAmount: Math.abs((executionPrice - order.stopLoss) * order.quantity),
      commission: order.commission
    });

    // Emit events
    this.emit('orderFilled', order);
    this.emit('positionOpened', position);

    // Move to history
    this.activeOrders.delete(order.id);
    this.orderHistory.push(order);

    return order;
  }

  /**
   * Create position from filled order
   * @param {Object} order - Filled order
   * @returns {Object} Position object
   */
  createPosition(order) {
    const positionId = `POS-${order.id}`;
    
    const position = {
      id: positionId,
      orderId: order.id,
      symbol: order.symbol,
      side: order.side,
      quantity: order.quantity,
      entryPrice: order.executedPrice,
      currentPrice: order.executedPrice,
      stopLoss: order.stopLoss,
      takeProfit: order.takeProfit,
      openTime: order.executionTime,
      commission: order.commission,
      swap: 0,
      profit: 0,
      profitPercent: 0,
      status: 'OPEN',
      magicNumber: order.magicNumber,
      comment: order.comment
    };

    this.positions.set(positionId, position);
    
    return position;
  }

  /**
   * Update position with current market price
   * @param {string} positionId - Position ID
   * @param {number} currentPrice - Current market price
   * @returns {Object} Updated position
   */
  updatePosition(positionId, currentPrice) {
    const position = this.positions.get(positionId);
    if (!position || position.status !== 'OPEN') {
      return null;
    }

    position.currentPrice = currentPrice;

    // Calculate P&L
    if (position.side === 'BUY') {
      position.profit = (currentPrice - position.entryPrice) * position.quantity;
    } else {
      position.profit = (position.entryPrice - currentPrice) * position.quantity;
    }

    position.profitPercent = (position.profit / (position.entryPrice * position.quantity)) * 100;

    // Check stop loss
    if (position.side === 'BUY' && currentPrice <= position.stopLoss) {
      return this.closePosition(positionId, currentPrice, 'STOP_LOSS');
    } else if (position.side === 'SELL' && currentPrice >= position.stopLoss) {
      return this.closePosition(positionId, currentPrice, 'STOP_LOSS');
    }

    // Check take profit
    if (position.side === 'BUY' && currentPrice >= position.takeProfit) {
      return this.closePosition(positionId, currentPrice, 'TAKE_PROFIT');
    } else if (position.side === 'SELL' && currentPrice <= position.takeProfit) {
      return this.closePosition(positionId, currentPrice, 'TAKE_PROFIT');
    }

    return position;
  }

  /**
   * Close a position
   * @param {string} positionId - Position ID
   * @param {number} closePrice - Closing price
   * @param {string} reason - Close reason
   * @returns {Object} Closed position
   */
  closePosition(positionId, closePrice, reason = 'MANUAL') {
    const position = this.positions.get(positionId);
    if (!position || position.status !== 'OPEN') {
      return null;
    }

    // Apply slippage to close price
    if (this.slippageEnabled && reason === 'MANUAL') {
      const slippage = (Math.random() - 0.5) * 2 * this.maxSlippagePercent / 100;
      closePrice = closePrice * (1 + slippage);
    }

    // Calculate final P&L
    if (position.side === 'BUY') {
      position.profit = (closePrice - position.entryPrice) * position.quantity;
    } else {
      position.profit = (position.entryPrice - closePrice) * position.quantity;
    }

    position.profitPercent = (position.profit / (position.entryPrice * position.quantity)) * 100;

    // Add closing commission
    const closingCommission = this.calculateCommission(position.quantity * closePrice);
    position.commission += closingCommission;
    position.profit -= closingCommission;

    // Update position status
    position.status = 'CLOSED';
    position.closePrice = closePrice;
    position.closeTime = Date.now();
    position.closeReason = reason;
    position.duration = position.closeTime - position.openTime;

    // Log position closure
    logger.info('Position closed', {
      positionId,
      symbol: position.symbol,
      side: position.side,
      entryPrice: position.entryPrice,
      closePrice,
      profit: position.profit.toFixed(2),
      profitPercent: position.profitPercent.toFixed(2) + '%',
      reason,
      duration: Math.floor(position.duration / 1000) + 's'
    });

    // Move to history
    this.positions.delete(positionId);
    this.positionHistory.push(position);

    // Emit event
    this.emit('positionClosed', position);

    return position;
  }

  /**
   * Close all open positions
   * @param {number} currentPrice - Current market price
   * @returns {Array} Closed positions
   */
  closeAllPositions(currentPrice) {
    const closedPositions = [];
    
    for (const [positionId, position] of this.positions) {
      if (position.status === 'OPEN') {
        const closed = this.closePosition(positionId, currentPrice, 'CLOSE_ALL');
        if (closed) {
          closedPositions.push(closed);
        }
      }
    }

    logger.info('All positions closed', { count: closedPositions.length });
    
    return closedPositions;
  }

  /**
   * Calculate commission for a trade
   * @param {number} value - Trade value
   * @returns {number} Commission amount
   */
  calculateCommission(value) {
    const commissionRate = 0.001; // 0.1%
    return value * commissionRate;
  }

  /**
   * Get open positions
   * @param {string} symbol - Filter by symbol (optional)
   * @returns {Array} Open positions
   */
  getOpenPositions(symbol = null) {
    const positions = Array.from(this.positions.values());
    
    if (symbol) {
      return positions.filter(p => p.symbol === symbol && p.status === 'OPEN');
    }
    
    return positions.filter(p => p.status === 'OPEN');
  }

  /**
   * Get position by ID
   * @param {string} positionId - Position ID
   * @returns {Object|null} Position object
   */
  getPosition(positionId) {
    return this.positions.get(positionId) || 
           this.positionHistory.find(p => p.id === positionId);
  }

  /**
   * Get order history
   * @param {Object} filters - Filter options
   * @returns {Array} Filtered order history
   */
  getOrderHistory(filters = {}) {
    let history = [...this.orderHistory];
    
    if (filters.symbol) {
      history = history.filter(o => o.symbol === filters.symbol);
    }
    
    if (filters.side) {
      history = history.filter(o => o.side === filters.side);
    }
    
    if (filters.startTime) {
      history = history.filter(o => o.timestamp >= filters.startTime);
    }
    
    if (filters.endTime) {
      history = history.filter(o => o.timestamp <= filters.endTime);
    }
    
    return history;
  }

  /**
   * Get position history
   * @param {Object} filters - Filter options
   * @returns {Array} Filtered position history
   */
  getPositionHistory(filters = {}) {
    let history = [...this.positionHistory];
    
    if (filters.symbol) {
      history = history.filter(p => p.symbol === filters.symbol);
    }
    
    if (filters.profitable !== undefined) {
      history = history.filter(p => filters.profitable ? p.profit > 0 : p.profit <= 0);
    }
    
    if (filters.startTime) {
      history = history.filter(p => p.openTime >= filters.startTime);
    }
    
    if (filters.endTime) {
      history = history.filter(p => p.closeTime <= filters.endTime);
    }
    
    return history;
  }

  /**
   * Calculate trading statistics
   * @param {string} symbol - Filter by symbol (optional)
   * @returns {Object} Trading statistics
   */
  getStatistics(symbol = null) {
    const positions = symbol 
      ? this.positionHistory.filter(p => p.symbol === symbol)
      : this.positionHistory;

    if (positions.length === 0) {
      return {
        totalTrades: 0,
        winningTrades: 0,
        losingTrades: 0,
        winRate: 0,
        totalProfit: 0,
        totalLoss: 0,
        profitFactor: 0,
        averageWin: 0,
        averageLoss: 0,
        largestWin: 0,
        largestLoss: 0,
        averageHoldingTime: 0,
        totalCommission: 0
      };
    }

    const winningTrades = positions.filter(p => p.profit > 0);
    const losingTrades = positions.filter(p => p.profit <= 0);
    
    const totalProfit = winningTrades.reduce((sum, p) => sum + p.profit, 0);
    const totalLoss = Math.abs(losingTrades.reduce((sum, p) => sum + p.profit, 0));
    const totalCommission = positions.reduce((sum, p) => sum + p.commission, 0);
    
    const avgHoldingTime = positions.reduce((sum, p) => sum + p.duration, 0) / positions.length;
    
    return {
      totalTrades: positions.length,
      winningTrades: winningTrades.length,
      losingTrades: losingTrades.length,
      winRate: (winningTrades.length / positions.length) * 100,
      totalProfit,
      totalLoss,
      netProfit: totalProfit - totalLoss,
      profitFactor: totalLoss > 0 ? totalProfit / totalLoss : totalProfit > 0 ? Infinity : 0,
      averageWin: winningTrades.length > 0 ? totalProfit / winningTrades.length : 0,
      averageLoss: losingTrades.length > 0 ? totalLoss / losingTrades.length : 0,
      largestWin: winningTrades.length > 0 ? Math.max(...winningTrades.map(p => p.profit)) : 0,
      largestLoss: losingTrades.length > 0 ? Math.min(...losingTrades.map(p => p.profit)) : 0,
      averageHoldingTime: avgHoldingTime,
      totalCommission
    };
  }

  /**
   * Reset order manager
   */
  reset() {
    this.orders.clear();
    this.orderHistory = [];
    this.activeOrders.clear();
    this.positions.clear();
    this.positionHistory = [];
    this.orderIdCounter = 1;
    
    logger.info('Order manager reset');
  }
}

module.exports = OrderManager;