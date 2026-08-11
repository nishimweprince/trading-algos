#!/usr/bin/env bash
# Control the pm2 units on the VPS. The server-side counterpart to dev.sh.
#
#   ./scripts/server.sh                 restart the daemons (the usual case)
#   ./scripts/server.sh restart all     also cycle the scheduled one-shots
#   ./scripts/server.sh start|stop|status|logs
#
# Two kinds of unit, and the difference decides what `restart` touches:
#
#   daemons    lt-worker, lt-api — long-lived, autorestart on. They hold the
#              imported modules in memory, so a code change only takes effect
#              when they are restarted. These are what you want after a deploy.
#
#   scheduled  lt-retrain, lt-calendar, lt-watchdog — `--no-autorestart` plus
#              `--cron-restart`. pm2 re-execs them from disk on every fire, so
#              they pick up new code on their own. Restarting one does not
#              refresh anything; it just runs it off-schedule. That is why
#              `restart` skips them unless you ask for `all`.
#
# This script only cycles units that are already registered. To create them,
# see ops/README.md — registration carries the cron expressions and arguments,
# and re-running it here would silently drift from what pm2 has.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_PORT="${LOOKUP_SERVER_PORT:-8000}"

DAEMONS=(lt-worker lt-api)
SCHEDULED=(lt-retrain lt-calendar lt-watchdog)

log() {
  printf '\033[1;36m[server]\033[0m %s\n' "$*"
}

warn() {
  printf '\033[1;33m[server]\033[0m %s\n' "$*" >&2
}

fail() {
  printf '\033[1;31m[server]\033[0m %s\n' "$*" >&2
  exit 1
}

command -v pm2 >/dev/null 2>&1 || fail "pm2 not found. This script runs on the VPS."

# Names pm2 actually knows about. Acting on an unregistered name is a no-op that
# pm2 reports as an error, which would abort the whole run under `set -e`.
registered() {
  pm2 jlist 2>/dev/null | python3 -c '
import json, sys
try:
    print("\n".join(p["name"] for p in json.load(sys.stdin)))
except Exception:
    pass
'
}

KNOWN="$(registered)"

known() {
  grep -qxF "$1" <<<"$KNOWN"
}

# Apply a pm2 verb to each name, skipping any that is not registered.
apply() {
  local verb="$1"
  shift
  local acted=0
  for name in "$@"; do
    if ! known "$name"; then
      warn "$name is not registered — skipping"
      continue
    fi
    log "pm2 $verb $name"
    # --update-env re-reads the shell environment; meaningless on stop.
    if [[ "$verb" == "stop" ]]; then
      pm2 "$verb" "$name" >/dev/null
    else
      pm2 "$verb" "$name" --update-env >/dev/null
    fi
    acted=$((acted + 1))
  done
  [[ "$acted" -gt 0 ]] || warn "nothing to $verb"
}

health() {
  log "Units:"
  pm2 list

  if ! known lt-api; then
    return 0
  fi

  # The API binds after the DuckDB connection opens; give it a moment before
  # calling a miss a failure.
  local url="http://127.0.0.1:${API_PORT}/meta-model/status"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf --max-time 3 "$url" >/dev/null 2>&1; then
      log "API healthy on :${API_PORT}"
      curl -sf --max-time 3 "$url" | python3 -c '
import json, sys
d = json.load(sys.stdin)
ledger = d.get("ledger") or {}
rows = [
    ("orders_enabled", d.get("orders_enabled")),
    ("last_run_at", ledger.get("last_run_at")),
    ("events", ledger.get("events")),
    ("forward_events", ledger.get("forward_events")),
]
for key, value in rows:
    print("  %-14s : %s" % (key, value))
' || true
      return 0
    fi
    sleep 2
  done
  warn "API did not answer on :${API_PORT} after 20s — check: pm2 logs lt-api --lines 50 --nostream"
}

cmd="${1:-restart}"
scope="${2:-daemons}"

case "$scope" in
  all) TARGETS=("${DAEMONS[@]}" "${SCHEDULED[@]}") ;;
  daemons) TARGETS=("${DAEMONS[@]}") ;;
  *) fail "Unknown scope: $scope (expected 'daemons' or 'all')" ;;
esac

case "$cmd" in
  restart)
    if [[ "$scope" == "all" ]]; then
      warn "'all' fires the scheduled units now, outside their cron windows."
      warn "lt-retrain self-gates on Saturday >= 12:00 UTC, so it will no-op off-window."
    fi
    apply restart "${TARGETS[@]}"
    health
    ;;
  start)
    apply start "${TARGETS[@]}"
    health
    ;;
  stop)
    # Stop every unit regardless of scope — a partial stop leaves the worker
    # writing to a ledger the API is no longer serving.
    apply stop "${DAEMONS[@]}" "${SCHEDULED[@]}"
    pm2 list
    ;;
  status)
    health
    ;;
  logs)
    # pm2 logs takes a single target; without one it tails everything, which is
    # what you want when chasing a restart.
    pm2 logs --lines 40
    ;;
  *)
    fail "Unknown command: $cmd (expected restart, start, stop, status or logs)"
    ;;
esac

log "Done. Run 'pm2 save' if you changed which units are running."
