#!/usr/bin/env python3
"""Gauge replays on raw pd-collect captures: (1) follow profitable wallets, (2) LP on fresh PumpSwap pools,
(3) mid-curve momentum. Train = first --train-fraction of the capture by time, test = the rest.

Costs: curve fee per TradeEvent (fee_bps + creator_fee_bps), --buy-fee / --sell-fee lamports per tx (priority +
tip), pool fee --pool-fee-bps per side, our orders land --land-slots after the decision (after every tx of
that slot). Interim gauges: small samples, no model of how others react to our orders.

  python3 replay_options.py --capture-dir /var/lib/pd-collect --report-output options.md
"""
from __future__ import annotations

import argparse
import base64
import bisect
import itertools
import math
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_paths_from_capture import CREATE_EVENT, MIGRATION_EVENT, records  # noqa: E402
from fetch_curve_paths import decode_trade_event  # noqa: E402
from fetch_pool_paths import Rpc, b58encode, canonical_pool, decode_pool  # noqa: E402
from replay import bootstrap_ci  # noqa: E402

LAMPORTS = 1_000_000_000
INIT_RTOK = 793_100_000_000_000
STANDARD_GAP = 279_900_000_000_000


class Pt:
    __slots__ = ("slot", "idx", "ms", "vsol", "vtok", "rtok", "fee", "user", "is_buy", "sol", "tok")

    def __init__(self, e):
        self.slot, self.idx, self.ms = e["slot"], e["transaction_index"] * 16 + e["event_index"], e["recv_ms"]
        self.vsol, self.vtok, self.rtok = e["vsol"], e["vtok"], e["rtok"]
        self.fee = int((e.get("fee_bps") or 95) + (e.get("creator_fee_bps") or 30)) or 125
        self.user, self.is_buy, self.sol, self.tok = e["user"], e["is_buy"], e["sol_amount"], e["token_amount"]


def buy_tokens(p: Pt, gross: int) -> int:
    net = gross * (1 - p.fee / 1e4)
    return int(p.vtok - p.vtok * p.vsol / (p.vsol + net)) if p.rtok > 0 else 0


def sell_sol(p: Pt, tokens: int) -> int:
    return int((p.vsol - p.vtok * p.vsol / (p.vtok + tokens)) * (1 - p.fee / 1e4))


def state_at(pts: list[Pt], slots: list[int], slot: int) -> int | None:
    i = bisect.bisect_right(slots, slot) - 1
    return i if i >= 0 else None


def summ(xs: list[float]) -> str:
    if not xs:
        return "n=0"
    lo, hi = bootstrap_ci(xs)
    return (f"n={len(xs)} mean={statistics.fmean(xs):+.2f}% med={statistics.median(xs):+.2f}% "
            f"win={100*sum(x>0 for x in xs)/len(xs):.0f}% ci95=[{lo:+.2f},{hi:+.2f}]")


def load(capture: Path):
    curves: dict[str, list[Pt]] = defaultdict(list)
    pools: dict[str, list[tuple[int, int, int, int, int]]] = defaultdict(list)  # mint -> (slot, idx, ms, base, quote)
    migr: dict[str, tuple[int, int]] = {}
    lo = hi = None
    gaps = []
    for f in sorted(capture.glob("cap-*.bin.zst")):
        for t, recv, slot, idx, sig, payer, body in records(f):
            ms = recv // 1_000_000
            lo = ms if lo is None else min(lo, ms)
            hi = ms if hi is None else max(hi, ms)
            if t == 9:
                gaps.append(ms)
            elif t == 1:
                ordv, ev = body
                if ev[:8] == MIGRATION_EVENT and len(ev) >= 72:
                    migr.setdefault(b58encode(ev[40:72]), (slot, ms))
                elif ev[:8] != CREATE_EVENT:
                    e = decode_trade_event(ev)
                    if e:
                        e.update(slot=slot, transaction_index=idx, event_index=ordv & 0x7FFF, recv_ms=ms)
                        curves[e["mint"]].append(Pt(e))
            elif t == 2:
                pool, mint, base, quote = body
                pools[b58encode(mint)].append((slot, idx, ms, base, quote))
    for v in curves.values():
        v.sort(key=lambda p: (p.slot, p.idx))
    for v in pools.values():
        v.sort()
    return curves, pools, migr, lo, hi, gaps


