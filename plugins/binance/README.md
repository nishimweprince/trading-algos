# ta-plugin-binance

Binance Spot market data for market-data-service's `crypto` market, from the
public API only (no key, no account):

- **Instruments** from `GET /api/v3/exchangeInfo`: tick size, step size, and
  quantity limits from the `PRICE_FILTER` and `LOT_SIZE` filters.
- **Candles** from `GET /api/v3/klines`. Closed bars only, stamped at the UTC
  end of their interval like every other provider; requests over 1000 bars
  page backwards.
- **Quotes** from the combined `<symbol>@bookTicker` WebSocket stream, with
  `GET /api/v3/ticker/bookTicker` to seed the cache at start and after every
  reconnect.

Request weight is budgeted client-side (`BINANCE_REQUEST_WEIGHT_PER_MINUTE`,
below Binance's 6000) and synchronised from `X-MBX-USED-WEIGHT-1M`. A 429 or
418 stops REST calls until `Retry-After` and surfaces as 503.

Configure with `BINANCE_SYMBOLS=BTCUSDT,ETHUSDT`; symbols are Binance's own
names and are also the canonical names. Published as `binance` in the
`ta.market_data` entry-point group only.
