# MT5 Signal Execution Service

An authenticated FastAPI service that validates and synchronously executes idempotent market,
limit, and stop entry signals through one locally installed MetaTrader 5 terminal.

The official `MetaTrader5` package is Windows-only. Production must run on 64-bit Windows beside
the configured terminal, with exactly one API worker. Tests and static checks run on any platform
because the package is isolated behind an adapter.

## API

`POST /v1/signals` requires `X-API-Key`. Required fields are `signal_id`, `occurred_at`,
`execution_type`, `symbol`, `direction`, and `volume`. `entry_price` is required for `limit` and
`stop` and prohibited for `market`. `stop_loss`, `take_profit`, `stop_loss_distance`,
`take_profit_distance`, `expires_at`, `deviation_points`, and `note` are optional.

### Absolute vs. distance-based risk levels

Each leg accepts either an absolute price (`stop_loss`, `take_profit`) or a distance in
price units (`stop_loss_distance`, `take_profit_distance`); supplying both for the same
leg is rejected. A distance is resolved against the **execution reference price** — the
live ask for a buy, the live bid for a sell, or `entry_price` for a pending order — and
the direction sign is applied here, so the caller sends an unsigned magnitude.

Prefer distances whenever the caller decided on a stop *size* rather than a stop *level*.
A strategy computing levels from a bar close cannot account for the spread or for drift
between its decision and the fill; only this service knows the price the order fills at.
Send absolute prices when the level itself is the intent — a structural level, or an
indicator line such as a supertrend trailing stop.

Example market entry:

```json
{
  "signal_id": "3a939594-f36e-4dc7-96a5-97e84e21c36e",
  "occurred_at": "2026-07-14T15:00:00Z",
  "execution_type": "market",
  "symbol": "EURUSD",
  "direction": "buy",
  "volume": "0.10",
  "stop_loss": "1.08000",
  "take_profit": "1.10000",
  "deviation_points": 10,
  "note": "strategy-a breakout"
}
```

Example expiring pending entry:

```json
{
  "signal_id": "c0c95e51-13d4-4567-b543-a0e41098f008",
  "occurred_at": "2026-07-14T15:00:00Z",
  "execution_type": "limit",
  "symbol": "EURUSD",
  "direction": "buy",
  "volume": "0.10",
  "entry_price": "1.08500",
  "expires_at": "2026-07-14T18:00:00Z"
}
```

```powershell
$headers = @{ "X-API-Key" = $env:API_KEY }
$body = Get-Content .\signal.json -Raw
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/signals `
  -Headers $headers -ContentType "application/json" -Body $body
```

Successful responses report `filled`, `partially_filled`, or `placed`, along with the broker
tickets and result. `GET /v1/signals/{signal_id}` retrieves the durable state. Interactive OpenAPI
documentation is available at `/docs`.

`GET /v1/market-data/candles` requires `X-API-Key` and returns historical OHLC bars straight from
the connected MT5 terminal — intended for market-data consumers such as
[`lux-algo`](../lux-algo). Query parameters: `quote` (required, an exact symbol from
`ALLOWED_SYMBOLS`), `timeframe` (optional, one of `M1`, `M5`, `M15`, `M30`, `H1`, `H4`, `D1`;
default `M1`), `count` (optional, number of most recent bars; default 500, capped by
`MAX_CANDLES_LOOKBACK`).

```powershell
$headers = @{ "X-API-Key" = $env:API_KEY }
Invoke-RestMethod -Uri "http://127.0.0.1:8000/v1/market-data/candles?quote=EURUSD&count=120" -Headers $headers
```

```json
{
  "symbol": "EURUSD",
  "timeframe": "M1",
  "candles": [
    {"time": 1721826000, "open": 1.10000, "high": 1.10050, "low": 1.09950, "close": 1.10020, "volume": 134}
  ]
}
```

`time` is epoch seconds and `volume` is MT5 tick volume. lux-algo's default `.env` values
(`DATA_QUOTE_PARAM=quote`, `DATA_COUNT_PARAM=count`) already match this endpoint's parameter
names, so only `DATA_API_URL` and `DATA_API_KEY` need to be set on that side.

## Console logs

The application writes one JSON object per line to standard output. At `LOG_LEVEL=INFO`, logs cover
the full lifecycle: accepted signal payload, idempotency reservation, terminal-lock acquisition,
symbol and tick metadata, constructed MT5 request, `order_check()` result, `order_send()` result,
normalized response, rejections, ambiguous outcomes, and startup reconciliation. This makes logs
suitable for Windows service capture or forwarding to a centralized log collector.

Account passwords, API-key values, and inbound authentication headers are never logged. An
authentication failure records only whether a key was present. Because valid signal payloads and
trading results are intentionally logged in full, console output must be treated as sensitive
trading data.

## Windows installation

1. Install 64-bit Python 3.11 or newer and MetaTrader 5. Log the intended account into the
   terminal and enable algorithmic trading.
2. Clone this repository to a local NTFS directory and open PowerShell there.
3. Create and install the environment:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   python -m pip install ".[dev]"
   Copy-Item .env.example.forex .env
   ```

