# Operator runbook

## Startup checklist

1. Confirm the Windows user session is active and the configured MT5 terminal is logged into the
   expected account.
2. Confirm algorithmic trading is enabled in both the terminal and account.
3. Confirm `.env` is readable only by the service user and `DATABASE_PATH` is writable.
4. Start exactly one `mt5-signal-service` process. Multiple workers or parallel scheduled-task
   instances are unsupported because MT5 exposes one shared terminal session.
5. Check `/health/live`, then `/health/ready`. The readiness response reports only non-secret
   connection flags and never account credentials.

## Response handling

- `401 unauthorized`: correct the caller's API key; do not log the key.
- `409 idempotency_conflict`: generate a new UUID only for a genuinely new trading decision.
- `409 signal_in_progress` or `execution_outcome_unknown`: inspect
  `GET /v1/signals/{signal_id}` and the MT5 terminal. Never resubmit the same decision with a new ID
  until a human has ruled out execution.
- `422`: fix the signal or risk constraint. Broker retcodes and comments are returned in structured
  error details.
- `503 terminal_not_ready`: check terminal connectivity, configured login, account permissions,
  and `TRADING_ENABLED`.

`order_send()` is never automatically retried. A transport failure after submission is persisted as
`unknown` because retrying could duplicate a live trade.

## Restart and reconciliation

On startup, records left in `executing` are matched against recent MT5 order and deal history using
the deterministic `sig:` broker comment. A match becomes `filled` or `placed`; no match becomes
`unknown`. Records interrupted before broker submission become `rejected`. The operator must inspect
unknown records in MT5 before deciding on any new signal.

## Backups and retention

- Back up the SQLite database and its `-wal`/`-shm` companions using a SQLite-aware backup process
  or while the service is stopped.
- Treat the ledger as sensitive trading metadata even though it contains no account password or API
  key.
- Retention and archival are operational policies; do not delete unresolved or unknown records.

## Key and account rotation

1. Stop inbound traffic and the service.
2. Update `.env` with the new API key or account values.
3. Confirm the terminal is logged into the matching account.
4. Restart once, verify readiness, then update callers.

Never switch accounts while the service is running.

