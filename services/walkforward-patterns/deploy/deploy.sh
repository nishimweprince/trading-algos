#!/usr/bin/env bash
# Ship walkforward-patterns (+ the ta-core / ta-notify packages it imports) to the jito FRA host and (re)start it.
# Usage: services/walkforward-patterns/deploy/deploy.sh <ssh-key> <user@host> [--no-start]
# .env is copied ONLY when the host has none (it holds the hub API key; never overwritten by a deploy).
# The journal (data/) lives on the host and is never copied either way.
set -euo pipefail
KEY=$1; HOST=$2; START=${3:-}
cd "$(dirname "$0")/../../.."   # repo root
SSH=(ssh -i "$KEY" "$HOST")
DEST=walkforward-patterns
"${SSH[@]}" "mkdir -p ~/$DEST/packages ~/$DEST/services/walkforward-patterns/data"
for d in packages/ta-core packages/ta-notify; do
  rsync -az --delete -e "ssh -i $KEY" --exclude .venv --exclude __pycache__ --exclude .pytest_cache "$d/" "$HOST:$DEST/$d/"
done
rsync -az --delete -e "ssh -i $KEY" --exclude .venv --exclude __pycache__ --exclude .pytest_cache \
  --exclude data --exclude '.env' services/walkforward-patterns/ "$HOST:$DEST/services/walkforward-patterns/"
if [ -f services/walkforward-patterns/.env ]; then
  rsync -az --ignore-existing -e "ssh -i $KEY" services/walkforward-patterns/.env "$HOST:$DEST/services/walkforward-patterns/.env"
fi
"${SSH[@]}" "set -e; cd ~/$DEST
  [ -x .venv/bin/python ] || python3.12 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -e packages/ta-core -e packages/ta-notify -e services/walkforward-patterns
  [ -f services/walkforward-patterns/.env ] && chmod 600 services/walkforward-patterns/.env
  sudo cp services/walkforward-patterns/deploy/walkforward-patterns.service /etc/systemd/system/
  sudo systemctl daemon-reload
  if [ '$START' = '--no-start' ]; then echo 'installed, not started'; exit 0; fi
  if ! grep -qE '^WFP_HUB_API_KEY=.+' services/walkforward-patterns/.env 2>/dev/null; then
    echo 'installed but NOT started: WFP_HUB_API_KEY missing in .env'; exit 0; fi
  sudo systemctl enable --now walkforward-patterns && sudo systemctl restart walkforward-patterns
  sleep 3; systemctl is-active walkforward-patterns"
