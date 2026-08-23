# Supervision

The same four jobs run under launchd on macOS; on the Linux VPS they are split
between pm2 (daemons) and cron (scheduled one-shots).
Market execution remains disabled by default. When it is deliberately enabled,
only `meta-shadow-worker` owns the configured provider client and its single
heartbeat monitor; scheduled jobs and the API never submit orders.

- **macOS / launchd** — the plists in this directory. See *Install* below.
- **Linux VPS** — pm2 supervises the two daemons; **cron** runs the three
  scheduled jobs. See *Linux VPS* below. Day-to-day control is
  `./scripts/server.sh`, and `./scripts/server.sh doctor` when something looks
  wrong.

| unit | what it does | cadence |
|---|---|---|
| `com.lookup-trader.meta-shadow-worker` | syncs candles, scores events, alerts, and optionally owns fail-closed execution | every 60s, `KeepAlive` |
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

## Linux VPS

Two mechanisms, split on purpose:

| job | mechanism | why |
|---|---|---|
| `lt-worker`, `lt-api` | pm2 | long-lived, need restart-on-crash |
| retrain, calendar, watchdog | cron | one-shots; the watchdog must not share a supervisor with the worker it watches |

### pm2 — the two daemons

Registration is one-time; `scripts/server.sh` deliberately does not re-run it,
because re-registering from two places would drift.

```bash
P=$HOME/trading-algos/lookup-trader/.venv/bin/python
D=$HOME/trading-algos/lookup-trader

pm2 start scripts/run_meta_shadow_worker.py --name lt-worker \
  --interpreter $P --interpreter-args="-u" --cwd $D \
  --max-memory-restart 3G --max-restarts 10

# Check the port is free first — this box runs a dozen services.
ss -ltnp | grep ':8100 ' && echo "8100 is taken, pick another"

pm2 start $P --name lt-api --cwd $D/server --max-restarts 10 \
  -- -m uvicorn app.main:app --host 127.0.0.1 --port 8100

pm2 save && pm2 startup     # then run the sudo line it prints
pm2 install pm2-logrotate   # a crash loop will otherwise fill the disk
```

`--interpreter-args="-u"` is not cosmetic: without unbuffered stdout, pm2 shows
no logs for minutes and a healthy process looks hung.

`--max-restarts 10` matters more than it looks. pm2 abandons a process only when
restarts come faster than `min_uptime` (1s); this app takes several seconds just
to import, so every crash looks "stable" and an unbounded loop can run for days
reporting `online`. That is exactly what happened — `lt-api` reached **3167
restarts** at 100% CPU before anyone noticed. With the cap, a loop ends in
`errored`, which is visible in `pm2 list`.

**The port is worth the paranoia.** uvicorn completes application startup
*before* it binds, so a port already in use produces a unit pm2 reports as
`online` that serves nothing, logging `[Errno 98] address already in use` and
re-running `bootstrap()` on every cycle. Registered on the shared default of
8000, `lt-api` did that **3438 times**. `server.sh` reads the port back out of
this registration rather than keeping its own copy, so there is nothing to keep
in sync; `LOOKUP_SERVER_PORT` overrides it for a one-off, and `doctor` warns if
that override disagrees. When the API stops answering, `server.sh status` names
whichever process holds the port.

Batch jobs such as a feature rebuild must be `pm2 delete`d before `pm2 save`,
or every reboot re-runs them.

### cron — the three scheduled jobs

These previously ran under pm2 `--cron-restart` with `--no-autorestart`. **That
never fired.** After two days their restart counters read 0, against an expected
~48 for the hourly retrain and ~576 for the 5-minute watchdog. Consequences were
silent: the calendar was never re-ingested, and the watchdog that exists to
report a dead worker had itself never run.

`crontab -e`, with absolute paths — cron has almost no environment:

```cron
P=/home/basis/trading-algos/lookup-trader/.venv/bin/python
D=/home/basis/trading-algos/lookup-trader
0 * * * * cd $D && $P scripts/retrain_meta_shadow.py --scheduled >> $D/data/logs/retrain.log 2>&1
0 5 * * * cd $D && $P scripts/refresh_live_calendar.py --yes >> $D/data/logs/calendar.log 2>&1
*/5 * * * * cd $D && $P scripts/check_meta_shadow_health.py >> $D/data/logs/watchdog.log 2>&1
```

`mkdir -p $D/data/logs` first. `check_meta_shadow_health.py` exits 1 when stale,
so cron surfaces it independently of the notification it sends.

`refresh_live_calendar.py --yes` is the only caller of `ingest_range`. The worker
reads the manifest and gates every event on `calendar_coverage_ok` but never
ingests, so if this job stops the pipeline silently expires once "now" walks past
the last ingested window.

After deploying code:

```bash
./scripts/server.sh              # restart lt-worker and lt-api
./scripts/server.sh doctor       # restart counts, crash-loop detection
```

`restart` covers only the daemons, because they are the only units holding
imported modules in memory. The cron jobs re-exec from disk on each fire and
pick up new code unattended.

`doctor` is the one to reach for when something looks wrong. It reports status,
restart count, uptime, cpu and memory per daemon, and when a restart count is
high it samples pm2 twice to answer the question `pm2 list` cannot: **is this
count still climbing?** A climbing count is a live crash loop (`FAIL`, exit 1);
a static one is history (`WARN`). It also flags a daemon that is not `online`,
a retired unit still registered under pm2, and a port mismatch between the
registration and `LOOKUP_SERVER_PORT`.

## Health

The worker is the only thing that turns candles into alerts, so a silent stop
means no alerts rather than an error.

```bash
./scripts/server.sh status      # resolves the port from the pm2 registration
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

Execution status is reported separately from candle/model health:

```bash
curl -s "http://127.0.0.1:${LOOKUP_SERVER_PORT:-8100}/health" | jq .execution
curl -s "http://127.0.0.1:${LOOKUP_SERVER_PORT:-8100}/meta-model/status" | jq .execution
```

`unhealthy` gates new submissions immediately. One alert is sent for each
outage, followed by one recovery alert; delivery uses the notification service
even when meta-event alerts are disabled. Durable broker outcomes can be audited
without exposing request credentials:

```sql
-- sqlite3 data/meta_shadow.sqlite3
SELECT provider, account_key, state, count(*)
FROM meta_execution_attempts
GROUP BY 1, 2, 3;
```

Deploy in demo first. Before setting `LOOKUP_MARKET_EXECUTION_ENABLED=true`,
verify the provider-side trading switch, the `lookup_trader` source allowlist,
the cTrader account alias when applicable, heartbeat readiness, notification
delivery, and an explicitly promoted immutable execution artifact. Disabling
the Lookup Trader flag and restarting prevents new orders but intentionally does
not manage or close existing broker positions.

## Notes

- The worker holds `.meta-shadow-worker.lock`, so loading it twice is safe: the
  second instance exits rather than double-notifying or starting another
  execution heartbeat monitor.
- `ThrottleInterval` of 60s stops a crash loop hammering Capital.com.
- The evaluator is checked hourly and gates itself using UTC. Its stored ISO
  week prevents duplicate Saturday evaluations and avoids launchd/DST drift.