# ---------------------------------------------------------------- option 1: follow wallets
def wallet_pnl(curves, t_end):
    """Per wallet: SOL P&L over trades before t_end; open tokens marked at the curve's last sell value."""
    pnl = defaultdict(float)
    buys = defaultdict(int)
    for mint, pts in curves.items():
        held = defaultdict(int)
        last = None
        for p in pts:
            if p.ms >= t_end:
                break
            last = p
            fee = p.fee / 1e4
            if p.is_buy:
                pnl[p.user] -= p.sol * (1 + fee); held[p.user] += p.tok; buys[p.user] += 1
            else:
                pnl[p.user] += p.sol * (1 - fee); held[p.user] -= p.tok
        if last:
            for u, tok in held.items():
                if tok > 0:
                    pnl[u] += sell_sol(last, tok) if last.rtok > 0 else tok * last.vsol / last.vtok
    return pnl, buys


def follow(curves, leaders: set[str], t0, t1, size, hold_s, a):
    out = []
    for mint, pts in curves.items():
        slots = [p.slot for p in pts]
        busy_until = -1
        for k, p in enumerate(pts):
            if not (t0 <= p.ms < t1) or p.user not in leaders or not p.is_buy or p.rtok == 0 or p.ms < busy_until:
                continue
            i = state_at(pts, slots, p.slot + a.land_slots)
            s = pts[i]
            if s.rtok == 0:
                continue  # completed before we land: no buy possible on the curve
            tokens = buy_tokens(s, size)
            # exit: the leader's next sell of this mint, else hold_s
            ex = next((q for q in pts[k + 1:] if q.user == p.user and not q.is_buy), None)
            limit_ms = p.ms + hold_s * 1000
            if ex is None or ex.ms > limit_ms:
                ex = next((q for q in pts[k + 1:] if q.ms >= limit_ms), None)
                if ex is None:
                    continue  # window runs past the data
            j = state_at(pts, slots, ex.slot + a.land_slots)
            e = pts[j]
            proceeds = sell_sol(e, tokens) if e.rtok > 0 else int(tokens * e.vsol / e.vtok * (1 - a.pool_fee_bps / 1e4))
            out.append(100 * (proceeds - a.sell_fee - size - a.buy_fee) / size)
            busy_until = ex.ms
    return out


# ---------------------------------------------------------------- option 3: mid-curve momentum
def momentum_features(curves):
    """Per standard curve: [(k, prog, net_10s, net_30s)] for every trade, net = signed SOL in the trailing window."""
    feats = {}
    for mint, pts in curves.items():
        if not pts or pts[0].vtok - pts[0].rtok != STANDARD_GAP:
            continue
        rows = []
        j10 = j30 = 0
        s10 = s30 = 0
        signed = [(p.sol if p.is_buy else -p.sol) for p in pts]
        for k, p in enumerate(pts):
            s10 += signed[k]; s30 += signed[k]
            while pts[j10].ms < p.ms - 10_000: s10 -= signed[j10]; j10 += 1
            while pts[j30].ms < p.ms - 30_000: s30 -= signed[j30]; j30 += 1
            if p.rtok > 0:
                rows.append((k, 1 - p.rtok / INIT_RTOK, s10, s30))
        feats[mint] = rows
    return feats


