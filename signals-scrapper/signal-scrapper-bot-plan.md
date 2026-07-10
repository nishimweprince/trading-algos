# Trading Ideas Logger Bot, Implementation Plan

A scheduled TypeScript bot that opens an already authenticated IC Markets research page (Trading Central or Autochartist), extracts the trading ideas on the page, detects new additions, and appends them to a JSONL log. What happens to the ideas after logging is out of scope for now.

---

## 1. Goal and Scope

**In scope**
- Load a configured research URL in a browser session that is already logged in.
- Extract every trading idea currently rendered (instrument, direction, timeframe, targets, pivot, levels, per-card timestamp).
- Keep a screenshot per run as an audit artifact.
- Detect which ideas are new since the last run and append only those to a JSONL file.
- Run automatically every 15 minutes.
- Support two source types, TRADING_CENTRAL and AUTOCHARTIST, from one running process, configured entirely by environment variables.

**Out of scope (for now)**
- Deciding what to do with logged ideas (alerts, trades, dashboards). That is a later phase.
- Placing any orders or touching the Trade buttons.

**Note on Terms of Service**
Automating access to IC Markets and Trading Central sits in ToS gray-area territory. Keep this to your own account, personal logging only, run it at a modest cadence, and do not redistribute the vendor data.

---

## 2. Runtime Decision: NestJS vs plain Node.js

**Recommendation: NestJS.**

For a one-off script, plain Node with `node-cron` would be lighter. But this project has three traits that make NestJS the better long-term fit, and you already know the stack from the timesheets app:

| Need | Why NestJS fits |
| --- | --- |
| Scheduled runs every 15 min | `@nestjs/schedule` gives you a `@Cron()` decorator with clean lifecycle handling, no hand-rolled interval loop. |
| Two providers behind one interface | Provider strategy maps cleanly onto NestJS modules and DI. Adding a third source later is a new provider class, not a rewrite. |
| Config from environment | `@nestjs/config` validates and types the `SOURCES` JSON at boot, so a malformed config fails fast instead of mid-run. |
| Likely growth | When you later decide what to do with the ideas, you can bolt on a REST controller, a queue, or a Postgres store without restructuring. |

**When plain Node would win instead:** if you were certain this stays a single throwaway script forever and you wanted the smallest possible footprint. Given your profile and the two-provider requirement, that is not the case here.

The rest of this plan assumes NestJS.

---

## 3. High-Level Architecture

```
             ┌──────────────────────────────────────────┐
             │            SchedulerService              │
             │   @Cron every 15 min -> runAllSources()   │
             └───────────────────┬──────────────────────┘
                                 │
                                 ▼
             ┌──────────────────────────────────────────┐
             │             ScraperService               │
             │  for each configured source:             │
             │    1. get/attach browser page            │
             │    2. navigate to source.url             │
             │    3. delegate to provider extractor     │
             │    4. screenshot (audit)                 │
             └───────────────────┬──────────────────────┘
                                 │
              ┌──────────────────┼───────────────────┐
              ▼                                      ▼
   ┌────────────────────┐              ┌────────────────────────┐
   │ TradingCentral     │              │ Autochartist           │
   │ Extractor          │              │ Extractor              │
   │ (implements        │              │ (implements            │
   │  IdeaExtractor)    │              │  IdeaExtractor)        │
   └─────────┬──────────┘              └───────────┬────────────┘
             │                                     │
             └──────────────┬──────────────────────┘
                            ▼
             ┌──────────────────────────────────────────┐
             │              DedupService                │
             │  hash each idea, filter already-seen     │
             └───────────────────┬──────────────────────┘
                                 ▼
             ┌──────────────────────────────────────────┐
             │            JsonlLoggerService            │
             │  append new ideas to ideas.jsonl         │
             └──────────────────────────────────────────┘
```

The `IdeaExtractor` interface is the seam. `ScraperService` never knows whether it is talking to Trading Central or Autochartist, it just calls `extractor.extract(page)`.

---

## 4. Environment Variables

One process handles both sources, so the config is a JSON array. Each entry pairs a URL with its type.

