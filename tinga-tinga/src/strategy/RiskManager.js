/**
 * Risk Manager for Tinga Tinga trading strategy
 * Handles position sizing, stop loss, take profit, and risk calculations
 */

const config = require('../utils/Config');
const logger = require('../utils/Logger');

class RiskManager {
  constructor() {
    this.openPositions = new Map();
    this.maxDrawdown = 0;
    this.peakBalance = 0;
  }

  /**
   * Calculate position size based on risk parameters
   * @param {number} balance - Current account balance
   * @param {number} entryPrice - Entry price for the trade
   * @param {number} stopLossPrice - Stop loss price
   * @param {Object} strategyConfig - Strategy configuration
   * @returns {Object} Position sizing details
   */
  calculatePositionSize(balance, entryPrice, stopLossPrice, strategyConfig = null) {
    const cfg = strategyConfig || config.strategy;
    
    // Calculate risk amount (percentage of balance)
    const riskAmount = balance * (cfg.balancePercentage / 100);
    
    // Calculate price difference to stop loss
    const priceDifference = Math.abs(entryPrice - stopLossPrice);
    const riskPerUnit = priceDifference;
    
    // Calculate position size
    let positionSize = riskAmount / riskPerUnit;
    
    // Apply lot size constraints
    positionSize = Math.max(cfg.minLotSize, positionSize);
    positionSize = Math.min(cfg.maxLotSize, positionSize);
    
    // Round to lot step
    positionSize = Math.round(positionSize / cfg.lotStep) * cfg.lotStep;
    
    // Calculate actual risk with adjusted position size
    const actualRiskAmount = positionSize * riskPerUnit;
    const actualRiskPercentage = (actualRiskAmount / balance) * 100;
    
    logger.debug('Position size calculation', {
      balance,
      entryPrice,
      stopLossPrice,
      riskAmount,
      positionSize,
      actualRiskAmount,
      actualRiskPercentage
    });
    
    return {
      size: positionSize,
      riskAmount: actualRiskAmount,
      riskPercentage: actualRiskPercentage,
      value: positionSize * entryPrice
    };
  }

  /**
   * Calculate stop loss price based on percentage
   * @param {number} entryPrice - Entry price
   * @param {string} direction - Trade direction ('BUY' or 'SELL')
   * @param {number} lossPercentage - Loss percentage
   * @returns {number} Stop loss price
   */
  calculateStopLoss(entryPrice, direction, lossPercentage = null) {
    const lossPct = lossPercentage || config.strategy.lossPercentage;
    
    if (direction === 'BUY') {
      return entryPrice * (1 - lossPct / 100);
    } else {
      return entryPrice * (1 + lossPct / 100);
    }
  }

  /**
   * Calculate take profit price based on percentage
   * @param {number} entryPrice - Entry price
   * @param {string} direction - Trade direction ('BUY' or 'SELL')
   * @param {number} profitPercentage - Profit percentage
   * @returns {number} Take profit price
   */
  calculateTakeProfit(entryPrice, direction, profitPercentage = null) {
    const profitPct = profitPercentage || config.strategy.profitPercentage;
    
    if (direction === 'BUY') {
      return entryPrice * (1 + profitPct / 100);
    } else {
      return entryPrice * (1 - profitPct / 100);
    }
  }

  /**
   * Check if a new position can be opened based on risk limits
   * @param {number} balance - Current balance
   * @param {number} proposedRisk - Proposed risk amount
   * @returns {Object} Validation result
   */
  validateNewPosition(balance, proposedRisk) {
    // Calculate total open risk
    let totalOpenRisk = 0;
    for (const [id, position] of this.openPositions) {
      totalOpenRisk += position.riskAmount;
    }
    
    const totalRiskWithNew = totalOpenRisk + proposedRisk;
    const totalRiskPercentage = (totalRiskWithNew / balance) * 100;
    
    // Check against maximum allowed risk
    const maxTotalRisk = 10; // Maximum 10% total risk across all positions
    const isValid = totalRiskPercentage <= maxTotalRisk;
    
    logger.debug('Position validation', {
      totalOpenRisk,
      proposedRisk,
      totalRiskWithNew,
      totalRiskPercentage,
      isValid
    });
    
    return {
      isValid,
      totalRiskPercentage,
      message: isValid 
        ? 'Position risk within limits' 
        : `Total risk ${totalRiskPercentage.toFixed(2)}% exceeds maximum ${maxTotalRisk}%`
    };
  }

  /**
   * Add a new position to track
   * @param {Object} position - Position details
   */
  addPosition(position) {
    this.openPositions.set(position.id, {
      ...position,
      openTime: new Date(),
      maxProfit: 0,
      maxLoss: 0
    });
    
    logger.info('Position added to risk manager', {
      id: position.id,
      symbol: position.symbol,
      direction: position.direction,
      size: position.size,
      riskAmount: position.riskAmount
    });
  }