def momentum(curves, feats, t0, t1, size, P, W, M, X, SL, H, a, hi_ms):
    out = []
    for mint, rows in feats.items():
        pts = curves[mint]
        slots = None
        for k, prog, n10, n30 in rows:
            p = pts[k]
            if not (t0 <= p.ms < t1) or prog < P or prog > P + 0.1:
                continue
            if (n10 if W == 10 else n30) < M * LAMPORTS:
                continue
            if p.ms + H * 1000 > hi_ms - 5000:
                break
            if slots is None:
                slots = [q.slot for q in pts]
            i = state_at(pts, slots, p.slot + a.land_slots)
            s = pts[i]
            if s.rtok == 0:
                break
            tokens = buy_tokens(s, size)
            basis = size + a.buy_fee
            exit_pt = None
            for q in pts[i + 1:]:
                if q.rtok == 0 or q.ms >= p.ms + H * 1000:
                    exit_pt = q; break
                g = 100 * (sell_sol(q, tokens) - a.sell_fee - basis) / size
                if g >= X or g <= -SL:
                    exit_pt = q; break
            if exit_pt is None:
                break
            j = state_at(pts, slots, exit_pt.slot + a.land_slots)
            e = pts[j]
            proceeds = sell_sol(e, tokens) if e.rtok > 0 else int(tokens * e.vsol / e.vtok * (1 - a.pool_fee_bps / 1e4))
            out.append(100 * (proceeds - a.sell_fee - basis) / size)
            break  # one entry per curve
    return out


