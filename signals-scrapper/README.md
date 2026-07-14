# Trading Ideas Logger Bot

Scheduled NestJS bot that reuses an authenticated IC Markets Chrome tab, extracts Trading Central ideas through screenshot OCR + Ollama Cloud, detects new additions, and appends them to a JSONL log.

## Quick start

```bash
npm install
cp .env.example .env
# Set OLLAMA_API_KEY and SOURCES. Chrome starts automatically when needed.
npm run start:dev
```

## Environment

See `.env.example`. Key variables:

| Variable | Description |
| --- | --- |
| `SOURCES` | JSON array of `{ "type": "TRADING_CENTRAL" \| "AUTOCHARTIST", "url": "..." }` |
| `BROWSER_MODE` | Trading Central requires `CDP` (attach to existing Chrome) |
| `USER_DATA_DIR` | dedicated persistent Chrome profile path (default `./.chrome-profile`) |
| `CDP_ENDPOINT` | e.g. `http://127.0.0.1:9222` when `BROWSER_MODE=CDP` |
| `HOST_OS` | `AUTO` (default), `MACOS`, or `WINDOWS`; explicit values must match the host |
| `CDP_AUTO_START` | start dedicated Chrome after a failed local CDP attach (default `true`) |
| `CHROME_EXECUTABLE_PATH` | optional absolute override for nonstandard Chrome installations |
| `CDP_STARTUP_TIMEOUT_MS` | maximum wait for launched Chrome CDP readiness (default `20000`) |
| `CRON_EXPRESSION` | default `*/15 * * * *` |
| `IDEAS_LOG_PATH` | JSONL output path |
| `SEEN_STATE_PATH` | versioned hashes, full signals, and extraction diagnostics |
| `SCREENSHOT_DIR` | per-run audit screenshots |
| `DEBUG_RUN_MAX_ENTRIES` | bounded success/failure history in `seen.json` (default 100) |
| `OLLAMA_API_KEY` | API key for direct Ollama Cloud access |
| `OLLAMA_HOST` | default `https://ollama.com` |
| `OLLAMA_MODEL` | default `ministral-3:3b` |
| `OLLAMA_TIMEOUT_MS` | default `30000` |
| `MT5_SIGNAL_TRADING_ENABLED` | opt-in MT5 submission switch; default `false` |
| `MT5_SIGNAL_API_URL` | loopback FastAPI base URL; default `http://127.0.0.1:8000` |
| `MT5_SIGNAL_API_KEY` | value sent only in the MT5 `X-API-Key` header |
| `MT5_SIGNAL_TIMEOUT_MS` | request timeout; default `70000` |
| `MT5_SIGNAL_RULES` | exact Trading Central instrument to broker symbol/volume JSON map |
| `MT5_EXECUTION_MAX_ENTRIES` | maximum retained terminal execution records; default `5000` |
| `HEADLESS` | keep `false` for first login; `true` later if the session is still valid |

Invalid `SOURCES` fails startup (no silent skip).

## Dedicated Chrome profile

Chrome 136+ does not expose its default profile through the remote-debugging port. When local CDP is unavailable, the scraper discovers Google Chrome on macOS or Windows, starts it with the non-default `USER_DATA_DIR`, opens the configured Trading Central URL, and waits for CDP before continuing. If Chrome is already available on `CDP_ENDPOINT`, it is only attached to and no process or tab is created.

The launched Chrome window remains running after the scraper stops so authentication survives scraper restarts. Log into IC Markets in that dedicated window and leave its Trading Central tab open. Scheduled runs reuse and reload the exact matching tab; they do not create, repurpose, or close tabs.

Use `CHROME_EXECUTABLE_PATH` when Chrome is outside the standard system/user Applications folders on macOS or the Program Files/LocalAppData locations on Windows. Automatic launch is intentionally disabled for non-loopback CDP URLs.

Manual macOS fallback:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --user-data-dir="$PWD/.chrome-profile" \
  https://secure.ic.com/TradingCentral/TradingCentral &
```

Manual Windows PowerShell fallback:

```powershell
$chrome = "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
$profile = Join-Path (Get-Location) ".chrome-profile"
& $chrome --remote-debugging-address=127.0.0.1 `
  --remote-debugging-port=9222 `
  "--user-data-dir=$profile" `
  https://secure.ic.com/TradingCentral/TradingCentral
```

On Windows, run the scraper from an interactive Task Scheduler session under the same user that owns Chrome and MT5. Disable parallel task instances.

## Trading Central extraction

Each run captures the full page for the active market category, runs English OCR with one reusable Tesseract.js worker, and sends the OCR text plus positional line hints to Ollama Cloud. Valid signals map the black chart marker to `entry`, Pivot to `stopLoss`, and Target to `takeProfit`.

Existing `seen.json` files are upgraded automatically to version 3. The original hash map remains intact, while full normalized signals, bounded OCR/Ollama diagnostics, and MT5 execution records are retained. New valid signals continue to be appended to `ideas.jsonl`.

## Optional MT5 execution

The scraper can send newly observed Trading Central ideas to the sibling `mt5-trader` FastAPI service. Both applications must run on the same 64-bit Windows host beside the logged-in MT5 terminal. Start `mt5-signal-service` first and verify `/health/ready`, then start this scraper.

Live execution requires both independent switches:

- `mt5-trader/.env`: `TRADING_ENABLED=true`
- `signals-scrapper/.env`: `MT5_SIGNAL_TRADING_ENABLED=true`

Keep the scraper switch false during initial demo verification. Signals observed while it is false are still logged and marked seen, but are not queued for later trading.

Configure every tradable instrument explicitly:

```dotenv
MT5_SIGNAL_API_URL=http://127.0.0.1:8000
MT5_SIGNAL_API_KEY=replace-with-the-mt5-service-api-key
MT5_SIGNAL_RULES='{
  "EUR/USD": { "symbol": "EURUSD", "volume": "0.10" },
  "AUD/JPY": { "symbol": "AUDJPY.a", "volume": "0.05" }
}'
```

Only new, non-neutral Trading Central ideas are eligible. They are submitted as market orders with their stop loss and take profit; the OCR entry marker is never sent as `entry_price`. Missing rules are recorded as `skipped` and broker symbols are never guessed.

`seen.json` version 3 contains the durable execution outbox. `pending` and `submitting` records are reconciled through `GET /v1/signals/{signal_id}` before any safe retry. `unknown` requires operator inspection, `blocked` indicates authentication/configuration intervention, and no replacement signal ID is generated automatically. The API key is never stored in this file.

## Tests

```bash
npm test
```

Tests use mocked OCR/Ollama responses. Live authenticated pages and an Ollama key are not required.

## Architecture

See `signal-scrapper-bot-plan.md` for the full design. Flow:

`SchedulerService` → matching CDP tab → screenshot → Tesseract.js → Ollama Cloud → JSONL + versioned seen/outbox state → MT5 readiness/status/API