4. Edit `.env`. Use a demo account initially, an absolute database path, exact broker symbols,
   a randomly generated API key, and the desired `LOG_LEVEL`. Keep `TRADING_ENABLED=false` for the
   first startup.
5. Run `mt5-signal-service`, then verify `GET /health/live`, `/health/ready`, and `/docs`.
   Readiness intentionally returns 503 until `TRADING_ENABLED=true`.
6. Stop the process, set `TRADING_ENABLED=true`, restart it, and submit a uniquely identified test
   signal on the demo account. Never reuse a signal ID with changed fields.

The command binds to `HOST:PORT` (default `127.0.0.1:8000`). Terminate TLS and enforce network restrictions in a reverse
proxy. Do not expose Uvicorn directly to the internet. To start automatically, configure Windows
Task Scheduler to run `.venv\Scripts\mt5-signal-service.exe` at logon under the same interactive
user that owns the terminal session. Set the working directory to this repository and disable
parallel task instances for the same profile.

## Profiles (multiple instances on one server)

Each profile loads a separate env file from the repository working directory:

| Command | Env file | Example template |
|---|---|---|
| `mt5-signal-service` | `.env` | `.env.example.forex` |
| `mt5-signal-service --profile forex` | `.env.forex` | `.env.example.forex` |
| `mt5-signal-service --profile deriv` | `.env.deriv` | `.env.example.deriv` |

Process environment variables still override file values. Use profiles to run forex and Deriv (or
any other broker) side by side from one clone without cwd tricks.

## Running a second broker (e.g. Deriv MT5)

This service is broker-agnostic: every broker-specific value is configuration, so a Deriv MT5
(DMT5) account needs no code changes. Because the `MetaTrader5` package attaches to exactly one
terminal per process, a second broker means a **second terminal installation and a second service
instance** — never a second account inside one process.

1. Install MetaTrader 5 a second time into its own directory (Deriv ships its own build), log the
   DMT5 account in, and enable algorithmic trading.
2. Copy the Deriv template and edit it. These values **must** differ from the first instance:

   ```powershell
   Copy-Item .env.example.deriv .env.deriv
   ```

   | Variable | Why it must differ |
   |---|---|
   | `PORT` | Two services cannot share a bind address |
   | `DATABASE_PATH` | The idempotency ledger is per-account; a shared file cross-contaminates signal state |
   | `MAGIC_NUMBER` | Startup reconciliation claims orders by magic number and would otherwise adopt the other instance's trades |
   | `MT5_TERMINAL_PATH`, `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` | The Deriv terminal and account |
   | `API_KEY` | Independent credentials per instance |

   `.env.example.deriv` already sets illustrative DMT5 symbols, port `8001`, and wider deviation
   defaults. Adjust paths and credentials for your host.

3. Start the Deriv instance:

   ```powershell
   mt5-signal-service --profile deriv
   ```

   Verify `http://127.0.0.1:8001/health/live` (or the `PORT` you configured).

4. For Task Scheduler, create a second task with the same working directory but arguments
   `--profile deriv`. Each profile must run exactly one process.

Synthetic indices trade continuously, so `SIGNAL_MAX_AGE_SECONDS` never trips on a weekend gap.
They do, however, carry much larger `trade_stops_level` and `point` values than forex majors.
Stop distances that are valid on EURUSD are frequently rejected with `stop_loss_too_close` or
`take_profit_too_close`; treat those 422s as a signal to widen the strategy's stop, not as a bug.
Slippage settings tuned for majors do not transfer either — size `DEFAULT_DEVIATION_POINTS` and
`MAXIMUM_DEVIATION_POINTS` against the synthetic's own tick size.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
ruff check .
ruff format --check .
```

The integration test is skipped by default. On a Windows demo host with all `.env` values set:

```powershell
$env:RUN_MT5_DEMO_INTEGRATION = "1"
pytest -m integration
```

This opt-in test only checks terminal connectivity and will refuse to run unless the broker server
name contains `demo`.

For failure handling, deployment checks, backup, and recovery procedures, see
[`docs/operator-runbook.md`](docs/operator-runbook.md).
