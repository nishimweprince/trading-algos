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
| `services/execution-service` | Python / FastAPI | 8010 (cTrader) · 8000/8001 (MT5) | One codebase, adapter chosen by `ADAPTERS` |
| `services/backtesting-service` | Python / FastAPI | 8012 | Backtests, research studies, paper trading |

`execution-service` runs three instances from one codebase:

| Host | `ADAPTERS` | Port | Replaces |
|---|---|---|---|
| macOS | `ctrader` | 8010 | ctrader-markets |
| Windows | `mt5` (forex profile) | 8000 | mt5-trader forex |
| Windows | `mt5` (deriv profile) | 8001 | mt5-trader deriv |

Ports 8000, 8001 and 8010 are unchanged on purpose: `lux-algo`, `ipda`,
`signals-scrapper` and `lookup-trader` point at them and have not migrated.

## Shared packages

| Package | Owns |
|---|---|
| `ta-core` | `ServiceError`, JSON logging + JSONL sink, settings base, FastAPI app factory, CLI bootstrap |
| `ta-contracts` | Every model that crosses a service boundary |
| `ta-store` | The durable idempotency and execution-event ledger |
| `ta-notify` | The notification-service client |
| `ta-clients` | Typed clients for our own services |
| `ta-plugin-api` | Provider discovery (`load_providers`), `MarketDataHub`, `SymbolResolutionError` |

## Plugins

| Plugin | Entry points | Owns |
|---|---|---|
| `plugins/ctrader` (`ta-plugin-ctrader`) | `ta.execution: ctrader` | protobuf wire stack, OAuth token rotation, account registry, `CTraderSession`/`CTraderGateway` |
| `plugins/mt5` (`ta-plugin-mt5`) | `ta.execution: mt5` | `MT5Adapter` terminal seam, `RealMT5Adapter` (Windows, `terminal` extra), symbol manifest |

Services choose a broker by name through `ta_plugin_api.load_providers`, never
by importing a plugin. Discovery fails closed: a configured provider that is
missing, published twice or fails to import stops the service from starting.
Each plugin ships a `testing` module (`FakeCTraderServer`, `FakeMT5Adapter`) for
consumers' tests. Plugin settings are structural protocols, so any service's
settings object with the right attributes can drive them.

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

## Adding a broker

1. Create `plugins/<broker>/` as `ta-plugin-<broker>`, laid out like a package
   (`src/ta_plugin_<broker>/`, ruff `extend`, `workspace = true` sources). The
   `plugins/*` glob makes it a workspace member.
2. Export a `FACTORY` satisfying `ta_plugin_api.ProviderFactory`: `name` equal to
   the entry-point name, and `missing_settings(settings)` returning the
   environment names it requires and lacks.
3. Publish it under `[project.entry-points."ta.execution"]`. Operators then
   enable it with `ADAPTERS=<broker>`, and `Settings.validate_adapter_requirements`
   picks it up with no edit.
4. Import the broker SDK lazily, behind an optional extra. The MT5 plugin does,
   which is why a macOS install never touches the Windows-only `MetaTrader5`.
5. Ship a `testing` module with a fake, and add a caller to
   `.github/workflows/plugins-ci.yml`.

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
