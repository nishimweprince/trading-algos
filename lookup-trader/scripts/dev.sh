#!/usr/bin/env bash
# Start the FastAPI server and Vite client together.
# Usage: ./scripts/dev.sh
# Stop with Ctrl+C — both processes are terminated.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_DIR="$ROOT/server"
CLIENT_DIR="$ROOT/client"
SERVER_PORT="${LOOKUP_SERVER_PORT:-8001}"
CLIENT_PORT="${LOOKUP_CLIENT_PORT:-5173}"
SERVER_PYTHON="${LOOKUP_SERVER_PYTHON:-$ROOT/.venv/bin/python}"

if [[ ! -x "$SERVER_PYTHON" ]]; then
  fail "Missing $ROOT/.venv/bin/python. Create it with: /opt/homebrew/bin/python3.12 -m venv .venv && .venv/bin/pip install -e './server[dev]'"
fi

"$SERVER_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
  || fail "Server needs Python 3.11+ (got $($SERVER_PYTHON -c 'import sys; print(sys.version.split()[0])')). Use LOOKUP_SERVER_PYTHON or recreate .venv."

SERVER_PID=""
CLIENT_PID=""

log() {
  printf '\033[1;36m[dev]\033[0m %s\n' "$*"
}

fail() {
  printf '\033[1;31m[dev]\033[0m %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"
}

cleanup() {
  local exit_code=$?
  log "Shutting down…"

  if [[ -n "$CLIENT_PID" ]] && kill -0 "$CLIENT_PID" 2>/dev/null; then
    kill "$CLIENT_PID" 2>/dev/null || true
    wait "$CLIENT_PID" 2>/dev/null || true
  fi

  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi

  exit "$exit_code"
}

trap cleanup EXIT INT TERM

require_cmd npm

if [[ ! -d "$CLIENT_DIR/node_modules" ]]; then
  log "Installing client dependencies…"
  (cd "$CLIENT_DIR" && npm install)
fi

if [[ ! -f "$ROOT/data/engine.duckdb" ]]; then
  log "Bootstrapping DuckDB (first run)…"
  (cd "$SERVER_DIR" && PYTHONPATH=. "$SERVER_PYTHON" -m app.db.bootstrap)
fi

log "Starting API  → http://localhost:${SERVER_PORT}"
(
  cd "$SERVER_DIR"
  PYTHONPATH=. "$SERVER_PYTHON" -m uvicorn app.main:app --reload --host 127.0.0.1 --port "$SERVER_PORT"
) &
SERVER_PID=$!

# Give the API a moment to bind before the client proxies to it.
sleep 1

log "Starting client → http://localhost:${CLIENT_PORT}"
(
  cd "$CLIENT_DIR"
  npm run dev -- --host 127.0.0.1 --port "$CLIENT_PORT"
) &
CLIENT_PID=$!

log "Both apps running. Press Ctrl+C to stop."
wait -n "$SERVER_PID" "$CLIENT_PID" 2>/dev/null || wait