```bash
# JSON array of sources. type is TRADING_CENTRAL or AUTOCHARTIST.
SOURCES='[
  {"type":"TRADING_CENTRAL","url":"https://secure.icmarkets.com/TradingCentral/TradingCentral"},
  {"type":"AUTOCHARTIST","url":"https://secure.icmarkets.com/AutoChartist/AutoChartist"}
]'

# How the bot reaches an already-authenticated browser (see section 6).
BROWSER_MODE=CDP                 # CDP or PERSISTENT
CDP_ENDPOINT=http://localhost:9222
USER_DATA_DIR=./.chrome-profile  # used when BROWSER_MODE=PERSISTENT

# Scheduling
CRON_EXPRESSION=*/15 * * * *     # every 15 minutes

# Output
IDEAS_LOG_PATH=./data/ideas.jsonl
SCREENSHOT_DIR=./data/screenshots
SEEN_STATE_PATH=./data/seen.json

# Behavior
HEADLESS=false                   # keep false when attaching to your real session
NAV_TIMEOUT_MS=30000
```

Validate `SOURCES` at boot with a schema (Zod or class-validator). Reject entries whose `type` is not one of the two known values, and fail startup rather than silently skipping.

---

## 5. Data Extraction Strategy

Use a layered approach. Prefer structured data, fall back to text, keep the image only as evidence.

**Primary: network interception.**
The cards are populated from the vendor backend. Subscribe with `page.on('response', ...)`, match the research API endpoint, and capture the JSON straight from the source. This gives you clean fields (instrument, direction, targets, pivot, levels, timestamp) with no parsing of rendered pixels or text. Find the endpoint once via DevTools Network tab, then filter on its URL pattern.

**Fallback: DOM scraping.**
If the endpoint is awkward to identify or changes, read the rendered text. All the values you need are real DOM text nodes, not baked into the chart image. Two things to plan for:
- The widget is usually inside an `<iframe>`, so use `page.frameLocator(...)` rather than top-level selectors.
- Selectors will be brittle to vendor markup changes, so isolate them in one file per provider so a break is a one-file fix.

**Audit only: screenshot.**
Take a full-page (or widget-clipped) screenshot each run and save it under `SCREENSHOT_DIR` named by source and timestamp. Do not OCR it as the primary path. Tesseract mis-reads small numbers like `1.4115` and decimals often enough to be a headache. The screenshot is there so you can eyeball what the page looked like when a given idea was logged.

Trading Central and Autochartist have different page structures and different idea shapes, so each provider gets its own extractor implementation, but both normalize to the same `TradingIdea` output type below.

---

## 6. Session and Authentication

You log in manually, the bot reuses that session. Two supported modes, selected by `BROWSER_MODE`.

**CDP attach (recommended, matches "after I have already logged in").**
1. Launch Chrome with `--remote-debugging-port=9222`.
2. Log in to IC Markets by hand in that window.
3. The bot attaches with `chromium.connectOverCDP(CDP_ENDPOINT)` and drives the existing tab.

The bot never sees your credentials, and your MFA/session stays intact. Downside: the Chrome window must stay open for the schedule to keep working.

**Persistent context (headless-friendly).**
Use `launchPersistentContext(USER_DATA_DIR, ...)`. Log in once, cookies persist in the profile across runs. Better for a server, but the session can expire and need a manual re-login. Add a login-wall detector (see section 9) so an expired session logs a clear warning instead of scraping an empty page.

Use Playwright. Its frame handling and response interception are cleaner than Puppeteer for this, and it drives real Chrome over CDP the same way.

---

## 7. Scheduling and "New Additions" Detection

**Trigger.** `@nestjs/schedule` `@Cron(process.env.CRON_EXPRESSION)` calls `runAllSources()`. Guard against overlap with a simple `isRunning` boolean, so a slow run does not stack on the next tick.

**What "new" means.** Each card carries its own timestamp (in your screenshot the cards range from 12:10 to 12:14 UTC-5), and the list rolls over time. So on each run you extract everything visible, then filter to ideas you have not seen before.

**Dedup key.** Build a stable hash per idea:

```
hash = sha256(provider + instrument + timeframe + ideaTimestamp + direction + target)
```

Include `target` and `direction` so a genuinely updated idea on the same instrument and timeframe counts as new rather than being swallowed as a duplicate.

**Seen state.** Persist seen hashes to `SEEN_STATE_PATH` (a JSON set) and load it on boot so restarts do not re-log old ideas. Cap the set (for example keep the last N thousand, or prune anything older than a few days) so it does not grow forever.

