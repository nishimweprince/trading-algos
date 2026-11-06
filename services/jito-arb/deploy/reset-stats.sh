#!/usr/bin/env bash
# Archive only runtime evidence; never pools, denylist, KILL, or /etc/jito-arb.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "reset-stats must run as root" >&2; exit 1; }
DATA=/var/lib/jito-arb
UNITS=(jito-arb.service jito-arb-ui.service jito-arb-recorder.service)
was_active=()
for u in "${UNITS[@]}"; do
  if systemctl is-active --quiet "$u"; then was_active+=("$u"); fi
done
# A failed stop may leave an open writer. Do not move anything or restart on failure.
for u in "${UNITS[@]}"; do
  if ! systemctl stop "$u"; then
    echo "failed to stop $u; no files archived; inspect services before retrying" >&2
    exit 1
  fi
done
for u in "${UNITS[@]}"; do
  state=$(systemctl show --property=ActiveState --value "$u")
  case "$state" in
    inactive|failed) ;;
    *) echo "$u is not stopped (ActiveState=$state); no files archived" >&2; exit 1 ;;
  esac
done
install -d -m 0750 -o jito-arb -g jito-arb "$DATA/archive"
# mktemp creates atomically, including for repeated resets within the same UTC second.
DEST=$(mktemp -d "$DATA/archive/$(date -u +%Y%m%dT%H%M%SZ).XXXXXX")
chown jito-arb:jito-arb "$DEST"
chmod 0750 "$DEST"
moved=0
shopt -s nullglob
for dir in decisions frames; do
  files=("$DATA/$dir"/*.bin)
  if [ "$dir" = decisions ]; then files+=("$DATA/$dir"/*.jsonl); fi
  for file in "${files[@]}"; do
    [ -f "$file" ] && [ ! -L "$file" ] || continue
    install -d -m 0750 -o jito-arb -g jito-arb "$DEST/$dir"
    mv -- "$file" "$DEST/$dir/"
    moved=$((moved + 1))
  done
done
echo "archived $moved files to $DEST"
# Always start bot and dashboard; restore the recorder only if it was running.
start=(jito-arb.service jito-arb-ui.service)
for u in "${was_active[@]}"; do
  [[ " ${start[*]} " == *" $u "* ]] || start+=("$u")
done
systemctl start "${start[@]}"
systemctl is-active "${start[@]}"
