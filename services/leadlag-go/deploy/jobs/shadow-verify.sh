#!/usr/bin/env bash
# Check the live shadow journal against a replay of the tape.
#   sudo systemctl start leadlag-research@shadow-verify.service
# The trader sends every completed shadow trade (and every entry attempt) to
# the observer, which appends it to /data/leadlag/shadow/<day>.jsonl.
# Shadow decisions depend only on the taped events and their receive times, so
# replaying the tape with the same params (only the runs that used them) must
# reproduce the same records. LL_SHADOW_PARAMS names the one params file.
# Writes replay.jsonl and replay.txt next to the journal and fails on a mismatch.
# A run in progress has its newest records still in the open tape file: live
# records emitted after the tape's end count as pending, not mismatches.
set -euo pipefail
bin=/home/ll/leadlag/current
params="${LL_SHADOW_PARAMS:-/home/ll/leadlag/shadow.json}"
tape="${LL_TAPE_DIR:-/data/leadlag/tape}"
out="${LL_JOURNAL_DIR:-/data/leadlag/shadow}"
mkdir -p "$out"
"$bin/leadlag" --replay "$tape" --shadow-params "$params" --shadow-trades "$out/replay.jsonl" | tee "$out/replay.txt"
python3 - "$out" "$params" <<'PY'
import glob, json, sys
out, params = sys.argv[1], sys.argv[2]
runs = [json.load(open(params))["run"]]
def key(t):
    if t.get("kind") == "attempt":
        return ("attempt", t["follower"], t["post_recv_ns"], t["end_reason"], t["filled"])
    return ("trade", t["follower"], t["signal_recv_ns"], t["entry_recv_ns"], t["exit_recv_ns"], t["net_bp"], t.get("exit_kind", "taker"))
emitted = lambda t: t.get("rec_ns") or t["exit_recv_ns"]
ends = [int(l.split()[1]) for l in open(f"{out}/replay.txt") if l.strip().startswith("tape_end_ns")]
tape_end = max(ends, default=0)
replay_all = [json.loads(l) for l in open(f"{out}/replay.jsonl")]
bad = False
for run in runs:
    # a finished epoch's journals are archived under <run>/ (DEPLOY.md, "Start a new epoch")
    journals = sorted(glob.glob(f"{out}/[0-9]*.jsonl") + glob.glob(f"{out}/{run}/[0-9]*.jsonl"))
    live_all = [t for f in journals for t in map(json.loads, open(f)) if t.get("run") == run]
    live = sorted(key(t) for t in live_all if emitted(t) <= tape_end)
    pending = len(live_all) - len(live)
    replayed = sorted(key(t) for t in replay_all if t.get("run") == run)
    missing = [t for t in live if t not in set(replayed)]
    extra = [t for t in replayed if t not in set(live)]
    print(f"run {run}: live journal {len(live_all)} records ({pending} emitted after the tape's end), replay {len(replayed)}, "
          f"live-only {len(missing)}, replay-only {len(extra)}")
    bad = bad or bool(missing or extra)
sys.exit(1 if bad else 0)
PY
