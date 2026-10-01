/**
 * Crypto market data for the Tinga Tinga strategy, from market-data-service.
 *
 * Keeps the public methods the strategy uses (getSymbolInfo, getKlines,
 * getCurrentPrice, streamPrices) but no longer calls Binance directly: the
 * service's `crypto` market (ta-plugin-binance) does, with one rate-limit
 * budget and one WebSocket for every consumer. Configure MARKET_DATA_URL and
 * MARKET_DATA_API_KEY.
 *
 * Two behaviours differ from the direct Binance client this replaced:
 * - Klines are CLOSED bars only. The forming bar is no longer the last row, so
 *   indicators settle on the bar close instead of moving within the bar.
 * - getCurrentPrice is the book mid, not the last trade price.
 *
 * Trades, order book, aggregated trades and 24h statistics are not part of the
 * market-data contract; nothing in this project used them, so they are gone.
 */

const { MarketDataClient } = require('@trading-algos/market-data');
const config = require('../utils/Config');
const logger = require('../utils/Logger');

// Binance interval names, as the strategy configures them, to platform timeframes.
const TIMEFRAMES = {
  '1m': 'M1',
  '3m': 'M3',
  '5m': 'M5',
  '15m': 'M15',
  '30m': 'M30',
  '1h': 'H1',
  '4h': 'H4',
  '12h': 'H12',
  '1d': 'D1',
  '1w': 'W1',
};
const MINUTES = { M1: 1, M3: 3, M5: 5, M15: 15, M30: 30, H1: 60, H4: 240, H12: 720, D1: 1440, W1: 10080 };
const SYMBOL_INFO_TTL_MS = 3600000;

class BinanceDataFeed {
  /**
   * @param {object} [options]
   * @param {MarketDataClient} [options.client] - injected client (tests)
   */
  constructor({ client } = {}) {
    const settings = config.marketData;
    if (!client && !settings.apiKey) {
      throw new Error('MARKET_DATA_API_KEY is required: prices come from market-data-service');
    }
    this.client =
      client ||
      new MarketDataClient({
        baseUrl: settings.url,
        apiKey: settings.apiKey,
        market: settings.market,
      });
    this.symbolInfoCache = new Map();
  }

  /**
   * Exchange-style symbol info, shaped like Binance's so callers that read
   * `filters` (LOT_SIZE, PRICE_FILTER) keep working.
   * @param {string} symbol
   */
  async getSymbolInfo(symbol) {
    const cached = this.symbolInfoCache.get(symbol);
    if (cached && Date.now() - cached.at < SYMBOL_INFO_TTL_MS) return cached.info;

    const instruments = await this.client.instruments();
    const instrument = instruments.find((item) => item.symbol === symbol.toUpperCase());
    if (!instrument) {
      throw new Error(`${symbol} is not served by the ${this.client.market} market`);
    }
    const [baseAsset, quoteAsset] = (instrument.description || '').split('/');
    const info = {
      symbol: instrument.symbol,
      status: 'TRADING',
      baseAsset: baseAsset || null,
      quoteAsset: quoteAsset || null,
      filters: [
        {
          filterType: 'PRICE_FILTER',
          tickSize: asString(instrument.price_increment),
        },
        {
          filterType: 'LOT_SIZE',
          minQty: asString(instrument.min_quantity),
          maxQty: asString(instrument.max_quantity),
          stepSize: asString(instrument.quantity_increment),
        },
      ],
    };
    this.symbolInfoCache.set(symbol, { info, at: Date.now() });
    logger.debug('Symbol info fetched', { symbol, minQty: info.filters[1].minQty });
    return info;
  }

  /**
   * Current price: the mid of the best bid and ask.
   * @param {string} symbol
   */
  async getCurrentPrice(symbol) {
    const quote = await this.client.quote(symbol.toUpperCase());
    logger.debug('Current price fetched', { symbol, price: quote.price });
    return quote.price;
  }

  /**
   * Closed candles, oldest first, in the shape the strategy expects.
   * @param {string} symbol
   * @param {string} interval - Binance interval name (1m, 5m, 1h, ...)
   * @param {number} limit - number of candles
   * @param {number|null} startTime - ms; with it, the first `limit` bars from here
   * @param {number|null} endTime - ms
   */
  async getKlines(symbol, interval = '1h', limit = 500, startTime = null, endTime = null) {
    const timeframe = TIMEFRAMES[interval];
    if (!timeframe) {
      throw new Error(`Unsupported interval ${interval}; use one of ${Object.keys(TIMEFRAMES).join(', ')}`);
    }
    const upper = symbol.toUpperCase();
    let candles;
    if (startTime) {
      candles = await this.client.candlesRange(
        upper,
        timeframe,
        new Date(startTime),
        endTime ? new Date(endTime) : undefined,
      );
      candles = candles.slice(0, limit);
    } else {
      candles = await this.client.candles(
        upper,
        timeframe,
        limit,
        endTime ? { to: new Date(endTime) } : {},
      );
    }
    const durationMs = MINUTES[timeframe] * 60000;
    const klines = candles.map((candle) => {
      const end = Date.parse(candle.ts);
      return {
        openTime: end - durationMs,
        open: candle.open,
        high: candle.high,
        low: candle.low,
        close: candle.close,
        volume: candle.volume,
        closeTime: end - 1,
        // Not in the market-data contract.
        quoteVolume: null,
        trades: null,
        takerBuyVolume: null,
        takerBuyQuoteVolume: null,
      };
    });
    logger.debug('Klines fetched', {
      symbol,
      interval,
      count: klines.length,
      latest: klines[klines.length - 1]?.closeTime,
    });
    return klines;
  }

  /**
   * Live prices over the service's SSE stream (no polling).
   * @param {string} symbol
   * @param {Function} callback - receives { symbol, price, bid, ask, timestamp }
   * @returns {Function} stop
   */
  streamPrices(symbol, callback) {
    const upper = symbol.toUpperCase();
    const subscription = this.client.stream([upper], (event) => {
      if (event.event === 'tick') {
        callback({
          symbol: upper,
          price: event.data.price,
          bid: event.data.bid,
          ask: event.data.ask,
          timestamp: Date.parse(event.data.ts),
        });
      } else if (event.data.state !== 'connected') {
        logger.warn('Price stream degraded', { symbol, state: event.data.state, error: event.data.error });
      }
    });
    subscription.done.catch((error) => {
      logger.error('Price stream error', { symbol, error: error.message });
    });
    return () => {
      subscription.close();
      logger.info('Price stream stopped', { symbol });
    };
  }
}

function asString(value) {
  return value === null || value === undefined ? null : String(value);
}

module.exports = BinanceDataFeed;
module.exports.TIMEFRAMES = TIMEFRAMES;
