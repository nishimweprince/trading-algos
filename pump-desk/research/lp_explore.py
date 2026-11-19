#!/usr/bin/env python3
"""Explore LP on fresh PumpSwap pools from pd-collect captures: what drives the result and which levers help.

Per non-mayhem canonical pool (vault balances after every recorded tx, virtual quote from the pool account):
  x = quote + virtual, k = x * base, price = x / base
  fee factor  F = sqrt(k_exit / k_entry)          (LP fee income per share, if nobody else adds/removes liquidity)
  LP gross    G = (q_exit + b_exit * p_exit) / (q_entry + b_entry * p_entry) - 1  for a fixed share
  net         = G - costs (half the deposit buys the token through the pool, the withdrawn token is sold back,
                three txs)
Levers: entry delay after the migrate, early-volume and early-price filters, hold time, stop-loss.
Caveat: other LPs' deposits/withdrawals also move k and the reserves; not separable from vault balances alone.

  HELIUS_HTTP_URL=... python3 lp_explore.py --capture-dir /var/lib/pd-collect
"""
from __future__ import annotations

import argparse
import base64
import itertools
import math
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_paths_from_capture import MIGRATION_EVENT, records  # noqa: E402
from fetch_pool_paths import Rpc, b58encode, canonical_pool, decode_pool  # noqa: E402
from replay import bootstrap_ci  # noqa: E402

