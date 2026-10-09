"""A small synthetic Helix kpc tape (``.hlxz``) for the decoder and study goldens.

    python crates/pk-research/tests/golden/helix/make_helix_tape.py

Writes ``tape/<Stream>/<YYYYMMDD>.hlxz`` next to this file: two days, three
15-minute assets (BTC, ETH, XRP) and one 5-minute asset (ignored by the
readers), six windows each, with what the real tape does that a reader must
survive:

- tag generations: canonical ns tags, W tags (43, 46, 47), legacy µs tags
  (15, 14, 7, 8) scaled by 1000;
- a recorder restart (a second FileHeader appended after a footer), a footer
  index on single-sub-file days, a stale-width block, an undecodable block and
  a partial tail;
- an HLXF frame-stream file (zstd over ``HLXF`` frames);
- Kalshi keyed by asset with the old contract's terminal book past each open
  and ``seq`` restarting on the new contract (one window without the restart),
  and ``changed = 0`` re-stamps;
- the monotonic receive clock drifting against wall time.

Deterministic (seeded); the bytes are committed, so the Rust tests need no Python.
"""

from __future__ import annotations

import math
import random
import struct
from pathlib import Path

import zstandard as zstd

HERE = Path(__file__).resolve().parent
OUT = HERE / "tape"
NS = 1_000_000_000
DAYS = {"20261001": (9, 30), "20261002": (14, 0)}  # day -> first window (UTC h, m)
N_WINDOWS = 6
ASSETS = {"BTC": 1, "ETH": 3, "XRP": 7}
SPOT = {"BTC": 15, "ETH": 16, "XRP": 18}
CL_PX = {"BTC": 61000.0, "ETH": 2400.0, "XRP": 0.52}
MONO_BASE = 1_780_000_000 * NS  # wall - mono at midnight of the first day
DRIFT_PER_S = 2.4 / 86400  # mono runs slow against wall

FILE_HDR = struct.Struct("<IHHHHQIQ")
BLK_HDR = struct.Struct("<IBBHIIIqq4x")
IDX_ENT = struct.Struct("<QIIB3sqq4x")
TRAILER = struct.Struct("<QIIQ")
FILE_MAGIC, BLK_MAGIC, FOOT_MAGIC = 0x31584C48, 0x42584C48, 0x31544F4F46584C48
FRAME_HDR = struct.Struct("<IHBBIIq")
FRAME_MAGIC = 0x484C5846

QUOTE = struct.Struct("<dddd qq Q BBBB 4x")
POLY_BBO = struct.Struct("<QQQ qqqq 80s 80s B 7x")
POLY_TRADE = struct.Struct("<80s 80s c 7x d d q q Q")
TRADE = struct.Struct("<16s c 7x dd Q qq B B 6x")
POLY_WINDOW = struct.Struct("<QQ 64s 80s 80s d B 7x")
RES = struct.Struct("<QQ64sdBBH4x")
KALSHI_WINDOW = struct.Struct("<QQ 48s d B 7x")

rng = random.Random(20261001)


def day_start(day: str) -> int:
    import datetime as dt

    d = dt.datetime.strptime(day, "%Y%m%d").replace(tzinfo=dt.UTC)
    return int(d.timestamp())


def mono(wall_ns: int) -> int:
    since = (wall_ns - day_start("20261001") * NS) / NS
    return wall_ns - MONO_BASE - int(since * DRIFT_PER_S * NS)


