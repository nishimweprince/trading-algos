# Trading Ideas Logger Bot

Scheduled NestJS bot that opens an already-authenticated IC Markets research page (Trading Central or Autochartist), extracts trading ideas, detects new additions, and appends them to a JSONL log.

## Quick start

```bash
npm install
npx playwright install chromium   # first time only
cp .env.example .env
# Edit SOURCES / paths as needed (default BROWSER_MODE=PERSISTENT)

# First run: browser opens — log into IC Markets, then leave the bot running
npm run start:dev
```

## Environment

See `.env.example`. Key variables:

| Variable | Description |
| --- | --- |
| `SOURCES` | JSON array of `{ "type": "TRADING_CENTRAL" \| "AUTOCHARTIST", "url": "..." }` |
| `BROWSER_MODE` | default `PERSISTENT` (saved profile) or `CDP` (attach to Chrome) |
| `USER_DATA_DIR` | Chrome profile path when `PERSISTENT` (default `./.chrome-profile`) |
| `CDP_ENDPOINT` | e.g. `http://127.0.0.1:9222` when `BROWSER_MODE=CDP` |
| `CRON_EXPRESSION` | default `*/15 * * * *` |
| `IDEAS_LOG_PATH` | JSONL output path |
| `SEEN_STATE_PATH` | persisted dedup hashes |
| `SCREENSHOT_DIR` | per-run audit screenshots |
| `HEADLESS` | keep `false` for first login; `true` later if the session is still valid |

Invalid `SOURCES` fails startup (no silent skip).

## Persistent profile (default)

1. Set `BROWSER_MODE=PERSISTENT` and `USER_DATA_DIR=./.chrome-profile` (defaults).
2. Keep `HEADLESS=false` for the first run.
3. Start the bot (`npm run start:dev` or `npm run build && npm run start:prod`).
4. In the window Playwright opens, log into IC Markets once.
5. Later runs reuse cookies under `USER_DATA_DIR`. If you see login-wall warnings, re-login with `HEADLESS=false`.

## CDP attach (optional)

1. Launch Chrome with `--remote-debugging-port=9222`.
2. Log in to IC Markets in that window.
3. Set `BROWSER_MODE=CDP` and `CDP_ENDPOINT=http://127.0.0.1:9222`.
4. Start the bot.

## Tests

```bash
npm test
```

Tests cover config validation, hashing/dedup, JSONL append, seen-state restart, both extractors against fixtures, multi-source orchestration with stubs, login-wall/timeout skip, and scheduler overlap guard. Live authenticated pages are not required.

## Architecture

See `signal-scrapper-bot-plan.md` for the full design. Flow:

`SchedulerService` → `ScraperService.runAllSources()` → provider `IdeaExtractor` → `DedupService` → `JsonlLoggerService`
