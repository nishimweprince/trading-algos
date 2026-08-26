# Phase 3.2–3.3 cutover evidence

Evidence captured on 2026-08-26. No credentials, tokens, API keys, raw account IDs or operation IDs
are recorded here.

## Phase 3.2 gates

| Gate | Evidence | Provenance |
|---|---|---|
| macOS cTrader | Production trading readiness returned ready; the account inventory reconciled two demo accounts and one usable live account; a disabled live account was excluded; XAUUSD returned a current broker quote. | Independently verified during this cutover. |
| Windows MT5 | The unified MT5 adapter was tested against the existing production `signals.db` and reported ready. | Operator-attested manual verification. Host-local artifacts are not accessible from this checkout. |
| Live idempotency | Reposting the identical `signal_id` replayed the stored result without placing a second broker order and left exactly one database record. | Operator-attested manual verification. Host-local database and broker history are not accessible from this checkout. |
| Shadow diff | Backtesting-service shadow payloads matched the recorded pre-migration payloads. | Operator-attested manual verification. Host-local comparison artifact is not accessible from this checkout. |

## Phase 3.3 macOS cutover

- `uv sync --all-packages` completed without dependency changes.
- `.env.production` explicitly selects `ADAPTERS=ctrader`.
- The failed legacy `com.ctrader-markets.production` unit was booted out.
- The manually launched unified process was terminated after its executable and profile were
  verified.
- `com.execution-service.production` was bootstrapped from the repository launchd template and is
  the sole owner of port 8010.
- `/health/trading-ready`, `/v1/accounts` and a real XAUUSD tick passed after supervisor startup.
- The pre-migration cTrader service at baseline `1a0dd73` was restored to a detached rollback
  worktree with a dedicated virtual environment. It passed `--profile production
  --validate-config` while reusing the protected current configuration and token/data directory.

## Phase 3.3 Windows cutover

- The operator confirmed that the Windows host can run the workspace's unified
  `execution-service` with the MT5 adapter after host synchronization.
- The service used the existing production `signals.db`; the host-local file was not copied or
  replaced from this checkout.
- Readiness and identical-signal replay behavior are covered by the operator-attested Phase 3.2
  checks above.
- Windows process-supervisor details and logs remain host-local and were not independently observed
  from macOS.

## Still required before Phase 3.3 completion

- Record the Windows workspace sync and unified-service supervisor cutover from the MT5 host.
- Observe one complete trading session on the new services and record reconnects, rejected
  operations, duplicate executions, database errors and account-routing issues.
- Run the complete verification matrix and determinism gate from `migration-spec.md`.
- Delete `mt5-trader/` only after the complete-session observation and all verification pass.

## Verification matrix

The full matrix was run after the macOS supervisor cutover:

| Member | Result |
|---|---|
| `ta-core` | Ruff clean; 19 tests passed. |
| `ta-contracts` | Ruff clean; 41 tests passed. |
| `ta-store` | Ruff clean; 24 tests passed. |
| `ta-notify` | Ruff clean; 14 tests passed. |
| `ta-clients` | Ruff clean; 34 tests passed. |
| `execution-service` | Ruff clean; 317 tests passed, 7 integration tests deselected. |
| `backtesting-service` | Ruff clean; 578 tests passed, 5 skipped. |
| `notification-service` | 18 tests passed. |
| Docs | Production build passed with 128 routes and 125 indexed content pages. |
| Frozen `mt5-trader` | 101 tests passed, 1 integration test deselected. |

The determinism gate passed with the four hashes recorded in `migration-spec.md` unchanged and 606
trades in total.

## Session monitoring

Observation began when the supervised service completed its startup handshake at
2026-08-26T05:07:51Z. A complete session is defined for this cutover as an uninterrupted 24-hour
window ending no earlier than 2026-08-27T05:07:51Z. Initial state:

- Both broker environments connected with no reconnect.
- Three usable accounts reconciled with full broker access.
- One broker-disabled live account was excluded as designed.
- No error-level startup event, database error, duplicate execution or routing error was observed.
- The existing execution ledger contained one succeeded smoke-test order.

Hourly monitoring is attached to the cutover task. This section must be updated with the complete
session window and anomaly totals before the frozen service is removed.
