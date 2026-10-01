# market-data-service

Provider-neutral market data: quotes, closed candles, instrument lists and tick
streams, per market. Every consumer in the repo reads prices here; none talks to
a broker or exchange for data, and execution-service serves none.

Each market (`forex`, `deriv`, `crypto`) is bound to one provider plugin and one
feed in this process's markets file. The plugins are discovered through the
`ta.market_data` entry points (see `packages/ta-plugin-api`); this service never
imports a broker to choose it.

| Market | Allowed providers | Typical source |
|---|---|---|
| `forex` | `ctrader`, `mt5` | cTrader forex account (macOS), HFM/FTMO MT5 terminals (Windows) |
| `deriv` | `mt5`, `ctrader` | Deriv MT5 terminal (default), or a Deriv cTrader account |
| `crypto` | `binance` | Binance Spot public market data (`plugins/binance`), no key needed |

## Profiles

One process per host for cTrader; one process per MT5 terminal, because the
MetaTrader5 package attaches a process to exactly one terminal.

| Profile | Host | Port | Serves |
|---|---|---|---|
| `ctrader` | macOS | 8020 | `forex` and `deriv` from cTrader accounts; `crypto` from Binance |
| `hfm` | Windows | 8021 | `forex` from the HFM terminal |
| `ftmo` | Windows | 8022 | `forex` from the FTMO terminal |
| `deriv` | Windows | 8023 | `deriv` from the Deriv MT5 terminal |

```bash
cd services/market-data-service
cp .env.example.ctrader .env.ctrader
cp accounts.ctrader.example.toml data/accounts.ctrader.toml
cp markets.ctrader.example.toml data/markets.ctrader.toml
../../.venv/bin/market-data-service --profile ctrader
```

The ctrader profile also serves `crypto` from Binance: set `BINANCE_SYMBOLS`
(Binance's own names, `BTCUSDT,ETHUSDT`, which are also the canonical names).
Binance candles support M1, M3, M5, M15, M30, H1, H4, H12, D1 and W1. The REST
weight budget (`BINANCE_REQUEST_WEIGHT_PER_MINUTE`, default 4800 of Binance's
6000) is enforced client-side, and a 429/418 pauses REST calls until
`Retry-After` and surfaces as 503. JavaScript consumers use
[`@trading-algos/market-data`](../../packages/ta-market-data-js/README.md).

On Windows, install with `uv sync --package market-data-service --extra mt5`,
copy `.env.example.<terminal>`, `markets.<terminal>.example.toml` and
`symbols.<terminal>.example.json` into place, and run one process per terminal.

**cTrader needs its own OAuth grant**, ideally view-only, and its own
`TOKEN_CACHE_PATH`. cTrader rotates the refresh token on every refresh and kills
the previous one, so sharing a grant or a cache with execution-service locks one
of them out.

**MT5 needs the server's UTC offset.** `MT5_SERVER_UTC_OFFSET_SECONDS` turns the
terminal's server-time bars and ticks into UTC (HFM is 10800). Startup logs
`mt5_server_offset_suspect` when the newest tick disagrees by over a minute.
Reverify after seasonal clock changes.

## Endpoints

All `/v1/*` routes require `X-API-Key`. `provider=` is optional on every route
and must match the market's configured provider.

| Method | Path | Returns |
|---|---|---|
| GET | `/v1/{market}/tick?symbol=XAUUSD` | `MarketQuote`: `price`, and `bid`/`ask`/`spread` when genuinely quoted |
| GET | `/v1/{market}/candles?symbol=&timeframe=H1&count=500&to=` | Closed bars, oldest first, stamped at the UTC interval **end** |
| GET | `/v1/{market}/symbols` | Instruments with digits, price and quantity increments, limits |
| GET | `/v1/{market}/capabilities` | Timeframes, streaming, bid/ask, `max_candles` |
| GET | `/v1/{market}/stream/ticks?symbols=` | SSE: `status` and `tick` (a `MarketQuote`) events |
| GET | `/health/live` | Process is up |
| GET | `/health/ready` | 200 when every provider is connected and no market's newest quote is stale |

Errors use the platform envelope `{"error": {"code", "message", "details"}}`:
`market_not_enabled` (404), `provider_not_available`, `symbol_not_allowed`,
`timeframe_not_supported`, `count_exceeds_limit`, `invalid_timestamp` (422),
`broker_not_ready`, `terminal_not_ready`, `tick_unavailable`,
`candles_unavailable` (503).

Python callers use `ta_clients.MarketDataClient`, which pages candles past
`MAX_CANDLES_LOOKBACK` on `to`.

## Design notes

- **Closed bars only.** The forming bar is dropped by each provider; MT5 asks for
  one extra row so `count` closed bars still come back.
- **Streams replay the last quote** per requested symbol on connect, then push.
  Each subscriber has a bounded queue (`SUBSCRIBER_QUEUE_SIZE`); on overflow the
  oldest quote is dropped and counted in `status` events, so one slow consumer
  never stalls the feed. MT5 quotes are polled (`MT5_QUOTE_POLL_SECONDS`) and
  published only when they change.
- **Stateless for history.** Candles are fetched on demand; consumers own their
  caches (`ta_clients.JsonlCandleCache`).
