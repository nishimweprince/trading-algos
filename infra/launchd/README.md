# Supervision

The supported execution deployment is `com.execution-service.production`: one process, one token
owner, and up to one cTrader connection per environment. This prevents refresh-token races while
allowing every authorized forex and Deriv cTrader account to share the appropriate connection.

```bash
cp .env.example.production .env.production
cp accounts.example.toml data/accounts.production.toml
# replace credentials, account IDs, exact symbol maps and MAX_VOLUME_LOTS
./infra/launchd/install.sh production
```

The installer rejects sample IDs/placeholders, validates the TOML registry without connecting,
permissions the secret files to `0600`, and creates the database/token/log directories. Start with
both trading switches false; production discovers authorized demo and live registry accounts.
Enable demo execution only after read-only health checks pass.

The older forex/deriv launch agents below remain for market-data-only compatibility. Do not run
them with the same OAuth refresh token as the production gateway.

One launchd agent per profile. Each owns a single cTrader connection and is the
only source of live prices for its consumers, so an unnoticed stop is a silent
data outage rather than an error.

| unit | profile | port | env file |
|---|---|---|---|
| `com.execution-service.forex` | forex | 8010 | `.env.forex` |
| `com.execution-service.deriv` | deriv | 8011 | `.env.deriv` |

Run exactly one process per broker account. Two processes on the same
credentials mean two sessions and two token-refresh races.

## Install

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example.forex .env.forex   # then fill it in — see the root README
./infra/launchd/install.sh forex
```

`infra/launchd/install.sh` is the supported path because it does the things whose absence
is invisible:

- creates `logs/` and `data/`, which are gitignored and absent from a fresh
  checkout. launchd creates the log *file* but not its parent directory, so
  without this the redirect silently fails and a crash loop leaves no trace.
- refuses to install while the env file still holds `replace-with-…`
  placeholders. Only the four secrets are rejected by the schema; an unedited
  `API_KEY` is 32 characters and would otherwise start the service with a key
  published in this repository.
- refuses a port already in use. `PORT` lives only in the env file — the plists
  set no `EnvironmentVariables`, so the ports in their comments are
  documentation, not configuration, and `config.py` defaults *both* profiles to
  8010.
- refuses two profiles sharing a `TOKEN_CACHE_PATH`. See
  [Token cache](#token-cache).

The plists hardcode absolute paths, because launchd expands neither `~` nor a
shell environment. Edit them if the checkout moves.

Secrets stay in `.env.<profile>`. Nothing sensitive belongs in a plist — they
are world-readable.

## Health

```bash
curl -s localhost:8010/health/ready | jq .details
```

`/health/live` answers as soon as the process is up. `/health/ready` returns 503
until the broker session is connected *and* a quote has arrived within
`TICK_STALENESS_SECONDS`, and its `details` carry `state`, `reconnects` and
`last_error`. Both are unauthenticated; every `/v1/*` route needs `X-API-Key`.

A ready service over a market close reports `"reason": "no quotes received yet"`
and stays ready — no ticks is normal there, and only staleness *after* quotes
have been flowing is a fault.

```bash
tail -f logs/forex.log                                  # stdout, JSON per line
jq -r .event logs/events.forex.jsonl | sort | uniq -c   # the durable record
```

`events.<profile>.jsonl` receives every event, including the ones also printed
to the console. Watch for `ctrader_connect_failed`, `access_token_rejected`,
`access_token_invalidated` and `stream_subscriber_lagging`.

## Restart and removal

```bash
launchctl kickstart -k gui/$(id -u)/com.execution-service.forex   # restart
launchctl print      gui/$(id -u)/com.execution-service.forex     # state, exit code
launchctl bootout    gui/$(id -u)/com.execution-service.forex     # stop and unload
rm ~/Library/LaunchAgents/com.execution-service.forex.plist       # uninstall
```

A restart is always safe: the service re-authenticates, reloads the symbol
catalog and re-subscribes from configuration alone. Nothing is resumed from
disk except the token pair.

## Failure modes

| symptom | likely cause | check |
|---|---|---|
| `logs/forex.log` empty, process restarting every 60s | crash before logging — bad env file, port in use, missing venv | run `.venv/bin/execution-service --profile forex` in the foreground |
| `/health/ready` 503, `last_error` mentions `CH_CLIENT_AUTH_FAILURE` | wrong `CTRADER_CLIENT_ID` / `CTRADER_CLIENT_SECRET` | re-check the application page at openapi.ctrader.com |
| 503 with `symbol_resolution_failed` in the log | a name in `SYMBOLS` is not exposed by this broker | `--discover-symbols`, copy exact `symbolName` values |
| repeated `access_token_rejected` then silence | refresh token expired or already rotated elsewhere | redo the OAuth flow, then `--refresh-token` |
| ready but no ticks on a weekday | symbols resolve but the market is closed, or the account has no feed | compare against a cTrader chart |
| `stream_subscriber_lagging` in the events log | an SSE consumer is too slow; oldest ticks are being dropped for it | the `dropped` counter on that subscriber's `status` events |

`ThrottleInterval` of 60s stops a crash loop from hammering the broker. Reconnect
backoff grows to `RECONNECT_MAX_BACKOFF_SECONDS` and only resets once a
connection has stayed up for `RECONNECT_STABILITY_SECONDS`.

## Token cache

`TOKEN_CACHE_PATH` is the only writable state in the service, and losing it means
redoing the browser OAuth flow by hand.

The refresh token **rotates**: `ProtoOARefreshTokenRes` returns a new one and the
old one dies immediately. Three consequences:

- Give every profile its own `TOKEN_CACHE_PATH`. Two profiles sharing one file
  invalidate each other's tokens on every refresh. `install.sh` refuses this,
  and an unset path now defaults to `data/token-cache.<profile>.json`.
- The path is relative and resolves against the plist's `WorkingDirectory`.
  Running the service by hand from another directory writes a *second* cache
  whose refresh kills the supervised one's token. Always run from the repo root.
- Back it up with your other secrets. A restore from a stale copy is useless —
  the refresh token it holds has already been spent.

```bash
cp data/token-cache.forex.json ~/secure-backups/   # after each manual refresh
```

## Log rotation

Nothing rotates `logs/*.log`; they grow unbounded. Add a `newsyslog.d` drop-in:

```bash
sudo tee /etc/newsyslog.d/execution-service.conf >/dev/null <<'EOF'
# logfilename                                                    mode count size when flags
/Users/nishimweprince/Documents/Markets/Apps/trading-algos/services/execution-service/logs/*.log 644 7 10240 * J
EOF
```

launchd holds the file open, so use `J` (compress) with size-based rotation
rather than moving files out from under the process.

## Migration note: the launchd labels changed

`ctrader-markets` is now `services/execution-service`, and its jobs were
relabelled from `com.ctrader-markets.*` to `com.execution-service.*`. launchd
keys on the label, so the old jobs keep running under the old name until they
are explicitly removed. On each host:

```bash
launchctl bootout gui/$(id -u)/com.ctrader-markets.forex
launchctl bootout gui/$(id -u)/com.ctrader-markets.deriv
launchctl bootout gui/$(id -u)/com.ctrader-markets.production
```

Then install the new jobs with `install.sh`. Do the bootout and the bootstrap in
one sitting: both point at the same port, so leaving the old job loaded means
the new one fails to bind.

The binary also moved. It is now the workspace virtualenv's console script,
`<repo>/.venv/bin/execution-service`, not a per-project venv — populate it with
`uv sync --all-packages`.
