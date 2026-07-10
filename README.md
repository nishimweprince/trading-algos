# Trading Algos

## Motivation
This is a long standing initiative to achieve a better technical understanding of the financial markets and to develop a set of trading algorithms that can be used to automate trading in the financial markets.

## Projects

### Active Trading Systems

- **[fu-strategy/](fu-strategy/README.md)** — FastAPI service for Capital.com trading. Uses HTF bias + zones with realtime FU triggers. Dispatches signals via WhatsApp/SMS and supports 1M auto-execution.

- **[vrvp-strategy/](vrvp-strategy/README.md)** — Forex strategy combining Supertrend (4H), StochRSI (1H), FVG, and Volume Profile. Includes FastAPI server with CLI backtest/paper modes and Resend email alerts.

- **[bitcoin9to5/](bitcoin9to5/README.md)** — BTC perpetual futures bot on Nado. Shorts 9:29–16:01 ET, longs overnight/weekends/holidays, with adaptive zone timing and TP-zone trailing stop.

- **[pump-fun/](pump-fun/README.md)** — Automated TypeScript trading system for pump.fun native graduations on Solana. Detects graduations, screens through strict guardrail engine (10 hard checks including mint/freeze authority, LP burn, holder concentration, sellability), enters small scalp positions targeting +50% moves, and exits within ~1 second. Phases 0–6 implemented with paper/dry-run/live modes, Jito primary + RPC fallback execution, pre-signed exit ladders, crash recovery, and operator dashboard at pumpdesk.nishimweprince.dev. Funded live pilot verification remains.

### Signal Detection & Execution

- **[telegram-bot/](telegram-bot/README.md)** — Standalone GramJS user-session poller that detects Gold/XAU buy/sell phrases in public Telegram channels and dispatches Pindo SMS to `NOTIFICATION_NUMBERS`. Features drift-aware polling loop, cold-start cursor management, parser v1 for signal extraction, and Express HTTP server for health/status endpoints. Production-ready with pm2 support.

- **[signals-scrapper/](signals-scrapper/README.md)** — Scheduled NestJS bot that scrapes IC Markets research pages (Trading Central/Autochartist) for trading ideas. Extracts ideas, detects new additions via dedup hashing, and appends to JSONL log. Supports CDP attach mode for authenticated sessions and includes comprehensive test coverage.

- **[forex-execution/](forex-execution/README.md)** — TypeScript/Fastify OANDA REST-v20 execution service. Phases 1–2 complete: validated configuration, practice/live URL resolution, authenticated OANDA client, normalized broker errors, protected internal HTTP routing, health endpoints, normalized account/instrument APIs, and account/instrument metadata persistence via Prisma/SQLite. Designed for pm2 deployment.

### Strategy Development & Backtesting

- **[jesse-strategies/](jesse-strategies/README.md)** — Jesse project template hosting custom strategies (e.g. `TingaTinga`). Provides framework for backtesting and live trading with Jesse's ecosystem.

- **[tinga-tinga/](tinga-tinga/README.md)** — Standalone JavaScript implementation of the Tinga Tinga RSI-crossover strategy against the Binance public API. Lightweight alternative to the Jesse-based version.

### Infrastructure & Documentation

- **[docs/](docs/README.md)** — Nextra + MDX documentation site that aggregates per-strategy documentation. Provides unified documentation portal for all trading systems.

- **[binance-crypto/](binance-crypto/)** — Scratch workspace for Binance-related experiments and prototypes. No formal README yet.

- **[telegram-metatrader/](telegram-metatrader/)** — Python-based integration between Telegram and MetaTrader platforms. Implementation details TBD.

- **[tradingview-mcp-jackson/](tradingview-mcp-jackson/)** — TradingView integration project with MCP (Model Context Protocol) support. Includes agents, skills, and safety checks for automated trading workflows.

## Quick Reference

### By Market
- **Forex:** fu-strategy, vrvp-strategy, forex-execution, telegram-metatrader
- **Crypto:** pump-fun, tinga-tinga, binance-crypto, jesse-strategies
- **Futures:** bitcoin9to5

### By Function
- **Signal Detection:** telegram-bot, signals-scrapper
- **Execution:** forex-execution, pump-fun
- **Strategy Development:** jesse-strategies, tinga-tinga
- **Documentation:** docs

## Author

- [Nishimwe Prince](https://www.linkedin.com/in/nishimweprince/)