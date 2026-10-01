# @trading-algos/market-data

TypeScript client for [market-data-service](../../services/market-data-service/README.md),
the one place JavaScript consumers get prices from. It mirrors the Python
`ta_clients.MarketDataClient`: one instance per market, candles are closed bars
stamped at the UTC **end** of their interval, and large candle requests page
backwards on `to`. No runtime dependencies (global `fetch`, Node 18.17+).

```js
const { MarketDataClient } = require('@trading-algos/market-data');

const crypto = new MarketDataClient({
  baseUrl: 'http://127.0.0.1:8020',
  apiKey: process.env.MARKET_DATA_API_KEY,
  market: 'crypto',
});
const quote = await crypto.quote('BTCUSDT');            // { bid, ask, price, ts, ... }
const bars = await crypto.candles('BTCUSDT', 'H1', 50); // closed bars, oldest first
const sub = crypto.stream(['BTCUSDT'], (e) => console.log(e.event, e.data));
sub.close();
```

Errors are `MarketDataError` with the service's `status`, `code` and `details`.

## Consuming it

npm workspaces stay off (see [NODE_WORKSPACES.md](../../NODE_WORKSPACES.md)), so
consumers depend on it by path, `"@trading-algos/market-data":
"file:../packages/ta-market-data-js"`. A `file:` dependency is linked, not
built, so the compiled `dist/` is committed; `npm run check` fails when it is
out of date with `src/`.

```bash
npm install && npm test   # builds, then runs node:test against a local fake service
```