class Hlxz:
    """One day file: sub-files of blocks, optional footers, and damage on request."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.index: list[tuple[int, int, int, int, int, int]] = []
        self.cctx = zstd.ZstdCompressor(level=3)

    def header(self) -> None:
        self.buf += FILE_HDR.pack(FILE_MAGIC, 1, 32, 0x0101, 0, 1_790_000_000 * NS, 1, 0)
        self.index = []

    def block(self, tag: int, rsz: int, recs: list[bytes], first: int, last: int,
              corrupt: bool = False) -> None:  # fmt: skip
        raw = b"".join(recs)
        comp = self.cctx.compress(raw)
        if corrupt:
            comp = comp[:4] + bytes(len(comp) - 4)
        off = len(self.buf)
        self.buf += BLK_HDR.pack(BLK_MAGIC, tag, 0, rsz, len(recs), len(comp), len(raw), first, last)
        self.buf += comp
        self.index.append((off, len(comp), len(recs), tag, first, last))

    def footer(self) -> None:
        idx = b"".join(IDX_ENT.pack(o, c, n, t, b"\0\0\0", f, la) for o, c, n, t, f, la in self.index)
        self.buf += idx
        self.buf += TRAILER.pack(FOOT_MAGIC, len(self.index), len(idx), len(self.buf) + 24)

    def blocks(self, tag: int, rsz: int, recs: list[tuple[int, bytes]], per: int = 400) -> None:
        for i in range(0, len(recs), per):
            part = recs[i : i + per]
            self.block(tag, rsz, [r for _, r in part], part[0][0], part[-1][0])

    def write(self, stream: str, day: str) -> None:
        p = OUT / stream / f"{day}.hlxz"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(bytes(self.buf))


def token() -> str:
    return str(rng.randrange(10**76, 10**77))


def cond() -> str:
    return "0x" + "".join(rng.choice("0123456789abcdef") for _ in range(64))


def e8(x: float) -> int:
    return round(x * 1e8)


def clip(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)


def poisson_times(t0: int, t1: int, rate: float) -> list[int]:
    out, t = [], t0
    while True:
        t += int(rng.expovariate(rate) * NS) + 1
        if t >= t1:
            return out
        out.append(t)


def build_day(day: str) -> dict:
    h, m = DAYS[day]
    first_open = day_start(day) + h * 3600 + m * 60
    opens = [first_open + k * 900 for k in range(N_WINDOWS)]
    win: list[dict] = []
    kalshi: dict[str, list[tuple[int, float, float, float, float, int, int]]] = {a: [] for a in ASSETS}
    yes_rows: list[tuple[int, dict, float, float, float, float]] = []
    no_rows: list[tuple[int, dict, float, float, float, float]] = []
    trades: list[tuple[int, dict, str, float, float, int]] = []
    chain: dict[str, list[tuple[int, float]]] = {a: [] for a in ASSETS}
    k_trades: list[tuple[int, int, str, float, float]] = []
    spot_books: list[tuple[int, int, float]] = []
    for asset, aid in ASSETS.items():
        s = CL_PX[asset] * (1 + rng.uniform(-0.01, 0.01))
        t = (opens[0] - 120) * NS
        while t < (opens[-1] + 900 + 60) * NS:
            t += int(rng.uniform(0.6, 1.4) * NS)
            s *= math.exp(rng.gauss(0, 0.0004))
            chain[asset].append((t, s))
        seq = 1
        for w_i, o in enumerate(opens):
            c = o + 900
            strike = round(chain[asset][0][1] * (1 + rng.uniform(-0.002, 0.002)), 6)
            w = {"asset": asset, "aid": aid, "open": o, "strike": strike, "cond": cond(),
                 "yes": token(), "no": token()}  # fmt: skip
            win.append(w)
            # The latent probability: Kalshi follows it at once, Polymarket after a lag;
            # a few jumps a window open the gap the rule trades.
            p, lag_s = rng.uniform(0.3, 0.7), rng.uniform(2.0, 8.0)
            path: list[tuple[float, float]] = []
            jumps = {rng.randrange(30, 820) for _ in range(rng.randint(1, 4))}
            for sec in range(-60, 911):
                p = clip(p + rng.gauss(0, 0.012), 0.03, 0.97)
                if sec in jumps:
                    p = clip(p + rng.choice((-1, 1)) * rng.uniform(0.10, 0.22), 0.03, 0.97)
                path.append((sec, p))

            def at(sec: float, path=path) -> float:
                i = int(math.floor(sec)) + 60
                return path[max(0, min(i, len(path) - 1))][1]

            w["outcome"] = "yes" if at(899) >= 0.5 else "no"
            # Kalshi: the old contract's terminal book past the open, then the new one.
            switch = o * NS + int(rng.uniform(0.06, 5.0) * NS)
            if w_i > 0:
                prev_last = kalshi[asset][-1]
                for k in range(rng.randint(1, 4)):
                    tt = o * NS + int(rng.uniform(0.01, 0.05) * NS) + k * 1000
                    kalshi[asset].append((tt, 0.0, 0.001, 0.0, 120.0, prev_last[5] + 1 + k, 1))
            if not (asset == "BTC" and w_i == 3):  # one roll without a seq restart
                seq = 1
            spread_wide = rng.random() < 0.3
            for tt in poisson_times(switch, c * NS, 0.8):
                sec = (tt / NS) - o
                q = at(sec)
                spr = rng.choice((0.01, 0.02, 0.03, 0.04)) + (0.08 if spread_wide and rng.random() < 0.3 else 0)
                bid = clip(math.floor((q - spr / 2) * 100) / 100, 0.01, 0.98)
                ask = clip(round(bid + spr, 2), bid + 0.01, 0.99)
                chg = 0 if rng.random() < 0.05 else 1
                kalshi[asset].append((tt, round(bid, 2), round(ask, 2), float(rng.choice((10, 50, 120, 400))),
                                      float(rng.choice((5, 60, 200))), seq, chg))  # fmt: skip
                seq += 1
                if rng.random() < 0.02:
                    k_trades.append((tt + 3 * NS // 100, aid, rng.choice("BS"), round(ask, 2), 7.0))
            # Polymarket YES (and its NO mirror), rows from a minute before the open.
            for tt in poisson_times((o - 60) * NS, (c + 10) * NS, 1.1):
                sec = (tt / NS) - o
                q = at(sec - lag_s)
                spr = rng.choice((0.01, 0.01, 0.01, 0.02, 0.03)) + (0.05 if rng.random() < 0.05 else 0)
                bid = clip(round(q - spr / 2, 2), 0.01, 0.98)
                if rng.random() < 0.05:
                    bid = clip(round(bid + 0.003, 3), 0.001, 0.98)
                ask = clip(round(bid + spr, 3), bid + 0.001, 0.999)
                bsz, asz = rng.choice((5.0, 12.5, 40.0, 150.0, 900.0)), rng.choice((5.0, 12.5, 40.0, 150.0, 900.0))
                if rng.random() < 0.08 and yes_rows and yes_rows[-1][1] is w:  # a repeated touch
                    _, _, bid, ask, bsz, asz = yes_rows[-1]
                yes_rows.append((tt, w, bid, ask, bsz, asz))
                tn = tt + int(rng.uniform(0.001, 0.03) * NS)
                if rng.random() < 0.9:
                    no_rows.append((tn, w, round(1 - ask, 3), round(1 - bid, 3), asz, bsz))
                else:
                    no_rows.append((tn, w, round(1 - ask - 0.01, 3), round(1 - bid, 3), asz, bsz))
                if rng.random() < 0.25:
                    side = rng.choice("BS")
                    leg = rng.choice(("yes", "no"))
                    px = (ask if side == "B" else bid) if leg == "yes" else (1 - bid if side == "B" else 1 - ask)
                    trades.append((tt + int(rng.uniform(0.0, 3.0) * NS), w, leg, round(px, 3),
                                   rng.choice((1.0, 5.0, 20.0)), side))  # fmt: skip
            if asset in ("BTC", "ETH") and day == "20261001":
                for tt in poisson_times(o * NS, c * NS, 1.0):
                    spot_books.append((tt, SPOT[asset], chain[asset][0][1] * (1 + 0.001 * (at((tt / NS) - o) - 0.5))))
    return {"opens": opens, "windows": win, "kalshi": kalshi, "yes": yes_rows, "no": no_rows,
            "trades": trades, "chain": chain, "k_trades": k_trades, "spot": spot_books}  # fmt: skip


def write_day(day: str, d: dict, five_min: list[dict]) -> None:
    us = day == "20261002"  # day 2 carries the legacy-tag blocks

    # Polymarket_Window: windows (W tag on day 2, canonical on day 1, one legacy
    # record), a restart re-emitting the first windows, 5-minute windows, resolutions.
    f = Hlxz()
    f.header()
    recs: list[tuple[int, bytes]] = []
    for w in d["windows"] + five_min:
        ts = (w["open"] - 30) * NS
        recs.append((ts, POLY_WINDOW.pack(ts, w["open"], w["cond"].encode(), w["yes"].encode(),
                                          w["no"].encode(), w["strike"], w["aid"])))  # fmt: skip
    recs.sort(key=lambda r: r[0])
    tag_w = 46 if day == "20261002" else 32
    f.blocks(tag_w, 256, recs[2:], per=8)
    legacy = [(r[0], r[1]) for r in recs[:2]]
    legacy_us = []
    for ts, raw in legacy:
        fields = list(POLY_WINDOW.unpack(raw))
        fields[0] = ts // 1000
        legacy_us.append((ts // 1000, POLY_WINDOW.pack(*fields)))
    f.block(7, 256, [r for _, r in legacy_us], legacy_us[0][0], legacy_us[-1][0])
    res = []
    by_asset: dict[str, list[dict]] = {}
    for w in d["windows"]:
        by_asset.setdefault(w["asset"], []).append(w)
    for ws in by_asset.values():
        for a, b in zip(ws, ws[1:] + [None]):
            close_px = b["strike"] if b else a["strike"] * 1.001
            ts = (a["open"] + 904) * NS
            res.append((ts, a, close_px))
    res.sort(key=lambda r: r[0])
    tag_r = 47 if day == "20261002" else 33
    f.block(tag_r, 96, [RES.pack(ts, w["open"], w["cond"][2:].encode(), px, 0xFF, w["aid"], w["aid"] if tag_r == 47 else 0)
                        for ts, w, px in res[1:]], res[1][0], res[-1][0])  # fmt: skip
    ts, w, px = res[0]
    f.block(8, 96, [RES.pack(ts // 1000, w["open"], w["cond"][2:].encode(), px, 0xFF, w["aid"], 0)], ts // 1000, ts // 1000)
    f.footer()
    f.header()  # restart: the recorder re-emits the windows it knows
    f.blocks(tag_w, 256, recs[2:6], per=8)
    f.write("Polymarket_Window", day)

    # Polymarket BBO: restart mid-day, a stale-width block, a corrupt block, a partial tail.
    for stream, rows, outcome in (("Polymarket_CLOB_Yes_BBO", d["yes"], 0), ("Polymarket_CLOB_No_BBO", d["no"], 1)):
        rows = sorted(rows, key=lambda r: r[0])
        f = Hlxz()
        f.header()
        recs = []
        for tt, w, bid, ask, bsz, asz in rows:
            tok = w["yes"] if outcome == 0 else w["no"]
            recs.append((mono(tt), (tt, w, bid, ask, bsz, asz, tok)))

        def pack(r, scale=1):
            tt, w, bid, ask, bsz, asz, tok = r
            return POLY_BBO.pack(mono(tt) // scale, tt // scale, (mono(tt) - 111 * NS) // scale,
                                 e8(bid), e8(bsz), e8(ask), e8(asz), w["cond"].encode(), tok.encode(), outcome)  # fmt: skip

        half = len(recs) // 2
        first = recs[:half]
        if us and outcome == 0:
            # The first 600 rows in the legacy µs generation (times truncated to µs).
            leg = [(m, (r[0] // 1000 * 1000,) + r[1:]) for m, r in first[:600]]
            for i in range(0, len(leg), 300):
                part = leg[i : i + 300]
                f.block(14, 224, [pack(r, 1000) for _, r in part], part[0][0] // 1000, part[-1][0] // 1000)
            first = first[600:]
        for i in range(0, len(first), 500):
            part = first[i : i + 500]
            f.block(27, 224, [pack(r) for _, r in part], part[0][0], part[-1][0])
        f.block(27, 56, [bytes(56)] * 10, first[-1][0], first[-1][0])  # stale width
        f.block(27, 224, [pack(r) for _, r in first[-5:]], first[-1][0], first[-1][0], corrupt=True)
        f.footer()
        f.header()
        rest = recs[half:]
        for i in range(0, len(rest), 500):
            part = rest[i : i + 500]
            f.block(27, 224, [pack(r) for _, r in part], part[0][0], part[-1][0])
        f.buf += BLK_HDR.pack(BLK_MAGIC, 27, 0, 224, 3, 4000, 672, 0, 0) + b"\x28\xb5\x2f\xfd" + bytes(100)
        f.write(stream, day)

    # Kalshi BBO (YES and the NO mirror): keyed by asset; footer on day 1.
    for stream, mirror in (("Kalshi_Yes_BBO", False), ("Kalshi_No_BBO", True)):
        rows = sorted(((r, a) for a, rs in d["kalshi"].items() for r in rs), key=lambda x: x[0][0])
        f = Hlxz()
        f.header()

        def kpack(r, aid, scale=1):
            tt, bid, ask, bsz, asz, seq, chg = r
            if mirror:
                bid, ask = (round(1 - ask, 3), round(1 - bid, 3)) if bid > 0 else (0.0, 0.0)
            return QUOTE.pack(bid, ask, bsz, asz, mono(tt) // scale, tt // scale, seq, 0x40, 0, aid, chg)

        recs = [(mono(r[0]), kpack(r, ASSETS[a])) for r, a in rows]
        if day == "20261001" and not mirror:
            leg = [(m, r, a) for (r, a), (m, _) in zip(rows[:400], recs[:400])]
            leg = [(m // 1000, kpack((r[0] // 1000 * 1000,) + r[1:], ASSETS[a], 1000)) for m, r, a in leg]
            f.block(15, 64, [r for _, r in leg], leg[0][0], leg[-1][0])
            recs = recs[400:]
        f.blocks(43 if day == "20261002" else 22, 64, recs, per=600)
        if day == "20261001":
            f.footer()
        f.write(stream, day)

    # Chainlink (by *Spot id), all changed.
    rows = sorted(((t, SPOT[a], px) for a, rs in d["chain"].items() for t, px in rs), key=lambda r: r[0])
    f = Hlxz()
    f.header()
    f.blocks(43, 64, [(mono(t), QUOTE.pack(px, px, 0.0, 0.0, mono(t), t, i, 0x60, 0, aid, 1))
                      for i, (t, aid, px) in enumerate(rows)], per=800)  # fmt: skip
    f.write("PolymarketChainlink_BBO", day)

    # Polymarket trades, YES and NO streams, monotonic receive time.
    for stream, leg in (("Polymarket_CLOB_Trade", "yes"), ("Polymarket_CLOB_No_Trade", "no")):
        rows = sorted((r for r in d["trades"] if r[2] == leg), key=lambda r: r[0])
        f = Hlxz()
        f.header()
        f.blocks(25, 208, [(mono(t), POLY_TRADE.pack(w["cond"].encode(), w[leg].encode(), side.encode(), px, sz,
                                                     t - 40_000_000, mono(t), i))
                           for i, (t, w, _l, px, sz, side) in enumerate(rows)], per=200)  # fmt: skip
        f.write(stream, day)

    # Kalshi trades: day 2 is an HLXF frame stream (the 2026-06 recorder format).
    recs = [TRADE.pack(b"KXBTC15M", side.encode(), px, sz, i, t + 25_000_000, t, 0x40, aid)
            for i, (t, aid, side, px, sz) in enumerate(sorted(d["k_trades"]))]  # fmt: skip
    if day == "20261002":
        body = bytearray()
        for i in range(0, len(recs), 7):
            part = recs[i : i + 7]
            body += FRAME_HDR.pack(FRAME_MAGIC, 0x0401, 24, 0, len(part), 72, 0) + b"".join(part)
        cctx = zstd.ZstdCompressor()
        half = len(body) // 2
        p = OUT / "Kalshi_Yes_Trade" / f"{day}.hlxz"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(cctx.compress(bytes(body[:half])) + cctx.compress(bytes(body[half:])))
    else:
        f = Hlxz()
        f.header()
        f.blocks(45, 72, [(0, r) for r in recs], per=50)
        f.write("Kalshi_Yes_Trade", day)

    # Kalshi windows (unused by the studies; the inventory lists the stream).
    f = Hlxz()
    f.header()
    f.blocks(48, 80, [(w["open"] * NS, KALSHI_WINDOW.pack(w["open"] * NS, w["open"], f"KX{w['asset']}15M".encode(),
                                                         0.0, w["aid"])) for w in d["windows"]], per=50)  # fmt: skip
    f.write("Kalshi_Window", day)

    # Binance spot book (day 1): 688 B HubOrderbook, two levels a side; a few pre-V3
    # records with no asset byte (the reader infers it from the price band).
    if d["spot"]:
        f = Hlxz()
        f.header()
        recs = []
        for i, (t, aid, mid) in enumerate(sorted(d["spot"])):
            tick = mid * 1e-5
            pairs = [0.0] * 80
            pairs[0:4] = [mid - tick / 2, 1.5, mid - 1.5 * tick, 3.0]
            pairs[40:44] = [mid + tick / 2, 2.0, mid + 1.5 * tick, 4.0]
            meta = struct.pack("<BB6xQqqBB14x", 2, 2, i, t - 5_000_000, mono(t), 0x15, 0 if i % 50 == 7 else aid)
            recs.append((mono(t), struct.pack("<80d", *pairs) + meta))
        f.blocks(23, 688, recs, per=300)
        f.write("BinanceSpot_Book", day)


def main() -> None:
    import shutil

    shutil.rmtree(OUT, ignore_errors=True)
    for day in DAYS:
        d = build_day(day)
        five = [{"asset": "BTC5", "aid": 2, "open": o + k * 300, "strike": 61000.0, "cond": cond(),
                 "yes": token(), "no": token()} for o in d["opens"][:2] for k in range(3)]  # fmt: skip
        write_day(day, d, five)
        outcomes = {(w["asset"], w["open"]): w["outcome"] for w in d["windows"]}
        (OUT / f"outcomes_{day}.txt").write_text(
            "".join(f"{a} {o} {v}\n" for (a, o), v in sorted(outcomes.items()))
        )
    total = sum(p.stat().st_size for p in OUT.rglob("*.hlxz"))
    print(f"{total / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
