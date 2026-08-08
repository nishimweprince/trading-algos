# Supervision

Two launchd agents. Neither can place an order — every artifact load asserts
`orders_enabled is False`, and there is no execution path in the codebase.

| unit | what it does | cadence |
|---|---|---|
| `com.lookup-trader.meta-shadow-worker` | syncs closed candles, discovers meta-events, scores them, sends alerts | every 60s, `KeepAlive` |
| `com.lookup-trader.meta-retrain` | evaluates the research-shadow challenger against the forward gates | Saturday, ≥ 12:00 UTC |

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
- The Saturday schedule is **local time**; `evaluate_weekly_shadow` gates on
  Saturday ≥ 12:00 **UTC** and returns `not_due` otherwise. Re-check the hour
  after a daylight-saving change.
