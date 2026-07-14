# MT5 Signal Execution Service

An authenticated FastAPI service that validates and synchronously executes idempotent market,
limit, and stop entry signals through one locally installed MetaTrader 5 terminal.

The official `MetaTrader5` package is Windows-only. Production must run on 64-bit Windows beside
the configured terminal, with exactly one API worker. Tests and static checks run on any platform
because the package is isolated behind an adapter.

## API

`POST /v1/signals` requires `X-API-Key`. Required fields are `signal_id`, `occurred_at`,
`execution_type`, `symbol`, `direction`, and `volume`. `entry_price` is required for `limit` and
`stop` and prohibited for `market`. `stop_loss`, `take_profit`, `expires_at`,
`deviation_points`, and `note` are optional.

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
   Copy-Item .env.example .env
   ```

4. Edit `.env`. Use a demo account initially, an absolute database path, exact broker symbols,
   a randomly generated API key, and the desired `LOG_LEVEL`. Keep `TRADING_ENABLED=false` for the
   first startup.
5. Run `mt5-signal-service`, then verify `GET /health/live`, `/health/ready`, and `/docs`.
   Readiness intentionally returns 503 until `TRADING_ENABLED=true`.
6. Stop the process, set `TRADING_ENABLED=true`, restart it, and submit a uniquely identified test
   signal on the demo account. Never reuse a signal ID with changed fields.

The command binds to `127.0.0.1:8000`. Terminate TLS and enforce network restrictions in a reverse
proxy. Do not expose Uvicorn directly to the internet. To start automatically, configure Windows
Task Scheduler to run `.venv\Scripts\mt5-signal-service.exe` at logon under the same interactive
user that owns the terminal session. Set the working directory to this repository and disable
parallel task instances.

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
