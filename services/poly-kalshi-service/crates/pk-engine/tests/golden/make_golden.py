"""Provenance of the golden files: a synthetic tape and the Python engine's journals on it.

Run once, against the Python engine (build noted in README.md), before the Python
service was deleted:

    python crates/pk-engine/tests/golden/make_golden.py

It writes, next to itself:
- ``tape.txt.gz``: ``<recv_ns> <venue> <frame>`` lines (the legacy tape format) for 3
  assets over 4 windows: Kalshi snapshots and fractional deltas, Polymarket books,
  price changes (with venue timestamps, best bid/ask), list-wrapped frames,
  ``best_bid_ask`` and ``market_resolved`` events, whole-venue and sharded
  reconnects, window/resolution control lines, a PolyBolt line and a bad frame.
- ``<config>.jsonl``: the ``full``-role engine's journal for each config below.
"""

from __future__ import annotations

import gzip
import json
import math
import random
import sys
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVICE = HERE.parents[3]
sys.path[:0] = [str(SERVICE / "src")]

from poly_kalshi_service.decision import StrategyParams  # noqa: E402
from poly_kalshi_service.engine import Engine  # noqa: E402
from poly_kalshi_service.ledger import Ledger  # noqa: E402
from poly_kalshi_service.risk import RiskLimits, RiskState  # noqa: E402
from poly_kalshi_service.shadow import DEFAULT_VARIANTS, Shadow, parse_variants  # noqa: E402
from poly_kalshi_service.venue import PaperVenue  # noqa: E402

NS = 1_000_000_000
OPEN = 1_791_297_900
WINDOWS = 8
ASSETS = ("BTC", "ETH", "SOL", "XRP")


