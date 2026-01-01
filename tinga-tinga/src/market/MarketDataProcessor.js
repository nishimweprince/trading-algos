/**
 * Market Data Processor for Tinga Tinga trading strategy
 * Processes and analyzes market data for strategy signals
 */

const TechnicalIndicators = require('../strategy/TechnicalIndicators');
const logger = require('../utils/Logger');

class MarketDataProcessor {
  constructor() {
    this.dataCache = new Map();
    this.indicatorCache = new Map();
    this.lastUpdateTime = new Map();
  }

  /**
   * Process raw kline data and calculate indicators
   * @param {string} symbol - Trading symbol
   * @param {Array} klines - Raw kline data
   * @param {Object} indicatorConfig - Indicator configuration
   * @returns {Object} Processed data with indicators
   */
  processKlines(symbol, klines, indicatorConfig = {}) {
    if (!klines || klines.length === 0) {
      logger.warn('No kline data to process', { symbol });
      return null;
    }

    // Extract price arrays
    const closes = klines.map(k => k.close);
    const highs = klines.map(k => k.high);
    const lows = klines.map(k => k.low);
    const opens = klines.map(k => k.open);
    const volumes = klines.map(k => k.volume);

    // Calculate indicators
    const indicators = {};

    // RSI calculation
    if (indicatorConfig.rsi !== false) {
      const rsiPeriod = indicatorConfig.rsiPeriod || 14;
      indicators.rsi = TechnicalIndicators.rsi(closes, rsiPeriod);
    }

    // Moving averages
    if (indicatorConfig.sma !== false) {
      const smaPeriods = indicatorConfig.smaPeriods || [20, 50, 200];
      indicators.sma = {};
      smaPeriods.forEach(period => {
        if (closes.length >= period) {
          indicators.sma[`sma${period}`] = TechnicalIndicators.sma(closes, period);
        }
      });
    }

    if (indicatorConfig.ema !== false) {
      const emaPeriods = indicatorConfig.emaPeriods || [12, 26];
      indicators.ema = {};
      emaPeriods.forEach(period => {
        if (closes.length >= period) {
          indicators.ema[`ema${period}`] = TechnicalIndicators.ema(closes, period);
        }
      });
    }

    // Bollinger Bands
    if (indicatorConfig.bollingerBands !== false) {
      const bbPeriod = indicatorConfig.bbPeriod || 20;
      const bbStdDev = indicatorConfig.bbStdDev || 2;
      if (closes.length >= bbPeriod) {
        indicators.bollingerBands = TechnicalIndicators.bollingerBands(closes, bbPeriod, bbStdDev);
      }
    }

    // MACD
    if (indicatorConfig.macd !== false) {
      const macdConfig = indicatorConfig.macdConfig || { fast: 12, slow: 26, signal: 9 };
      if (closes.length >= macdConfig.slow) {
        indicators.macd = TechnicalIndicators.macd(
          closes, 
          macdConfig.fast, 
          macdConfig.slow, 
          macdConfig.signal
        );
      }
    }

    // ATR
    if (indicatorConfig.atr !== false) {
      const atrPeriod = indicatorConfig.atrPeriod || 14;
      if (klines.length >= atrPeriod + 1) {
        indicators.atr = TechnicalIndicators.atr(klines, atrPeriod);
      }
    }

    // Volume analysis
    const volumeAnalysis = this.analyzeVolume(volumes);

    // Price action analysis
    const priceAction = this.analyzePriceAction(klines);

    // Trend detection
    const trend = TechnicalIndicators.detectTrend(klines, indicators);

    // Cache the processed data
    const processedData = {
      symbol,
      timestamp: Date.now(),
      klines,
      closes,
      highs,
      lows,
      opens,
      volumes,
      indicators,
      volumeAnalysis,
      priceAction,
      trend,
      latestCandle: klines[klines.length - 1],
      previousCandle: klines[klines.length - 2]
    };

    this.dataCache.set(symbol, processedData);
    this.lastUpdateTime.set(symbol, Date.now());

    logger.debug('Market data processed', {
      symbol,
      candleCount: klines.length,
      trend,
      latestPrice: processedData.latestCandle.close
    });

    return processedData;
  }

