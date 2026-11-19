#!/usr/bin/env python3
"""Build curve-paths-v2.json.gz (the replay_curve.py input) from pd-collect captures, without per-curve RPC.

pd-collect records every pump.fun event, migrate txs and the canonical pool's vault balances after each
completion (see collector/src/main.rs for the record layout). This rebuilds what fetch_curve_paths.py used to
fetch: for every curve whose completion cost first fell into (0, --admit-cost-sol], the curve's state after each
trade from admission (minus a lookback) to the migrate (or a stall horizon), the holder history since the create,
and the first --pool-horizon-seconds of its canonical pool. The only RPC is one getAccountInfo per migrated pool
(virtual quote reserve, mayhem flag), read from HELIUS_HTTP_URL when set; without it those pools are left out.

  python3 pump-desk/research/build_paths_from_capture.py --capture-dir /var/lib/pd-collect \
      --output pump-desk/research/data/curve-paths-v2.json.gz
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import struct
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_curve_paths import decode_trade_event  # noqa: E402
from fetch_pool_paths import (  # noqa: E402
    PUMP_PROGRAM, Rpc, b58encode, canonical_pool, decode_pool, find_program_address, b58decode,
)

HDR = struct.Struct("<BQQQ64s32s")
MIGRATION_EVENT = bytes.fromhex("bde95db95c94ea94")  # sha256("event:CompletePumpAmmMigrationEvent")[:8]
CREATE_EVENT = bytes.fromhex("1b72a94ddeeb6376")     # sha256("event:CreateEvent")[:8]
LAMPORTS = 1_000_000_000


def records(path: Path):
    """Yield (type, recv_ns, slot, idx, sig, payer, body) from one .bin.zst (a truncated tail is tolerated)."""
    proc = subprocess.Popen(["zstd", "-dcq", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    data = proc.stdout.read()
    proc.wait()
    p = 0
    n = len(data)
    while p + HDR.size <= n:
        t, recv, slot, idx, sig, payer = HDR.unpack_from(data, p)
        p += HDR.size
        if t == 1:
            if p + 4 > n: break
            ordv, ln = struct.unpack_from("<HH", data, p)
            if p + 4 + ln > n: break
            body = (ordv, data[p + 4:p + 4 + ln]); p += 4 + ln
        elif t == 2:
            if p + 80 > n: break
            body = (data[p:p + 32], data[p + 32:p + 64]) + struct.unpack_from("<QQ", data, p + 64); p += 80
        elif t in (3, 9):
            body = None
        else:
            break  # unknown type: stop rather than misparse
        yield t, recv, slot, idx, sig, payer, body


def completion_cost_sol(e: dict[str, Any]) -> float:
    """SOL (gross of fee) to buy every remaining real token, as replay_curve.completion_cost."""
    rtok, vtok, vsol = e["rtok"], e["vtok"], e["vsol"]
    if rtok <= 0 or vtok <= rtok:
        return 0.0
    net = vsol * vtok // (vtok - rtok) - vsol
    fee = (e.get("fee_bps") or 95) + (e.get("creator_fee_bps") or 30)
    return net / (1 - fee / 1e4) / LAMPORTS


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capture-dir", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--admit-cost-sol", type=float, default=5.0)
    ap.add_argument("--lookback-seconds", type=int, default=5)
    ap.add_argument("--stall-horizon-seconds", type=int, default=900)
    ap.add_argument("--pool-horizon-seconds", type=int, default=60)
    args = ap.parse_args()

    trades: dict[str, list[dict[str, Any]]] = defaultdict(list)  # mint -> trade points (recv order)
    created: dict[str, int] = {}  # mint -> create recv ms
    migrations: dict[str, dict[str, Any]] = {}
    pool_pts: dict[str, list[dict[str, Any]]] = defaultdict(list)  # mint -> points
    gaps: list[int] = []  # recv ms of every (re)connect: data before it may be missing
    first_ms = None
    last_ms = 0
    files = sorted(args.capture_dir.glob("cap-*.bin.zst"))
    for f in files:
        mig_txs: dict[bytes, tuple[int, int, int, str]] = {}
        for t, recv, slot, idx, sig, payer, body in records(f):
            ms = recv // 1_000_000
            first_ms = ms if first_ms is None else first_ms
            last_ms = max(last_ms, ms)
            if t == 9:
                gaps.append(ms)
            elif t == 3:
                mig_txs[sig] = (ms, slot, idx, b58encode(payer))
            elif t == 1:
                ordv, ev = body
                if ev[:8] == CREATE_EVENT:
                    # CreateEvent: name, symbol, uri (borsh strings) then mint
                    try:
                        q = 8
                        for _ in range(3):
                            (ln,) = struct.unpack_from("<I", ev, q); q += 4 + ln
                        created.setdefault(b58encode(ev[q:q + 32]), ms)
                    except struct.error:
                        pass
                elif ev[:8] == MIGRATION_EVENT and len(ev) >= 72:
                    mint = b58encode(ev[40:72])
                    m = mig_txs.get(sig) or (ms, slot, idx, b58encode(payer))
                    migrations.setdefault(mint, {"migration_ts": m[0], "migration_slot": m[1],
                                                 "migration_signature": b58encode(sig), "caller": m[3]})
                else:
                    e = decode_trade_event(ev)
                    if e:
                        e.update(signature=b58encode(sig), slot=slot, transaction_index=idx,
                                 event_index=ordv & 0x7FFF, recv_ms=ms, signer=b58encode(payer))
                        trades[e["mint"]].append(e)
            elif t == 2:
                pool, mint, base, quote = body
                pool_pts[b58encode(mint)].append({"slot": slot, "transaction_index": idx, "recv_ms": ms,
                                                   "base": base, "quote": quote})
    print(f"files {len(files)}, mints traded {len(trades)}, creates seen {len(created)}, "
          f"migrations {len(migrations)}, reconnects {len(gaps)}", file=sys.stderr)

    url = os.environ.get("HELIUS_HTTP_URL", "").strip()
    rpc = Rpc(url, 5.0) if url else None
    out = []
    for mint, pts in trades.items():
        pts.sort(key=lambda e: (e["slot"], e["transaction_index"], e["event_index"]))
        admit = next((e for e in pts if 0 < completion_cost_sol(e) <= args.admit_cost_sol), None)
        if not admit:
            continue
        start_ms = admit["recv_ms"] - args.lookback_seconds * 1000
        mig = migrations.get(mint)
        end_ms = mig["migration_ts"] + 2000 if mig else admit["recv_ms"] + args.stall_horizon_seconds * 1000
        if end_ms > last_ms - 5000 or start_ms < (first_ms or 0) + 5000:
            continue  # the window is not fully inside the capture
        window = [e for e in pts if start_ms <= e["recv_ms"] <= end_ms]
        prefix = [e for e in pts if e["recv_ms"] < start_ms]
        truncated = any(start_ms <= g <= end_ms for g in gaps)
        curve = b58encode(find_program_address([b"bonding-curve", b58decode(mint)], PUMP_PROGRAM))
        cp = [{k: e.get(k) for k in ("signature", "slot", "transaction_index", "event_index", "signer", "user",
                                       "is_buy", "sol_amount", "token_amount", "creator", "vsol", "vtok", "rsol",
                                       "rtok", "fee_bps", "creator_fee_bps")} | {"block_time": e["recv_ms"] // 1000}
              for e in window]
        rec: dict[str, Any] = {
            "curve": curve, "mint": mint, "admit_ts": admit["recv_ms"], "admit_slot": admit["slot"],
            "admit_cost_sol": completion_cost_sol(admit), "status": "ok" if cp else "no_trades",
            "curve_pages": 0, "curve_truncated": truncated, "curve_points": cp,
            "creator": next((e.get("creator") for e in pts if e.get("creator")), None),
            "holder_pages": 0,
            "holder_truncated": mint not in created or any(created[mint] < g <= start_ms for g in gaps),
            "holder_trades": [[e["slot"], e["transaction_index"] * 16 + e["event_index"], e["user"], e["is_buy"],
                               e["token_amount"]] for e in prefix],
            "source": "pd-collect",
        }
        if mig:
            rec.update(mig)
            pool_addr = canonical_pool(mint)
            horizon = mig["migration_ts"] + args.pool_horizon_seconds * 1000
            pp = sorted((p for p in pool_pts.get(mint, []) if p["recv_ms"] <= horizon),
                        key=lambda p: (p["slot"], p["transaction_index"]))
            pool: dict[str, Any] = {"pool": pool_addr, "points": [{k: p[k] for k in ("slot", "transaction_index", "base", "quote")} for p in pp],
                                    "truncated": any(mig["migration_ts"] - 2000 <= g <= horizon for g in gaps)}
            meta = None
            if rpc:
                acct = rpc.call("getAccountInfo", [pool_addr, {"encoding": "base64", "commitment": "confirmed"}])
                v = (acct or {}).get("value")
                if v and isinstance(v.get("data"), list):
                    meta = decode_pool(base64.b64decode(v["data"][0]), str(v.get("owner")), pool_addr)
            if meta:
                pool.update(meta)
                pool["status"] = "ok" if pp else "no_points"
            else:
                pool["status"] = "no_metadata" if rpc else "no_rpc"
            rec["pool"] = pool
        out.append(rec)
    out.sort(key=lambda r: r["admit_ts"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as h:
        json.dump({"schema": 2, "generated_at": int(time.time()), "source": "pd-collect", "records": out},
                  h, separators=(",", ":"))
    tmp.replace(args.output)
    mig_n = sum(1 for r in out if "migration_ts" in r)
    print(f"wrote {args.output}: {len(out)} admitted curves, {mig_n} migrated, "
          f"{sum(1 for r in out if r['curve_truncated'])} truncated", file=sys.stderr)


if __name__ == "__main__":
    main()