**Flow per run:**
1. Extract all current ideas for the source.
2. Compute each hash.
3. Drop any hash already in the seen set.
4. Append the remainder to JSONL.
5. Add their hashes to the seen set and persist.

---

## 8. Data Model and Logging

**Normalized idea (both providers map to this):**

```ts
interface TradingIdea {
  provider: 'TRADING_CENTRAL' | 'AUTOCHARTIST';
  instrument: string;        // "USD/CAD"
  timeframe: string;         // "30 MIN"
  direction: 'UP' | 'DOWN' | 'NEUTRAL';
  expectedMovePips?: [number, number]; // [19, 39]
  target?: number;           // 1.4115
  pivot?: number;            // 1.4170
  levels?: { support: number[]; resistance: number[] };
  ideaTimestamp: string;     // ISO, from the card
  capturedAt: string;        // ISO, when the bot saw it
  sourceUrl: string;
  screenshotPath?: string;
  raw?: unknown;             // original JSON payload if intercepted
  hash: string;
}
```

**Logging.** Append one JSON object per line to `IDEAS_LOG_PATH` (JSONL). This is trivial to `tail`, grep, and later stream into Postgres or an analytics job when you decide what to do with the data. Keep `raw` when you got it from network interception, it is the highest-fidelity record and costs almost nothing to store.

---

## 9. Reliability and Edge Cases

- **Login wall / expired session.** Before extracting, check for a known logged-in marker (or absence of a login form). If missing, log a warning, save the screenshot, and skip that source rather than logging garbage.
- **Empty or partial render.** Wait for the widget/iframe and at least one card to be present before extracting, with `NAV_TIMEOUT_MS` as the ceiling. Timeouts log a warning and move on.
- **Overlapping runs.** The `isRunning` guard from section 7.
- **Vendor markup change.** Isolated per-provider selectors keep breakage contained. If network interception is your primary path, DOM changes matter less.
- **Screenshots piling up.** Optional retention job to delete images older than N days.
- **Graceful shutdown.** On SIGINT/SIGTERM, flush the seen state and close (or detach from) the browser.

---

## 10. Suggested Project Structure

```
src/
  main.ts
  app.module.ts
  config/
    config.module.ts
    sources.schema.ts          # validate SOURCES JSON
  browser/
    browser.service.ts         # CDP attach or persistent context
  scraper/
    scraper.service.ts         # orchestrates a run over all sources
    idea-extractor.interface.ts
    extractors/
      trading-central.extractor.ts
      autochartist.extractor.ts
  dedup/
    dedup.service.ts
    seen-store.ts
  logging/
    jsonl-logger.service.ts
  scheduler/
    scheduler.service.ts       # @Cron -> scraper.runAllSources()
  models/
    trading-idea.model.ts
data/                          # gitignored: ideas.jsonl, seen.json, screenshots/
```

---

## 11. Build Phases

**Phase 1, skeleton.**
NestJS app, `@nestjs/config` loading and validating `SOURCES`, `BrowserService` that attaches over CDP and opens a URL, screenshot on demand. Prove you can reach the authenticated page.

**Phase 2, one extractor.**
Implement `TradingCentralExtractor`. Try network interception first, DOM fallback second. Normalize to `TradingIdea`. Print results to console.

**Phase 3, logging and dedup.**
Add `JsonlLoggerService` and `DedupService` with persisted seen state. Confirm re-running does not duplicate.

**Phase 4, scheduling.**
Wire `@Cron` at 15 minutes with the overlap guard. Let it run for a few cycles and watch new ideas append over time.

**Phase 5, second provider.**
Implement `AutochartistExtractor` behind the same interface. Confirm one process handles both sources from the `SOURCES` array.

**Phase 6, hardening.**
Login-wall detection, timeouts, screenshot retention, graceful shutdown, structured logs.

---

## 12. Open Questions to Resolve While Building

- Is the Trading Central widget same-origin or cross-origin in an iframe? Determines whether frame access or interception is smoother.
- What exactly does the vendor research endpoint look like, and does it return all visible ideas in one payload? Confirm via the Network tab before committing to interception.
- CDP attach vs persistent context: are you running this on your own always-on machine (CDP is fine) or a headless server (persistent context, with re-login handling)?
