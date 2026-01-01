/**
 * Technical Indicators for Tinga Tinga trading strategy
 * Implements RSI and other technical analysis calculations
 */

class TechnicalIndicators {
  /**
   * Calculate Simple Moving Average (SMA)
   * @param {number[]} prices - Array of prices
   * @param {number} period - Period for SMA calculation
   * @returns {number[]} Array of SMA values
   */
  static sma(prices, period) {
    if (prices.length < period) {
      return [];
    }

    const smaValues = [];
    for (let i = period - 1; i < prices.length; i++) {
      let sum = 0;
      for (let j = 0; j < period; j++) {
        sum += prices[i - j];
      }
      smaValues.push(sum / period);
    }
    return smaValues;
  }

  /**
   * Calculate Exponential Moving Average (EMA)
   * @param {number[]} prices - Array of prices
   * @param {number} period - Period for EMA calculation
   * @returns {number[]} Array of EMA values
   */
  static ema(prices, period) {
    if (prices.length < period) {
      return [];
    }

    const multiplier = 2 / (period + 1);
    const emaValues = [];
    
    // Start with SMA for the first value
    let sum = 0;
    for (let i = 0; i < period; i++) {
      sum += prices[i];
    }
    emaValues.push(sum / period);

    // Calculate EMA for remaining values
    for (let i = period; i < prices.length; i++) {
      const ema = (prices[i] - emaValues[emaValues.length - 1]) * multiplier + emaValues[emaValues.length - 1];
      emaValues.push(ema);
    }

    return emaValues;
  }

  /**
   * Calculate Relative Strength Index (RSI)
   * @param {number[]} prices - Array of closing prices
   * @param {number} period - RSI period (typically 14)
   * @returns {number[]} Array of RSI values
   */
  static rsi(prices, period = 14) {
    if (prices.length < period + 1) {
      return [];
    }

    const rsiValues = [];
    const gains = [];
    const losses = [];

    // Calculate price changes
    for (let i = 1; i < prices.length; i++) {
      const change = prices[i] - prices[i - 1];
      gains.push(change > 0 ? change : 0);
      losses.push(change < 0 ? Math.abs(change) : 0);
    }

    // Calculate initial average gain/loss
    let avgGain = 0;
    let avgLoss = 0;
    for (let i = 0; i < period; i++) {
      avgGain += gains[i];
      avgLoss += losses[i];
    }
    avgGain /= period;
    avgLoss /= period;

    // Calculate RSI values
    for (let i = period; i < gains.length + 1; i++) {
      if (avgLoss === 0) {
        rsiValues.push(100);
      } else {
        const rs = avgGain / avgLoss;
        const rsi = 100 - (100 / (1 + rs));
        rsiValues.push(rsi);
      }

      if (i < gains.length) {
        // Smooth the averages using Wilder's method
        avgGain = ((avgGain * (period - 1)) + gains[i]) / period;
        avgLoss = ((avgLoss * (period - 1)) + losses[i]) / period;
      }
    }

    return rsiValues;
  }

  /**
   * Detect RSI crossover
   * @param {number} previousRSI - Previous RSI value
   * @param {number} currentRSI - Current RSI value
   * @param {number} threshold - Crossover threshold (default 50)
   * @returns {string|null} 'BUY', 'SELL', or null
   */
  static detectRSICrossover(previousRSI, currentRSI, threshold = 50) {
    if (previousRSI === undefined || currentRSI === undefined) {
      return null;
    }

    // Buy signal: RSI crosses above threshold
    if (previousRSI <= threshold && currentRSI > threshold) {
      return 'BUY';
    }

    // Sell signal: RSI crosses below threshold
    if (previousRSI >= threshold && currentRSI < threshold) {
      return 'SELL';
    }

    return null;
  }

  /**
   * Calculate Average True Range (ATR)
   * @param {Object[]} candles - Array of candle objects with high, low, close
   * @param {number} period - ATR period
   * @returns {number[]} Array of ATR values
   */
  static atr(candles, period = 14) {
    if (candles.length < period + 1) {
      return [];
    }

    const trueRanges = [];
    
    // Calculate True Range for each candle
    for (let i = 1; i < candles.length; i++) {
      const high = candles[i].high;
      const low = candles[i].low;
      const prevClose = candles[i - 1].close;
      
      const tr = Math.max(
        high - low,
        Math.abs(high - prevClose),
        Math.abs(low - prevClose)
      );
      
      trueRanges.push(tr);
    }

    // Calculate ATR using Wilder's smoothing
    const atrValues = [];
    let atr = 0;

    // Initial ATR is simple average
    for (let i = 0; i < period; i++) {
      atr += trueRanges[i];
    }
    atr /= period;
    atrValues.push(atr);

    // Subsequent ATR values use Wilder's smoothing
    for (let i = period; i < trueRanges.length; i++) {
      atr = ((atr * (period - 1)) + trueRanges[i]) / period;
      atrValues.push(atr);
    }

    return atrValues;
  }