  /**
   * Update position with current market price
   * @param {string} positionId - Position ID
   * @param {number} currentPrice - Current market price
   * @returns {Object} Position status
   */
  updatePosition(positionId, currentPrice) {
    const position = this.openPositions.get(positionId);
    if (!position) {
      return null;
    }
    
    // Calculate P&L
    let pnl;
    if (position.direction === 'BUY') {
      pnl = (currentPrice - position.entryPrice) * position.size;
    } else {
      pnl = (position.entryPrice - currentPrice) * position.size;
    }
    
    const pnlPercentage = (pnl / (position.entryPrice * position.size)) * 100;
    
    // Update max profit/loss
    position.maxProfit = Math.max(position.maxProfit, pnl);
    position.maxLoss = Math.min(position.maxLoss, pnl);
    
    // Check if stop loss or take profit hit
    const stopLossHit = position.direction === 'BUY' 
      ? currentPrice <= position.stopLoss 
      : currentPrice >= position.stopLoss;
      
    const takeProfitHit = position.direction === 'BUY'
      ? currentPrice >= position.takeProfit
      : currentPrice <= position.takeProfit;
    
    return {
      position,
      currentPrice,
      pnl,
      pnlPercentage,
      stopLossHit,
      takeProfitHit,
      shouldClose: stopLossHit || takeProfitHit
    };
  }

  /**
   * Close a position
   * @param {string} positionId - Position ID
   * @param {number} closePrice - Closing price
   * @returns {Object} Closed position details
   */
  closePosition(positionId, closePrice) {
    const position = this.openPositions.get(positionId);
    if (!position) {
      return null;
    }
    
    // Calculate final P&L
    let pnl;
    if (position.direction === 'BUY') {
      pnl = (closePrice - position.entryPrice) * position.size;
    } else {
      pnl = (position.entryPrice - closePrice) * position.size;
    }
    
    const pnlPercentage = (pnl / (position.entryPrice * position.size)) * 100;
    const holdingTime = new Date() - position.openTime;
    
    // Remove from open positions
    this.openPositions.delete(positionId);
    
    logger.info('Position closed', {
      id: positionId,
      closePrice,
      pnl: pnl.toFixed(2),
      pnlPercentage: pnlPercentage.toFixed(2),
      holdingTime: Math.floor(holdingTime / 1000 / 60) + ' minutes'
    });
    
    return {
      ...position,
      closePrice,
      closeTime: new Date(),
      pnl,
      pnlPercentage,
      holdingTime
    };
  }

  /**
   * Calculate and update drawdown
   * @param {number} currentBalance - Current account balance
   */
  updateDrawdown(currentBalance) {
    // Update peak balance
    if (currentBalance > this.peakBalance) {
      this.peakBalance = currentBalance;
    }
    
    // Calculate current drawdown
    const drawdown = this.peakBalance > 0 
      ? ((this.peakBalance - currentBalance) / this.peakBalance) * 100 
      : 0;
    
    // Update max drawdown
    if (drawdown > this.maxDrawdown) {
      this.maxDrawdown = drawdown;
      logger.warn('New maximum drawdown', {
        maxDrawdown: this.maxDrawdown.toFixed(2) + '%',
        peakBalance: this.peakBalance,
        currentBalance
      });
    }
    
    return {
      currentDrawdown: drawdown,
      maxDrawdown: this.maxDrawdown
    };
  }

  /**
   * Get current risk exposure
   * @param {number} balance - Current balance
   * @returns {Object} Risk exposure details
   */
  getRiskExposure(balance) {
    let totalRisk = 0;
    let totalValue = 0;
    const positions = [];
    
    for (const [id, position] of this.openPositions) {
      totalRisk += position.riskAmount;
      totalValue += position.size * position.entryPrice;
      positions.push({
        id,
        symbol: position.symbol,
        direction: position.direction,
        risk: position.riskAmount
      });
    }
    
    return {
      openPositions: positions.length,
      totalRisk,
      totalRiskPercentage: (totalRisk / balance) * 100,
      totalValue,
      positions,
      maxDrawdown: this.maxDrawdown
    };
  }

  /**
   * Calculate risk-reward ratio
   * @param {number} entryPrice - Entry price
   * @param {number} stopLoss - Stop loss price
   * @param {number} takeProfit - Take profit price
   * @returns {number} Risk-reward ratio
   */
  calculateRiskRewardRatio(entryPrice, stopLoss, takeProfit) {
    const risk = Math.abs(entryPrice - stopLoss);
    const reward = Math.abs(takeProfit - entryPrice);
    return reward / risk;
  }

  /**
   * Check if trading should be halted due to risk limits
   * @param {number} balance - Current balance
   * @param {number} initialBalance - Initial balance
   * @returns {Object} Trading halt status
   */
  checkTradingHalt(balance, initialBalance) {
    const totalLossPercentage = ((initialBalance - balance) / initialBalance) * 100;
    const maxLossPercentage = 20; // Halt trading if 20% loss
    
    const shouldHalt = totalLossPercentage >= maxLossPercentage || this.maxDrawdown >= 15;
    
    return {
      shouldHalt,
      reason: shouldHalt 
        ? totalLossPercentage >= maxLossPercentage 
          ? `Total loss ${totalLossPercentage.toFixed(2)}% exceeds maximum`
          : `Max drawdown ${this.maxDrawdown.toFixed(2)}% exceeds limit`
        : null,
      totalLossPercentage,
      maxDrawdown: this.maxDrawdown
    };
  }
}

module.exports = RiskManager;