# ta-plugin-binance-futures

Binance USDⓈ-M perpetuals, public data plus read-only account reads. Built for
the OFI scalper (`ofi-scalper-plan.md`); nothing here can place an order.

Published as `binance_futures` in the `ta.market_data` group. The factory also
exposes this plugin's own typed extras, reached through the factory a service
gets from `load_providers` (never by constructing classes):

| Factory method | What it gives |
|---|---|
| `market_data(settings)` | Platform `MarketDataProvider`: instruments, closed candles, book-ticker quotes. Passes `MarketDataConformance`. |
| `streams(settings, rest=, on_raw=)` | `FuturesStreams`: typed `DepthUpdate`, `BookTick`, `AggTrade`, `MarkPrice`, `Liquidation`, `StreamStatus`, `BookStatus` events, each stamped with local `recv_ns`, and a verified `LocalOrderBook` per symbol. `on_raw(channel, recv_ns, text)` sees every raw frame and every depth snapshot (for recorders). |
| `account(settings, rest=)` | `AccountReader`: `commission_rate`, `account_config`, `server_time_ms`, timed round trips. Signed GETs only. |
| `rest(settings)` | One shared `FapiRest` (one request-weight budget) to pass to the above. |

## Routing

Since Binance's 2026-03 WebSocket split (legacy URLs retired 2026-04-23) the
plugin opens two connections under `BINANCE_FUTURES_WS_URL`:

- `/public/stream`: `<s>@depth@<speed>`, `<s>@bookTicker`
- `/market/stream`: `<s>@aggTrade`, `<s>@markPrice@1s`, `<s>@forceOrder`

## Local book

`depth.DepthSync` follows Binance's "manage a local order book correctly":
buffer, snapshot (`/fapi/v1/depth`), drop `u < lastUpdateId`, bridge with
`U <= lastUpdateId <= u`, then require `pu == previous u`. A break, a crossed
book, a public-channel reconnect or a consumer overflow discards the book and
resyncs; each emits a `BookStatus`. Diffs are applied as the consumer dequeues
them, so the book always matches the events the consumer has seen.

## Settings (`BinanceFuturesSettingsMixin`)

| Env | Default |
|---|---|
| `BINANCE_FUTURES_SYMBOLS` | *(required)* e.g. `BTCUSDT,ETHUSDT` |
| `BINANCE_FUTURES_REST_URL` | `https://fapi.binance.com` |
| `BINANCE_FUTURES_WS_URL` | `wss://fstream.binance.com` |
| `BINANCE_FUTURES_DEPTH_SPEED` | `0ms` (`100ms`/`250ms`/`500ms`) |
| `BINANCE_FUTURES_DEPTH_SNAPSHOT_LIMIT` | `1000` |
| `BINANCE_FUTURES_REQUEST_WEIGHT_PER_MINUTE` | `1200` (Binance allows 2400) |
| `BINANCE_FUTURES_TIMEOUT_SECONDS` | `10` |
| `BINANCE_FUTURES_RECONNECT_MAX_BACKOFF_SECONDS` | `30` |
| `BINANCE_FUTURES_API_KEY` / `_API_SECRET` | unset; **read-only** key for fee tier and RTT |
| `BINANCE_FUTURES_RECV_WINDOW_MS` | `5000` |

`limiter.OrderRateGovernor` (a fraction of 300/10 s and 1200/min) is here for
the future order path; nothing uses it yet.
