# MT5 OCO implementation status

## Profile and candle-feed follow-up

The user subsequently requested renaming **backtesting-service** `.env.hfm` to
`.env.forex`, while keeping HFM as the broker. The execution-service profile stays
`hfm`. The renamed profile now explicitly uses `MARKET_DATA_PROVIDER=mt5`, exact
symbol `XAUUSDb`, and server UTC offset `10800`; cTrader is no longer required for
its paper loop. The running HFM gateway was restarted to load the corrected symbol
manifest. This supersedes the earlier rollout instructions' backtesting filename.

The actual CLI startup test used `--profile forex`, shadow execution, and isolated
paper state. `/health/live` and `/health/ready` returned 200, and the paper loop
warmed with HFM H1 candles. No orders were sent. See
[startup evidence](reports/hfm-forex-startup.json). Start the configured service with
`uv run backtesting-service --profile forex`.

Bounded broker verification now distinguishes the gateway and strategy profiles:
pass `--profile hfm --strategy-profile forex` to `scripts/verify_mt5_oco.py`.

## Original OCO verification

Implemented the [plan](MT5_OCO_IMPLEMENTATION_PLAN.md), including both explicit
execution paths. Active backtesting-service and execution-service `.env.hfm`
files were not changed. Existing execution-service processes were not restarted.

## Result

- `local_market` preserves OCO strategy staging and dispatches one protected market
  entry when the closed-bar engine observes the chosen trigger. Stale observations
  are skipped; durable dispatch intent and an event outbox prevent duplicate entries.
- `broker_pending` submits a durable two-leg group to the authenticated MT5 OCO API.
  The gateway monitors fills independently of candle polling, cancels the sibling and
  partial-fill remainder, confirms protection against the actual fill, and recovers
  from live inventory and history. Unknown outcomes and incidents block new groups.
- Broker lifecycle, resting orders, fills, volume, protection, cancellations and
  paper prediction differences are exposed through `/v1/execution` and the client.
  Actual group accounting is separate from model P&L; a paper exit retains broker
  tracking. Netting, trailing/partial strategy exits, BE management and time exits
  remain outside the first-release contract.
- HFM gold spot maps to exact broker symbol `XAUUSDb`; strategy candles retain
  `XAUUSD`. The gateway keeps UTC deadlines and converts broker expiration using
  an explicit, fresh-quote-validated server offset. HFM currently uses `10800`.

## Authorized live verification — 2026-09-15

The user authorized small test sizes on the configured live account. This replaced
the plan's proposed demo-account gate. Verification used **0.01 lot**, account
`261017367`, server `HFMarketsGlobal-Live20`, USD currency, hedge mode
(`margin_mode=2`) and `XAUUSDb` specified expiry support (`expiration_mode=15`).

| Check | Observed result |
| --- | --- |
| Long fill | Intended winner; sibling terminal; actual-fill SL/TP confirmed; cancellation latency 0.739 s. |
| Short fill | Intended winner; sibling terminal; actual-fill SL/TP confirmed; cancellation latency 0.695 s. |
| No-fill expiry | Both pending legs expired and no owned exposure remained. |
| Disconnect and coordinator recovery | Unavailability detected; persisted group restored; no duplicate entry submissions. |
| Fresh Python process recovery | Both resting tickets recovered from SQLite; zero new entries; subsequent cleanup confirmed terminal state. |

Final owned orders and positions were empty after both verification runs. The two
filled test positions were explicitly closed; recorded realized net P&L, including
broker charges, was **-$0.38 total**. These are observed examples, not latency guarantees.
Sub-minute expiry requests were rejected with retcode `10022`; 120-second
deadlines were accepted. Rejections produced no test exposure.

Evidence: [full live report](reports/mt5-oco-verification.json),
[process restart report](reports/mt5-oco-restart-verification.json).
The child recovery snapshot intentionally records the two orders while they were
resting; the parent restart report records their completed cleanup.

## Validation

- Backtesting suite: **631 passed, 5 skipped**, with one existing active-config
  assertion deselected. That assertion expects 13:00 UTC excluded, while the user's
  current `.env` does not exclude it. Tests used `PYTHONUTF8=1` for Windows text decoding.
- MT5 adapter, legacy signal, OCO lifecycle and API tests: **96 passed** after the
  final ledger resource-management and protection-confirmation changes.
- Shared-contract suite: **41 passed**, including unchanged signal serialization.
- Client: TypeScript check and production build passed; **33 tests passed**.
- Ruff passed for backtesting source/tests/verification script and the changed
  execution-service files.
- Broad execution-service regression run: **342 passed, 5 failed, 7 integration
  tests deselected**. Existing failures are the relative-path expectation for shared
  `resolve_env_file`, two template tests that ignore `AliasChoices` when checking
  `MAXIMUM_DEVIATION_POINTS`, and two POSIX `0600` assertions on Windows. The HFM
  symbol expectation was updated to the verified broker symbol. No OCO lifecycle
  test fails. See [lifecycle test results](reports/mt5-oco-lifecycle-tests.xml) and
  [broad gateway test results](reports/mt5-oco-gateway-tests.xml).

Shadow mode was verified to record the bracket without gateway calls. A separate
test verifies that broker lifecycle changes are persisted when no new candles arrive.

## Deliberate rollout settings

Set these in the backtesting HFM deployment:

```dotenv
ENTRY_MODE=oco_bracket
MT5_OCO_EXECUTION=broker_pending
MT5_EXECUTION_SYMBOL=XAUUSDb
TP_MODE=fixed_r
BE_TRIGGER_R=0
TIME_EXIT_MODE=none
```

Set these in the matching execution gateway:

```dotenv
MT5_OCO_ENABLED=true
MT5_OCO_POLL_SECONDS=0.25
MT5_OCO_SERVER_UTC_OFFSET_SECONDS=10800
```

Reverify the server offset after seasonal clock changes. Preserve existing API
keys, profile/account identity, broker volume and trading gates. Reconcile owned
exposure before restarting deployments or changing paths. Start with shadow mode,
then verify gateway readiness before enabling the strategy loop. Feature defaults
continue to reject MT5 OCO unless a path is explicitly selected; the original
startup error therefore remains actionable until the profile is deliberately updated.

See [gateway operations and recovery](../execution-service/docs/mt5-oco.md).
