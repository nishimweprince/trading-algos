# ta-plugin-binance-futures

Binance USDⓈ-M perpetuals for the OFI scalper: public market data, read-only
account reads, and (through execution-service only) order entry.

Published as `binance_futures` in two groups:

- `ta.market_data` → `FACTORY` (no keys needed);
- `ta.execution` → `EXECUTION_FACTORY` (needs the trading key), used by
  execution-service with `ADAPTERS=binance_futures`.

## Order entry (`execution.py`)

`BinanceFuturesExecution` is an `ExecutionProvider` and an `AccountControlVenue`:

- **Environments:** `BINANCE_FUTURES_ENV=testnet` (demo trading: `demo-fapi` /
  `demo-fstream`, the default) or `mainnet` (also needs `LIVE_TRADING_ENABLED`).
- **Orders:** `newClientOrderId = operation_id.hex`; quantity in base asset
  checked against LOT_SIZE / MARKET_LOT_SIZE / MIN_NOTIONAL / tick size before
  the ledger; `post_only` → GTX, `reduce_only` → `reduceOnly`; stops are
  STOP_MARKET. SL/TP on an order is refused (exits are their own reduce-only
  orders); amend is 501 (cancel and place).
- **Outcomes:** 4xx with a Binance code → REJECTED (`post_only_would_take`,
  `reduce_only_rejected`, ...); no response / 5xx / `-1007` → UNKNOWN, then
  reconciled by client order id and never resubmitted. The user-data stream
  (`/private/ws?listenKey=…`) settles targets as orders fill or cancel; states
  only move forward. Every reconnect resyncs open orders and positions and
  settles orders that finished while the stream was down.
- **Positions:** one-way positions have no id at Binance; `position_id` is a
  stable crc32 of the symbol, and `CLOSE_POSITION` sends a reduce-only market.
- **Kill controls:** `cancel_all` (allOpenOrders), `flatten` (reduce-only market
  from fresh positions), `dead_man` (`countdownCancelAll`; 0 disarms).
- **Preflight** (verified at start and retried, never changed): one-way mode,
  single-asset margin, leverage ≤ `BINANCE_FUTURES_MAX_LEVERAGE`, isolated
  margin. Until it passes the provider is not ready and readiness lists the fix.

The factory also
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
