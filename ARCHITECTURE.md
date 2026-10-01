# Architecture

## Layout

```
services/     deployable services
packages/     shared Python libraries (ta-*)
plugins/      broker and exchange providers (ta-plugin-*), discovered by entry point
apps/         docs site
infra/        deployment templates
```

Everything still at the top level (`fu-strategy`, `vrvp-strategy`,
`telegram-bot`, …) is unmigrated and keeps its own virtualenv. It is not part of
the uv workspace.

Two exceptions, added during §3.5 of the migration: `ipda` and
`lookup-trader/server` are workspace members, because they consume `ta-core` and
`ta-notify` and `workspace = true` sources only resolve for members. They are
still top-level projects and still own their own deployment; membership buys
them dependency resolution, not a move. (`lux-algo` was the third, until the
project was deleted.)

## Services

| Service | Language | Port | Notes |
|---|---|---|---|
| `services/notification-service` | TypeScript / NestJS | 3010 | Telegram, email, SMS, WhatsApp |
| `services/execution-service` | Python / FastAPI | 8010 (cTrader) · 8000/8001 (MT5) | Orders only; broker chosen by `ADAPTERS` |
| `services/market-data-service` | Python / FastAPI | 8020 (cTrader + Binance) · 8021–8023 (MT5) | Quotes, candles, streams per market |
| `services/backtesting-service` | Python / FastAPI | 8012 | Backtests, research studies, paper trading |

`execution-service` runs three instances from one codebase:

| Host | `ADAPTERS` | Port | Replaces |
|---|---|---|---|
| macOS | `ctrader` (production) | 8010 | ctrader-markets |
| Windows | `mt5` (hfm profile) | 8000 | mt5-trader forex |
| Windows | `mt5` (ftmo profile) | 8001 | mt5-trader deriv |

`market-data-service` runs one process per cTrader host and one per MT5
terminal, because the MetaTrader5 package attaches a process to one terminal:

| Host | Profile | Port | Markets |
|---|---|---|---|
| macOS | `ctrader` | 8020 | `forex`, `deriv` from cTrader accounts; `crypto` from Binance |
| Windows | `hfm` | 8021 | `forex` |
| Windows | `ftmo` | 8022 | `forex` |
| Windows | `deriv` | 8023 | `deriv` (Deriv MT5 terminal) |

Execution ports 8000, 8001 and 8010 are unchanged on purpose: `ipda`,
`signals-scrapper` and `lookup-trader` send orders there. Market data is never
read from them: execution-service returns 404 for the old data routes.

## Shared packages

| Package | Owns |
|---|---|
| `ta-core` | `ServiceError`, JSON logging + JSONL sink, settings base, FastAPI app factory, CLI bootstrap |
| `ta-contracts` | Every model that crosses a service boundary |
| `ta-store` | The durable idempotency and execution-event ledger |
| `ta-notify` | The notification-service client |
| `ta-clients` | Typed clients for our own services |
| `ta-market-data-js` | The TypeScript market-data-service client (`@trading-algos/market-data`); npm, not a uv member |
| `ta-plugin-api` | Provider discovery (`load_providers`), the `MarketDataProvider`, `ExecutionProvider` and `OcoVenue` protocols, `MarketDataHub`, `SymbolResolutionError` |

## Plugins

| Plugin | Entry points | Owns |
|---|---|---|
| `plugins/ctrader` (`ta-plugin-ctrader`) | `ta.execution`, `ta.market_data`: `ctrader` | protobuf wire stack, OAuth token rotation, account registry, `CTraderGateway`, `CTraderExecution`, `CTraderMarketData` |
| `plugins/mt5` (`ta-plugin-mt5`) | `ta.execution`, `ta.market_data`: `mt5` | `MT5Adapter` terminal seam, `RealMT5Adapter` (Windows, `terminal` extra), symbol manifest, `MT5Execution` (order policy for `/v1/orders` and `/v1/signals`), `MT5Oco` (OCO venue), `MT5MarketData` |
| `plugins/binance` (`ta-plugin-binance`) | `ta.market_data`: `binance` | Binance Spot public REST and `bookTicker` WebSocket, request-weight limiter, `BinanceMarketData` |

Services choose a broker by name through `ta_plugin_api.load_providers` and
never construct a provider, gateway or terminal themselves; they may import a
plugin's settings mixin, error types and type names. `infra/check_plugin_boundary.py`
enforces that in CI (the `boundary` job of `plugins-ci.yml`). Discovery fails closed: a configured provider that is
missing, published twice or fails to import stops the service from starting.
Each plugin ships a `testing` module (`FakeCTraderServer`, `FakeMT5Adapter`) for
consumers' tests. Each also ships a settings mixin (`CTraderSettingsMixin`,
`MT5TerminalSettingsMixin`) so every service binds the same environment names;
the plugins themselves read settings structurally.

A market-data plugin implements `ta_plugin_api.MarketDataProvider` and serves
one or more *feeds* (a cTrader account alias; `None` for an MT5 terminal). It
returns `MarketQuote` and closed `Candle`s stamped at their UTC interval end,
and raises `ServiceError` with the codes listed in `ta_plugin_api.market_data`.
`ta_plugin_api.testing.assert_closed_utc_candles` pins the bar contract.

