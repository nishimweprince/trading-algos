# Architecture

## Layout

```
services/     deployable services
packages/     shared Python libraries (ta-*)
apps/         docs site
infra/        deployment templates
```

Everything still at the top level (`ipda`, `lux-algo`, `fu-strategy`,
`vrvp-strategy`, `lookup-trader`, `telegram-bot`, …) is unmigrated and keeps its
own virtualenv. It is not part of the uv workspace.

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

Implement `execution_service.ports.BrokerAdapter` under
`src/execution_service/adapters/<broker>/`, and add the name to the `known` set
in `Settings.validate_adapter_requirements` along with whatever configuration it
requires. Import the broker SDK lazily — the MT5 adapter does, which is why a
macOS install never touches the Windows-only `MetaTrader5` package.

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
