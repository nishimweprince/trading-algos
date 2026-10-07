"""PLAN 0.1a: validate the Helix decoder on a day before any number depends on it.

    python -m research.helix.validate --day 20260920 [--gamma 40] [--out research/data]

Checks, each reported with its numbers in ``helix_validation_<day>.json``:

1. **Identity:** the array path (``reader.*_series``) against ``recv3``'s typed
   records, field for field, on the first records of the day's files.
2. **Mirror:** Polymarket ``yes_bid ≈ 1 - no_ask`` (and ``yes_ask ≈ 1 - no_bid``)
   at every YES tick; Kalshi YES against Kalshi NO the same way.
3. **Window span:** where each token's ticks fall against its window's open
   and close (from the Window stream).
4. **Convergence:** the YES mid in the last seconds before close against the
   resolution record (close >= strike is Up), and that against Polymarket's own
   ``outcomePrices`` on Gamma for a sample of windows (cached).
5. **Tick rates** per stream, raw and after dedup.
6. **Clocks:** wall minus monotonic per stream (one host or several), and the
   venue-to-receive latency per venue, which says where the recorder sat.
7. **Kalshi roll:** the Kalshi stream is keyed by asset, not by ticker, so the
   first quotes after each open are checked for the old contract's terminal book.
"""

from __future__ import annotations

import argparse
import itertools
import json
import time
from pathlib import Path
from typing import Any

import numpy as np

from . import reader, recv3
from .gamma import GammaCache

NS = 1_000_000_000


def _pct(values: np.ndarray | list[float], qs=(0.01, 0.5, 0.99)) -> dict[str, float]:
    arr = np.asarray(values, dtype=float)
    if not len(arr):
        return {}
    return {f"p{round(q * 100)}": round(float(np.quantile(arr, q)), 4) for q in qs} | {
        "n": len(arr)
    }


def check_identity(day: str, root: Path, n: int = 500_000) -> dict[str, Any]:
    """The first ``n`` records of each BBO file, recv3 against the block path."""
    out: dict[str, Any] = {}
    p = str(reader.path("Polymarket_CLOB_Yes_BBO", day, root))
    typed = list(itertools.islice(recv3.iter_poly_bbo(p), n))
    arrays = []
    for raw, scale in reader.iter_blocks(p, reader._tags(recv3.TAG_POLY_BBO_NS), 224):
        arrays.append((np.frombuffer(raw, dtype=reader._POLY_BBO_DT), scale))
        if sum(len(a) for a, _ in arrays) >= n:
            break
    a = np.concatenate([x for x, _ in arrays])[:n]
    scale = arrays[0][1]
    tokens = [reader._token_key(r.token_id) for r in typed]
    out["poly_bbo"] = {
        "records": len(typed),
        "wall": bool(np.array_equal(a["wall"] * scale, [r.ts_wall_ns for r in typed])),
        "recv": bool(np.array_equal(a["recv"] * scale, [r.ts_received_ns for r in typed])),
        "bid": bool(
            np.array_equal(
                reader._px(a["bid"], 1e8) / reader.PX, [round(r.bid_px, 4) for r in typed]
            )
        ),
        "ask": bool(
            np.array_equal(
                reader._px(a["ask"], 1e8) / reader.PX, [round(r.ask_px, 4) for r in typed]
            )
        ),
        "sizes": bool(
            np.allclose(a["bsz"] / 1e8, [r.bid_sz for r in typed])
            and np.allclose(a["asz"] / 1e8, [r.ask_sz for r in typed])
        ),
        "token": bool(
            np.array_equal(
                np.stack([a["k0"], a["k1"], a["k2"]], axis=1), np.array(tokens, dtype=np.uint64)
            )
        ),
    }
    p = str(reader.path("Kalshi_Yes_BBO", day, root))
    typed_k = list(itertools.islice(recv3.iter_quotes(p, only_changed=False), n))
    arrays = []
    for raw, scale in reader.iter_blocks(p, reader._tags(recv3.TAG_QUOTE_NS), 64):
        arrays.append((np.frombuffer(raw, dtype=reader._QUOTE_DT), scale))
        if sum(len(x) for x, _ in arrays) >= n:
            break
    k = np.concatenate([x for x, _ in arrays])[: len(typed_k)]
    out["kalshi_bbo"] = {
        "records": len(typed_k),
        "wall": bool(np.array_equal(k["wall"] * arrays[0][1], [r.ts_wall_ns for r in typed_k])),
        "bid_ask": bool(
            np.array_equal(k["bid"], [r.bid for r in typed_k])
            and np.array_equal(k["ask"], [r.ask for r in typed_k])
        ),
        "asset": bool(np.array_equal(k["aid"], [r.asset_id for r in typed_k])),
        "changed": bool(np.array_equal(k["chg"], [r.changed for r in typed_k])),
    }
    out["pass"] = all(v for part in out.values() for kk, v in part.items() if kk != "records")
    return out


