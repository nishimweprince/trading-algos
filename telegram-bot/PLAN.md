# Telegram Signal Bot — build plan (locked)

This document mirrors the locked product and engineering decisions for `telegram-signal-bot`. The running code lives under `src/`; operational steps are in `README.md`.

## Decisions

- **Runtime:** TypeScript on Node.js 20 LTS.
- **Telegram:** GramJS (`telegram` on npm), user account, session string on disk.
- **HTTP:** Express 4 — `GET /health`, `GET /status`.
- **Channels:** Public usernames in `TELEGRAM_CHANNELS` (comma-separated).
- **Catch-up:** On new channel or long downtime, advance cursor to latest message id without SMS.
- **State:** JSON under `STATE_DIR` (`cursors.json`, `handled.json`, `session.txt`).
- **Logs:** JSONL under `LOGS_DIR` (signals, sms, errors, optional raw).
- **SMS:** Pindo via Axios; no retry; `NOTIFICATION_NUMBERS` matches fu-strategy convention.
- **Parser v1:** Gold / XAU phrases + buy|sell within ~40 chars; ambiguous buy+sell rejected.

## Layout

See repository tree in the original specification; implemented paths match `src/`, `scripts/`, `tests/`, `ecosystem.config.cjs`.

## Environment

Documented in `.env.example` and validated in `src/config.ts` (Zod).

## Polling

Drift-free scheduler in `src/scheduler/pollLoop.ts`. Per-channel serial `getMessages` with `minId` cursor. `FloodWaitError` sleeps `seconds + 1` and skips remainder of tick for that channel after sleep (current implementation processes channels sequentially so later channels in the list may be delayed).

## Stretch (out of scope v1)

Realtime handlers, per-channel parser configs, level extraction, Telegram fallback, multi-instance Redis, ops web UI, stale-channel alerting.
