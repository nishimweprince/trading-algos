#!/usr/bin/env bash
# Control the pm2 units on the VPS. The server-side counterpart to dev.sh.
#
#   ./scripts/server.sh                 restart the daemons (the usual case)
#   ./scripts/server.sh doctor          health report; detects a crash loop
#   ./scripts/server.sh start|stop|status|logs
#
# pm2 owns two long-lived daemons and nothing else:
#
#   lt-worker   60s loop — syncs candles, discovers events, scores, alerts
#   lt-api      uvicorn — serves /meta-model/status
#
# The three scheduled jobs (calendar refresh, retrain evaluation, staleness
# watchdog) are deliberately NOT here. They ran under pm2's `--cron-restart`
# and never fired once: after two days `pm2 list` showed 0 restarts against an
# expected ~576 for the 5-minute watchdog. They now run from the user crontab —
# see ops/README.md. The watchdog in particular must not share a supervisor
# with the worker it watches.
#
# Restart only cycles the daemons because they are the only units holding
# imported modules in memory. Cron re-execs its jobs from disk on every fire,
# so they pick up new code unattended.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DAEMONS=(lt-worker lt-api)
# Retired from pm2 in favour of cron. Flagged if still registered, because both
# mechanisms firing the same job is worse than neither.
RETIRED=(lt-calendar lt-retrain lt-watchdog)

# A daemon this far above zero is not "a few restarts", it is a symptom.
RESTART_WARN="${LOOKUP_RESTART_WARN:-20}"
# Gap between samples when deciding whether a restart count is still climbing.
LOOP_SAMPLE_SECONDS="${LOOKUP_LOOP_SAMPLE_SECONDS:-15}"

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

_jlist() {
  pm2 jlist 2>/dev/null
}

# Names pm2 actually knows about. Acting on an unregistered name is a no-op that
# pm2 reports as an error, which would abort the whole run under `set -e`.
KNOWN="$(_jlist | python3 -c '
import json, sys
try:
    print("\n".join(p["name"] for p in json.load(sys.stdin)))
except Exception:
    pass
')"

known() {
  grep -qxF "$1" <<<"$KNOWN"
}

# The port lives in the pm2 registration; read it from there rather than keeping
# a second copy in sync. A stale copy is not a cosmetic problem — this script
# polling one port while uvicorn binds another is how a dead API reads as merely
# slow. LOOKUP_SERVER_PORT still overrides, and doctor flags the disagreement.
_registered_port() {
  _jlist | python3 -c '
import json, sys
try:
    procs = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for proc in procs:
    if proc.get("name") != "lt-api":
        continue
    args = [str(a) for a in ((proc.get("pm2_env") or {}).get("args") or [])]
    if "--port" in args:
        index = args.index("--port")
        if index + 1 < len(args):
            print(args[index + 1])
    break
'
}

if [[ -n "${LOOKUP_SERVER_PORT:-}" ]]; then
  API_PORT="$LOOKUP_SERVER_PORT"
  PORT_SOURCE=env
else
  API_PORT="$(_registered_port)"
  PORT_SOURCE=registration
  if [[ -z "$API_PORT" ]]; then
    API_PORT=8000
    PORT_SOURCE=default
  fi
fi

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

# Is any daemon carrying enough restarts to be worth a second sample?
_suspicious() {
  DAEMONS="${DAEMONS[*]}" RESTART_WARN="$RESTART_WARN" python3 -c '
import json, os, sys
names = set(os.environ["DAEMONS"].split())
warn = int(os.environ["RESTART_WARN"])
try:
    procs = json.load(sys.stdin)
except Exception:
    sys.exit(1)
sys.exit(0 if any(
    p["name"] in names and (p.get("pm2_env") or {}).get("restart_time", 0) >= warn
    for p in procs
) else 1)
' <<<"$1"
}