def ids(asset: str, o: int) -> dict:
    return {
        "type": "window", "asset": asset, "open_unix": o,
        "kalshi_ticker": f"KX{asset}15M-{o}", "yes_token": f"{abs(hash((asset, o, 'y'))) % 10**20}{o}",
        "no_token": f"{abs(hash((asset, o, 'n'))) % 10**20}{o}", "condition_id": f"0x{asset.lower()}{o:x}",
        "fee_rate": 0.07, "fee_exponent": 1.0,
    }


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def make_tape(rng: random.Random) -> list[tuple[int, str, str]]:
    lines: list[tuple[int, str, str]] = []

    def add(t: float, venue: str, body) -> None:
        text = body if isinstance(body, str) else json.dumps(body, separators=(",", ":"))
        lines.append((int(t * NS) + rng.randrange(1000), venue, text))

    win = {}
    for w in range(WINDOWS):
        o = OPEN + w * 900
        for a in ASSETS:
            win[(a, o)] = {**ids(a, o), "yes_token": f"{a}y{o}{rng.randrange(10**9)}",
                           "no_token": f"{a}n{o}{rng.randrange(10**9)}"}
            add(o - 60 + rng.random(), "c", win[(a, o)])
        # The windows themselves.
        for a in ASSETS:
            ident = win[(a, o)]
            q = rng.uniform(0.3, 0.7)          # the "true" probability
            poly_q = q                         # Polymarket follows with a lag
            t = o + rng.uniform(0.2, 1.5)
            kb = {}  # Kalshi yes bids: price -> size (deci-cents)
            kn = {}  # Kalshi no bids
            # Kalshi snapshot
            kp = round(q - 0.01, 2)
            for i in range(3):
                kb[round(kp - 0.01 * i, 4)] = round(rng.uniform(50, 900), 2)
                kn[round(1 - kp - 0.02 - 0.01 * i, 4)] = round(rng.uniform(50, 900), 2)
            add(t, "k", {"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": {
                "market_ticker": ident["kalshi_ticker"],
                "yes_dollars_fp": [[f"{p:.4f}", f"{s:.2f}"] for p, s in kb.items()],
                "no_dollars_fp": [[f"{p:.4f}", f"{s:.2f}"] for p, s in kn.items()]}})

            def poly_levels(mid: float):
                bid = clamp(round(mid - 0.01 * rng.choice([1, 1, 1, 2, 5, 12]) / 2, 2), 0.01, 0.98)
                ask = clamp(round(bid + 0.01 * rng.choice([1, 1, 1, 2, 3, 11]), 2), bid + 0.01, 0.99)
                bids = [(round(bid - 0.01 * i, 2), round(rng.uniform(5, 400), 2)) for i in range(4) if bid - 0.01 * i > 0]
                asks = [(round(ask + 0.01 * i, 2), round(rng.uniform(1, 120), 2)) for i in range(4) if ask + 0.01 * i < 1]
                return bids, asks

            def book_frame(token: str, mid: float, ts_ms: int):
                bids, asks = poly_levels(mid)
                return {"event_type": "book", "asset_id": token, "market": ident["condition_id"],
                        "timestamp": str(ts_ms),
                        "bids": [{"price": f"{p:g}", "size": f"{s:g}"} for p, s in bids],
                        "asks": [{"price": f"{p:g}", "size": f"{s:g}"} for p, s in asks]}, bids, asks

            t += rng.uniform(0.01, 0.3)
            yb, y_bids, y_asks = book_frame(ident["yes_token"], poly_q, int(t * 1000) - 8)
            nb, n_bids, n_asks = book_frame(ident["no_token"], 1 - poly_q, int(t * 1000) - 8)
            add(t, "p", [yb, nb] if rng.random() < 0.5 else yb)
            if not isinstance(lines[-1][2], str) or '"book"' in lines[-1][2] and ident["no_token"] not in lines[-1][2]:
                add(t + 0.001, "p", [nb])
            books = {ident["yes_token"]: [dict(y_bids), dict(y_asks)], ident["no_token"]: [dict(n_bids), dict(n_asks)]}
            seq = 1
            end = o + 900 + rng.uniform(1, 4)
            while t < end:
                t += rng.expovariate(4.0)
                # the latent moves; Kalshi leads, Polymarket catches up in bursts
                jump = rng.uniform(0.08, 0.25) * rng.choice([-1, 1]) if rng.random() < 0.006 else 0.0
                q = clamp(q + rng.gauss(0, 0.004) + jump, 0.05, 0.95)
                r = rng.random()
                if r < 0.45:
                    # Kalshi: liquidity toward q on one side; whatever would cross is cancelled
                    side = rng.choice(["yes", "no"])
                    mine, other = (kb, kn) if side == "yes" else (kn, kb)
                    target = q if side == "yes" else 1 - q
                    p = round(clamp(target - 0.01 * rng.choice([1, 1, 2, 3]), 0.01, 0.97), 2 if rng.random() < 0.8 else 3)
                    ops = [(side, p, round(rng.uniform(1, 300), 2))]
                    for op in [x for x in other if x + p >= 1 - 1e-9]:
                        ops.append(("no" if side == "yes" else "yes", op, -other[op]))
                    if mine and rng.random() < 0.4:
                        far = max(mine, key=lambda x: abs(x - target))
                        if abs(far - target) > 0.04 and far != p:
                            ops.append((side, far, -mine[far]))
                    for sd, price, d in ops:
                        book = kb if sd == "yes" else kn
                        book[price] = round(book.get(price, 0.0) + d, 2)
                        if book[price] <= 1e-9:
                            book.pop(price)
                        seq += 1
                        msg = {"market_ticker": ident["kalshi_ticker"], "price_dollars": f"{price:.4f}",
                               "delta_fp": f"{d:.2f}", "side": sd, "ts_ms": int(t * 1000) - rng.randrange(40, 60)}
                        if rng.random() < 0.1 and abs(price * 100 - round(price * 100)) < 1e-9:
                            msg = {"market_ticker": ident["kalshi_ticker"], "price": int(round(price * 100)),
                                   "delta": d, "side": sd}
                        add(t, "k", {"type": "orderbook_delta", "sid": 1, "seq": seq, "msg": msg})
                        t += 0.0002
                elif r < 0.93:
                    # Polymarket price change on YES (and the mirrored NO)
                    if rng.random() < 0.08:
                        poly_q = poly_q + (q - poly_q) * rng.uniform(0.3, 1.0)
                    tok_y, tok_n = ident["yes_token"], ident["no_token"]
                    changes = []
                    for tok, mid in ((tok_y, poly_q), (tok_n, 1 - poly_q)):
                        bids, asks = books[tok]
                        if rng.random() < 0.5:
                            p = round(clamp(mid - 0.01 * rng.choice([0, 1, 1, 2]), 0.01, 0.98), 2)
                            if asks and p >= min(asks):
                                p = round(min(asks) - 0.01, 2)
                            s = 0.0 if (rng.random() < 0.25 and p in bids) else round(rng.uniform(1, 300), 2)
                            if p > 0:
                                if s:
                                    bids[p] = s
                                else:
                                    bids.pop(p, None)
                                changes.append((tok, p, s, "BUY", bids, asks))
                        else:
                            p = round(clamp(mid + 0.01 * rng.choice([0, 1, 1, 2]), 0.02, 0.99), 2)
                            if bids and p <= max(bids):
                                p = round(max(bids) + 0.01, 2)
                            s = 0.0 if (rng.random() < 0.25 and p in asks) else round(rng.uniform(1, 80), 2)
                            if p < 1:
                                if s:
                                    asks[p] = s
                                else:
                                    asks.pop(p, None)
                                changes.append((tok, p, s, "SELL", bids, asks))
                    lag = rng.choice([3, 5, 8, 9, 12, 20, 40, 120, 300]) if rng.random() > 0.01 else 1200
                    pcs = []
                    for tok, p, s, side, bids, asks in changes:
                        bb = max(bids) if bids else 0
                        ba = min(asks) if asks else 1
                        pc = {"asset_id": tok, "price": f"{p:g}", "size": f"{s:g}", "side": side,
                              "hash": "0x", "best_bid": f"{bb:g}", "best_ask": f"{ba:g}"}
                        if rng.random() < 0.02:
                            pc["best_bid"] = f"{max(bb - 0.01, 0):g}"  # the venue's field lagging
                        pcs.append(pc)
                    if pcs:
                        frame = {"event_type": "price_change", "market": ident["condition_id"],
                                 "timestamp": str(int(t * 1000) - lag), "price_changes": pcs}
                        add(t, "p", [frame] if rng.random() < 0.1 else frame)
                    if rng.random() < 0.05:
                        bids, asks = books[tok_y]
                        add(t + 0.0005, "p", {"event_type": "best_bid_ask", "asset_id": tok_y,
                                              "best_bid": f"{max(bids) if bids else 0:g}",
                                              "best_ask": f"{min(asks) if asks else 1:g}",
                                              "timestamp": str(int(t * 1000))})
                elif r < 0.94:
                    # A full re-snapshot of one Polymarket token (as after a reconnect)
                    tok = rng.choice([ident["yes_token"], ident["no_token"]])
                    mid = poly_q if tok == ident["yes_token"] else 1 - poly_q
                    frame, b, a_ = book_frame(tok, mid, int(t * 1000) - 6)
                    books[tok] = [dict(b), dict(a_)]
                    add(t, "p", frame)
                elif r < 0.9415:
                    # Reconnects: a whole venue, or one shard's tokens; books resnapshot after
                    if rng.random() < 0.8:
                        add(t, "c", {"type": "reconnect", "venue": "p", "shard": 0,
                                     "tokens": [ident["yes_token"], ident["no_token"]]})
                    else:
                        add(t, "c", {"type": "reconnect", "venue": "p"})
                    t += rng.uniform(0.05, 2.5)
                    for tok, mid in ((ident["yes_token"], poly_q), (ident["no_token"], 1 - poly_q)):
                        frame, b, a_ = book_frame(tok, mid, int(t * 1000) - 6)
                        books[tok] = [dict(b), dict(a_)]
                        add(t, "p", frame)
                    seq += 1
                    add(t, "k", {"type": "orderbook_snapshot", "sid": 1, "seq": seq, "msg": {
                        "market_ticker": ident["kalshi_ticker"],
                        "yes_dollars_fp": [[f"{p:.4f}", f"{s:.2f}"] for p, s in kb.items()],
                        "no_dollars_fp": [[f"{p:.4f}", f"{s:.2f}"] for p, s in kn.items()]}})
                elif r < 0.943:
                    add(t, "b", '{"topic":"price.crypto","symbol":"btcusd","value":1}')
                elif r < 0.9432:
                    add(t, "p", "{not json")
            # Resolutions: Polymarket's WS event or a polled control line; Kalshi's control line.
            outcome = "yes" if rng.random() < q else "no"
            k_out = outcome if rng.random() < 0.9 else ("no" if outcome == "yes" else "yes")
            tr = o + 900 + rng.uniform(5, 40)
            if rng.random() < 0.6:
                add(tr, "p", {"event_type": "market_resolved", "market": ident["condition_id"],
                              "winning_asset_id": ident["yes_token"] if outcome == "yes" else ident["no_token"]})
            else:
                add(tr, "c", {"type": "resolution", "asset": a, "open_unix": o, "venue": "poly", "outcome": outcome})
            add(tr + rng.uniform(1, 20), "c", {"type": "resolution", "asset": a, "open_unix": o,
                                                "venue": "kalshi", "outcome": k_out})
    lines.sort(key=lambda x: x[0])
    return lines


