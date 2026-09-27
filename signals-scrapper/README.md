# Trading Ideas Logger Bot

Scheduled NestJS bot that attaches to your already-open main Chrome window, reuses its authenticated IC Markets tabs, extracts Trading Central / Autochartist ideas from screenshots with OpenAI vision, detects new additions, appends them to a JSONL log, and optionally submits them to `services/execution-service` (MT5).

## Quick start

```bash
npm ci
cp .env.example .env
# Set OPENAI_API_KEY and SOURCES.
# In your main Chrome: open chrome://inspect/#remote-debugging and enable remote debugging.
npm run start:dev
# Click "Allow" on the Chrome prompt when the scraper first connects.
```

## Environment

See `.env.example`. Key variables:

| Variable | Description |
| --- | --- |
| `SOURCES` | JSON array of `{ "type": "TRADING_CENTRAL" \| "AUTOCHARTIST", "url": "..." }` |
| `BROWSER_MODE` | Trading Central / Autochartist require `CDP` (attach to existing Chrome) |
| `CHROME_PROFILE` | `MAIN` (default): attach to the everyday Chrome window, never launch Chrome. `DEDICATED`: separate profile below |
| `CHROME_USER_DATA_DIR` | `MAIN` only: absolute main-profile dir; empty = OS default |
| `CDP_CONNECT_TIMEOUT_MS` | `MAIN` only: time allowed to click Chrome's "Allow" prompt (default `120000`) |
| `HOST_OS` | `AUTO` (default), `MACOS`, or `WINDOWS`; explicit values must match the host |
| `USER_DATA_DIR` | `DEDICATED` only: Chrome profile path (default `./.chrome-profile`) |
| `CDP_ENDPOINT` | `DEDICATED` only: e.g. `http://127.0.0.1:9222` |
| `CDP_AUTO_START` | `DEDICATED` only: start that Chrome after a failed local CDP attach (default `true`) |
| `CHROME_EXECUTABLE_PATH` | `DEDICATED` only: absolute override for nonstandard Chrome installations |
| `CDP_STARTUP_TIMEOUT_MS` | `DEDICATED` only: maximum wait for launched Chrome CDP readiness (default `20000`) |
| `SIGNAL_CRON_EXPRESSION` | screenshot/OpenAI extraction schedule; default `*/15 * * * *` (`CRON_EXPRESSION` remains a legacy fallback) |
| `AUTH_REFRESH_CRON_EXPRESSION` | authenticated-tab reload schedule; default `*/5 * * * *` |
| `IDEAS_LOG_PATH` | JSONL output path |
| `SEEN_STATE_PATH` | versioned hashes, full signals, and extraction diagnostics |
| `SCREENSHOT_DIR` | per-run audit screenshots |
| `DEBUG_RUN_MAX_ENTRIES` | bounded success/failure history in `seen.json` (default 100) |
| `OPENAI_API_KEY` | OpenAI API key used only by the Trading Central screenshot extraction path |
| `OPENAI_MODEL` | OpenAI vision model; default `gpt-5.6-luna` |
| `OPENAI_TIMEOUT_MS` | OpenAI request timeout; default `60000` |
| `MT5_SIGNAL_TRADING_ENABLED` | opt-in MT5 submission switch; default `false` |
| `MT5_SIGNAL_API_URL` | execution-service MT5 profile URL; `http://127.0.0.1:8000` (hfm, default) or `:8001` (ftmo) |
| `MT5_SIGNAL_API_KEY` | that profile's `API_KEY`; sent only in the `X-API-Key` header |
| `MT5_SIGNAL_TIMEOUT_MS` | request timeout; default `70000` |
| `MT5_SIGNAL_RULES` | extracted instrument → broker `mt5_symbol`/volume JSON map (symbol must be in the profile's `SYMBOLS_FILE`) |
| `MT5_EXECUTION_MAX_ENTRIES` | maximum retained terminal execution records; default `5000` |
| `HEADLESS` | keep `false` for first login; `true` later if the session is still valid |

Invalid `SOURCES` fails startup (no silent skip).

## Main Chrome window (default)

With `CHROME_PROFILE=MAIN` the scraper attaches to the Chrome you already use and works inside its existing window. It never launches Chrome and never opens a new window.

Chrome 136+ ignores `--remote-debugging-port` for the default profile, so this uses the in-browser switch that Chrome 144+ provides:

1. In your main Chrome, open `chrome://inspect/#remote-debugging` and enable **Allow remote debugging for this browser instance**. Chrome then writes `DevToolsActivePort` into its user-data dir, and the scraper reads the endpoint from that file. The setting lasts for the Chrome session, so repeat it after Chrome restarts.
2. Start the scraper and click **Allow** on Chrome's connection prompt. The connection is held for the whole process, so you approve it once per scraper start or reconnect, not on every cron tick. Re-attaches are logged at WARN.
3. Log into IC Markets in that Chrome. The Trading Central tab is reused if it is already open. If it is not, the scraper opens exactly one tab in the current window and keeps reusing it. Autochartist gets its own reused tab.

Before each screenshot the scraper brings the tab to the front, because Chrome throttles background tabs, and this briefly switches the active tab once per signal run. The authentication-refresh job only reloads the matching tabs; it takes no screenshots and makes no OpenAI calls. An overlap guard stops a refresh from interrupting extraction. Stopping the scraper only detaches; your Chrome keeps running.

If debugging is not enabled, runs fail with `cdp_unavailable` and a message explaining how to enable it. No Chrome is spawned.

For hourly signal checks with a five-minute authentication refresh:

```dotenv
SIGNAL_CRON_EXPRESSION=0 * * * *
AUTH_REFRESH_CRON_EXPRESSION=*/5 * * * *
```

### Dedicated profile (fallback)

Set `CHROME_PROFILE=DEDICATED` for a fully unattended host with no prompts. The scraper attaches to `CDP_ENDPOINT`. If that fails and `CDP_AUTO_START=true` on a loopback endpoint, it starts Chrome once with `USER_DATA_DIR`. If a Chrome already holds that profile but CDP is unreachable, it fails with `chrome_launch_failed` instead of spawning again, because spawning again would only open another window. In that case, quit that Chrome and let the scraper restart it.

## Trading Central extraction## Trading Central extraction

Each run captures the full page for the active market category and sends the PNG directly to the OpenAI Responses API with original image detail and a strict Zod-backed output schema. Valid signals map the black chart marker to `entry`, Pivot to `stopLoss`, and Target to `takeProfit`; incomplete or contradictory cards are rejected locally.

Existing `seen.json` files are upgraded automatically to version 3. The original hash map remains intact, while full normalized signals, bounded OpenAI diagnostics, legacy OCR/Ollama diagnostic fields, and MT5 execution records are retained. New valid signals continue to be appended to `ideas.jsonl`.

## Optional MT5 execution

New ideas can be submitted to `services/execution-service` running an MT5 profile (the compat API, which replaces the old `mt5-trader` service). Run both on the same Windows host beside the logged-in MT5 terminal.

| Profile | Command (from `services/execution-service/`) | `MT5_SIGNAL_API_URL` | Allowed symbol |
| --- | --- | --- | --- |
| hfm | `execution-service --profile hfm` | `http://127.0.0.1:8000` | `XAUUSDb` |
| ftmo | `execution-service --profile ftmo` | `http://127.0.0.1:8001` | `XAUUSD` |

Set up the profile's `.env.<profile>` like this:

- `API_KEY` equals this scraper's `MT5_SIGNAL_API_KEY`.
- `ALLOWED_SIGNAL_SOURCES` includes `trading_central,autochartist`.
- `SYMBOLS_FILE` lists every broker symbol used in `MT5_SIGNAL_RULES`. Anything else is rejected with `symbol_not_allowed`.
- Volumes stay at or below `MAXIMUM_VOLUME`.

Start execution-service first, confirm `GET /health/ready` returns `{"status":"ready"}`, then start this scraper. Execution outcomes are sent to notification-service by execution-service itself, so the scraper needs no notification configuration.

Execution needs all of these switches:

- `services/execution-service/.env.<profile>`: `TRADING_ENABLED=true`, plus `LIVE_TRADING_ENABLED=true` on a live account
- `signals-scrapper/.env`: `MT5_SIGNAL_TRADING_ENABLED=true`

Keep the scraper switch false during initial demo verification. Signals observed while it is false are still logged and marked seen, but are not queued for later trading.

Configure every tradable instrument explicitly:

```dotenv
MT5_SIGNAL_API_URL=http://127.0.0.1:8000
MT5_SIGNAL_API_KEY=replace-with-the-execution-service-profile-API_KEY
MT5_SIGNAL_RULES='{
  "XAU/USD": { "symbol": "XAUUSDb", "volume": "0.01" }
}'
```

Only new, non-neutral Trading Central / Autochartist ideas are eligible. They are submitted as market orders with their stop loss and take profit; the OCR entry marker is never sent as `entry_price`. Missing rules are recorded as `skipped` and broker symbols are never guessed.

`seen.json` version 3 contains the durable execution outbox. `pending` and `submitting` records are reconciled through `GET /v1/signals/{signal_id}` before any safe retry. `unknown` requires operator inspection, `blocked` indicates authentication/configuration intervention, and no replacement signal ID is generated automatically. The API key is never stored in this file.

## Live deployment (Windows MT5 host)

1. On the host, from `signals-scrapper/`, run `npm ci` and then `npm run build`. The build produces `dist/main.js`.
2. Copy `.env.example` to `.env` and fill in `OPENAI_API_KEY`, `SOURCES`, `MT5_SIGNAL_API_URL`, `MT5_SIGNAL_API_KEY` and `MT5_SIGNAL_RULES`. Keep `MT5_SIGNAL_TRADING_ENABLED=false` for the first run.
3. In the logged-in user's Chrome, enable `chrome://inspect/#remote-debugging` and sign into IC Markets.
4. Create a Task Scheduler task that runs `scripts/run-windows.ps1`. The header of that script has the exact program, arguments and settings. The important parts:
   - trigger at log on
   - **Run only when user is logged on**, so that Chrome and its Allow prompt are on the interactive desktop
   - restart on failure
   - no parallel instances

   Logs go to `logs/scraper-YYYY-MM-DD.log`.
5. Start execution-service (with `TRADING_ENABLED=false` for a dry run) before the scraper. Click **Allow** in Chrome when the scraper connects.
6. Check the log for `CDP connection ready`. Check that screenshots land in `data/screenshots` and ideas in `data/ideas.jsonl`, and that no new Chrome window or process appeared.
7. Only then turn on `MT5_SIGNAL_TRADING_ENABLED=true` and the execution-service trading switches. Confirm the outcomes in execution-service's `logs/signals.<profile>.jsonl`.

To check the vision pipeline on its own: `npm run smoke:vision -- ./data/screenshots/<file>.png`.

## Tests

```bash
npm test
```

Tests use mocked OpenAI, Chrome and execution-service responses. Live authenticated pages and an OpenAI key are not required.

## Architecture

See `signal-scrapper-bot-plan.md` for the full design. Flow:

`SchedulerService` → main Chrome (CDP via DevToolsActivePort) → matching tab → screenshot → OpenAI Responses API → JSONL + versioned seen/outbox state → execution-service MT5 compat API (`/health/ready`, `/v1/signals`, `/v1/signals/{id}`, `/v1/market-data/tick`)
