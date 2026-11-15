#!/usr/bin/env bash
# Ship callisto/ to a host and (re)start the monitor + UI.
# Usage: deploy/deploy.sh <ssh-key> <user@host>
# Copies code, .env (secrets, chmod 600) and the classification cache (so old screenshots
# are recognised as reposts). The monitor state is NOT copied: a fresh host starts from the
# channel's latest post and does not replay history as alerts.
set -euo pipefail
KEY=$1; HOST=$2
cd "$(dirname "$0")/.."
SSH=(ssh -i "$KEY" "$HOST")
"${SSH[@]}" 'mkdir -p ~/callisto/data/results ~/callisto/ui ~/callisto/deploy'
rsync -az -e "ssh -i $KEY" --exclude .venv --exclude data --exclude __pycache__ \
  --exclude .pytest_cache ./ "$HOST:callisto/"
rsync -az -e "ssh -i $KEY" data/classified.jsonl "$HOST:callisto/data/"
[ -f data/results/summary.json ] && rsync -az -e "ssh -i $KEY" data/results/summary.json "$HOST:callisto/data/results/"
"${SSH[@]}" 'set -e; cd ~/callisto; chmod 600 .env
  [ -x .venv/bin/python ] || python3.12 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
  sudo cp deploy/callisto-monitor.service deploy/callisto-ui.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now callisto-monitor callisto-ui
  sudo systemctl restart callisto-monitor callisto-ui
  sleep 3; systemctl is-active callisto-monitor callisto-ui'
