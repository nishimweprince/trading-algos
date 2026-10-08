#!/usr/bin/env bash
# PumpDesk Frankfurt setup — run as the `pumpdesk` user on the new VM.
# Idempotent: re-running updates the checkout and dependencies.
#   bash deploy/fra/setup.sh            (from a checkout)  or
#   curl … | bash                       (first time, see README §3)
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/nishimweprince/trading-algos.git}"
BRANCH="${BRANCH:-feat/fra-lean}"
NODE_VERSION="${NODE_VERSION:-26}"
ROOT="$HOME/trading-algos"

# 1. Node via nvm (matches the old host's nvm layout).
if [ ! -d "$HOME/.nvm" ]; then
  curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
fi
export NVM_DIR="$HOME/.nvm"
# shellcheck disable=SC1091
. "$NVM_DIR/nvm.sh"
nvm install "$NODE_VERSION" >/dev/null
nvm alias default "$NODE_VERSION" >/dev/null
command -v pm2 >/dev/null || npm install -g pm2 >/dev/null

# 2. Checkout.
if [ ! -d "$ROOT/.git" ]; then
  git clone --branch "$BRANCH" "$REPO_URL" "$ROOT"
else
  git -C "$ROOT" fetch origin "$BRANCH"
  git -C "$ROOT" checkout "$BRANCH"
  git -C "$ROOT" pull --ff-only origin "$BRANCH"
fi

# 3. Dependencies + dashboard bundle.
cd "$ROOT/pump-fun"
npm ci
npm run dashboard:build
mkdir -p data data/backup

# 4. Sanity: typecheck + tests (fast; catches a bad checkout before pm2 does).
npm run typecheck
npx vitest run --reporter=dot

if [ ! -f .env ]; then
  echo "!! $ROOT/pump-fun/.env is missing — create it (README §4) before starting pm2." >&2
  exit 1
fi
echo "setup complete. Next: node --experimental-strip-types deploy/fra/probe-latency.ts, then README §6."
