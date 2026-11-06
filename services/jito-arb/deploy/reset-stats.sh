#!/usr/bin/env bash
# Start the dashboard and decision stats from zero: archive the decision logs (and recorder frames) to
# /var/lib/jito-arb/archive/<UTC time>/, then restart the services. Counters, the dashboard history, the tip
# controller and the untradable memo live in memory and reset with the restart.
# Never touches pools.json (the units need it), config/denylist.json, a KILL file, or /etc/jito-arb.
#   sudo services/jito-arb/deploy/reset-stats.sh
set -euo pipefail
DATA=/var/lib/jito-arb
STAMP=$(date -u +%Y%m%d-%H%M%S)
DEST="$DATA/archive/$STAMP"
UNITS=(jito-arb.service jito-arb-ui.service jito-arb-recorder.service)

was_active=()
for u in "${UNITS[@]}"; do
  if systemctl is-active --quiet "$u"; then was_active+=("$u"); fi
done
systemctl stop "${UNITS[@]}" 2>/dev/null || true

install -d -m 0750 -o jito-arb -g jito-arb "$DATA/archive" "$DEST"
moved=0
for dir in decisions frames; do
  [ -d "$DATA/$dir" ] || continue
  shopt -s nullglob
  files=("$DATA/$dir"/*.bin)
  shopt -u nullglob
  if [ ${#files[@]} -gt 0 ]; then
    install -d -m 0750 -o jito-arb -g jito-arb "$DEST/$dir"
    mv "${files[@]}" "$DEST/$dir/"
    moved=$((moved + ${#files[@]}))
  fi
done
echo "archived $moved files to $DEST"

# Restart what was running, and always the bot and the dashboard.
start=(jito-arb.service jito-arb-ui.service)
for u in "${was_active[@]}"; do
  [[ " ${start[*]} " == *" $u "* ]] || start+=("$u")
done
systemctl start "${start[@]}"
systemctl is-active "${start[@]}"