def _asof(t_from: np.ndarray, t_at: np.ndarray) -> np.ndarray:
    """Index of the last ``t_from`` row at or before each ``t_at`` (-1 if none)."""
    return np.searchsorted(t_from, t_at, side="right") - 1


def _same_instant(poly: reader.Series, kalshi: reader.Series) -> np.ndarray:
    order = np.argsort(poly.cols["recv"])
    recv, off = poly.cols["recv"][order], (poly.cols["t"] - poly.cols["recv"])[order]
    kr = kalshi.cols["recv"]
    inside = (kr >= recv[0]) & (kr <= recv[-1])
    return (kalshi.cols["t"] - kr)[inside] - np.interp(kr[inside], recv, off)


def check_day(day: str, root: Path, gamma_sample: int, cache_dir: Path) -> dict[str, Any]:
    started = time.time()
    windows = list(reader.iter_windows(day, root))
    res = {(r.asset, r.open_unix): r for r in reader.iter_resolutions(day, root)}
    yes_tokens = [w.token_yes for w in windows]
    no_tokens = [w.token_no for w in windows]
    yes = reader.poly_bbo_series(
        "Polymarket_CLOB_Yes_BBO", day, yes_tokens, root, extra=("recv", "pub")
    )
    no = reader.poly_bbo_series("Polymarket_CLOB_No_BBO", day, no_tokens, root)
    aids = list(reader.HELIX_15M)
    ky = reader.kalshi_quote_series("Kalshi_Yes_BBO", day, aids, root, extra=("recv", "seq"))
    out_span = (int(ky.cols["t"].max()) - int(ky.cols["t"].min())) / 3.6e12 if len(ky) else 0
    kn = reader.kalshi_quote_series("Kalshi_No_BBO", day, aids, root)
    out: dict[str, Any] = {
        "day": day,
        "windows": len(windows),
        "resolutions": len(res),
        "kalshi_span_hours": round(out_span, 2),
    }

    # 2. mirror
    mirror_exact = mirror_tick = n_mirror = 0
    for k in range(len(windows)):
        ys, ns_ = yes.rows(k), no.rows(k)
        ty, tn = yes.cols["t"][ys], no.cols["t"][ns_]
        if not len(ty) or not len(tn):
            continue
        j = _asof(tn, ty)
        ok = (j >= 0) & (yes.cols["bid"][ys] > 0) & (yes.cols["ask"][ys] > 0)
        j = j[ok]
        if not len(j):
            continue
        yb, ya = yes.cols["bid"][ys][ok].astype(int), yes.cols["ask"][ys][ok].astype(int)
        nb, na = no.cols["bid"][ns_][j].astype(int), no.cols["ask"][ns_][j].astype(int)
        d = np.maximum(np.abs(yb - (reader.PX - na)), np.abs(ya - (reader.PX - nb)))
        n_mirror += len(d)
        mirror_exact += int((d == 0).sum())
        mirror_tick += int((d <= 100).sum())
    out["poly_mirror"] = {
        "ticks": n_mirror,
        "exact": round(mirror_exact / max(n_mirror, 1), 4),
        "within_1c": round(mirror_tick / max(n_mirror, 1), 4),
    }
    k_exact = k_n = 0
    for k in range(len(aids)):
        ys, ns_ = ky.rows(k), kn.rows(k)
        ty, tn = ky.cols["t"][ys], kn.cols["t"][ns_]
        if not len(ty) or not len(tn):
            continue
        j = _asof(tn, ty)
        ok = j >= 0
        d = np.abs(
            ky.cols["bid"][ys][ok].astype(int)
            - (reader.PX - kn.cols["ask"][ns_][j[ok]].astype(int))
        )
        k_n += len(d)
        k_exact += int((d == 0).sum())
    out["kalshi_yes_no_mirror"] = {"ticks": k_n, "exact": round(k_exact / max(k_n, 1), 4)}

    # 3. window span, 4. convergence
    inside = before = after = 0
    agree = disagree = undecided = 0
    for k, w in enumerate(windows):
        t = yes.cols["t"][yes.rows(k)]
        o, c = w.open_unix * NS, (w.open_unix + 900) * NS
        before += int((t < o).sum())
        after += int((t >= c).sum())
        inside += int(((t >= o) & (t < c)).sum())
        r = res.get((w.asset, w.open_unix))
        if r is None or not w.strike or w.strike != w.strike:
            continue
        up = r.close_px >= w.strike
        sl = yes.rows(k)
        j = _asof(yes.cols["t"][sl], np.array([c - 5 * NS]))[0]
        if j < 0:
            continue
        # Near close the book is one-sided: 0.999 bid and no ask (Up), or no bid
        # and a 0.001 ask (Down). Read the side that is there.
        b, a = int(yes.cols["bid"][sl][j]), int(yes.cols["ask"][sl][j])
        if b >= 9000 or (a and a >= 9000 and not b):
            decided_up = True
        elif (a and a <= 1000) or (b and b <= 1000 and not a):
            decided_up = False
        else:
            undecided += 1
            continue
        if decided_up == up:
            agree += 1
        else:
            disagree += 1
    out["window_span"] = {
        "inside": inside, "before_open": before, "after_close": after,
        "inside_share": round(inside / max(inside + before + after, 1), 4),
    }  # fmt: skip
    out["convergence_5s_before_close"] = {
        "agree": agree, "disagree": disagree, "undecided_0.1_0.9": undecided,
        "agree_share_of_decided": round(agree / max(agree + disagree, 1), 4),
    }  # fmt: skip

    # 4b. Gamma sample: tape resolution against Polymarket's own outcome.
    if gamma_sample:
        gamma = GammaCache(cache_dir / "gamma")
        rng = np.random.default_rng(int(day))
        picks = rng.choice(len(windows), size=min(gamma_sample, len(windows)), replace=False)
        rows = []
        for k in picks:
            w = windows[int(k)]
            r = res.get((w.asset, w.open_unix))
            if r is None:
                continue
            outcome = gamma.outcome(reader.HELIX_15M[w.asset], w.open_unix)
            rows.append((outcome, "yes" if r.close_px >= w.strike else "no", w.token_yes))
        gamma.save()
        known = [r for r in rows if r[0] is not None]
        out["gamma_check"] = {
            "sampled": len(rows),
            "gamma_known": len(known),
            "agree": sum(1 for g, tape, _ in known if g == tape),
            "disagree": [(g, tape) for g, tape, _ in known if g != tape],
        }

    # 5. tick rates
    span = 86400
    out["tick_rates_per_s"] = {
        "poly_yes_raw": round(yes.raw_rows / span, 1),
        "poly_yes_dedup": round(len(yes) / span, 1),
        "poly_no_raw": round(no.raw_rows / span, 1),
        "kalshi_yes_raw": round(ky.raw_rows / span, 1),
        "kalshi_yes_dedup": round(len(ky) / span, 1),
    }

    # 6. clocks and latency
    off_poly = yes.cols["t"] - yes.cols["recv"]
    pub = yes.cols["pub"]
    lat_p = (yes.cols["recv"] - pub)[pub > 0] / 1e6
    out["clocks"] = {
        "wall_minus_mono_poly_s": _pct((off_poly - off_poly[0]) / 1e9),
        # Kalshi's wall-minus-monotonic offset against Polymarket's *at the same
        # instants* (the offset drifts ~2.4 s a day, so medians over different
        # spans of the day would differ for nothing): ~0 means one host clock.
        "kalshi_vs_poly_offset_same_instant_ms": _pct(_same_instant(yes, ky) / 1e6),
        # Not a latency: Polymarket BBO's ``pub`` sits ~111 s off receive in some
        # other clock plane. Reported so a change shows; never used.
        "poly_bbo_recv_minus_pub_ms": _pct(lat_p),
    }
    lat_pt, lat_kt = [], []
    # The monotonic clock drifts against wall (~2.4 s a day), so a trade's
    # monotonic receive time is mapped to wall through the BBO rows nearest it.
    sample = np.argsort(yes.cols["recv"])[:: max(1, len(yes) // 200_000)]
    mono, off = yes.cols["recv"][sample], off_poly[sample]
    for r in itertools.islice(reader.iter_poly_trades("Polymarket_CLOB_Trade", day, root), 200_000):
        if r.ts_publisher_ns > 0:
            wall = r.ts_received_ns + float(np.interp(r.ts_received_ns, mono, off))
            lat_pt.append((wall - r.ts_publisher_ns) / 1e6)
    for r in itertools.islice(reader.iter_kalshi_trades("Kalshi_Yes_Trade", day, root), 200_000):
        lat_kt.append((r.ts_received_ns - r.ts_wall_ns) / 1e6)
    out["clocks"]["poly_trade_recv_minus_venue_ms"] = _pct(lat_pt)
    out["clocks"]["kalshi_trade_recv_minus_venue_ms"] = _pct(lat_kt)

    # 7. Kalshi roll: the asset-keyed stream carries the old contract's terminal
    # book past the open; reader.kalshi_contract_start finds the new contract.
    switch_ms, missing, stale_after_open = [], 0, []
    by_asset = {aid: k for k, aid in enumerate(aids)}
    for w in windows:
        sl = ky.rows(by_asset[w.asset])
        t, seq = ky.cols["t"][sl], ky.cols["seq"][sl]
        o = w.open_unix * NS
        i = reader.kalshi_contract_start(t, seq, o)
        at_open = int(np.searchsorted(t, o))
        prev = int(seq[max(at_open - 1, 0)])
        if i == at_open and (at_open >= len(t) or 0 <= int(seq[at_open]) - prev <= 50_000):
            missing += 1
        if i < len(t):
            switch_ms.append((t[i] - o) / 1e6)
            stale_after_open.append(max(0, i - at_open))
    out["kalshi_roll"] = {
        "switch_after_open_ms": _pct(switch_ms, (0.01, 0.5, 0.9, 0.99)),
        "old_contract_rows_after_open": _pct(stale_after_open, (0.5, 0.99)),
        "windows_without_seq_break": missing,
    }
    out["seconds"] = round(time.time() - started, 1)
    return out


def _main() -> None:
    parser = argparse.ArgumentParser(prog="research.helix.validate")
    parser.add_argument("--day", required=True)
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--gamma", type=int, default=40, help="windows to check on Gamma")
    parser.add_argument("--out", type=Path, default=Path("research/data"))
    args = parser.parse_args()
    root = reader.tape_dir(args.root)
    result = {"identity": check_identity(args.day, root)}
    result |= check_day(args.day, root, args.gamma, args.out)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"helix_validation_{args.day}.json").write_text(json.dumps(result, indent=1))
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    _main()
