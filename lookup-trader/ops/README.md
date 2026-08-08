# Supervision

Four launchd agents. None can place an order — every artifact load asserts
`orders_enabled is False`, and there is no execution path in the codebase.

| unit | what it does | cadence |
|---|---|---|
| `com.lookup-trader.meta-shadow-worker` | syncs closed candles, discovers meta-events, scores them, sends alerts | every 60s, `KeepAlive` |
| `com.lookup-trader.meta-retrain` | evaluates the research-shadow challenger against the forward gates | Saturday, ≥ 12:00 UTC |
| `com.lookup-trader.live-calendar` | refreshes the current live calendar window and retained snapshots | daily |
| `com.lookup-trader.meta-shadow-watchdog` | independently alerts when successful worker cycles become stale | every 5 minutes |

## Install

Both files hardcode absolute paths, because launchd expands neither `~` nor a
shell environment. Edit them if the checkout moves.

```bash
mkdir -p data/logs
cp ops/*.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.lookup-trader.meta-shadow-worker.plist
launchctl load ~/Library/LaunchAgents/com.lookup-trader.meta-retrain.plist
```

Secrets stay in `server/.env`, which `pydantic-settings` reads. Nothing
sensitive belongs in a plist — they are world-readable.

The separately built notification service must also be running. Its supervised
unit lives at `../notification-service/ops/com.notification-service.plist`;
provider credentials and recipient lists remain in that service's `.env`.

## Health

The worker is the only thing that turns candles into alerts, so a silent stop
means no alerts rather than an error.

```bash
curl -s localhost:8000/meta-model/status | jq '.ledger | {last_run_at, forward_events}'
```

`last_run_at` older than a few minutes means the worker is down. Restart with
`launchctl kickstart -k gui/$(id -u)/com.lookup-trader.meta-shadow-worker`.

Delivery failures are now recorded per event rather than only counted, so this
answers "was anyone actually told?":

```sql
-- sqlite3 data/meta_shadow.sqlite3
SELECT notification_status, count(*)
FROM meta_live_events
WHERE forward_evaluation_eligible = 1
GROUP BY 1;
```

Rows stuck at `failed` with `notification_attempts >= 5` have exhausted retries
and need manual attention — the alert never reached anyone.

## Notes

- The worker holds `.meta-shadow-worker.lock`, so loading it twice is safe: the
  second instance exits rather than double-notifying.
- `ThrottleInterval` of 60s stops a crash loop hammering Capital.com.
- The evaluator is checked hourly and gates itself using UTC. Its stored ISO
  week prevents duplicate Saturday evaluations and avoids launchd/DST drift.
