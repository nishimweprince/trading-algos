#!/usr/bin/env bash
#
# Install and start one profile under launchd.
#
# Every check here corresponds to a failure that is otherwise invisible: launchd
# redirects stdout into logs/, so a process that dies before it can log leaves
# nothing behind at all, and KeepAlive turns that into a permanent 60-second
# crash loop with an empty log file.
#
#   ./ops/install.sh forex
#
set -euo pipefail

profile="${1:-}"
if [[ -z "$profile" ]]; then
  echo "usage: ops/install.sh <profile>   # e.g. forex, deriv" >&2
  exit 64
fi

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

label="com.ctrader-markets.${profile}"
plist="ops/${label}.plist"
env_file=".env.${profile}"
binary=".venv/bin/ctrader-markets"
target="${HOME}/Library/LaunchAgents/${label}.plist"

fail() { echo "error: $*" >&2; exit 1; }

# --- preflight ---------------------------------------------------------------

[[ -f "$plist" ]] || fail "no plist for profile '${profile}' (looked for ${plist})"
[[ -f "$env_file" ]] || fail "missing ${env_file}. Copy .env.example.${profile} and fill it in."
[[ -x "$binary" ]] || fail "missing ${binary}. Run: python3.12 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'"

# launchd creates StandardOutPath's file but not its parent directory. Without
# these the redirect silently fails and every log line is discarded.
mkdir -p logs data

# Placeholders pass schema validation for anything except the four secrets, and
# an unedited API_KEY is published in this repository.
if grep -qE '^[A-Z_]+=replace-with-' "$env_file"; then
  echo "error: ${env_file} still contains template placeholders:" >&2
  grep -nE '^[A-Z_]+=replace-with-' "$env_file" | sed 's/=.*/=.../' >&2
  exit 1
fi

port="$(grep -E '^PORT=' "$env_file" | tail -1 | cut -d= -f2 | tr -d '[:space:]')"
port="${port:-8010}"

# A port collision under KeepAlive is a silent crash loop, so catch it here
# rather than letting uvicorn die 60 seconds at a time. Skipped when the holder
# is this same profile being reinstalled.
if lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1; then
  if launchctl print "gui/$(id -u)/${label}" >/dev/null 2>&1; then
    echo "note: port ${port} is held by ${label}; it will be replaced."
  else
    echo "error: port ${port} is already in use by another process:" >&2
    lsof -nP -iTCP:"${port}" -sTCP:LISTEN >&2
    echo "Give this profile its own PORT in ${env_file}." >&2
    exit 1
  fi
fi

# Two profiles sharing a token cache mutually invalidate each other's rotated
# refresh tokens, which can only be undone by redoing the browser OAuth flow.
cache_path="$(grep -E '^TOKEN_CACHE_PATH=' "$env_file" | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
if [[ -n "$cache_path" ]]; then
  for other in .env.*; do
    [[ "$other" == "$env_file" || "$other" == .env.example.* ]] && continue
    if grep -qE "^TOKEN_CACHE_PATH=${cache_path}$" "$other" 2>/dev/null; then
      fail "${other} uses the same TOKEN_CACHE_PATH (${cache_path}). Refresh rotation would kill both."
    fi
  done
fi

# --- install -----------------------------------------------------------------

mkdir -p "${HOME}/Library/LaunchAgents"
cp "$plist" "$target"

# bootout first so a reinstall picks up the new plist. `|| true` because the
# unit legitimately may not be loaded yet.
launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$target"
launchctl kickstart -k "gui/$(id -u)/${label}"

echo "Installed ${label}."
echo
echo "  Health:  curl -s localhost:${port}/health/ready | jq .details"
echo "  Logs:    tail -f ${repo}/logs/${profile}.log"
echo "  Events:  tail -f ${repo}/logs/events.${profile}.jsonl"
echo "  Restart: launchctl kickstart -k gui/$(id -u)/${label}"
echo "  Remove:  launchctl bootout gui/$(id -u)/${label} && rm ${target}"
echo
echo "It takes a few seconds to authenticate and resolve symbols before"
echo "/health/ready reports 'ready'."
