# MT5 operator runbook

Restored from `mt5-trader/docs/operator-runbook.md`, which was dropped rather than moved when
mt5-trader folded into execution-service. The procedures are unchanged; the commands, paths and
settings names are the current ones.

This covers the MetaTrader 5 adapter only — the process started with `ADAPTERS=mt5`, which runs on
the Windows host. A cTrader deployment is a different profile of the same codebase; see
`apps/docs/app/execution-service/operations` for the macOS launchd side.

## Startup checklist

1. Confirm the Windows user session is active and the configured MT5 terminal is logged into the
   expected account.
2. Confirm algorithmic trading is enabled in both the terminal and account.
3. Confirm the profile env file (`.env` or `.env.{profile}`) sets `ADAPTERS=mt5`, is readable only
   by the service user, and that `DATABASE_PATH` is writable.
4. Start exactly one `execution-service` process per profile. Multiple workers or parallel
   scheduled-task instances for the same profile are unsupported because MT5 exposes one shared
   terminal session per process.
5. Check `/health/live`, then `/health/ready`, then `/health/trading-ready`. The readiness responses
   report only non-secret connection flags and never account credentials.

### Profile commands

Run from `services/execution-service/` as the working directory.

| Profile | Env file | Start command |
|---|---|---|
| Default | `.env` | `execution-service` |
| Named (e.g. forex) | `.env.forex` | `execution-service --profile forex` |
| Deriv | `.env.deriv` | `execution-service --profile deriv` |

`execution-service --profile NAME --validate-config` checks the environment without connecting.

For Task Scheduler, set the working directory to `services/execution-service/` and pass
`--profile NAME` in the task arguments when not using the default `.env`.

## Console logging

- Logs are newline-delimited JSON on standard output and default to `LOG_LEVEL=INFO`.
- Capture both standard output and standard error in Task Scheduler or the Windows service wrapper.
- **Console:** lifecycle events, one `signal_post` per new signal (terminal outcome), and failed
  requests (`request_validation_failed`, `service_error_response`). HTTP access logs are off —
  candle polling is silent on stdout.
- **Files:** `SIGNALS_LOG_PATH` (default `logs/signals.jsonl`) — one summary per terminal outcome;
  `EVENTS_LOG_PATH` (default `logs/events.jsonl`) — full execution trace. Both are gitignored; back
  up operationally as needed.
- **SQLite:** `DATABASE_PATH` (default `data/signals.db`) — idempotency ledger, separate from the
  JSONL files.
- Passwords and API-key values are excluded, but logs still contain sensitive trading details and
  must use access controls and retention appropriate for account activity.
- Use `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` for `LOG_LEVEL`. The `service_starting` log
  includes `profile` when `--profile` was used.

## Notifications

When `NOTIFICATIONS_ENABLED=true`, each profile POSTs to notification-service after a **new** signal
reaches a terminal state and after a **failed inbound request** (Pydantic validation errors and
non-auth service errors). Signal execution failures are notified once via the signal outcome, not
again from the HTTP error handler — `POST /v1/signals` is excluded from the error-notifying
middleware for exactly this reason. Set `NOTIFICATION_CHANNELS` per profile; configure recipients on
notification-service. Failures log `notification_failed` and do not block trading. Duplicate
`signal_id` replays do not re-notify.

## Response handling

- `401 unauthorized`: correct the caller's API key; do not log the key.
- `409 idempotency_conflict`: generate a new UUID only for a genuinely new trading decision.
- `409 signal_in_progress` or `execution_outcome_unknown`: inspect `GET /v1/signals/{signal_id}` and
  the MT5 terminal. Never resubmit the same decision with a new ID until a human has ruled out
  execution.
- `422`: fix the signal or risk constraint. Broker retcodes and comments are returned in structured
  error details.
- `503 terminal_not_ready`: check terminal connectivity, configured login, account permissions, and
  `TRADING_ENABLED`.

`order_send()` is never automatically retried. A transport failure after submission is persisted as
`unknown` because retrying could duplicate a live trade.

## Restart and reconciliation

On startup, `reconcile_startup` matches records left in `executing` against recent MT5 order and
deal history using the deterministic `sig:` broker comment. A match becomes `filled` or `placed`; no
match becomes `unknown`. Records interrupted before broker submission become `rejected`. If the
terminal is not ready, reconciliation fails loudly rather than guessing. The operator must inspect
unknown records in MT5 before deciding on any new signal.

## Backups and retention

- Back up the SQLite database and its `-wal`/`-shm` companions using a SQLite-aware backup process
  or while the service is stopped.
- Treat the ledger as sensitive trading metadata even though it contains no account password or API
  key.
- Retention and archival are operational policies; do not delete unresolved or unknown records.

## Key and account rotation

1. Stop inbound traffic and the service for the affected profile.
2. Update the profile env file (`.env` or `.env.{profile}`) with the new API key or account values
   (`API_KEY`, `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`).
3. Confirm the terminal is logged into the matching account.
4. Restart once, verify readiness, then update callers.

Never switch accounts while the service is running.
