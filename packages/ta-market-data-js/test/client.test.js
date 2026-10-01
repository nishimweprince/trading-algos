'use strict';

// Runs against the compiled dist/ (npm test builds first), over a real local
// HTTP server that answers the way market-data-service does.
const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');

const { MarketDataClient, MarketDataError } = require('../dist');

const API_KEY = 'test-api-key-at-least-16';
const MINUTE = 60_000;
const NOW = Date.parse('2026-10-01T10:07:00Z');
const requests = [];
let server;
let baseUrl;
let streamResponse;

function candle(endMs) {
  return {
    ts: new Date(endMs).toISOString().replace('.000Z', 'Z'),
    open: 1, high: 2, low: 0.5, close: 1.5, volume: 3,
    provider: 'binance', source_instrument: 'BTCUSDT', spread: null, spread_source: 'unavailable',
  };
}

before(async () => {
  server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    requests.push(url);
    const send = (status, body) => {
      res.writeHead(status, { 'content-type': 'application/json' });
      res.end(JSON.stringify(body));
    };
    if (url.pathname === '/health/ready') return send(503, { status: 'not_ready' });
    if (req.headers['x-api-key'] !== API_KEY) {
      return send(401, { error: { code: 'unauthorized', message: 'Invalid or missing API key' } });
    }
    const route = url.pathname.replace('/v1/crypto/', '');
    if (route === 'tick') {
      if (url.searchParams.get('symbol') !== 'BTCUSDT') {
        return send(422, { error: { code: 'symbol_not_allowed', message: 'no', details: { configured: ['BTCUSDT'] } } });
      }
      return send(200, {
        symbol: 'BTCUSDT', source_instrument: 'BTCUSDT', provider: 'binance',
        ts: '2026-10-01T10:06:59Z', price: 100.5, bid: 100, ask: 101, spread: 1,
      });
    }
    if (route === 'candles') {
      // Inclusive `to`, at most 500 per page, like the service with a small cap.
      const count = Math.min(Number(url.searchParams.get('count')), 500);
      const to = url.searchParams.has('to') ? Date.parse(url.searchParams.get('to')) : NOW;
      const newest = Math.floor(to / MINUTE) * MINUTE;
      const rows = [];
      for (let i = count - 1; i >= 0; i -= 1) rows.push(candle(newest - i * MINUTE));
      return send(200, { symbol: 'BTCUSDT', timeframe: 'M1', candles: rows });
    }
    if (route === 'symbols') {
      return send(200, { market: 'crypto', provider: 'binance', instruments: [{ symbol: 'BTCUSDT', digits: 2 }] });
    }
    if (route === 'capabilities') {
      return send(200, { market: 'crypto', provider: 'binance', timeframes: ['M1'], streaming: true, bid_ask: true, max_candles: 500 });
    }
    if (route === 'stream/ticks') {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write('event: status\r\ndata: {"state":"connected","dropped":0}\r\n\r\n');
      res.write(': ping\n\n');
      res.write('event: tick\ndata: {"symbol":"BTCUSDT","source_instrument":"BTCUSDT",');
      res.write('"provider":"binance","ts":"2026-10-01T10:07:00Z","price":100.5,"bid":100,"ask":101,"spread":1}\n\n');
      streamResponse = res;
      return undefined;
    }
    return send(404, { error: { code: 'not_found', message: route } });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  baseUrl = `http://127.0.0.1:${server.address().port}/`;
});

after(() => new Promise((resolve) => server.close(resolve)));

const client = () => new MarketDataClient({ baseUrl, apiKey: API_KEY, market: 'crypto' });

test('quote, instruments and capabilities hit the market routes with the key', async () => {
  const quote = await client().quote('BTCUSDT');
  assert.equal(quote.bid, 100);
  assert.equal((await client().instruments())[0].symbol, 'BTCUSDT');
  assert.equal((await client().capabilities()).max_candles, 500);
  assert.equal(requests.at(-1).pathname, '/v1/crypto/capabilities');
});

test('provider is passed through when pinned', async () => {
  const pinned = new MarketDataClient({ baseUrl, apiKey: API_KEY, market: 'crypto', provider: 'binance' });
  await pinned.quote('BTCUSDT');
  assert.equal(requests.at(-1).searchParams.get('provider'), 'binance');
});

test('errors carry the structured code', async () => {
  await assert.rejects(client().quote('ETHUSDT'), (error) => {
    assert.ok(error instanceof MarketDataError);
    assert.equal(error.status, 422);
    assert.equal(error.code, 'symbol_not_allowed');
    assert.deepEqual(error.details, { configured: ['BTCUSDT'] });
    return true;
  });
  const anonymous = new MarketDataClient({ baseUrl, apiKey: 'wrong', market: 'crypto' });
  await assert.rejects(anonymous.quote('BTCUSDT'), { code: 'unauthorized', status: 401 });
});

test('candles page backwards across the inclusive cursor without losing a bar', async () => {
  const pages = new MarketDataClient({ baseUrl, apiKey: API_KEY, market: 'crypto', pageSize: 500 });
  const candles = await pages.candles('BTCUSDT', 'M1', 1200);
  assert.equal(candles.length, 1200);
  const stamps = candles.map((c) => Date.parse(c.ts));
  for (let i = 1; i < stamps.length; i += 1) assert.equal(stamps[i] - stamps[i - 1], MINUTE);
  assert.equal(stamps.at(-1), NOW);
});

test('candlesRange filters to the closed interval', async () => {
  const from = new Date(NOW - 10 * MINUTE);
  const to = new Date(NOW - 5 * MINUTE);
  const candles = await client().candlesRange('BTCUSDT', 'M1', from, to);
  assert.deepEqual(candles.map((c) => Date.parse(c.ts)), [0, 1, 2, 3, 4, 5].map((i) => from.getTime() + i * MINUTE));
});

test('ready reports the service health without the key', async () => {
  assert.deepEqual(await client().ready(), { ready: false, reason: 'status 503' });
});

test('stream parses status and tick events and closes cleanly', async () => {
  const events = [];
  const subscription = client().stream(['BTCUSDT'], (event) => {
    events.push(event);
    if (events.length === 2) subscription.close();
  });
  await subscription.done;
  streamResponse?.end();
  assert.deepEqual(events.map((e) => e.event), ['status', 'tick']);
  assert.equal(events[0].data.state, 'connected');
  assert.equal(events[1].data.ask, 101);
  assert.equal(requests.at(-1).searchParams.get('symbols'), 'BTCUSDT');
});
