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

- The operator confirmed that the Windows workspace synchronization and supervisor cutover are
  complete and that the host runs the unified `execution-service` with `ADAPTERS=mt5`.
- The service used the existing production `signals.db`; the host-local file was not copied or
  replaced, recreated or migrated to an empty database.
- Readiness, a current market-data tick and identical-signal replay behavior are covered by the
  operator-attested Phase 3.2 checks above. The real signal was not repeated during cutover.
- Windows process-supervisor details and logs are operator-attested because they remain host-local
  and were not independently observed from macOS. Their inaccessibility is not a cutover blocker.

## Rollback preservation

- The detached rollback worktree remains at
  `/Users/nishimweprince/Documents/Markets/Apps/trading-algos-rollback-1a0dd73` on baseline commit
  `1a0dd731348eb78840a990c70bce852f91088be8`.
- Its dedicated `ctrader-markets/.venv/bin/ctrader-markets` binary exists. A non-connecting
  `--profile production --validate-config` run passed. The baseline validator sees the two
  statically enabled demo entries, which is the baseline service's fail-closed registry behavior.
- `.env.production` is a symlink to the protected unified-service production environment file and
  `data` is a symlink to the protected unified-service data directory. The rollback therefore
  reuses the current rotating token cache and execution ledger; it does not copy, rotate or fork
  either one.
- The baseline plist's historical checkout paths are intentionally not used. A path-correct,
  validated rollback plist is staged at
  `/Users/nishimweprince/Documents/Markets/Apps/trading-algos-rollback-1a0dd73/ctrader-markets/ops/com.ctrader-markets.production.rollback.plist`
  (SHA-256 `2ee26ecb0c595b2fbcfcebb4782805de2c1e87b4f36d43aa14bf1d8bf45cf4dc`).

Exact rollback procedure, only if rollback is required:

```bash
# Stop the unified token owner before starting the baseline token owner.
launchctl bootout gui/$(id -u)/com.execution-service.production
lsof -nP -iTCP:8010 -sTCP:LISTEN

# Load the path-correct baseline unit. The preceding lsof command must show no listener.
cp /Users/nishimweprince/Documents/Markets/Apps/trading-algos-rollback-1a0dd73/ctrader-markets/ops/com.ctrader-markets.production.rollback.plist \
  /Users/nishimweprince/Library/LaunchAgents/com.ctrader-markets.production.plist
launchctl bootstrap gui/$(id -u) \
  /Users/nishimweprince/Library/LaunchAgents/com.ctrader-markets.production.plist
launchctl print gui/$(id -u)/com.ctrader-markets.production
curl -fsS http://127.0.0.1:8010/health/trading-ready
lsof -nP -iTCP:8010 -sTCP:LISTEN
```

If the baseline unit cannot become ready, boot it out before re-bootstrapping the preserved unified
plist at `/Users/nishimweprince/Library/LaunchAgents/com.execution-service.production.plist`.

## Frozen-service retirement audit

The deletion target was audited before the time gate so cleanup can remain bounded and
reviewable:

- `mt5-trader/` contains 31 tracked files. Its only ignored contents are a dedicated `.venv`, Ruff
  and pytest caches, and Python bytecode caches.
- No `.env`, `signals.db`, SQLite database, token file, `data/` directory or `logs/` directory is
  present beneath the deletion target. Removing it therefore cannot delete unverified production
  state. The operator-attested Windows `signals.db` is host-local and outside this macOS checkout.
- The final frozen-service test must run before deletion. After it passes, remove the 31 tracked
  files and the disposable ignored environment/caches, plus `.github/workflows/mt5-trader-ci.yml`,
  whose job would otherwise point at a removed working directory.
- Update the Phase 3.3 status, root project index and the two cTrader warnings that currently point
  at `mt5-trader/.env.example.deriv`. Preserve `mt5-trader` strings that are compatibility wire
  names, historical provenance, or part of the separately tracked documentation cleanup in Phase
  3.6.
- Do not remove the detached cTrader rollback worktree as part of this retirement. It is independent
  of the frozen Windows MT5 directory and remains required throughout Phase 3.3.

The safe pre-deletion reference cleanup is complete: the root project index and signals-scrapper
example now name the unified MT5 adapter, and the execution-service README/examples use the
workspace `execution-service` console script rather than the retired `ctrader-markets` binary. The
cTrader/Deriv warnings describe the incompatible MT5 symbol names directly instead of linking to a
file scheduled for deletion. The documented workspace command was verified through its non-
connecting `--help` path.