# ---------------------------------------------------------------- option 2: LP on fresh pools
def lp(pools, migr, meta, size, horizon_s, a, hi_ms):
    out = []
    for mint, pp in pools.items():
        m = meta.get(mint)
        if not m or m.get("mayhem") or len(pp) < 2:
            continue
        virt = m.get("virtual_quote", 0)
        s0 = next((x for x in pp if x[0] >= pp[0][0] + a.land_slots), None)
        if s0 is None or s0[2] + horizon_s * 1000 > min(hi_ms, pp[0][2] + 1_790_000):
            continue
        _, _, ms0, b0, q0 = s0
        half = size / 2
        # buy base with half (pool constant product incl. virtual quote), then deposit both sides
        net = half * (1 - a.pool_fee_bps / 1e4)
        bought = b0 * net / (q0 + virt + net)
        b1, q1 = b0 - bought, q0 + net
        f = min(bought / b1, half / q1)  # LP share we can mint
        h = next((x for x in pp if x[2] >= ms0 + horizon_s * 1000), pp[-1])
        _, _, _, bh, qh = h
        # withdraw our share, plus whatever did not fit the deposit ratio
        wb = f * bh + max(0.0, bought - f * b1)
        wq = f * qh + max(0.0, half - f * q1)
        sell = (qh + virt) * wb / (bh + wb) * (1 - a.pool_fee_bps / 1e4)
        value = wq + min(sell, qh)
        out.append(100 * (value - size - 2 * a.buy_fee - a.sell_fee) / size)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capture-dir", type=Path, required=True)
    ap.add_argument("--report-output", type=Path, required=True)
    ap.add_argument("--train-fraction", type=float, default=0.6)
    ap.add_argument("--buy-fee", type=int, default=1_000_000)
    ap.add_argument("--sell-fee", type=int, default=1_450_000)
    ap.add_argument("--pool-fee-bps", type=float, default=125.0)
    ap.add_argument("--land-slots", type=int, default=2)
    a = ap.parse_args()
    curves, pools, migr, lo, hi, gaps = load(a.capture_dir)
    cut = lo + int((hi - lo) * a.train_fraction)
    L = ["# Option gauges on pd-collect data", "",
         f"Capture {(hi-lo)/60000:.0f} min, {len(curves)} curves traded, {len(pools)} pools recorded, "
         f"{len(migr)} migrations, {len(gaps)} (re)connects. Train = first {a.train_fraction:.0%} by time.", ""]

    # 1. follow wallets
    pnl, nbuys = wallet_pnl(curves, cut)
    L += ["## 1. Follow profitable wallets", ""]
    for min_buys, top in ((3, 10), (3, 30), (5, 10)):
        ranked = sorted((w for w in pnl if nbuys[w] >= min_buys and pnl[w] > 0), key=lambda w: -pnl[w])[:top]
        for size_sol in (0.05, 0.25):
            for hold in (10, 60):
                size = int(size_sol * LAMPORTS)
                tr = follow(curves, set(ranked), lo, cut, size, hold, a)
                te = follow(curves, set(ranked), cut, hi, size, hold, a)
                L.append(f"- top {top} (≥{min_buys} buys) size {size_sol:g} hold≤{hold}s: train {summ(tr)} | **test {summ(te)}**")
    top10 = sorted(pnl.items(), key=lambda kv: -kv[1])[:5]
    L += ["", "Top train wallets by curve P&L: " + ", ".join(f"{w[:6]}… {v/LAMPORTS:+.2f} SOL ({nbuys[w]} buys)" for w, v in top10), ""]

    # 3. momentum
    L += ["## 3. Mid-curve momentum (standard curves, one entry per curve)", ""]
    feats = momentum_features(curves)
    for size_sol in (0.05, 0.25):
        size = int(size_sol * LAMPORTS)
        rows = []
        for P, W, M, X, SL, H in itertools.product((0.3, 0.5, 0.7), (10, 30), (1, 3, 5), (10, 20, 50), (15, 30), (30, 120)):
            tr = momentum(curves, feats, lo, cut, size, P, W, M, X, SL, H, a, hi)
            if len(tr) >= 10:
                rows.append((statistics.fmean(tr), (P, W, M, X, SL, H), tr))
        rows.sort(key=lambda r: -r[0])
        if not rows:
            L.append(f"- size {size_sol:g}: too few train entries"); continue
        for mean, prm, tr in rows[:3]:
            te = momentum(curves, feats, cut, hi, size, *prm, a, hi)
            L.append(f"- size {size_sol:g} P={prm[0]} W={prm[1]}s M={prm[2]} SOL X={prm[3]}% SL={prm[4]}% H={prm[5]}s: "
                     f"train {summ(tr)} | **test {summ(te)}**")
        allm = [statistics.fmean(r[2]) for r in rows]
        L.append(f"- size {size_sol:g}: {len(rows)} rules with ≥10 train trades; train means median {statistics.median(allm):+.2f}%, "
                 f"{sum(m > 0 for m in allm)} positive")
    L.append("")

    # 2. LP
    url = os.environ.get("HELIUS_HTTP_URL", "").strip()
    meta = {}
    if url:
        rpc = Rpc(url, 5.0)
        for mint in pools:
            pool = canonical_pool(mint)
            acct = rpc.call("getAccountInfo", [pool, {"encoding": "base64", "commitment": "confirmed"}])
            v = (acct or {}).get("value")
            if v and isinstance(v.get("data"), list):
                d = decode_pool(base64.b64decode(v["data"][0]), str(v.get("owner")), pool)
                if d:
                    meta[mint] = d
    L += ["## 2. LP on fresh PumpSwap pools (non-mayhem; half buys the token, deposit both; withdraw + sell at horizon)", "",
          f"Pools with metadata: {len(meta)} of {len(pools)} (mayhem {sum(1 for m in meta.values() if m.get('mayhem'))})."]
    for size_sol in (0.25, 0.5):
        for hz in (60, 300, 900, 1800):
            r = lp(pools, migr, meta, int(size_sol * LAMPORTS), hz, a, hi)
            L.append(f"- size {size_sol:g} SOL, hold {hz//60} min: {summ(r)}")
    L.append("")
    rep = "\n".join(L) + "\n"
    a.report_output.write_text(rep, encoding="utf-8")
    print(rep)


if __name__ == "__main__":
    main()
