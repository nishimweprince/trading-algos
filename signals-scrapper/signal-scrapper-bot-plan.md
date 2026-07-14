# Trading Ideas Logger Bot — Current Design

## Runtime flow

The scheduled NestJS process handles configured sources sequentially and guards against overlapping cron runs.

Trading Central:

```text
existing matching CDP tab
  → reload authenticated tab
  → wait for Recognia widget
  → full-page screenshot of active market category
  → reusable English Tesseract.js worker
  → plain + positioned OCR text
  → Ollama Cloud JSON formatting
  → Zod validation
  → JSONL + versioned seen state
```

Autochartist retains its network-response-first extraction with DOM fallback.

The bot never places trades or interacts with the vendor Trade buttons.

## Browser and authentication

Trading Central requires `BROWSER_MODE=CDP`. Chrome must be started with `--remote-debugging-port=9222` and, on Chrome 136+, a non-default `--user-data-dir`. The user logs in once and leaves a tab open whose normalized origin and path match the configured source URL.

The bot selects and reloads only that matching tab. It does not create, repurpose, navigate, or close another tab/window. Missing Chrome and missing-tab conditions return `cdp_unavailable` and `matching_tab_not_found` respectively and are persisted as failed debug runs.

## Trading Central signal contract

OCR output is sent as both plain text and TSV-derived positioned lines so the formatter can keep the three-column card layout separated. The formatter uses the following mapping:

- Unlabelled black chart marker → nullable `entry`
- Pivot → `stopLoss` and compatibility alias `pivot`
- Target → `takeProfit` and compatibility alias `target`
- Expected Move arrow → `UP` or `DOWN`

A valid signal requires instrument, timeframe, non-neutral direction, stop-loss, and take-profit. Entry, idea timestamp, expected move, and secondary support/resistance levels may be absent. The formatter must reject rather than infer incomplete or ambiguous cards.

Ollama Cloud is called directly through `https://ollama.com`, using `OLLAMA_API_KEY` and the configurable `OLLAMA_MODEL` (default `gemma4:31b`, a vision-capable cloud model). Every formatting call attaches the source screenshot alongside the OCR text so the model can cross-reference exact digits/decimals against the image rather than relying on OCR text alone. Model availability and true multimodal support vary per account/tier and are not reliably reflected in the public catalog -- some models tagged "vision" require a paid upgrade or silently ignore attached images; verify with `GET /api/tags` against the configured `OLLAMA_HOST` and a real image-bearing `/api/chat` call before changing the model.

## Deduplication and persistence

When `ideaTimestamp` exists, hashes retain the legacy formula:

```text
sha256(provider|instrument|timeframe|ideaTimestamp|direction|target)
```

When OCR misses the timestamp, the stable fallback hashes provider, instrument, timeframe, direction, stop-loss, and take-profit. Entry is never hashed because the current market price changes between runs.

`ideas.jsonl` receives only new valid signals. `seen.json` version 2 contains:

- The original hash→first-seen map, preserving restart compatibility
- Full normalized signals keyed by hash
- Bounded success/failure run diagnostics, including screenshot path, OCR text/confidence, model response, rejected cards, stage, and error

Hash-only version 1 files migrate automatically. Failed runs and rejected cards never add seen hashes.

## Reliability

- Login walls are screenshotted, recorded, and skipped.
- OCR, Ollama, validation, browser, screenshot, and persistence failures record the failing stage.
- A single Tesseract worker is reused and terminated on shutdown.
- External CDP Chrome is never closed by application shutdown.
- Debug runs and seen signals are capped independently by `DEBUG_RUN_MAX_ENTRIES` and `SEEN_MAX_ENTRIES`.

## Validation

Automated tests cover existing-tab selection, URL normalization, no-tab failures, OCR worker lifecycle and TSV grouping, Ollama validation/repair, strict card rejection, v1→v2 seen migration, missing-timestamp hashing, full OCR orchestration, JSONL deduplication, Autochartist network capture, login walls, timeouts, and scheduler overlap protection.
