# Trading Algos

## Motivation
This is a long standing initiative to achieve a better technical understanding of the financial markets and to develop a set of trading algorithms that can be used to automate trading in the financial markets.

## Projects

- [fu-strategy/](fu-strategy/README.md) — FastAPI service, Capital.com, HTF bias + zones, realtime FU triggers, WhatsApp/SMS dispatch, 1M auto-execution.

- [telegram-bot/](telegram-bot/README.md) — **new** standalone GramJS user-session poller; detects Gold / XAU buy/sell phrases in public Telegram channels and fans out Pindo SMS to `NOTIFICATION_NUMBERS`. Reuses the `NOTIFICATION_NUMBERS` env naming from `fu-strategy` but does not import `fu-strategy` at runtime.

- [vrvp-strategy/](vrvp-strategy/README.md) — Forex strategy combining Supertrend (4H), StochRSI (1H), FVG, and Volume Profile; FastAPI server + CLI backtest / paper modes; Resend email alerts.

- [jesse-strategies/](jesse-strategies/README.md) — Jesse project template hosting custom strategies (e.g. `TingaTinga`).

- [tinga-tinga/](tinga-tinga/README.md) — Standalone JS implementation of the Tinga Tinga RSI-crossover strategy against the Binance public API.

- [bitcoin9to5/](bitcoin9to5/README.md) — BTC perpetual futures bot on Nado: short 9:29–16:01 ET, long overnight/weekends/holidays, with adaptive zone timing and a TP-zone trailing stop.

- [binance-crypto/](binance-crypto/) — Scratch workspace (no README yet).

- [docs/](docs/README.md) — Nextra + MDX documentation site that aggregates per-strategy docs.

## Telegram Signal Bot (new)

Implementation lives under [telegram-bot/](telegram-bot/README.md). Quick start, env vars, and log paths are documented there and in [telegram-bot/PLAN.md](telegram-bot/PLAN.md).

- **Runtime:** Node.js 20+, TypeScript, GramJS (`telegram` on npm) with a persisted user session at `TELEGRAM_SESSION_PATH` (created by `npm run login`).
- **Polling:** Drift-aware loop in `telegram-bot/src/scheduler/pollLoop.ts`; `POLL_INTERVAL_SECONDS` (default 60s); per-channel `getMessages` with `minId` from `cursors.json`; `POLL_MAX_MESSAGES_PER_CHANNEL` caps batch size.
- **Cold start:** The first time a channel is seen, the cursor jumps to the latest message id **without** sending SMS, so backlog messages are not replayed as live signals.
- **Parser v1:** Gold / XAU phrasing plus `buy` or `sell` within a short window (~40 characters); messages that imply both buy and sell are treated as ambiguous and skipped.
- **SMS:** Pindo HTTP API via Axios; messages targeted to roughly one GSM segment (~150 characters) with `PINDO_SENDER_ID`; no automatic retry on failure (time-sensitive).
- **State:** `STATE_DIR` holds `cursors.json`, `handled.json` (recent dedupe), and the session file.
- **Logs:** `LOGS_DIR` JSONL for parsed signals, SMS attempts, errors, and optional raw polled text when `LOG_RAW_MESSAGES=true`.
- **HTTP:** Express on `HTTP_HOST`:`HTTP_PORT` (defaults `127.0.0.1:8081`) — `GET /health`, `GET /status` (uptime, Telegram connection, cursors, counters).
- **Ops:** `npm run login`, `npm run verify`, `npm run build`, `npm start`; production-friendly `pm2 start telegram-bot/ecosystem.config.cjs`. Run a **single** process unless you add shared state for cursors and dedupe.

## Author

- [Nishimwe Prince](https://www.linkedin.com/in/nishimweprince/)
