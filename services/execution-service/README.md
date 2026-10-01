# Execution service

A FastAPI gateway for durable, idempotent trade execution through the cTrader or MT5 broker plugin
(`plugins/ctrader`, `plugins/mt5`). Market data — quotes, candles, tick streams — is served by
[market-data-service](../market-data-service/README.md), not here. cTrader production owns one OAuth token store, one demo connection and
one live connection; each connection authenticates every token-authorized registry account in its
broker-reported environment.

Other apps in this repo place orders through this service over HTTP instead of embedding their own
broker client.

## Why a separate service

The cTrader Open API is a persistent, authenticated, protobuf-over-TLS session with a heartbeat and
a reconnect protocol. Every consumer that wants to trade should not have to own that. One process
holds the execution connection and its idempotency ledger, and everything else makes an HTTP call.

## Profiles

The `production` profile is the supported cTrader deployment; cTrader always runs through the
account registry. The former single-account `forex` and `deriv` profiles served market data only and
moved to market-data-service with it.

```bash
../../.venv/bin/execution-service --profile production # .env.production + registry → :8010
../../.venv/bin/execution-service --profile hfm        # .env.hfm, MT5 on :8000
../../.venv/bin/execution-service --profile ftmo       # .env.ftmo, MT5 on :8001
../../.venv/bin/execution-service                      # reads .env
```

These commands run from `services/execution-service/`, which is also the launchd working directory.
Running without `--profile` reads `.env`. A missing env file is a startup error naming the example
to copy.

For the Windows MT5 deployments, copy `.env.example.hfm` to `.env.hfm` and
`.env.example.ftmo` to `.env.ftmo`. Fill the terminal path, account login/password, exact broker
server, service API key, and notification-service API key. Each profile owns a different loopback
port, magic number, database, and log files; run exactly one process per profile. Both templates
load a gold-only, profile-specific JSON manifest through `SYMBOLS_FILE`. Change `mt5_symbol` to the
exact, case-sensitive Market Watch name if either broker adds a symbol suffix. `ALLOWED_SYMBOLS`
remains supported for legacy profiles, but it cannot be combined with `SYMBOLS_FILE`.

For production, copy `.env.example.production` to `.env.production` and
`accounts.example.toml` to `data/accounts.production.toml`. The registry gives every account a
stable alias and canonical-to-broker symbol map. At startup, production intersects the registry with
the account list returned for the OAuth token and treats cTrader's `isLive` flag as authoritative;
stale `enabled` and `environment` values cannot hide or misroute an authorized account. Accounts
returned by cTrader but missing from the registry are counted by `GET /v1/accounts` and remain
unusable until an alias and instrument map are added. cTrader can also list closed or disabled
accounts before rejecting their per-account authentication; production skips those accounts,
reports them in `unavailable_authorized_accounts`, and keeps the other accounts connected.

`TRADING_ENABLED` still gates all execution, and `LIVE_TRADING_ENABLED` independently gates live
accounts. Discovery never bypasses either fuse.

## Setup

