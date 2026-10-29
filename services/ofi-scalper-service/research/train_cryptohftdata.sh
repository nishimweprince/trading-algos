#!/usr/bin/env bash
# OFI scalper training on CryptoHFTData history (research VM, Tokyo).
#   tmux new-session -d -s train "bash research/train_cryptohftdata.sh >> ~/logs/train.log 2>&1"
# 1. waits for the bulk conversion (tmux "convert") and retries failed days once;
# 2. runs the binding pipeline: features (live-code replay), CatBoost per
#    horizon x barrier, selection on validation months, gates on the held-out month.
# Rerunnable: finished days, features, candidates and the selection are skipped.
set -uo pipefail
cd ~/trading-algos/services/ofi-scalper-service
PY=../../.venv/bin/python
START=2026-06-01
END=2026-10-28
RUN=${RUN:-chd-binding-1}
JOBS=${JOBS:-4}
say() { echo "$(date -u +%FT%TZ) $*"; }

say "waiting for conversion"
while tmux has-session -t convert 2>/dev/null; do sleep 60; done
say "retry pass"
PYTHONPATH=src $PY -m research.collect.cryptohftdata --start $START --end $END \
  --dest /data/ofi/raw/cryptohftdata --keep-parquet /data/ofi/parquet --jobs 4 >> ~/logs/convert.log 2>&1

say "pipeline $RUN jobs=$JOBS"
$PY -m research.pipeline --profile dev --binding --run "$RUN" --from $START --to $END \
  --host-tag cryptohftdata --horizons 300,600,900 --barrier-bp "300:20|25,600:25|30,900:30|40" \
  --order-rtt-ms 20 --row-stride 50 --jobs "$JOBS"
say "pipeline exit $?"