LAMPORTS = 1_000_000_000


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def summ(xs):
    if not xs:
        return "n=0"
    lo, hi = bootstrap_ci(xs)
    return (f"n={len(xs):3d} mean={statistics.fmean(xs):+6.2f}% med={statistics.median(xs):+6.2f}% "
            f"win={100*sum(x>0 for x in xs)/len(xs):3.0f}% ci95=[{lo:+.2f},{hi:+.2f}]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture-dir", type=Path, required=True)
    ap.add_argument("--size-sol", type=float, default=0.5)
    ap.add_argument("--pool-fee-bps", type=float, default=125.0)
    ap.add_argument("--tx-fee-lamports", type=int, default=1_200_000)
    a = ap.parse_args()

    pools = defaultdict(list)  # mint -> [(ms, base, quote)]
    migr = {}
    hi_ms = 0
    restarts = []
    for f in sorted(a.capture_dir.glob("cap-*.bin.zst")):
        for t, recv, slot, idx, sig, payer, body in records(f):
            ms = recv // 1_000_000
            hi_ms = max(hi_ms, ms)
            if t == 9:
                restarts.append(ms)
            elif t == 2:
                pools[b58encode(body[1])].append((slot, idx, ms, body[2], body[3]))
            elif t == 1 and body[1][:8] == MIGRATION_EVENT and len(body[1]) >= 72:
                migr.setdefault(b58encode(body[1][40:72]), ms)
    rpc = Rpc(os.environ["HELIUS_HTTP_URL"], 5.0)
    series = {}
    for mint, pts in pools.items():
        pts.sort()
        acct = rpc.call("getAccountInfo", [canonical_pool(mint), {"encoding": "base64", "commitment": "confirmed"}])
        v = (acct or {}).get("value")
        meta = decode_pool(base64.b64decode(v["data"][0]), str(v.get("owner")), canonical_pool(mint)) if v else None
        if not meta or meta.get("mayhem"):
            continue
        virt = meta.get("virtual_quote", 0)
        t0 = pts[0][2]
        # watched for 1800 s from completion; a collector restart drops the watch
        end = min(hi_ms - 5000, t0 + 1_790_000, next((r for r in restarts if r > t0), 10**15))
        s = [((ms - t0) / 1000, b, qq, qq + virt) for _, _, ms, b, qq in pts if ms <= end and b > 0]
        if len(s) >= 3:
            series[mint] = (s, (end - t0) / 1000)
    print(f"non-mayhem pools with paths: {len(series)} (of {len(pools)} recorded)")

    # ---- descriptive: fee yield and price over time after the migrate
    print("\n## What happens after the migrate (per pool, from the first recorded state)")
    for h in (5, 60, 300, 900, 1500):
        F, P, V = [], [], []
        for mint, (s, dur) in series.items():
            if dur < h:
                continue
            e = next(p for p in s if p[0] >= h) if s[-1][0] >= h else s[-1]
            k0, kh = s[0][3] * s[0][1], e[3] * e[1]
            F.append(100 * (math.sqrt(kh / k0) - 1))
            P.append(100 * ((e[3] / e[1]) / (s[0][3] / s[0][1]) - 1))
            vol = sum(abs(s[i][2] - s[i - 1][2]) for i in range(1, len(s)) if s[i][0] <= h) / LAMPORTS
            V.append(vol)
        if F:
            print(f"- {h:4d}s: pools {len(F):3d} | fee yield sqrt(k) med {statistics.median(F):+.2f}% p90 {q(F,.9):+.2f}% | "
                  f"price med {statistics.median(P):+.1f}% p10 {q(P,.1):+.1f}% p90 {q(P,.9):+.1f}% | "
                  f"quote volume med {statistics.median(V):.0f} SOL p90 {q(V,.9):.0f}")

    size = a.size_sol * LAMPORTS
    fee = a.pool_fee_bps / 1e4

    def trade(s, dur, d, h, stop):
        """LP from the first state at >= d s after the first record to d+h s (or the stop). None if not covered."""
        if dur < d + h:
            return None
        i0 = next((i for i, p in enumerate(s) if p[0] >= d), None)
        if i0 is None:
            return None
        _, b0, q0, x0 = s[i0]
        half = size / 2
        net = half * (1 - fee)
        bought = b0 * net / (x0 + net)
        b1, q1, x1 = b0 - bought, q0 + net, x0 + net
        f = min(bought / b1, half / q1)
        lb, lq = bought - f * b1, half - f * q1  # leftovers that did not fit the ratio
        def value(p):
            _, b, qq, x = p
            wb, wq = f * b + lb, f * qq + lq
            return wq + min(x * wb / (b + wb) * (1 - fee), qq)
        exit_p = None
        for p in s[i0 + 1:]:
            if p[0] >= s[i0][0] + h:
                exit_p = p; break
            if stop and value(p) < size * (1 - stop):
                exit_p = p; break
        if exit_p is None:
            exit_p = s[-1]
        return 100 * (value(exit_p) - size - 3 * a.tx_fee_lamports) / size

    def early(s, d):
        """(quote volume SOL, price change %) over the first d seconds."""
        pre = [p for p in s if p[0] <= d]
        vol = sum(abs(pre[i][2] - pre[i - 1][2]) for i in range(1, len(pre))) / LAMPORTS
        pc = 100 * ((pre[-1][3] / pre[-1][1]) / (s[0][3] / s[0][1]) - 1)
        return vol, pc

    print(f"\n## LP grid, size {a.size_sol:g} SOL, pool fee {a.pool_fee_bps:g} bps/side on the token leg, "
          f"{a.tx_fee_lamports} lamports per tx x3")
    print("\n### Entry delay x hold (no filter, no stop)")
    for d, h in itertools.product((5, 30, 60, 180, 300), (60, 300, 900, 1500)):
        r = [x for x in (trade(s, dur, d, h, 0) for s, dur in series.values()) if x is not None]
        print(f"- delay {d:3d}s hold {h:4d}s: {summ(r)}")
    print("\n### Filters at delay 60 s, hold 300 s")
    feats = {m: early(s, 60) for m, (s, dur) in series.items()}
    vols = sorted(v for v, _ in feats.values())
    for label, keep in (
        ("all", lambda v, pc: True),
        ("early volume >= median", lambda v, pc: v >= statistics.median(vols)),
        ("early volume >= p75", lambda v, pc: v >= q(vols, .75)),
        ("price since migrate >= -10%", lambda v, pc: pc >= -10),
        ("price since migrate >= 0", lambda v, pc: pc >= 0),
        ("volume >= median and price >= -10%", lambda v, pc: v >= statistics.median(vols) and pc >= -10),
    ):
        r = [x for m, (s, dur) in series.items() if keep(*feats[m])
             for x in [trade(s, dur, 60, 300, 0)] if x is not None]
        print(f"- {label:38s}: {summ(r)}")
    print("\n### Stop-loss (delay 5 s)")
    for h, stop in itertools.product((180, 300, 600), (0, 0.05, 0.08, 0.10, 0.15)):
        r = [x for x in (trade(s, dur, 5, h, stop) for s, dur in series.values()) if x is not None]
        print(f"- hold {h:4d}s stop {int(stop*100):2d}%: {summ(r)}")


if __name__ == "__main__":
    main()
