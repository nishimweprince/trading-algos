# ta-contracts

Every model that crosses a service boundary.

- `market` — `Timeframe`, `Tick`, `Candle`, `CandlesResponse`, `SymbolInfo`,
  `SymbolsResponse`, `Environment`. The canonical market-data shapes.
- `execution` — the operation model: one idempotent client-identified intent
  fanned out across accounts, each target settling independently.
- `signals` — the legacy `POST /v1/signals` contract that lux-algo, ipda,
  signals-scrapper and lookup-trader still speak, plus the `Legacy*` market
  shapes mt5-trader returns, which are genuinely different from the canonical
  ones and must stay that way on the legacy path.

`Candle` previously existed as three field-identical copies (ctrader-markets,
session-hedging, lookup-trader), each documented as mirroring the others. Drift
between them is silent and corrupts backtests; this package is the fix.
