#!/usr/bin/env bash
# M1 completion: the screen window (2025-10-01 → 2026-01-31) for the leaders
# and the 30 screen candidates. Candidates are drawn by `llfetch candidates`
# (30-day median volume, listed before 2025) and saved next to the archive so
# the screen is reproducible.
#   sudo systemctl start leadlag-research@fetch-screen.service
set -euo pipefail
bin=/home/ll/leadlag/current
root="${LL_ARCHIVE_ROOT:-/data/leadlag/archive}"
mkdir -p "$root"
cands="$root/screen-candidates.txt"
if [[ ! -s "$cands" ]]; then
  "$bin/llfetch" candidates --n 30 > "$cands"
fi
syms="BTCUSDT,ETHUSDT,$(cat "$cands")"
"$bin/llfetch" plan   --symbols "$syms" --from 2025-10-01 --to 2026-01-31
"$bin/llfetch" fetch  --symbols "$syms" --from 2025-10-01 --to 2026-01-31 --concurrency 6
"$bin/llfetch" verify --symbols "$syms" --from 2025-10-01 --to 2026-01-31 | tee "$root/verify-screen.txt"
