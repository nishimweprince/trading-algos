# Trading Ideas Logger Bot

Scheduled NestJS bot that reuses an authenticated IC Markets Chrome tab, extracts Trading Central ideas through screenshot OCR + Ollama Cloud, detects new additions, and appends them to a JSONL log.

## Quick start

```bash
npm install
cp .env.example .env
# Set OLLAMA_API_KEY and SOURCES, then start the dedicated Chrome profile below.
npm run start:dev
```

## Environment

See `.env.example`. Key variables:

| Variable | Description |
| --- | --- |
| `SOURCES` | JSON array of `{ "type": "TRADING_CENTRAL" \| "AUTOCHARTIST", "url": "..." }` |
| `BROWSER_MODE` | Trading Central requires `CDP` (attach to existing Chrome) |
| `USER_DATA_DIR` | Chrome profile path when `PERSISTENT` (default `./.chrome-profile`) |
| `CDP_ENDPOINT` | e.g. `http://127.0.0.1:9222` when `BROWSER_MODE=CDP` |
| `CRON_EXPRESSION` | default `*/15 * * * *` |
| `IDEAS_LOG_PATH` | JSONL output path |
| `SEEN_STATE_PATH` | versioned hashes, full signals, and extraction diagnostics |
| `SCREENSHOT_DIR` | per-run audit screenshots |
| `DEBUG_RUN_MAX_ENTRIES` | bounded success/failure history in `seen.json` (default 100) |
| `OLLAMA_API_KEY` | API key for direct Ollama Cloud access |
| `OLLAMA_HOST` | default `https://ollama.com` |
| `OLLAMA_MODEL` | default `ministral-3:3b` |
| `OLLAMA_TIMEOUT_MS` | default `30000` |
| `HEADLESS` | keep `false` for first login; `true` later if the session is still valid |

Invalid `SOURCES` fails startup (no silent skip).

## Dedicated Chrome profile

Chrome 136+ does not expose its default profile through the remote-debugging port. Start a dedicated profile on macOS:

```bash
mkdir -p "$PWD/.chrome-cdp-profile"
open -na "Google Chrome" --args \
  --remote-debugging-port=9222 \
  --user-data-dir="$PWD/.chrome-cdp-profile"
```

Log into IC Markets in that instance and leave a tab open at the configured Trading Central origin/path. The bot reloads the matching tab; it never creates, repurposes, or closes a tab/window.

## Trading Central extraction

Each run captures the full page for the active market category, runs English OCR with one reusable Tesseract.js worker, and sends the OCR text plus positional line hints to Ollama Cloud. Valid signals map the black chart marker to `entry`, Pivot to `stopLoss`, and Target to `takeProfit`.

Existing `seen.json` files are upgraded automatically to version 2. The original hash map remains intact, while full normalized signals and bounded OCR/Ollama success/failure records are added. New valid signals continue to be appended to `ideas.jsonl`.

## Tests

```bash
npm test
```

Tests use mocked OCR/Ollama responses. Live authenticated pages and an Ollama key are not required.

## Architecture

See `signal-scrapper-bot-plan.md` for the full design. Flow:

`SchedulerService` → matching CDP tab → screenshot → Tesseract.js → Ollama Cloud → `DedupService` → `JsonlLoggerService`