```bash
cd ../..
uv sync --all-packages
cd services/execution-service
cp .env.example.production .env.production
cp accounts.example.toml data/accounts.production.toml
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

### 3. Account IDs

Registry accounts are numeric `ctidTraderAccountId`s, **not** your account login numbers. Listing
them needs only the access token:

```bash
../../.venv/bin/execution-service --profile production --discover-accounts
```

### 4. Instrument maps

Each registry account maps canonical names to exact, case-sensitive cTrader `symbolName` values.
Startup fails closed if any cannot be resolved, so list the real ones per account:

```bash
../../.venv/bin/execution-service --profile production --discover-symbols --account forex_demo
```

> For a cTrader Deriv profile, do not use MT5 synthetic-index names such as
> `Volatility 75 Index` or `Boom 500 Index`; they will not resolve on a cTrader broker.

Start against `CTRADER_ENVIRONMENT=demo` with a demo account. Demo and live are fully separated
connections and cannot be mixed.

## Endpoints

All `/v1/*` routes require an `X-API-Key` header matching `API_KEY`. Health routes are unauthenticated.

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/orders` | Idempotent market, limit or stop order across explicit account targets. |
| POST | `/v1/orders/amend` | Amend reconciled pending orders. |
| POST | `/v1/orders/cancel` | Cancel reconciled pending orders. |
| POST | `/v1/positions/protection` | Amend position SL/TP. |
| POST | `/v1/positions/close` | Fully or partially close positions. |
| GET | `/v1/operations/{operation_id}` | Durable parent and per-account execution state. |
| GET | `/v1/accounts` | Usable accounts, demo/live classification, access rights, execution gates and skipped-account counts. |
| GET | `/v1/accounts/{alias}/orders` | Reconciled pending orders. |
| GET | `/v1/accounts/{alias}/positions` | Reconciled open positions. |
| GET | `/health/live` | Process is up. |
| GET | `/health/ready` | 200 when every broker connection is up, else 503 with details. |
| GET | `/health/trading-ready` | 200 when accounts, ledger and execution gates are ready. |

Quotes, candles, symbols and the tick stream are market-data-service's `/v1/{market}/…` routes.
This service returns 404 for the old `/v1/market-data/*`, `/v1/symbols` and `/v1/stream/ticks`
paths on purpose: a consumer still pointed here fails loudly rather than reading stale data.

Read endpoints also accept a numeric `ctidTraderAccountId` and resolve it to the stable registry
alias. Order targets continue to require the alias so stored idempotency payloads remain stable.

### One execution path, every broker

The `/v1/orders`, `/v1/positions/*`, `/v1/operations` and `/v1/accounts` routes run through one
`ExecutionService` whichever broker serves a target. Each target's account alias picks its provider
(`ta_plugin_api.ExecutionProvider`, discovered from the `ta.execution` entry points):

- **cTrader** serves every registry alias. Outcomes are event-driven: the first event settles the
  request, later fills and cancels settle the ledger asynchronously.
- **MT5** serves one alias, the process profile (`hfm`, `ftmo`; `mt5` without `--profile`), because
  one process attaches to exactly one terminal. Outcomes are synchronous: preflight, send and result
  run in a thread under the terminal lock. The order comment carries the client order ID
  (`o-` plus 24 hex characters of the operation ID), which is what restart reconciliation matches
  in the terminal's history. `instrument` matches the case-sensitive MT5 name case-insensitively.
  Not supported on MT5: changing a pending order's volume (cancel and replace) and trailing stops.
  On an MT5-only host `ALLOWED_ORDER_SOURCES` falls back to `ALLOWED_SIGNAL_SOURCES`.

`/v1/accounts` reports a `provider` per account; `ctid_trader_account_id` is null for MT5.

### The legacy `/v1/signals` contract

MT5 hosts keep `POST /v1/signals` and `GET /v1/signals/{id}` byte-compatible for ipda,
signals-scrapper and lookup-trader, but a signal is now a one-target `place_order` operation in the
same ledger, `EXECUTION_DATABASE_PATH`, with the signal ID as its operation ID. Its payload hash is
still `sha256(SignalRequest.canonical_json())`, and the exact response or error body is stored, so a
replay returns the first call's body and a changed payload still returns 409 `idempotency_conflict`.

The pre-unification ledger, `DATABASE_PATH` (`signals.db`), is imported once at startup and then
only read: a marker in the new ledger stops a second import, and no imported signal ID can overwrite
one the unified service already holds. To preview or run the import by hand:

```bash
../../.venv/bin/execution-service --profile hfm --migrate-legacy-ledger --dry-run
../../.venv/bin/execution-service --profile hfm --migrate-legacy-ledger [path/to/signals.db]
```

### Execution contract

MT5 profiles also expose an optional gateway-owned OCO group API under `/v1/mt5`.
It preserves the legacy signal contract and requires explicit `MT5_OCO_ENABLED=true` plus a
matching hedge account and specified symbol expiry. See [MT5 OCO operations](docs/mt5-oco.md)
for routes, pending-order lifecycle, protection confirmation and incident recovery.

Every mutation requires a unique `operation_id`, timezone-aware `occurred_at`, allowlisted `source`
and explicit account targets. Prices and lot volumes are JSON decimal strings. The gateway validates
all targets before dispatch, persists them in SQLite, and uses deterministic per-target client
order IDs (cTrader `clientOrderId`, the MT5 order comment) to make retries safe. Replaying the same ID and payload returns stored state;
changing the payload returns 409.

Completed operations return 201. If a broker result remains pending or ambiguous after
`EXECUTION_RESPONSE_TIMEOUT_SECONDS`, the API returns 202 with a `Location` header. Cross-account
execution cannot be atomic, so mixed results are reported as `partial_failure` and are never rolled
back automatically.

`TRADING_ENABLED` gates every order. A live target additionally requires
`LIVE_TRADING_ENABLED=true`. Both default to false in the production template.

## Design notes

### No Twisted

The published `ctrader-open-api` client is built on Twisted's reactor, which cannot share a process
with uvicorn's asyncio loop. This service does not depend on that package at all: the schemas are
vendored in the cTrader plugin's [proto/](../../plugins/ctrader/proto/) and compiled locally (see
[proto/README.md](../../plugins/ctrader/proto/README.md) for why),
and the wire protocol is implemented directly on `asyncio` — 4-byte big-endian length prefix,
`ProtoMessage` envelope, `clientMsgId` correlation, 5-second heartbeat.

Twisted is therefore absent from the dependency tree entirely, not merely unused.
`plugins/ctrader/tests/test_proto.py` asserts `"twisted" not in sys.modules` so it cannot creep back
in.

The practical payoff: the sample client's callback state machine becomes straight-line `await`s in
`ta_plugin_ctrader/gateway.py`.

### Quotes

The gateway still subscribes to spot prices for every registry instrument. It serves none of them:
a MARKET order's distance-based SL/TP is priced from the account's latest quote
(`ta_plugin_api.MarketDataHub`), which must be the same connection that fills the order.

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

The execution smoke is separately gated because it places real broker orders. It refuses to run
if any configured account is live or if `LIVE_TRADING_ENABLED` is true, uses each symbol's minimum
volume, restarts with a pending order open to exercise reconciliation, and cleans up in `finally`:

```bash
CTRADER_EXECUTION_INTEGRATION=1 CTRADER_PROFILE=production \
  .venv/bin/pytest tests/test_execution_integration_demo.py -s
```

It is the only thing that can settle the protocol facts the specification does not state — whether
trendbars are bid-side or mid, and whether the forming bar is included in a history response. **It
has never been run**, so both remain open. Run it before trusting the service; the answers get
recorded in `plugins/ctrader/src/ta_plugin_ctrader/decode.py`.

## Deployment

`infra/launchd/` has the plists plus `install.sh`, which is the supported install path — it creates
the `logs/` and `data/` directories launchd cannot create for itself, and refuses to install an env
file that still holds template placeholders or a port already in use. See
[infra/launchd/README.md](../../infra/launchd/README.md).

```bash
./infra/launchd/install.sh production
```
