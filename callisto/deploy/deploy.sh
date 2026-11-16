#!/usr/bin/env bash
# Ship callisto/ to a host and (re)start the monitor + UI.
# Usage: deploy/deploy.sh <ssh-key> <user@host>
# Copies code and the classification cache (so old screenshots are recognised as reposts).
# .env is copied ONLY when the host has none: the host's .env holds settings edited there
# (cTrader credentials, live switches, SL_PIPS) and must never be overwritten by a deploy.
# The monitor state is NOT copied: a fresh host starts from the channel's latest post and
# does not replay history as alerts.
set -euo pipefail
KEY=$1; HOST=$2
cd "$(dirname "$0")/.."
SSH=(ssh -i "$KEY" "$HOST")
"${SSH[@]}" 'mkdir -p ~/callisto/data/results ~/callisto/ui ~/callisto/deploy'
rsync -az -e "ssh -i $KEY" --exclude .venv --exclude data --exclude __pycache__ \
  --exclude .pytest_cache --exclude '.env' --exclude '.env.bak-*' ./ "$HOST:callisto/"
rsync -az --ignore-existing -e "ssh -i $KEY" .env "$HOST:callisto/.env"
rsync -az -e "ssh -i $KEY" data/classified.jsonl "$HOST:callisto/data/"
[ -f data/results/summary.json ] && rsync -az -e "ssh -i $KEY" data/results/summary.json "$HOST:callisto/data/results/"
"${SSH[@]}" 'set -e; cd ~/callisto; chmod 600 .env
  [ -x .venv/bin/python ] || python3.12 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
  rm -rf vendor   # the executor no longer embeds the cTrader plugin
  sudo cp deploy/callisto-monitor.service deploy/callisto-ui.service \
    deploy/callisto-executor.service /etc/systemd/system/
  sudo systemctl daemon-reload
  # The executor trades through services/execution-service; it starts once .env points at it.
  if grep -qE "^EXECUTION_API_KEY=.+" .env && grep -qE "^EXECUTION_ACCOUNT=.+" .env; then
    sudo systemctl enable --now callisto-executor && sudo systemctl restart callisto-executor
  else
    echo "callisto-executor installed but not started: EXECUTION_API_KEY/ACCOUNT missing in .env"
  fi
  sudo systemctl enable --now callisto-monitor callisto-ui
  sudo systemctl restart callisto-monitor callisto-ui
  sleep 3; systemctl is-active callisto-monitor callisto-ui'
