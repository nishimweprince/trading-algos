'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');

const BinanceDataFeed = require('../src/market/BinanceDataFeed');

const MINUTE = 60000;

function fakeClient() {
  const calls = [];
  return {
    market: 'crypto',
    calls,
    async instruments() {
      calls.push(['instruments']);
      return [{
        symbol: 'BTCUSDT', source_instrument: 'BTCUSDT', provider: 'binance', digits: 2,
        description: 'BTC/USDT', price_increment: 0.01, quantity_increment: 0.00001,
        min_quantity: 0.00001, max_quantity: 9000,
      }];
    },
    async quote(symbol) {
      calls.push(['quote', symbol]);
      return { symbol, price: 100.5, bid: 100, ask: 101, ts: '2026-10-01T10:00:00Z' };
    },
    async candles(symbol, timeframe, count, options) {
      calls.push(['candles', symbol, timeframe, count, options]);
      return [1, 2].map((i) => ({
        ts: new Date(Date.parse('2026-10-01T10:00:00Z') + i * 60 * MINUTE).toISOString(),
        open: i, high: i + 1, low: i - 1, close: i + 0.5, volume: 10,
      }));
    },
    async candlesRange(symbol, timeframe, from, to) {
      calls.push(['candlesRange', symbol, timeframe, from, to]);
      return [1, 2, 3].map((i) => ({
        ts: new Date(from.getTime() + i * 60 * MINUTE).toISOString(),
        open: i, high: i, low: i, close: i, volume: i,
      }));
    },
    stream(symbols, onEvent) {
      calls.push(['stream', symbols]);
      onEvent({ event: 'status', data: { state: 'connected', dropped: 0 } });
      onEvent({ event: 'tick', data: { symbol: 'BTCUSDT', price: 100.5, bid: 100, ask: 101, ts: '2026-10-01T10:00:00Z' } });
      let closed = false;
      return { close: () => { closed = true; }, done: Promise.resolve(), get closed() { return closed; } };
    },
  };
}

test('requires a market-data key unless a client is injected', () => {
  delete process.env.MARKET_DATA_API_KEY;
  assert.throws(() => new BinanceDataFeed(), /MARKET_DATA_API_KEY/);
});

test('klines map closed candles to open/close times in the strategy shape', async () => {
  const client = fakeClient();
  const feed = new BinanceDataFeed({ client });

  const klines = await feed.getKlines('btcusdt', '1h', 2);

  assert.deepEqual(client.calls[0].slice(0, 4), ['candles', 'BTCUSDT', 'H1', 2]);
  const end = Date.parse('2026-10-01T11:00:00Z');
  assert.equal(klines[0].openTime, end - 60 * MINUTE);
  assert.equal(klines[0].closeTime, end - 1);
  assert.equal(klines[0].close, 1.5);
  assert.equal(klines[0].quoteVolume, null);
});

test('a start time asks for a range and keeps the first `limit` bars', async () => {
  const client = fakeClient();
  const feed = new BinanceDataFeed({ client });
  const start = Date.parse('2026-09-01T00:00:00Z');

  const klines = await feed.getKlines('BTCUSDT', '1h', 2, start, start + 10 * 60 * MINUTE);

  assert.equal(client.calls[0][0], 'candlesRange');
  assert.equal(klines.length, 2);
});

test('unsupported intervals fail clearly', async () => {
  const feed = new BinanceDataFeed({ client: fakeClient() });
  await assert.rejects(feed.getKlines('BTCUSDT', '2h', 5), /Unsupported interval 2h/);
});

test('symbol info keeps the Binance filter shape the strategy reads', async () => {
  const client = fakeClient();
  const feed = new BinanceDataFeed({ client });

  const info = await feed.getSymbolInfo('BTCUSDT');
  await feed.getSymbolInfo('BTCUSDT');

  assert.equal(info.filters.find((f) => f.filterType === 'LOT_SIZE').minQty, '0.00001');
  assert.equal(info.filters.find((f) => f.filterType === 'PRICE_FILTER').tickSize, '0.01');
  assert.equal(info.baseAsset, 'BTC');
  assert.equal(client.calls.filter((c) => c[0] === 'instruments').length, 1, 'cached');
  await assert.rejects(feed.getSymbolInfo('DOGEUSDT'), /not served/);
});

test('current price is the book mid and streams push ticks', async () => {
  const feed = new BinanceDataFeed({ client: fakeClient() });
  assert.equal(await feed.getCurrentPrice('btcusdt'), 100.5);

  const prices = [];
  const stop = feed.streamPrices('btcusdt', (tick) => prices.push(tick));
  stop();

  assert.deepEqual(prices, [{
    symbol: 'BTCUSDT', price: 100.5, bid: 100, ask: 101, timestamp: Date.parse('2026-10-01T10:00:00Z'),
  }]);
});