  /**
   * Calculate Bollinger Bands
   * @param {number[]} prices - Array of prices
   * @param {number} period - Period for moving average
   * @param {number} stdDev - Number of standard deviations
   * @returns {Object} Object with upper, middle, and lower bands
   */
  static bollingerBands(prices, period = 20, stdDev = 2) {
    const sma = this.sma(prices, period);
    const bands = {
      upper: [],
      middle: sma,
      lower: []
    };

    for (let i = period - 1; i < prices.length; i++) {
      // Calculate standard deviation
      let sum = 0;
      for (let j = 0; j < period; j++) {
        sum += Math.pow(prices[i - j] - sma[i - period + 1], 2);
      }
      const std = Math.sqrt(sum / period);

      bands.upper.push(sma[i - period + 1] + (stdDev * std));
      bands.lower.push(sma[i - period + 1] - (stdDev * std));
    }

    return bands;
  }

  /**
   * Calculate MACD (Moving Average Convergence Divergence)
   * @param {number[]} prices - Array of closing prices
   * @param {number} fastPeriod - Fast EMA period
   * @param {number} slowPeriod - Slow EMA period
   * @param {number} signalPeriod - Signal line EMA period
   * @returns {Object} Object with MACD line, signal line, and histogram
   */
  static macd(prices, fastPeriod = 12, slowPeriod = 26, signalPeriod = 9) {
    const fastEMA = this.ema(prices, fastPeriod);
    const slowEMA = this.ema(prices, slowPeriod);
    
    // Calculate MACD line
    const macdLine = [];
    const startIdx = slowPeriod - 1;
    for (let i = 0; i < fastEMA.length && i + startIdx - fastPeriod + 1 < slowEMA.length; i++) {
      macdLine.push(fastEMA[i + startIdx - fastPeriod + 1] - slowEMA[i]);
    }

    // Calculate signal line
    const signalLine = this.ema(macdLine, signalPeriod);

    // Calculate histogram
    const histogram = [];
    for (let i = 0; i < signalLine.length; i++) {
      histogram.push(macdLine[i + signalPeriod - 1] - signalLine[i]);
    }

    return {
      macd: macdLine,
      signal: signalLine,
      histogram: histogram
    };
  }

  /**
   * Detect trend direction using multiple indicators
   * @param {Object[]} candles - Recent candles
   * @param {Object} indicators - Calculated indicators
   * @returns {string} 'BULLISH', 'BEARISH', or 'NEUTRAL'
   */
  static detectTrend(candles, indicators) {
    let bullishSignals = 0;
    let bearishSignals = 0;

    // RSI trend
    if (indicators.rsi && indicators.rsi.length > 0) {
      const currentRSI = indicators.rsi[indicators.rsi.length - 1];
      if (currentRSI > 50) bullishSignals++;
      else if (currentRSI < 50) bearishSignals++;
    }

    // Price vs moving average
    if (indicators.sma && indicators.sma.length > 0 && candles.length > 0) {
      const currentPrice = candles[candles.length - 1].close;
      const currentSMA = indicators.sma[indicators.sma.length - 1];
      if (currentPrice > currentSMA) bullishSignals++;
      else if (currentPrice < currentSMA) bearishSignals++;
    }

    // MACD trend
    if (indicators.macd && indicators.macd.histogram && indicators.macd.histogram.length > 0) {
      const currentHistogram = indicators.macd.histogram[indicators.macd.histogram.length - 1];
      if (currentHistogram > 0) bullishSignals++;
      else if (currentHistogram < 0) bearishSignals++;
    }

    if (bullishSignals > bearishSignals) return 'BULLISH';
    if (bearishSignals > bullishSignals) return 'BEARISH';
    return 'NEUTRAL';
  }
}

module.exports = TechnicalIndicators;