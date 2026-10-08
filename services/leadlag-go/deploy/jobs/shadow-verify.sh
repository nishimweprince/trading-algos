#!/usr/bin/env bash
# Check the live shadow journal against a replay of the tape.
#   sudo systemctl start leadlag-research@shadow-verify.service
# The trader sends every completed shadow trade to the observer, which appends
# it to /data/leadlag/shadow/<day>.jsonl. Shadow decisions depend only on the
# taped events and their receive times, so replaying the tape with the same
# params (only the runs that used them) must reproduce the same trades. Writes
# replay-<run>.jsonl and .txt next to the journal and fails on a mismatch.
# A run still in progress has trades whose exits are not taped yet: run it
# after a restart, or expect the replay to trail the journal by a few trades.
set -euo pipefail
bin=/home/ll/leadlag/current
params="${LL_SHADOW_PARAMS:-/home/ll/leadlag/shadow.json}"
tape="${LL_TAPE_DIR:-/data/leadlag/tape}"
out="${LL_JOURNAL_DIR:-/data/leadlag/shadow}"
run="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["run"])' "$params")"
mkdir -p "$out"
"$bin/leadlag" --replay "$tape" --shadow-params "$params" --shadow-trades "$out/replay-$run.jsonl" | tee "$out/replay-$run.txt"
python3 - "$out" "$run" <<'PY'
import glob, json, sys
out, run = sys.argv[1], sys.argv[2]
key = lambda t: (t["follower"], t["signal_recv_ns"], t["entry_recv_ns"], t["exit_recv_ns"], t["net_bp"])
live = sorted(key(t) for f in sorted(glob.glob(f"{out}/[0-9]*.jsonl")) for t in map(json.loads, open(f)) if t.get("run") == run)
replayed = sorted(key(t) for t in map(json.loads, open(f"{out}/replay-{run}.jsonl")))
missing = [t for t in live if t not in set(replayed)]
extra = [t for t in replayed if t not in set(live)]
print(f"run {run}: live journal {len(live)} trades, replay {len(replayed)}, live-only {len(missing)}, replay-only {len(extra)}")
sys.exit(1 if missing or extra else 0)
PY