  /**
   * Analyze volume patterns
   * @param {number[]} volumes - Volume array
   * @returns {Object} Volume analysis
   */
  analyzeVolume(volumes) {
    if (!volumes || volumes.length === 0) {
      return null;
    }

    const recentVolumes = volumes.slice(-20);
    const avgVolume = recentVolumes.reduce((a, b) => a + b, 0) / recentVolumes.length;
    const latestVolume = volumes[volumes.length - 1];
    const volumeRatio = latestVolume / avgVolume;

    // Detect volume spikes
    const volumeSpike = volumeRatio > 2;
    const lowVolume = volumeRatio < 0.5;

    // Volume trend
    const volumeTrend = this.calculateTrend(recentVolumes);

    return {
      avgVolume,
      latestVolume,
      volumeRatio,
      volumeSpike,
      lowVolume,
      volumeTrend,
      interpretation: volumeSpike ? 'High volume - potential breakout' :
                     lowVolume ? 'Low volume - lack of interest' :
                     'Normal volume'
    };
  }

  /**
   * Analyze price action patterns
   * @param {Array} klines - Kline data
   * @returns {Object} Price action analysis
   */
  analyzePriceAction(klines) {
    if (!klines || klines.length < 3) {
      return null;
    }

    const latest = klines[klines.length - 1];
    const previous = klines[klines.length - 2];
    
    // Candlestick patterns
    const patterns = [];

    // Doji detection
    const bodySize = Math.abs(latest.close - latest.open);
    const candleRange = latest.high - latest.low;
    if (bodySize / candleRange < 0.1) {
      patterns.push('doji');
    }

    // Hammer/Shooting star
    const upperShadow = latest.high - Math.max(latest.open, latest.close);
    const lowerShadow = Math.min(latest.open, latest.close) - latest.low;
    
    if (lowerShadow > bodySize * 2 && upperShadow < bodySize * 0.5) {
      patterns.push('hammer');
    }
    if (upperShadow > bodySize * 2 && lowerShadow < bodySize * 0.5) {
      patterns.push('shooting_star');
    }

    // Engulfing patterns
    if (previous && latest) {
      const prevBody = Math.abs(previous.close - previous.open);
      const currBody = Math.abs(latest.close - latest.open);
      
      if (previous.close < previous.open && latest.close > latest.open &&
          latest.open <= previous.close && latest.close >= previous.open &&
          currBody > prevBody) {
        patterns.push('bullish_engulfing');
      }
      
      if (previous.close > previous.open && latest.close < latest.open &&
          latest.open >= previous.close && latest.close <= previous.open &&
          currBody > prevBody) {
        patterns.push('bearish_engulfing');
      }
    }

    // Support/Resistance levels
    const recentKlines = klines.slice(-50);
    const supportResistance = this.findSupportResistance(recentKlines);

    // Price momentum
    const momentum = this.calculateMomentum(klines.slice(-10).map(k => k.close));

    return {
      patterns,
      supportLevels: supportResistance.support,
      resistanceLevels: supportResistance.resistance,
      momentum,
      currentPrice: latest.close,
      priceChange: latest.close - previous.close,
      priceChangePercent: ((latest.close - previous.close) / previous.close) * 100
    };
  }

  /**
   * Find support and resistance levels
   * @param {Array} klines - Recent klines
   * @returns {Object} Support and resistance levels
   */
  findSupportResistance(klines) {
    const highs = klines.map(k => k.high);
    const lows = klines.map(k => k.low);
    
    // Simple approach: find local maxima/minima
    const resistance = [];
    const support = [];
    
    for (let i = 2; i < highs.length - 2; i++) {
      // Resistance: local high
      if (highs[i] > highs[i-1] && highs[i] > highs[i-2] &&
          highs[i] > highs[i+1] && highs[i] > highs[i+2]) {
        resistance.push(highs[i]);
      }
      
      // Support: local low
      if (lows[i] < lows[i-1] && lows[i] < lows[i-2] &&
          lows[i] < lows[i+1] && lows[i] < lows[i+2]) {
        support.push(lows[i]);
      }
    }
    
    // Sort and remove duplicates
    const uniqueResistance = [...new Set(resistance)].sort((a, b) => b - a).slice(0, 3);
    const uniqueSupport = [...new Set(support)].sort((a, b) => a - b).slice(0, 3);
    
    return {
      resistance: uniqueResistance,
      support: uniqueSupport
    };
  }

  /**
   * Calculate price momentum
   * @param {number[]} prices - Recent prices
   * @returns {string} Momentum direction
   */
  calculateMomentum(prices) {
    if (prices.length < 2) return 'neutral';
    
    let upMoves = 0;
    let downMoves = 0;
    
    for (let i = 1; i < prices.length; i++) {
      if (prices[i] > prices[i-1]) upMoves++;
      else if (prices[i] < prices[i-1]) downMoves++;
    }
    
    if (upMoves > downMoves * 1.5) return 'strong_bullish';
    if (upMoves > downMoves) return 'bullish';
    if (downMoves > upMoves * 1.5) return 'strong_bearish';
    if (downMoves > upMoves) return 'bearish';
    return 'neutral';
  }

