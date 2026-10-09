#!/usr/bin/env bash
# Check the live shadow journal against a replay of the tape.
#   sudo systemctl start leadlag-research@shadow-verify.service
# The trader sends every completed shadow trade to the observer, which appends
# it to /data/leadlag/shadow/<day>.jsonl. Shadow decisions depend only on the
# taped events and their receive times, so replaying the tape with the same
# params (only the runs that used them) must reproduce the same trades. Writes
# replay-<run>.jsonl and .txt next to the journal and fails on a mismatch.
# A run in progress has its newest trades' exits still in the open tape file:
# live trades that exit after the tape's end count as pending, not mismatches.
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
key = lambda t: (t["follower"], t["signal_recv_ns"], t["entry_recv_ns"], t["exit_recv_ns"], t["net_bp"], t.get("exit_kind", "taker"))
ends = [int(l.split()[1]) for l in open(f"{out}/replay-{run}.txt") if l.strip().startswith("tape_end_ns")]
tape_end = max(ends, default=0)
# a finished epoch's journals are archived under <run>/ (DEPLOY.md, "Start a new epoch")
journals = sorted(glob.glob(f"{out}/[0-9]*.jsonl") + glob.glob(f"{out}/{run}/[0-9]*.jsonl"))
live_all = sorted(key(t) for f in journals for t in map(json.loads, open(f)) if t.get("run") == run)
live = [t for t in live_all if t[3] <= tape_end]  # t[3] = exit_recv_ns
pending = len(live_all) - len(live)
replayed = sorted(key(t) for t in map(json.loads, open(f"{out}/replay-{run}.jsonl")))
missing = [t for t in live if t not in set(replayed)]
extra = [t for t in replayed if t not in set(live)]
print(f"run {run}: live journal {len(live_all)} trades ({pending} exit after the tape's end), replay {len(replayed)}, "
      f"live-only {len(missing)}, replay-only {len(extra)}")
sys.exit(1 if missing or extra else 0)
PY
