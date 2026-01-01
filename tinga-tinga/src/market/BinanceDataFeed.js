/**
 * Binance Data Feed for Tinga Tinga trading strategy
 * Handles real-time and historical market data from Binance API
 */

const axios = require('axios');
const config = require('../utils/Config');
const logger = require('../utils/Logger');

class BinanceDataFeed {
  constructor() {
    this.baseUrl = config.binance.baseUrl;
    this.endpoints = config.binance.endpoints;
    this.rateLimits = config.binance.rateLimits;
    
    // Rate limiting tracking
    this.requestCount = 0;
    this.requestTimestamps = [];
    this.weightUsed = 0;
    this.weightResetTime = Date.now() + 60000; // Reset every minute
    
    // Cache for symbol info
    this.symbolInfoCache = new Map();
    this.lastSymbolInfoUpdate = 0;
    
    // Initialize axios instance with defaults
    this.api = axios.create({
      baseURL: this.baseUrl,
      timeout: 10000,
      headers: {
        'Content-Type': 'application/json'
      }
    });
    
    // Add response interceptor for error handling
    this.api.interceptors.response.use(
      response => response,
      error => this.handleApiError(error)
    );
  }

  /**
   * Check and enforce rate limits
   * @param {number} weight - Request weight
   * @returns {Promise<void>}
   */
  async checkRateLimit(weight = 1) {
    // Reset weight counter if minute has passed
    if (Date.now() > this.weightResetTime) {
      this.weightUsed = 0;
      this.weightResetTime = Date.now() + 60000;
    }
    
    // Check if adding this request would exceed limit
    if (this.weightUsed + weight > this.rateLimits.weight) {
      const waitTime = this.weightResetTime - Date.now();
      logger.warn(`Rate limit approaching, waiting ${waitTime}ms`);
      await new Promise(resolve => setTimeout(resolve, waitTime));
      this.weightUsed = 0;
      this.weightResetTime = Date.now() + 60000;
    }
    
    this.weightUsed += weight;
  }

  /**
   * Handle API errors
   * @param {Error} error - Axios error
   */
  handleApiError(error) {
    if (error.response) {
      const { status, data } = error.response;
      
      if (status === 429) {
        logger.error('Rate limit exceeded', { 
          retryAfter: error.response.headers['retry-after'] 
        });
      } else if (status === 418) {
        logger.error('IP banned', { until: data.msg });
      } else {
        logger.error('API error', { 
          status, 
          message: data.msg || data.error || 'Unknown error' 
        });
      }
    } else if (error.request) {
      logger.error('Network error', { message: error.message });
    } else {
      logger.error('Request setup error', { message: error.message });
    }
    
    throw error;
  }

  /**
   * Get exchange info for a symbol
   * @param {string} symbol - Trading pair symbol
   * @returns {Promise<Object>} Symbol information
   */
  async getSymbolInfo(symbol) {
    // Check cache first
    const cached = this.symbolInfoCache.get(symbol);
    const cacheAge = Date.now() - this.lastSymbolInfoUpdate;
    
    if (cached && cacheAge < 3600000) { // Cache for 1 hour
      return cached;
    }
    
    try {
      await this.checkRateLimit(10);
      
      const response = await this.api.get(this.endpoints.exchangeInfo, {
        params: { symbol }
      });
      
      const symbolInfo = response.data.symbols[0];
      this.symbolInfoCache.set(symbol, symbolInfo);
      this.lastSymbolInfoUpdate = Date.now();
      
      logger.debug('Symbol info fetched', { 
        symbol, 
        minQty: symbolInfo.filters.find(f => f.filterType === 'LOT_SIZE')?.minQty 
      });
      
      return symbolInfo;
    } catch (error) {
      logger.error('Failed to fetch symbol info', { symbol });
      throw error;
    }
  }

  /**
   * Get current price for a symbol
   * @param {string} symbol - Trading pair symbol
   * @returns {Promise<number>} Current price
   */
  async getCurrentPrice(symbol) {
    try {
      await this.checkRateLimit(1);
      
      const response = await this.api.get(this.endpoints.ticker, {
        params: { symbol }
      });
      
      const price = parseFloat(response.data.price);
      
      logger.debug('Current price fetched', { symbol, price });
      
      return price;
    } catch (error) {
      logger.error('Failed to fetch current price', { symbol });
      throw error;
    }
  }

