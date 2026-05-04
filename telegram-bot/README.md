# Telegram Signal Bot

Standalone TypeScript service that polls public Telegram channels with a **user** GramJS session, detects compact trading signals (for example Gold buy/sell), and fans out SMS via [Pindo](https://pindo.io/) to every number in `NOTIFICATION_NUMBERS` (same convention as `fu-strategy`).

This package does **not** import or run `fu-strategy`; it only mirrors notification env naming for operators.

## Requirements

- Node.js 20+
- Telegram API id/hash from [my.telegram.org](https://my.telegram.org)
- Pindo API token and approved sender id

## Quick start

```bash
cd trading-algos/telegram-bot
npm install
cp .env.example .env
# Edit .env: TELEGRAM_*, TELEGRAM_CHANNELS, PINDO_*, NOTIFICATION_NUMBERS

npm run login          # interactive; writes TELEGRAM_SESSION_PATH
npm run verify         # optional: list dialogs and resolve channel usernames
npm run build
npm start              # or: pm2 start ecosystem.config.cjs
```

Health and status (default bind `127.0.0.1:8081`):

- `GET /health` → `{ "status": "ok" }`
- `GET /status` → uptime, Telegram connected flag, per-channel cursors, counters

## Logs and state

Directories `./state/` and `./logs/` are created at startup (gitignored).

| Path | Purpose |
| --- | --- |
| `state/session.txt` | GramJS session string (secret) |
| `state/cursors.json` | Last processed Telegram message id per channel |
| `state/handled.json` | Recent `(channelId, messageId)` dedup set |
| `logs/signals.jsonl` | Each parsed signal |
| `logs/sms.jsonl` | Each Pindo attempt |
| `logs/errors.jsonl` | Parser ambiguity, Telegram errors, SMS failures |
| `logs/raw.jsonl` | Optional raw polled text when `LOG_RAW_MESSAGES=true` |

## Behaviour notes

- **Cold start:** the first time a channel appears, the service advances the cursor to the latest message id **without** sending SMS for that message (stale signals are skipped).
- **SMS:** one GSM segment target (~150 chars), branded with `PINDO_SENDER_ID`. No retry on failure (time-sensitive).
- **Singleton:** run one process (`pm2` `fork`, one instance). Multiple instances would need shared state.

See `PLAN.md` for the full design decisions and failure-mode table.