CONFIGS = {
    "base": dict(params=dict(gap_threshold=0.15, max_entry_px=0.60, notional_usd=50.0), latency_ms=150,
                 limit_offset=0.0, size_to_depth=False, cash=1000.0, max_open=500.0, loss=250.0),
    "offset_depth_lag": dict(params=dict(gap_threshold=0.10, max_entry_px=0.60, notional_usd=40.0,
                                         max_poly_lag_ms=250.0, max_poly_spread=0.03),
                             latency_ms=60, limit_offset=0.02, size_to_depth=True, cash=400.0,
                             max_open=100.0, loss=150.0),
}


class Capture:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def write(self, t_ns: int, record: dict) -> None:
        self.records.append(record)

    def close(self) -> None:
        pass


def run(lines, cfg) -> list[dict]:
    params = replace(StrategyParams(), **cfg["params"])
    cap = Capture()
    engine = Engine(
        params, ASSETS,
        ledger=Ledger(start_cash=cfg["cash"], cash=cfg["cash"]),
        risk=RiskState(RiskLimits(max_open_notional_usd=cfg["max_open"], daily_loss_limit_usd=cfg["loss"])),
        venue=PaperVenue(cfg["latency_ms"]),
        journal=cap,  # type: ignore[arg-type]
        shadow=Shadow(parse_variants(DEFAULT_VARIANTS, params), params),
        limit_offset=cfg["limit_offset"],
        size_to_depth=cfg["size_to_depth"],
        role="full",
    )
    for recv_ns, venue, text in lines:
        engine.handle_line(recv_ns, venue, text)
    # A last line after every horizon so pending markouts resolve.
    engine.handle_line(lines[-1][0] + 200 * NS, "c", '{"type":"noop"}')
    tail = {"type": "final", "counters": dict(sorted(engine.counters.items())),
            "ledger": engine.ledger.snapshot(engine.last_ns)}
    return cap.records + [tail]


def main() -> None:
    rng = random.Random(20261009)
    lines = make_tape(rng)
    with gzip.open(HERE / "tape.txt.gz", "wt", encoding="utf-8") as f:
        for recv_ns, venue, text in lines:
            f.write(f"{recv_ns} {venue} {text}\n")
    for name, cfg in CONFIGS.items():
        records = run(lines, cfg)
        with (HERE / f"{name}.jsonl").open("w") as f:
            for r in records:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
        kinds: dict[str, int] = {}
        for r in records:
            kinds[r["type"]] = kinds.get(r["type"], 0) + 1
        print(name, len(lines), "lines", kinds)


if __name__ == "__main__":
    main()