  /**
   * Get historical klines (candlestick) data
   * @param {string} symbol - Trading pair symbol
   * @param {string} interval - Kline interval (1m, 5m, 1h, etc.)
   * @param {number} limit - Number of klines to fetch
   * @param {number} startTime - Start time in milliseconds (optional)
   * @param {number} endTime - End time in milliseconds (optional)
   * @returns {Promise<Array>} Array of kline data
   */
  async getKlines(symbol, interval = '1h', limit = 500, startTime = null, endTime = null) {
    try {
      await this.checkRateLimit(1);
      
      const params = {
        symbol,
        interval,
        limit: Math.min(limit, 1000) // Binance max is 1000
      };
      
      if (startTime) params.startTime = startTime;
      if (endTime) params.endTime = endTime;
      
      const response = await this.api.get(this.endpoints.klines, { params });
      
      // Parse kline data
      const klines = response.data.map(k => ({
        openTime: k[0],
        open: parseFloat(k[1]),
        high: parseFloat(k[2]),
        low: parseFloat(k[3]),
        close: parseFloat(k[4]),
        volume: parseFloat(k[5]),
        closeTime: k[6],
        quoteVolume: parseFloat(k[7]),
        trades: k[8],
        takerBuyVolume: parseFloat(k[9]),
        takerBuyQuoteVolume: parseFloat(k[10])
      }));
      
      logger.debug('Klines fetched', { 
        symbol, 
        interval, 
        count: klines.length,
        latest: klines[klines.length - 1]?.closeTime 
      });
      
      return klines;
    } catch (error) {
      logger.error('Failed to fetch klines', { symbol, interval });
      throw error;
    }
  }

  /**
   * Get recent trades for a symbol
   * @param {string} symbol - Trading pair symbol
   * @param {number} limit - Number of trades to fetch
   * @returns {Promise<Array>} Array of recent trades
   */
  async getRecentTrades(symbol, limit = 100) {
    try {
      await this.checkRateLimit(1);
      
      const response = await this.api.get('/api/v3/trades', {
        params: { 
          symbol,
          limit: Math.min(limit, 1000)
        }
      });
      
      const trades = response.data.map(t => ({
        id: t.id,
        price: parseFloat(t.price),
        quantity: parseFloat(t.qty),
        time: t.time,
        isBuyerMaker: t.isBuyerMaker
      }));
      
      logger.debug('Recent trades fetched', { 
        symbol, 
        count: trades.length 
      });
      
      return trades;
    } catch (error) {
      logger.error('Failed to fetch recent trades', { symbol });
      throw error;
    }
  }

  /**
   * Get order book depth
   * @param {string} symbol - Trading pair symbol
   * @param {number} limit - Depth limit (5, 10, 20, 50, 100, 500, 1000, 5000)
   * @returns {Promise<Object>} Order book data
   */
  async getOrderBook(symbol, limit = 20) {
    try {
      // Weight varies by limit
      const weight = limit <= 100 ? 1 : limit <= 500 ? 5 : limit <= 1000 ? 10 : 50;
      await this.checkRateLimit(weight);
      
      const response = await this.api.get('/api/v3/depth', {
        params: { symbol, limit }
      });
      
      const orderBook = {
        lastUpdateId: response.data.lastUpdateId,
        bids: response.data.bids.map(b => ({
          price: parseFloat(b[0]),
          quantity: parseFloat(b[1])
        })),
        asks: response.data.asks.map(a => ({
          price: parseFloat(a[0]),
          quantity: parseFloat(a[1])
        }))
      };
      
      logger.debug('Order book fetched', { 
        symbol, 
        bidCount: orderBook.bids.length,
        askCount: orderBook.asks.length 
      });
      
      return orderBook;
    } catch (error) {
      logger.error('Failed to fetch order book', { symbol });
      throw error;
    }
  }

