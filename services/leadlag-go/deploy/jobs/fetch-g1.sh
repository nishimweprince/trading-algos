#!/usr/bin/env bash
# After `llresearch freeze`: the G1 window (PREREG 1.1) for the leaders and the
# frozen followers only: 2025-01-01 → 2025-09-30 and 2026-02-01 → 2026-03-31.
set -euo pipefail
bin=/home/ll/leadlag/current
prereg=/data/leadlag/research/prereg.json
syms=$(jq -r '(.leaders + .followers) | join(",")' "$prereg")
for w in "2025-01-01 2025-09-30" "2026-02-01 2026-03-31"; do
  set -- $w
  "$bin/llfetch" plan   --symbols "$syms" --from "$1" --to "$2"
  "$bin/llfetch" fetch  --symbols "$syms" --from "$1" --to "$2" --concurrency 6
  "$bin/llfetch" verify --symbols "$syms" --from "$1" --to "$2" | tee "/data/leadlag/research/verify-g1-$1.txt"
done
