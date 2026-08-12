# Operator runbook

`ipda` runs on the **same host as its paired mt5-trader instance** — it reaches the
market-data and signal endpoints on `127.0.0.1`, and mt5-trader must run on 64-bit Windows
beside the MetaTrader 5 terminal. One `ipda` process per profile, always.

## Startup checklist

1. Confirm the paired mt5-trader instance is up: `GET /health/live`, then `/health/ready`.
   `ipda` refuses to submit while `REQUIRE_READY=true` and readiness is failing.
2. Confirm `notification-service` is reachable at `NOTIFICATION_SERVICE_URL` when
   `NOTIFICATIONS_ENABLED=true`. Out-of-session signals are only visible through it.
3. Confirm the profile env file is readable only by the service user, and that `LOGS_DIR`
   is writable — `open_trades.json` lives there.
4. Confirm `PIP_SIZE` matches the instrument. Every pip-denominated setting scales off it;
   a wrong `PIP_SIZE` silently produces stops that are an order of magnitude off.
5. Confirm the trigger is the intended one. The service trades the **reversal** signal
   (Buy Chance / Sell Chance, RSI 14 crossing 25 / 75), not the ▲/▼ Supertrend labels.
   `SUPERTREND_*` values in the env file are inert.
6. Start exactly one process per profile.

### Profile commands

| Profile | Env file | Start command |
|---|---|---|
| Default (forex) | `.env` | `ipda` |
| Deriv | `.env.deriv` | `ipda --profile deriv` |

## Deployment (Windows)

Mirror the mt5-trader setup. Configure Task Scheduler to run
`.venv\Scripts\ipda.exe` at logon, under the same interactive user that owns the terminal
session, with:

- **Working directory** set to this repository (the env file is resolved relative to it).
- **Arguments** `--profile deriv` for a second instance; none for the default profile.
- **Parallel instances disabled** for the same profile.
- Standard output and standard error captured — logs are newline-delimited JSON on stdout.

Start order at boot: MetaTrader 5 terminal → mt5-trader → notification-service → `ipda`.
`ipda` tolerates the others being late (it logs `data_poll_failed` and retries next poll),
but a signal fired during the gap is lost, not queued.

## Verifying the configuration took effect

The `startup` log event reports the live values. Check it after every config change:

```
trigger: reversal
reversal_sensitivity: 14
reversal_levels: [25, 75]
target_tf_minutes: 3
trading_sessions: ["tokyo", "new_york"]
notifications_enabled: true
mfe_break_even_pips: 30
tracked_trades_restored: <n>
```

A bad session name, an unknown notification channel, a malformed session spec, inverted
reversal levels, or `USE_HARD_TARGETS=false` all fail at startup with a message on stderr
and exit code 1 — never at 03:00 on a live signal.

Confirm the timeframe took by checking that a `signal_fired` record's `bucket_start` lands
on a `:00 / :03 / :06 / :09` boundary.

## Logging

- **Console:** newline-delimited JSON on stdout, `LOG_LEVEL=INFO` by default.
- **`{LOGS_DIR}/signals.jsonl`** — `signal_fired`, `signal_skipped_out_of_session`,
  `break_even_reached`.
- **`{LOGS_DIR}/executions.jsonl`** — one record per submit outcome.
- **`{LOGS_DIR}/errors.jsonl`** — `data_poll_failed`, `tick_poll_failed`.
- **`{LOGS_DIR}/open_trades.json`** — the tracked-trade state, rewritten atomically on
  every change. Safe to delete while the service is stopped; doing so abandons the
  break-even watch on any trade still open.

Logs contain trading detail. Apply the same access controls and retention as the
mt5-trader ledger.

## Handling events

### `signal_skipped_out_of_session`

Working as configured: a signal fired outside `TRADING_SESSIONS` and no order was sent. The
notification carries the entry and the stop/target that would have been used. If you decide
to take it manually, note that the price has moved since the bar close quoted in the alert.

Seeing these constantly during hours you expect to be trading means the session windows or
the host clock are wrong. Check the `startup` event's `trading_sessions`, then the host
timezone.

### `break_even_reached`

**Move the stop-loss to the entry price in MT5 yourself.** This service never modifies a
stop. The alert fires once per trade and is not repeated, including across a restart.

If the trade already closed before you saw the alert, ignore it — the tracker infers closes
from price and can lag the broker by up to one poll interval.

### `tracked_trade_closed`

Advisory bookkeeping only, with `reason` one of `take_profit_reached`, `stop_loss_reached`,
or `ttl_expired`. **This is inferred from price, not read from the broker.** Never treat it
as confirmation that a position is flat — check the terminal.

### `signal_submitted` with a non-success outcome

Read across to mt5-trader's runbook: `unready` (terminal or `TRADING_ENABLED`), `rejected`
(422 — usually `stop_loss_too_close` on a synthetic; widen the stop rather than retrying),
`unauthorized` (401 — fix `MT5_SIGNAL_API_KEY`), `unknown` (503 — a human must inspect the
terminal before any new signal on that symbol).

### `notification_failed`

Trading is unaffected by design. Fix `notification-service` or the API key; the missed
notification is not retried, but the underlying event is still in `signals.jsonl`.

## Restart behaviour

- **Signal de-duplication is in-memory.** After a restart the bucket lock is empty, so a
  signal still present on the forming candle can fire a second time. The signal id is a
  deterministic UUIDv5 of (symbol, bucket start, direction), so mt5-trader's idempotency
  ledger rejects the duplicate rather than double-trading — but expect a `409` in the log.
- **Tracked trades survive.** `open_trades.json` is reloaded, and `tracked_trades_restored`
  in the `startup` event reports the count. Already-alerted trades stay silent.

## Rollout

1. `pytest` and `ruff check .` clean.
2. Start with `NOTIFICATIONS_ENABLED=true` and mt5-trader's `TRADING_ENABLED=false`.
   Submissions come back `unready`; confirm the session gate, the logs, and the Telegram
   path all behave without any order reaching a broker.
3. Enable `TRADING_ENABLED=true` on a **demo** account. Wait for one in-session signal.
   Verify the fill carries a 40-pip stop and a 50-pip target, then verify the break-even
   notification arrives once the trade reaches +30 pips and does not repeat.
4. Only then point the profile at a live account.
