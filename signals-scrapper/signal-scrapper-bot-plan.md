# Trading Ideas Logger Bot — Current Design

## Runtime flow

The scheduled NestJS process handles configured sources sequentially and guards against overlapping cron runs.

Trading Central:

```text
existing matching CDP tab
  → reload authenticated tab
  → wait for Recognia widget
  → full-page screenshot of active market category
  → OpenAI Responses API vision extraction
  → Zod Structured Outputs + local validation
  → JSONL + versioned seen state
```

Autochartist retains its network-response-first extraction with DOM fallback.

The bot never places trades or interacts with the vendor Trade buttons.

## Browser and authentication

Trading Central requires `BROWSER_MODE=CDP`. Chrome must be started with `--remote-debugging-port=9222` and, on Chrome 136+, a non-default `--user-data-dir`. The user logs in once and leaves a tab open whose normalized origin and path match the configured source URL.

The bot selects and reloads only that matching tab. It does not create, repurpose, navigate, or close another tab/window. Missing Chrome and missing-tab conditions return `cdp_unavailable` and `matching_tab_not_found` respectively and are persisted as failed debug runs.

## Trading Central signal contract

The screenshot is sent directly to OpenAI at original image detail. Structured Outputs keep the extraction contract typed, and local validation uses the following mapping:

- Unlabelled black chart marker → nullable `entry`
- Pivot → `stopLoss` and compatibility alias `pivot`
- Target → `takeProfit` and compatibility alias `target`
- Expected Move arrow → `UP` or `DOWN`

A valid signal requires instrument, timeframe, non-neutral direction, stop-loss, and take-profit. Entry, idea timestamp, expected move, and secondary support/resistance levels may be absent. The formatter must reject rather than infer incomplete or ambiguous cards.

OpenAI is configured with `OPENAI_API_KEY`, `OPENAI_MODEL` (default `gpt-5.6-luna`), and `OPENAI_TIMEOUT_MS`. The response schema permits nullable image-derived fields so unreadable values are rejected deterministically instead of invented.

## Deduplication and persistence

When `ideaTimestamp` exists, hashes retain the legacy formula:

```text
sha256(provider|instrument|timeframe|ideaTimestamp|direction|target)
```

When vision extraction misses the timestamp, the stable fallback hashes provider, instrument, timeframe, direction, stop-loss, and take-profit. Entry is never hashed because the current market price changes between runs.

`ideas.jsonl` receives only new valid signals. `seen.json` version 3 contains:

- The original hash→first-seen map, preserving restart compatibility
- Full normalized signals keyed by hash
- Bounded success/failure run diagnostics, including screenshot path, OpenAI model response, rejected cards, stage, and error

Older state files migrate automatically. Failed runs and rejected cards never add seen hashes.

## Reliability

- Login walls are screenshotted, recorded, and skipped.
- OpenAI, validation, browser, screenshot, and persistence failures record the failing stage.
- External CDP Chrome is never closed by application shutdown.
- Debug runs and seen signals are capped independently by `DEBUG_RUN_MAX_ENTRIES` and `SEEN_MAX_ENTRIES`.

## Validation

Automated tests cover existing-tab selection, URL normalization, no-tab failures, OpenAI image request construction, Structured Outputs, strict card rejection, legacy seen-state migration, missing-timestamp hashing, full vision orchestration, JSONL deduplication, Autochartist network capture, login walls, timeouts, and scheduler overlap protection.
