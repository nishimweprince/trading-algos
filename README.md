# Trading Algos

## Motivation
This is a long standing initiative to achieve a better technical understanding of the financial markets and to develop a set of trading algorithms that can be used to automate trading in the financial markets.

## Projects

### Active Trading Systems

- **[fu-strategy/](fu-strategy/README.md)** — FastAPI service for Capital.com trading. Uses HTF bias + zones with realtime FU triggers. Dispatches signals via WhatsApp/SMS and supports 1M auto-execution.

- **[vrvp-strategy/](vrvp-strategy/README.md)** — Forex strategy combining Supertrend (4H), StochRSI (1H), FVG, and Volume Profile. Includes FastAPI server with CLI backtest/paper modes and Resend email alerts.

- **[lux-algo/](lux-algo/README.md)** — LuxAlgo Supertrend signal service that polls candles, applies confluence overlays, and submits market orders to `mt5-trader`.

- **[ipda/](ipda/README.md)** — IPDA_Full Pine indicator plus a Python signal service that ports only the IPDA Supertrend×SMA buy/sell entry and submits to `mt5-trader`.

- **[bitcoin9to5/](bitcoin9to5/README.md)** — BTC perpetual futures bot on Nado. Shorts 9:29–16:01 ET, longs overnight/weekends/holidays, with adaptive zone timing and TP-zone trailing stop.

- **[pump-fun/](pump-fun/README.md)** — Automated TypeScript trading system for pump.fun native graduations on Solana. Detects graduations, screens through strict guardrail engine (10 hard checks including mint/freeze authority, LP burn, holder concentration, sellability), enters small scalp positions targeting +50% moves, and exits within ~1 second. Phases 0–6 implemented with paper/dry-run/live modes, Jito primary + RPC fallback execution, pre-signed exit ladders, crash recovery, and operator dashboard at pumpdesk.nishimweprince.dev. Funded live pilot verification remains.

### Signal Detection & Execution

- **[mt5-trader/](mt5-trader/README.md)** — Authenticated FastAPI service that validates and idempotently executes trading signals through a local MetaTrader 5 terminal. Supports forex and Deriv profiles, distance-based SL/TP, and optional notification-service fan-out.

- **[telegram-bot/](telegram-bot/README.md)** — Standalone GramJS user-session poller that detects Gold/XAU buy/sell phrases in public Telegram channels and dispatches Pindo SMS to `NOTIFICATION_NUMBERS`. Features drift-aware polling loop, cold-start cursor management, parser v1 for signal extraction, and Express HTTP server for health/status endpoints. Production-ready with pm2 support.

- **[signals-scrapper/](signals-scrapper/README.md)** — Scheduled NestJS bot that scrapes IC Markets research pages (Trading Central/Autochartist) for trading ideas. Extracts ideas, detects new additions via dedup hashing, and appends to JSONL log. Supports CDP attach mode for authenticated sessions and includes comprehensive test coverage.

- **[telegram-metatrader/](telegram-metatrader/README.md)** — Python Telethon user-session copier that reads one Telegram chat and places fixed-lot MT5 market orders on a local Windows terminal (dry-run by default).

- **[forex-execution/](forex-execution/README.md)** — TypeScript/Fastify OANDA REST-v20 execution service. Phases 1–2 complete: validated configuration, practice/live URL resolution, authenticated OANDA client, normalized broker errors, protected internal HTTP routing, health endpoints, normalized account/instrument APIs, and account/instrument metadata persistence via Prisma/SQLite. Designed for pm2 deployment.

### Strategy Development & Research

- **[lookup-trader/](lookup-trader/README.md)** — Local bar-replay and manual trade labelling tool for building a pattern-based probability database (HistData → Parquet/DuckDB → replay UI → triple-barrier labels → outcome and meta models). Capital.com demo forward shadow only — no order path. Live signals dashboard at [lookup.nishimweprince.dev](https://lookup.nishimweprince.dev).

- **[jesse-strategies/](jesse-strategies/README.md)** — Jesse project template hosting custom strategies (e.g. `TingaTinga`). Provides framework for backtesting and live trading with Jesse's ecosystem.

- **[tinga-tinga/](tinga-tinga/README.md)** — Standalone JavaScript implementation of the Tinga Tinga RSI-crossover strategy against the Binance public API. Lightweight alternative to the Jesse-based version.

### Services

These live under `services/` and are the supported, first-class deployables.
See [ARCHITECTURE.md](ARCHITECTURE.md) for the layout, the shared `ta-*`
packages, and how to add the next one.

- **[services/execution-service/](services/execution-service/README.md)** — Broker-agnostic
  market-data and durable trade-execution gateway. One codebase, one broker adapter per
  host: `ADAPTERS=ctrader` on macOS (port 8010), `ADAPTERS=mt5` on the Windows terminal
  host (8000 forex / 8001 deriv). Merges the former `ctrader-markets` and `mt5-trader`,
  and keeps `POST /v1/signals` byte-compatible for the callers that have not migrated.

- **[services/backtesting-service/](services/backtesting-service/README.md)** — Backtest,
  research and paper-trading service (formerly `session-hedging`). Session-open hedge
  strategy plus the S1–S9 research studies. Strategies register through a plugin registry.

- **[services/notification-service/](services/notification-service/README.md)** — Standalone
  NestJS multi-channel notification API (Telegram, Resend email, Pindo SMS, Meta WhatsApp).
  TypeORM + SQLite delivery log, env-based recipients, optional API-key auth.

- **[apps/docs/](apps/docs/README.md)** — Nextra + MDX documentation site aggregating
  per-strategy documentation.

- **[mt5-trader/](mt5-trader/FROZEN.md)** — Frozen. Merged into `execution-service` as the
  `mt5` adapter; kept runnable until the Windows host cuts over.

### Infrastructure

- **[packages/](ARCHITECTURE.md#shared-packages)** — `ta-core`, `ta-contracts`, `ta-store`,
  `ta-notify`, `ta-clients`: the scaffolding, wire models, execution ledger and clients
  every Python service shares.

- **[infra/launchd/](infra/launchd/README.md)** — launchd plists and the installer.

- **[binance-crypto/](binance-crypto/)** — Scratch workspace for Binance-related experiments
  and prototypes. No formal README yet.

## Quick Reference

### By Market
- **Forex:** fu-strategy, vrvp-strategy, lux-algo, ipda, execution-service, forex-execution, telegram-metatrader, lookup-trader
- **Crypto:** pump-fun, tinga-tinga, binance-crypto, jesse-strategies
- **Futures:** bitcoin9to5

### By Function
- **Signal Detection:** telegram-bot, signals-scrapper, lux-algo, ipda
- **Execution:** execution-service, forex-execution, pump-fun, telegram-metatrader
- **Market Data:** execution-service
- **Notifications:** notification-service
- **Research / Labelling:** lookup-trader
- **Strategy Development:** jesse-strategies, tinga-tinga, fu-strategy, vrvp-strategy, bitcoin9to5
- **Documentation:** apps/docs

## Author

- [Nishimwe Prince](https://www.linkedin.com/in/nishimweprince/)