  /**
   * Get 24hr ticker statistics
   * @param {string} symbol - Trading pair symbol
   * @returns {Promise<Object>} 24hr statistics
   */
  async get24hrStats(symbol) {
    try {
      await this.checkRateLimit(1);
      
      const response = await this.api.get('/api/v3/ticker/24hr', {
        params: { symbol }
      });
      
      const stats = {
        symbol: response.data.symbol,
        priceChange: parseFloat(response.data.priceChange),
        priceChangePercent: parseFloat(response.data.priceChangePercent),
        weightedAvgPrice: parseFloat(response.data.weightedAvgPrice),
        prevClosePrice: parseFloat(response.data.prevClosePrice),
        lastPrice: parseFloat(response.data.lastPrice),
        lastQty: parseFloat(response.data.lastQty),
        bidPrice: parseFloat(response.data.bidPrice),
        bidQty: parseFloat(response.data.bidQty),
        askPrice: parseFloat(response.data.askPrice),
        askQty: parseFloat(response.data.askQty),
        openPrice: parseFloat(response.data.openPrice),
        highPrice: parseFloat(response.data.highPrice),
        lowPrice: parseFloat(response.data.lowPrice),
        volume: parseFloat(response.data.volume),
        quoteVolume: parseFloat(response.data.quoteVolume),
        openTime: response.data.openTime,
        closeTime: response.data.closeTime,
        count: response.data.count
      };
      
      logger.debug('24hr stats fetched', { 
        symbol, 
        lastPrice: stats.lastPrice,
        volume: stats.volume 
      });
      
      return stats;
    } catch (error) {
      logger.error('Failed to fetch 24hr stats', { symbol });
      throw error;
    }
  }

  /**
   * Stream real-time price updates (simulated with polling)
   * @param {string} symbol - Trading pair symbol
   * @param {Function} callback - Callback for price updates
   * @param {number} interval - Polling interval in milliseconds
   * @returns {Function} Stop function
   */
  streamPrices(symbol, callback, interval = 1000) {
    let isRunning = true;
    
    const pollPrice = async () => {
      while (isRunning) {
        try {
          const price = await this.getCurrentPrice(symbol);
          callback({ symbol, price, timestamp: Date.now() });
        } catch (error) {
          logger.error('Price stream error', { symbol, error: error.message });
        }
        
        await new Promise(resolve => setTimeout(resolve, interval));
      }
    };
    
    pollPrice();
    
    // Return stop function
    return () => {
      isRunning = false;
      logger.info('Price stream stopped', { symbol });
    };
  }

  /**
   * Get aggregated trade data
   * @param {string} symbol - Trading pair symbol
   * @param {number} fromId - Trade ID to fetch from (optional)
   * @param {number} startTime - Start time in milliseconds (optional)
   * @param {number} endTime - End time in milliseconds (optional)
   * @param {number} limit - Number of trades (max 1000)
   * @returns {Promise<Array>} Aggregated trades
   */
  async getAggTrades(symbol, { fromId, startTime, endTime, limit = 500 } = {}) {
    try {
      await this.checkRateLimit(1);
      
      const params = { symbol, limit };
      if (fromId) params.fromId = fromId;
      if (startTime) params.startTime = startTime;
      if (endTime) params.endTime = endTime;
      
      const response = await this.api.get('/api/v3/aggTrades', { params });
      
      const trades = response.data.map(t => ({
        aggTradeId: t.a,
        price: parseFloat(t.p),
        quantity: parseFloat(t.q),
        firstTradeId: t.f,
        lastTradeId: t.l,
        timestamp: t.T,
        isBuyerMaker: t.m
      }));
      
      logger.debug('Aggregated trades fetched', { 
        symbol, 
        count: trades.length 
      });
      
      return trades;
    } catch (error) {
      logger.error('Failed to fetch aggregated trades', { symbol });
      throw error;
    }
  }

  /**
   * Get mini ticker for all symbols or specific symbol
   * @param {string} symbol - Trading pair symbol (optional)
   * @returns {Promise<Object|Array>} Mini ticker data
   */
  async getMiniTicker(symbol = null) {
    try {
      await this.checkRateLimit(symbol ? 1 : 40);
      
      const endpoint = '/api/v3/ticker/24hr';
      const params = symbol ? { symbol } : {};
      
      const response = await this.api.get(endpoint, { params });
      
      const processTicker = (t) => ({
        symbol: t.symbol,
        lastPrice: parseFloat(t.lastPrice),
        openPrice: parseFloat(t.openPrice),
        highPrice: parseFloat(t.highPrice),
        lowPrice: parseFloat(t.lowPrice),
        volume: parseFloat(t.volume),
        quoteVolume: parseFloat(t.quoteVolume)
      });
      
      const result = Array.isArray(response.data) 
        ? response.data.map(processTicker)
        : processTicker(response.data);
      
      logger.debug('Mini ticker fetched', { 
        symbol: symbol || 'all',
        count: Array.isArray(result) ? result.length : 1
      });
      
      return result;
    } catch (error) {
      logger.error('Failed to fetch mini ticker', { symbol });
      throw error;
    }
  }
}

module.exports = BinanceDataFeed;