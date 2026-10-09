#!/usr/bin/env bash
# zone-trend research pipeline: Binance Vision data -> universe -> walk-forward (M4 gate).
#
# Run on the research VM, never on a laptop (tens of GB of downloads).
#   services/zone-trend/ops/research_pipeline.sh all        # everything, in order
#   services/zone-trend/ops/research_pipeline.sh data       # stages 1-4 only
#   services/zone-trend/ops/research_pipeline.sh wf         # stage 5 (needs data)
#   services/zone-trend/ops/research_pipeline.sh ablate     # stage 6 (needs a wf dir)
#
# Every stage is safe to re-run: syncs skip months already built from monthly
# archives, and symbol tapes are cached under data/cache. Run it inside tmux.
#
# Environment (all optional):
#   ZT_DATA_DIR   data root (default services/zone-trend/data; put it on the big disk)
#   ZT_FROM       first month to rank and test from (default 2020-01)
#   ZT_TO         last month incl. the 9-month holdout (default: last full month)
#   ZT_WORKERS    parallel processes for downloads and the walk-forward (default nproc)
#   ZT_DROP_RAW   1 = delete each zip after converting it to Parquet (halves disk use)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
STAGE="${1:-all}"
FROM="${ZT_FROM:-2020-01}"
TO="${ZT_TO:-$(date -u -d "$(date -u +%Y-%m-01) -1 day" +%Y-%m)}"
WORKERS="${ZT_WORKERS:-$(nproc)}"
DROP=()
[[ "${ZT_DROP_RAW:-0}" == "1" ]] && DROP=(--drop-raw)
mkdir -p logs
LOG="logs/research-$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "$LOG") 2>&1

zt() { uv run --package zone-trend zt "$@"; }
say() { printf '\n== %s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
field() { grep "^# $1" | sed 's/.*: //' || true; }

say "zone-trend research: stage=$STAGE from=$FROM to=$TO workers=$WORKERS log=$LOG"
say "commit $(git rev-parse --short HEAD); disk free: $(df -h "${ZT_DATA_DIR:-data}" 2>/dev/null | awk 'NR==2{print $4}' || echo '?')"

data_stage() {
  say "1/4 daily klines for every USDT perpetual (universe ranking input; ~1h)"
  zt data exchange-info
  zt data sync --symbols all --datasets klines --intervals 1d --from 2019-09 \
    --workers "$WORKERS" | tail -3

  say "2/4 rank; hourly klines for the candidate pool (the range filter needs them)"
  local out missing
  out="$(zt data universe --from "$FROM" --to "$TO" 2>&1)"; echo "$out" | tail -2
  missing="$(echo "$out" | field 'pool symbols without 1h data')"
  if [[ -n "$missing" ]]; then
    zt data sync --symbols "$missing" --datasets klines --intervals 1h --from 2019-09 \
      --workers "$WORKERS" | tail -3
  fi

  say "3/4 re-rank with ranges; full history for every symbol that ever makes a monthly top 10"
  out="$(zt data universe --from "$FROM" --to "$TO" 2>&1)"; echo "$out" | tail -2
  local union
  union="$(echo "$out" | field union)"
  zt data sync --symbols "$union" --datasets klines,markPriceKlines,fundingRate --from 2019-09 \
    --workers "$WORKERS" "${DROP[@]}" | tail -3

  say "4/4 validate, then quarantine-aware ranking (and sync any newcomer)"
  zt data validate --quiet | tail -10
  out="$(zt data universe --from "$FROM" --to "$TO" 2>&1)"; echo "$out" | tail -2
  local union2 extra
  union2="$(echo "$out" | field union)"
  extra="$(comm -13 <(tr , '\n' <<<"$union" | sort) <(tr , '\n' <<<"$union2" | sort) | paste -sd, -)"
  if [[ -n "$extra" ]]; then
    say "union grew by: $extra"
    zt data sync --symbols "$extra" --datasets klines,markPriceKlines,fundingRate --from 2019-09 \
      --workers "$WORKERS" "${DROP[@]}" | tail -3
    zt data validate --quiet | tail -5
    zt data universe --from "$FROM" --to "$TO" 2>&1 | tail -2
  fi
  du -sh "${ZT_DATA_DIR:-data}"/raw "${ZT_DATA_DIR:-data}"/parquet 2>/dev/null || true
}

wf_stage() {
  say "5 walk-forward over the universe (several hours; prints the acceptance table)"
  zt wf --from "$FROM" --to "$TO" --workers "$WORKERS" --out "runs/wf-$FROM-$TO" || true
  say "walk-forward written to runs/wf-$FROM-$TO (verdict above; 'fail' exits non-zero)"
}

ablate_stage() {
  say "6 ablations (each enabled filter must add OOS expectancy)"
  zt ablate --wf "runs/wf-$FROM-$TO" --component all --workers "$WORKERS"
  zt accept --wf "runs/wf-$FROM-$TO" || true
}

case "$STAGE" in
  data) data_stage ;;
  wf) wf_stage ;;
  ablate) ablate_stage ;;
  all) data_stage; wf_stage; ablate_stage ;;
  *) echo "usage: $0 all|data|wf|ablate"; exit 2 ;;
esac
say "done. The holdout is deliberately not run here: run 'zt holdout --wf runs/wf-$FROM-$TO' once, after reviewing the walk-forward."