  /**
   * Calculate trend direction
   * @param {number[]} values - Price or indicator values
   * @returns {string} Trend direction
   */
  calculateTrend(values) {
    if (values.length < 3) return 'neutral';
    
    const firstThird = values.slice(0, Math.floor(values.length / 3));
    const lastThird = values.slice(-Math.floor(values.length / 3));
    
    const firstAvg = firstThird.reduce((a, b) => a + b, 0) / firstThird.length;
    const lastAvg = lastThird.reduce((a, b) => a + b, 0) / lastThird.length;
    
    const changePercent = ((lastAvg - firstAvg) / firstAvg) * 100;
    
    if (changePercent > 5) return 'strong_up';
    if (changePercent > 1) return 'up';
    if (changePercent < -5) return 'strong_down';
    if (changePercent < -1) return 'down';
    return 'sideways';
  }

  /**
   * Get trading signals based on processed data
   * @param {Object} processedData - Processed market data
   * @param {Object} strategyConfig - Strategy configuration
   * @returns {Object} Trading signals
   */
  getSignals(processedData, strategyConfig) {
    if (!processedData || !processedData.indicators.rsi) {
      return { signal: 'NONE', strength: 0, reasons: ['Insufficient data'] };
    }

    const signals = [];
    const reasons = [];
    
    // RSI signals
    const currentRSI = processedData.indicators.rsi[processedData.indicators.rsi.length - 1];
    const previousRSI = processedData.indicators.rsi[processedData.indicators.rsi.length - 2];
    
    if (currentRSI && previousRSI) {
      const rsiCrossover = TechnicalIndicators.detectRSICrossover(
        previousRSI, 
        currentRSI, 
        strategyConfig.rsiBuyThreshold || 50
      );
      
      if (rsiCrossover === 'BUY') {
        signals.push(2);
        reasons.push(`RSI crossed above ${strategyConfig.rsiBuyThreshold || 50}`);
      } else if (rsiCrossover === 'SELL') {
        signals.push(-2);
        reasons.push(`RSI crossed below ${strategyConfig.rsiSellThreshold || 50}`);
      }
    }

    // Trend alignment
    if (processedData.trend === 'BULLISH') {
      signals.push(1);
      reasons.push('Bullish trend detected');
    } else if (processedData.trend === 'BEARISH') {
      signals.push(-1);
      reasons.push('Bearish trend detected');
    }

    // Volume confirmation
    if (processedData.volumeAnalysis) {
      if (processedData.volumeAnalysis.volumeSpike) {
        signals.push(processedData.priceAction.priceChange > 0 ? 1 : -1);
        reasons.push('Volume spike detected');
      }
    }

    // Price action patterns
    if (processedData.priceAction && processedData.priceAction.patterns.length > 0) {
      const patterns = processedData.priceAction.patterns;
      if (patterns.includes('bullish_engulfing') || patterns.includes('hammer')) {
        signals.push(1);
        reasons.push(`Bullish pattern: ${patterns.join(', ')}`);
      }
      if (patterns.includes('bearish_engulfing') || patterns.includes('shooting_star')) {
        signals.push(-1);
        reasons.push(`Bearish pattern: ${patterns.join(', ')}`);
      }
    }

    // Calculate signal strength
    const totalSignal = signals.reduce((a, b) => a + b, 0);
    const signalStrength = Math.min(Math.abs(totalSignal) / signals.length, 1);

    return {
      signal: totalSignal > 1 ? 'BUY' : totalSignal < -1 ? 'SELL' : 'NONE',
      strength: signalStrength,
      totalSignal,
      reasons,
      rsi: currentRSI,
      price: processedData.latestCandle.close
    };
  }

  /**
   * Get cached data for a symbol
   * @param {string} symbol - Trading symbol
   * @returns {Object|null} Cached processed data
   */
  getCachedData(symbol) {
    return this.dataCache.get(symbol);
  }

  /**
   * Clear cache for a symbol or all symbols
   * @param {string} symbol - Trading symbol (optional)
   */
  clearCache(symbol = null) {
    if (symbol) {
      this.dataCache.delete(symbol);
      this.indicatorCache.delete(symbol);
      this.lastUpdateTime.delete(symbol);
    } else {
      this.dataCache.clear();
      this.indicatorCache.clear();
      this.lastUpdateTime.clear();
    }
    
    logger.debug('Cache cleared', { symbol: symbol || 'all' });
  }
}

module.exports = MarketDataProcessor;