# Full report. Takes one pm2 jlist snapshot per line on stdin; a second line, if
# present, is a later sample used to tell a live crash loop from a stale count.
_report() {
  DAEMONS="${DAEMONS[*]}" RETIRED="${RETIRED[*]}" RESTART_WARN="$RESTART_WARN" \
  API_PORT="$API_PORT" SAMPLE="$LOOP_SAMPLE_SECONDS" PORT_SOURCE="$PORT_SOURCE" python3 -c '
import json, os, sys, time

daemons = os.environ["DAEMONS"].split()
retired = os.environ["RETIRED"].split()
warn_at = int(os.environ["RESTART_WARN"])
api_port = os.environ["API_PORT"]
sample = os.environ["SAMPLE"]

lines = [line for line in sys.stdin.read().splitlines() if line.strip()]
if not lines:
    print("pm2 returned no process list", file=sys.stderr)
    sys.exit(1)
first = {p["name"]: p for p in json.loads(lines[0])}
later = {p["name"]: p for p in json.loads(lines[1])} if len(lines) > 1 else {}

problems, warnings = [], []


def age(started_ms):
    if not started_ms:
        return "-"
    secs = max(0, int(time.time() - started_ms / 1000))
    days, rest = divmod(secs, 86400)
    hours, rest = divmod(rest, 3600)
    mins, secs = divmod(rest, 60)
    if days:
        return "%dd%dh" % (days, hours)
    if hours:
        return "%dh%dm" % (hours, mins)
    if mins:
        return "%dm%ds" % (mins, secs)
    return "%ds" % secs


def mem(size):
    size = float(size or 0)
    for unit in ("b", "kb", "mb"):
        if size < 1024:
            return "%.0f%s" % (size, unit)
        size /= 1024
    # Decimals from gb up: the difference between 1.8gb and 2.4gb is the
    # difference between fine and about to be a problem.
    return "%.1fgb" % size if size < 1024 else "%.1ftb" % (size / 1024)


print("%-14s%-10s%9s%9s%6s%9s" % ("unit", "status", "restarts", "uptime", "cpu", "memory"))
for name in daemons:
    proc = first.get(name)
    if proc is None:
        print("%-14s%-10s%9s%9s%6s%9s" % (name, "ABSENT", "-", "-", "-", "-"))
        problems.append("%s is not registered with pm2" % name)
        continue
    env = proc.get("pm2_env") or {}
    monit = proc.get("monit") or {}
    restarts = env.get("restart_time", 0)
    status = env.get("status", "?")
    print("%-14s%-10s%9d%9s%6s%9s" % (
        name, status, restarts, age(env.get("pm_uptime")),
        "%s%%" % monit.get("cpu", 0), mem(monit.get("memory")),
    ))
    if status != "online":
        problems.append("%s is %s — a daemon should be online" % (name, status))
    if restarts >= warn_at:
        now = (later.get(name, {}).get("pm2_env") or {}).get("restart_time")
        if now is None:
            warnings.append("%s has %d restarts" % (name, restarts))
        elif now > restarts:
            problems.append(
                "%s is in an ACTIVE CRASH LOOP: +%d restarts in %ss (now %d). "
                "Diagnose with: pm2 logs %s --err --lines 60 --nostream"
                % (name, now - restarts, sample, now, name)
            )
        else:
            warnings.append(
                "%s carries %d restarts but the count is static — historical, not current"
                % (name, restarts)
            )

for name in retired:
    if name in first:
        warnings.append(
            "%s is still registered with pm2, but cron owns it now — so it "
            "cannot double-fire, run: pm2 delete %s && pm2 save" % (name, name)
        )

# Only meaningful when the port was supplied explicitly. Otherwise it was read
# from this same registration and cannot disagree.
api = first.get("lt-api")
if api and os.environ.get("PORT_SOURCE") == "env":
    args = [str(a) for a in ((api.get("pm2_env") or {}).get("args") or [])]
    if "--port" in args:
        index = args.index("--port")
        if index + 1 < len(args) and args[index + 1] != api_port:
            warnings.append(
                "LOOKUP_SERVER_PORT is %s but lt-api is registered on %s — unset the "
                "variable to follow the registration, or re-register on %s"
                % (api_port, args[index + 1], api_port)
            )

if warnings or problems:
    print()
for item in warnings:
    print("  WARN  %s" % item)
for item in problems:
    print("  FAIL  %s" % item)
if not warnings and not problems:
    print("\nAll daemons healthy.")

sys.exit(1 if problems else 0)
'
}

doctor() {
  local first second=""
  first="$(_jlist)"
  [[ -n "$first" ]] || fail "pm2 jlist returned nothing"
  if _suspicious "$first"; then
    warn "High restart count — resampling in ${LOOP_SAMPLE_SECONDS}s to see whether it is still climbing."
    sleep "$LOOP_SAMPLE_SECONDS"
    second="$(_jlist)"
  fi
  printf '%s\n%s\n' "$first" "$second" | _report
}

health() {
  log "Units:"
  pm2 list

  if ! known lt-api; then
    return 0
  fi

  # The API binds after DuckDB opens; give it a moment before calling a miss a
  # failure. A persistent miss is what `doctor` explains.
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
  warn "API did not answer on :${API_PORT} after 20s — run './scripts/server.sh doctor'"
  # A bind conflict is invisible in `pm2 list`: uvicorn completes startup, then
  # fails to bind, so the unit reports `online` while nothing is served. Name the
  # listener, because on a shared box it is usually somebody else's app.
  if command -v ss >/dev/null 2>&1; then
    local listener
    listener="$(ss -ltnp 2>/dev/null | grep ":${API_PORT} " || true)"
    if [[ -n "$listener" ]]; then
      warn "Something is already listening on :${API_PORT} — if this is not lt-api, that is the conflict:"
      printf '  %s\n' "$listener" >&2
    fi
  fi
}

cmd="${1:-restart}"

case "$cmd" in
  restart)
    apply restart "${DAEMONS[@]}"
    health
    ;;
  start)
    apply start "${DAEMONS[@]}"
    health
    ;;
  stop)
    apply stop "${DAEMONS[@]}"
    pm2 list
    ;;
  status)
    health
    ;;
  doctor)
    doctor
    exit $?
    ;;
  logs)
    # pm2 logs takes a single target; without one it tails everything, which is
    # what you want when chasing a restart.
    pm2 logs --lines 40
    ;;
  *)
    fail "Unknown command: $cmd (expected restart, start, stop, status, doctor or logs)"
    ;;
esac

log "Done. Run 'pm2 save' if you changed which units are running."
