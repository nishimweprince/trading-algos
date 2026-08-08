# ctrader-markets

A profile-scoped FastAPI service that owns one persistent cTrader Open API connection and re-exposes
it over HTTP: live spot ticks as Server-Sent Events, plus REST access to the latest tick, closed
trendbars (OHLC), and symbol metadata.

Other apps in this repo consume this service over HTTP instead of embedding their own broker client.

## Why a separate service

The cTrader Open API is a persistent, authenticated, protobuf-over-TLS session with a heartbeat and
a reconnect protocol. Every consumer that wants a price should not have to own that. One process per
broker account holds the connection, and everything else makes an HTTP call.

## Profiles

Each profile is one broker account, one `.env` file, and one process on its own port — the same
model as `mt5-trader`.

```bash
ctrader-markets --profile forex    # reads .env.forex   → :8010
ctrader-markets --profile deriv    # reads .env.deriv   → :8011
ctrader-markets                    # reads .env
```

Running without `--profile` reads `.env`. A missing env file is a startup error naming the example
to copy.

## Setup

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example.forex .env.forex
```

Then fill in the four credentials, in this order.

### 1. `CTRADER_CLIENT_ID` / `CTRADER_CLIENT_SECRET`

Register an application at <https://openapi.ctrader.com/>. The client id and secret are shown on the
application page.

### 2. `CTRADER_ACCESS_TOKEN` / `CTRADER_REFRESH_TOKEN`

A one-time browser OAuth2 flow, done by hand. Open this URL (substituting your client id and the
redirect URI registered with the application):

```
https://openapi.ctrader.com/apps/auth
  ?client_id=YOUR_CLIENT_ID
  &redirect_uri=YOUR_REDIRECT_URI
  &scope=trading
```

Log in, approve, and copy the `code` query parameter from the redirect. Exchange it for tokens:

```bash
curl -s 'https://openapi.ctrader.com/apps/token' \
  -d grant_type=authorization_code \
  -d code=THE_CODE \
  -d redirect_uri=YOUR_REDIRECT_URI \
  -d client_id=YOUR_CLIENT_ID \
  -d client_secret=YOUR_CLIENT_SECRET
```

Put `accessToken` and `refreshToken` into the env file. The service refreshes them from then on and
persists the rotated pair to `TOKEN_CACHE_PATH` — see [Token lifecycle](#token-lifecycle).

### 3. `CTRADER_ACCOUNT_ID`

This is the numeric `ctidTraderAccountId`, **not** your account login number. It needs only the
access token, so discover it once the tokens are in place:

```bash
ctrader-markets --profile forex --discover-accounts
```

### 4. `SYMBOLS`

Exact, case-sensitive cTrader `symbolName` values. Startup fails closed if any cannot be resolved,
so list the real ones:

```bash
ctrader-markets --profile forex --discover-symbols
```

> For a Deriv profile, do not copy the symbol names from `mt5-trader/.env.example.deriv`. Those are
> Deriv's MT5 synthetic indices and will not resolve on a cTrader broker.

Start against `CTRADER_ENVIRONMENT=demo` with a demo account. Demo and live are fully separated
connections and cannot be mixed.

## Endpoints

All `/v1/*` routes require an `X-API-Key` header matching `API_KEY`. Health routes are unauthenticated.

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/stream/ticks?symbols=EURUSD,XAUUSD` | SSE stream of live bid/ask. `symbols` optional; omit for all. |
| GET | `/v1/market-data/tick?symbol=EURUSD` | Latest cached quote. |
| GET | `/v1/market-data/candles?symbol=EURUSD&timeframe=H1&count=200` | Closed trendbars. |
| GET | `/v1/symbols` | Resolved catalog: `symbolId`, `digits`, `enabled`. |
| GET | `/health/live` | Process is up. |
| GET | `/health/ready` | 200 when connected and ticks are fresh, else 503 with details. |

```bash
curl -N -H 'X-API-Key: …' 'localhost:8010/v1/stream/ticks?symbols=EURUSD'

event: tick
data: {"symbol":"EURUSD","bid":1.08532,"ask":1.08545,"spread":0.00013,"ts":"…Z","provider":"ctrader"}
```

The stream replays the last known tick for each requested symbol on connect, so a client joining
mid-session does not wait for the next quote on a quiet instrument. It also emits `status` events on
connection state changes, carrying a `dropped` counter — see [Backpressure](#backpressure).

### Candle shape

`GET /v1/market-data/candles` returns candles **stamped at the END of their UTC interval**, matching
lookup-trader's `app/providers/base.py::Candle` field for field:

```json
{"ts": "2026-08-08T14:00:00Z", "open": 1.0853, "high": 1.0861, "low": 1.0849,
 "close": 1.0857, "volume": 4210.0, "provider": "ctrader",
 "source_instrument": "EURUSD", "spread": null, "spread_source": null}
```

cTrader sends `utcTimestampInMinutes` as the interval *start*; the conversion happens server-side in
`decode.py`. Only closed bars are returned — the currently-forming bar is dropped.

## Design notes

### No Twisted

The published `ctrader-open-api` client is built on Twisted's reactor, which cannot share a process
with uvicorn's asyncio loop. This service depends on that package **only for its generated protobuf
message classes** and implements the wire protocol directly on `asyncio` — 4-byte big-endian length
prefix, `ProtoMessage` envelope, `clientMsgId` correlation, 5-second heartbeat. Importing the
package still pulls Twisted in transitively, but no reactor is ever started. That import is confined
to `src/ctrader/proto.py`.

The practical payoff: the sample client's callback state machine becomes straight-line `await`s in
`session.py`.

### Backpressure

One broker connection fans out to N SSE subscribers through `hub.py`. Each subscriber has a bounded
queue (`SUBSCRIBER_QUEUE_SIZE`, default 256) and publishing is **synchronous and non-blocking** — on
overflow the *oldest* tick is dropped, because a newer quote supersedes a stale one. A wedged or slow
SSE client can therefore never stall the reader loop. Drops are counted per subscriber, logged on
first occurrence, and reported in the periodic `status` event so a consumer can tell it fell behind.

### Subscriptions

The service subscribes to every symbol in `SYMBOLS` at startup, not on demand. The subscribed set is
a pure function of configuration rather than of live HTTP connections, which is what makes reconnect
trivially correct — it just re-sends the same list — and keeps `/v1/market-data/tick` an O(1) cache
read instead of a subscribe-wait-unsubscribe round trip.

### Token lifecycle

Access tokens expire, and an expired token means the service silently stops reconnecting. Refresh is
automatic: reactively when account auth fails with an expired-token error (refresh, retry once, then
fall back to normal backoff), and proactively at 80% of the token lifetime.

`ProtoOARefreshTokenRes` returns a **rotated** refresh token — the old one dies on use. The new pair
is written atomically to `TOKEN_CACHE_PATH` with mode `0600`. **That file is the only writable state
in the service. Losing it means redoing the browser OAuth flow.** It is gitignored; back it up with
your other secrets.

## Development

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/pytest -m "not integration"
```

The protocol client is tested against an in-memory fake that drives `asyncio.StreamReader` directly
(`tests/fakes.py`), so the full suite runs with no network and asserts on the exact bytes written to
the wire.

The integration suite needs a real demo account and is skipped by default:

```bash
CTRADER_INTEGRATION=1 .venv/bin/pytest -m integration
```

It is the only thing that validates the protocol facts that could not be confirmed from the
specification — whether trendbars are bid-side or mid, and whether the forming bar is included in a
history response. Run it before trusting the service.

## Deployment

`ops/` has one launchd plist per profile. See [ops/README.md](ops/README.md).
