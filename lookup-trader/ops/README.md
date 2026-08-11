# Supervision

The same four units run under launchd on macOS and under pm2 on the Linux VPS.
None can place an order — every artifact load asserts `orders_enabled is False`,
and there is no execution path in the codebase.

- **macOS / launchd** — the plists in this directory. See *Install* below.
- **Linux VPS / pm2** — see *pm2* below. Day-to-day control is
  `./scripts/server.sh`.

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

## pm2 (Linux VPS)

Registration is one-time. The cron expressions and arguments live in these
commands, so `scripts/server.sh` deliberately does not re-run them — it only
cycles units pm2 already knows, and re-registering from two places would drift.

```bash
P=$HOME/trading-algos/lookup-trader/.venv/bin/python
D=$HOME/trading-algos/lookup-trader

# daemon: 60s loop, autorestart on
pm2 start scripts/run_meta_shadow_worker.py --name lt-worker \
  --interpreter $P --interpreter-args="-u" --cwd $D

# hourly tick; the script self-gates on Saturday >= 12:00 UTC
pm2 start scripts/retrain_meta_shadow.py --name lt-retrain \
  --interpreter $P --interpreter-args="-u" --cwd $D \
  --no-autorestart --cron-restart "0 * * * *" -- --scheduled

pm2 start scripts/refresh_live_calendar.py --name lt-calendar \
  --interpreter $P --interpreter-args="-u" --cwd $D \
  --no-autorestart --cron-restart "0 5 * * *" -- --yes

pm2 start scripts/check_meta_shadow_health.py --name lt-watchdog \
  --interpreter $P --interpreter-args="-u" --cwd $D \
  --no-autorestart --cron-restart "*/5 * * * *"

pm2 start $P --name lt-api --cwd $D/server \
  -- -m uvicorn app.main:app --host 127.0.0.1 --port 8000

pm2 save && pm2 startup     # then run the sudo line it prints
```

`--interpreter-args="-u"` is not cosmetic: without unbuffered stdout, pm2 shows
no logs for minutes and a healthy process looks hung.

Batch jobs such as a feature rebuild must be `pm2 delete`d before `pm2 save`,
or every reboot re-runs them.

After deploying code:

```bash
./scripts/server.sh              # restart lt-worker and lt-api
./scripts/server.sh status       # pm2 list + /meta-model/status
```

`restart` covers only the daemons, because they are the only units holding
imported modules in memory. The three cron units re-exec from disk on each fire
and pick up new code unattended; `restart all` would just run them off-schedule.

## Health

The worker is the only thing that turns candles into alerts, so a silent stop
means no alerts rather than an error.

```bash
curl -s localhost:8000/meta-model/status | jq '.ledger | {last_run_at, forward_events}'
```

`last_run_at` older than a few minutes means the worker is down. Restart with
`launchctl kickstart -k gui/$(id -u)/com.lookup-trader.meta-shadow-worker` on
macOS, or `./scripts/server.sh restart` on the VPS.

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