## Prepared full-window audit method

The final audit will use the following authoritative sources and boundaries:

- Window: include events with timestamps from `2026-08-26T05:07:51Z` through the final snapshot at
  or after `2026-08-27T05:07:51Z`. The durable JSONL sink uses the field `ts`; console JSON uses
  `timestamp`. Both must be parsed explicitly so an empty count is not caused by using the wrong
  field name.
- Supervision: compare launchd label, PID, `runs`, last termination information, executable,
  arguments, working directory and log paths with the initial snapshot; independently confirm the
  sole port-8010 listener and legacy-label absence.
- Runtime: capture sanitized `/health/live`, `/health/ready`, `/health/trading-ready`, `/v1/accounts`
  and XAUUSD tick results. Record only account aliases/classifications and aggregate status, never
  raw broker account IDs or credentials.
- Logs: count every level and event name in both production logs. Explicitly classify connection
  closures/failures, reconnect/reauth attempts, token rejection/invalidation, broker server errors,
  heartbeat failures, event-handling failures, subscriber lag, rejected/failed trade operations,
  database/routing/reconciliation errors and unexpected service stops.
- Ledger: run SQLite integrity checking; count operations and target states, failed/rejected/error
  targets, duplicate operation IDs, duplicate `(source, payload_hash)` groups, and execution-event
  types. Report aggregate totals only.
- Acceptance: a current ready snapshot alone is not evidence for the full window. PID/run
  continuity and complete time-bounded log/ledger counts must agree before the session checkbox is
  closed.

These checks are implemented as the read-only, sanitized `infra/cutover_audit.py` command. It does
not print credentials, raw broker account IDs, operation IDs, broker order IDs, prices or payload
bodies, and it opens SQLite in read-only/query-only mode. Its dedicated tests and Ruff checks pass
(6 tests):

```bash
.venv/bin/python infra/cutover_audit.py \
  --start 2026-08-26T05:07:51Z \
  --expected-pid 6657 \
  --expected-runs 2

# At or after the boundary, add --require-acceptance.
```

The live exercises correctly left only `minimum_24_hours` false; all other runtime, account,
reconnect, log/stderr, exact binary/profile/path, supervision and ledger checks passed. A launchd
parser regression found during the first exercise was fixed and is covered by a test that
distinguishes the top-level `state = running` from nested coalition `state = active` fields.

The exact migration matrix is wrapped by `infra/verify_cutover.sh`. `pre-delete` runs every command
from `migration-spec.md`, the audit-checker tests, and the frozen service's 101-test suite while
asserting that `mt5-trader/` still exists. `post-delete` reruns the maintained matrix and asserts
that both the directory and its obsolete CI workflow are absent. Bash syntax validation passed;
the time-gated full run is intentionally deferred until the observation boundary.

## Still required before Phase 3.3 completion

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

### Checkpoint: 2026-08-26T05:41:16Z

- `com.execution-service.production` remained running as PID 6657, the same process that completed
  startup. launchd reported `runs = 2`; the recorded SIGTERM was the known installation-time
  replacement before this observation window, not a post-cutover restart.
- The workspace console script and `--profile production` were the process executable/arguments;
  the working directory and stdout/stderr paths matched the production plist.
- PID 6657 was the sole TCP listener on 127.0.0.1:8010. The legacy
  `com.ctrader-markets.production` label remained unloaded.
- `/health/live`, `/health/ready` and `/health/trading-ready` returned HTTP 200. Database health and
  both execution gates were true.
- Two demo accounts and one live account were connected, reconciled and reported full broker
  access. There were zero reconnects and zero unconfigured accounts. The one broker-disabled live
  account remained excluded, with the unavailable-account count at one.
- XAUUSD returned a cTrader quote less than one second old; bid and ask were present and spread was
  non-negative.
- Since the 2026-08-26T05:07:51Z boundary, the durable event stream contained no ERROR or WARNING
  event, reconnect, authentication failure, rejected/failed execution, database error, routing or
  reconciliation failure, duplicate-execution indication, subscriber lag or event-loop failure.
  The expected `RET_ACCOUNT_DISABLED` warning occurred immediately before the observation boundary
  and remains classified as the designed exclusion rather than a session anomaly.
- SQLite integrity was `ok`. The execution ledger still contained one succeeded smoke-test
  operation with one placed target, zero target errors, zero duplicate operation IDs and zero
  duplicate source/payload-hash groups.
- `infra/launchd/install.sh production --check` passed without mutating or restarting the service.