An execution plugin implements `ta_plugin_api.ExecutionProvider` and serves one
or more account aliases (cTrader: every registry alias; MT5: the process
profile). execution-service runs every operation through one `ExecutionService`
on the ta-store ledger: it reserves the operation, asks each target's provider
to `prepare` (broker validation, before any ledger row exists) and `dispatch`,
and writes the returned `TargetOutcome`. Event-driven providers settle later
events through the `LedgerPort` they are given. Generic gates — source
allowlist, freshness, `TRADING_ENABLED` — stay in the service.

OCO groups follow the same split. execution-service's `OcoCoordinator` owns the
group document, idempotency, the leg and group state machines and incident
handling; a plugin that can host groups publishes a `ta_plugin_api.OcoVenue`
(today only `MT5Oco`) that sends leg orders and turns its own inventory into
the facts the coordinator decides on. Groups live in the ledger database.

Three contracts in here are load-bearing and should not be "tidied":

- **`ta-notify.Notifier.send` never raises.** A notification failure must not
  propagate into a trading path.
- **`ta-clients.ExecutionClient` returns `UNKNOWN`, never `REJECTED`, on a
  transport failure.** The order may have reached the broker; the caller
  reconciles rather than resubmits. This is the deliberate opposite of the rule
  above.
- **`OPERATION_NAMESPACE` and `SignalRequest.canonical_json` are frozen.** Both
  feed idempotency hashes checked against live databases. Changing either
  re-executes already-filled orders.

## Adding a service

1. `mkdir -p services/<name>/src/<name_underscored> services/<name>/tests`.
2. Write `pyproject.toml`. Depend on `ta-core` and `ta-contracts`, add
   `[tool.uv.sources]` entries marking them `workspace = true`, and inherit lint
   settings with `[tool.ruff] extend = "../../pyproject.toml"`. Build with
   `packages = ["src/<name_underscored>"]` — **not** `sources = ["src"]`, which
   installs modules at top level where `config` and `models` collide with every
   other member.
3. Subclass `ta_core.BaseServiceSettings`. Override `port`; add
   `ta_notify.NotificationSettings` if the service notifies.
4. Build the app with `ta_core.create_base_app`, which wires the API-key
   dependency, both exception handlers and `/health/live` + `/health/ready`.
   Register only your own routes.
5. Entry point: `ta_core.base_parser`, `load_or_exit`, `serve`.
6. Add an ~8-line CI caller (see `.github/workflows/execution-service-ci.yml`).
7. Add a launchd plist from `infra/launchd/`, and record the port above.

`uv sync --package <name> --group dev` picks it up; the workspace globs
`services/*`, so nothing else needs editing.

## Adding a plugin

1. Create `plugins/<name>/` as `ta-plugin-<name>` (a broker or an exchange),
   laid out like a package (`src/ta_plugin_<name>/`, ruff `extend`, `workspace = true` sources). The
   `plugins/*` glob makes it a workspace member.
2. Export a `FACTORY` satisfying `ta_plugin_api.ProviderFactory`: `name` equal to
   the entry-point name, and `missing_settings(settings)` returning the
   environment names it requires and lacks. For execution, add
   `execution(settings)` returning an `ExecutionProvider`; for market data,
   `market_data(settings)` returning a `MarketDataProvider`.
3. Publish it under `[project.entry-points."ta.execution"]` and/or
   `"ta.market_data"`. Operators enable execution with `ADAPTERS=<broker>`, and
   market data by naming it in a market-data profile's markets file; neither
   service needs an edit. A market-data provider also needs adding to the
   allowed set in `market_data_service/markets/<market>.py`.
4. Import the broker SDK lazily, behind an optional extra. The MT5 plugin does,
   which is why a macOS install never touches the Windows-only `MetaTrader5`.
5. Ship a settings mixin for its environment names, a `testing` module with a
   fake, and a caller in `.github/workflows/plugins-ci.yml`. Add the class
   names a service must not construct to `infra/check_plugin_boundary.py`.
6. Run the conformance kit: in `tests/test_conformance.py`, subclass
   `ta_plugin_api.testing.MarketDataConformance` and/or `ExecutionConformance`
   and supply the fixtures their docstrings list from your fakes. They pin
   what consumers rely on and cannot check themselves: closed bars stamped at
   UTC interval ends, `to` honoured, unknown symbols and timeframes as
   structured 422s, case-insensitive symbol resolution, uncrossed quotes,
   deterministic client order IDs that fit the broker, and `KeyError` for an
   unknown account.

### What each provider does not do, by design

| Provider | Not supported | Instead |
|---|---|---|
| cTrader | OCO groups (`/v1/oco*` answers 501 `oco_not_supported`) | Bracket with stop loss and take profit on one order |
| Binance | Execution; it publishes `ta.market_data` only | Market data for the `crypto` market |
| MT5 | Trailing stops (422 `trailing_stop_not_supported`) | They run in the terminal, not through the trade API |
| MT5 | Changing a pending order's volume (422 `amend_volume_not_supported`) | Cancel and place a new order |

## Adding a strategy

Register a `backtesting_service.registry.StrategyPlugin` under the
`ta.strategies` entry-point group. Callers select it with `strategy` on
`BacktestRequest`. Built-ins win over plugins, and a plugin that fails to import
is skipped rather than taking the service down.

## Changing the backtest engine

Run the determinism gate before and after:

```bash
uv run --package backtesting-service python services/backtesting-service/scripts/determinism_gate.py services/backtesting-service package
```

Four backtests over the committed XAUUSD candles must hash identically. This is
what makes splitting a 3,800-line engine a checkable operation rather than a
hopeful one.
