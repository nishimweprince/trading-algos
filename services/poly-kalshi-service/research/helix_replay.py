"""PLAN 0.1b / 3.3: the Helix kpc tape through the live ``decide()``, per day.

    python -m research.helix_replay run [--days 20260801-20260930] [--jobs 3] \
        [--out research/data/helix] [--shift-ms k=0,p=0]

For every 15-minute window of every day, the three legs the live engine reads
(Kalshi YES BBO, Polymarket YES and NO BBO) are merged by time, and **the same
``decide()``** the live engine calls is evaluated for every variant in
``VARIANTS``. Nothing is re-implemented: a vectorised pass finds the ticks where
a variant *could* fire (both legs valid, inside its timing, ``|gap|`` within a
hair of its threshold), and ``decide()`` is called on those ticks, in order,
until one consumes the window. The live rule's float arithmetic therefore
decides every boundary case, exactly as live.

What the live engine guarantees and how it carries over:

- **Leg validity** (``Book.quote``): both sides present, not crossed, and
  updated since the window opened. Polymarket BBO is keyed by token, so
  "updated since open" is "a row of this token at or after the open". Kalshi's
  stream is keyed by asset, and carries the old contract's terminal book for a
  few seconds past the open: the leg counts only from
  ``reader.kalshi_contract_start`` (the ``seq`` restart of the new contract).
- **Prices** are 1e-4 ticks, as ``book.px`` rounds them.
- **Evaluation on every update** of any of the three legs (kpc trap 5).

Settlement is Polymarket's own outcome (``resolutions.parquet``, from Gamma),
never the tape's strike comparison. Per day, under ``<out>/<day>/``:

- ``grid.parquet``: per window per second, the three legs' touch at the end of
  the second (rows where all three are valid), plus Chainlink's price ``s``.
- ``crossings.parquet``: per variant per window, the consuming decision: time,
  second, action, side, entry price, gap, every leg's touch and touch sizes.
- ``windows.parquet``: per window, the uncensored gap (``max_abs_gap_full``,
  ``first_cross_s_<t>`` and its side for each t in ``THRESHOLDS``), Chainlink
  at open and close (last tick and 60 s mean), and the Kalshi contract switch.
- ``slices.parquet``: for each distinct crossing time, every BBO row of the two
  Polymarket legs and Kalshi, and every Polymarket trade on the window's
  tokens, from -2 s to +5 s.
- ``summary.json``: row counts, decode timings, and the measured feed geometry.

``--shift-ms k=…,p=…`` delays each venue's rows before the merge (PLAN 3.1),
for re-timing a tape recorded somewhere other than the host it stands in for.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

from poly_kalshi_service.book import Quote
from poly_kalshi_service.decision import WINDOW_S, Action, StrategyParams, decide
from research.helix import reader

NS = 1_000_000_000
PX = reader.PX
THRESHOLDS = (0.08, 0.10, 0.12, 0.15, 0.20)
SLICE_BEFORE_NS, SLICE_AFTER_NS = 2 * NS, 5 * NS
MIN_FREE_GB = 30.0

BASE = StrategyParams(gap_threshold=0.10, max_entry_px=0.60, notional_usd=50.0)


@dataclass(frozen=True)
class Variant:
    name: str
    params: StrategyParams


def _v(name: str, **changes: Any) -> Variant:
    return Variant(name, replace(BASE, **changes))


# PLAN 1.2's shadow set plus the 3.3 sweeps. ``base`` is the live rule.
VARIANTS = (
    _v("base"),
    _v("t08", gap_threshold=0.08),
    _v("t12", gap_threshold=0.12),
    _v("t15", gap_threshold=0.15),
    _v("t20", gap_threshold=0.20),
    _v("early60", entry_early_s=60),
    _v("early120", entry_early_s=120),
    _v("early180", entry_early_s=180),
    _v("kspread03", max_kalshi_spread=0.03),
    _v("kspread05", max_kalshi_spread=0.05),
    _v("kspread10", max_kalshi_spread=0.10),
    _v("cap50", max_entry_px=0.50),
)


@dataclass
class Leg:
    t: np.ndarray
    bid: np.ndarray
    ask: np.ndarray
    bsz: np.ndarray
    asz: np.ndarray

    @classmethod
    def of(cls, s: reader.Series, rows: slice, shift_ns: int = 0) -> Leg:
        c = s.cols
        return cls(
            c["t"][rows] + shift_ns, c["bid"][rows], c["ask"][rows], c["bsz"][rows], c["asz"][rows]
        )

    def clip(self, lo: int, hi: int) -> Leg:
        a, b = np.searchsorted(self.t, lo), np.searchsorted(self.t, hi)
        return Leg(self.t[a:b], self.bid[a:b], self.ask[a:b], self.bsz[a:b], self.asz[a:b])


def _at(leg: Leg, times: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Row index of ``leg`` in force at each time (-1 before its first row), and validity."""
    idx = np.searchsorted(leg.t, times, side="right") - 1
    if not len(leg.t):  # a leg with no rows in this window (a feed gap): never valid
        return idx, np.zeros(len(times), dtype=bool)
    safe = np.maximum(idx, 0)
    b, a = leg.bid[safe], leg.ask[safe]
    valid = (idx >= 0) & (b > 0) & (a > 0) & (b < a)
    return idx, valid


def _quote(leg: Leg, i: int, valid: bool) -> Quote:
    if not valid:
        return Quote(False)
    return Quote(True, int(leg.bid[i]) / PX, int(leg.ask[i]) / PX)


def scan_window(
    k: Leg, y: Leg, n: Leg, open_unix: int, variants: tuple[Variant, ...] = VARIANTS
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every variant's consuming decision in one window, and the uncensored gap.

    ``k``, ``y``, ``n`` hold only rows that may count for this window: Kalshi
    from the contract switch, Polymarket from the open, all before the close.
    """
    o = open_unix * NS
    times = np.unique(np.concatenate([k.t, y.t, n.t]))
    stats: dict[str, Any] = {"events": len(times)}
    if not len(times):
        return [], stats
    ki, kv = _at(k, times)
    yi, yv = _at(y, times)
    ni, nv = _at(n, times)
    sec = (times - o) / NS
    both = kv & yv
    # Doubled mids in ticks: the gap is exact in integers, |gap| = |g2| / (2 PX).
    g2 = np.zeros(len(times), dtype=np.int64)
    kk, yy = ki[both], yi[both]
    g2[both] = (k.bid[kk].astype(np.int64) + k.ask[kk]) - (y.bid[yy].astype(np.int64) + y.ask[yy])

    entry = both & (sec >= BASE.entry_early_s) & (sec <= WINDOW_S - BASE.entry_late_s)
    abs_g = np.abs(g2) / (2 * PX)
    stats["max_abs_gap_full"] = float(abs_g[entry].max()) if entry.any() else None
    for thr in THRESHOLDS:
        hit = np.nonzero(entry & (abs_g >= thr - 1e-9))[0]
        key = f"{thr:.2f}"[2:]
        stats[f"first_cross_s_{key}"] = float(sec[hit[0]]) if len(hit) else None
        stats[f"first_cross_side_{key}"] = (
            None if not len(hit) else ("yes" if g2[hit[0]] > 0 else "no")
        )

    out = []
    for variant in variants:
        p = variant.params
        # A hair under the threshold: decide()'s float subtraction has the last word.
        cand = both & (sec >= p.entry_early_s) & (sec <= WINDOW_S - p.entry_late_s)
        cand &= abs_g >= p.gap_threshold - 2e-4
        for j in np.nonzero(cand)[0]:
            kq = _quote(k, int(ki[j]), bool(kv[j]))
            yq = _quote(y, int(yi[j]), bool(yv[j]))
            nq = _quote(n, int(ni[j]), bool(nv[j]))
            d = decide(kq, yq, nq, p, float(sec[j]), False)
            if not d.consumes_window:
                continue
            leg, li = (y, int(yi[j])) if d.buy_yes else (n, int(ni[j]))
            out.append(
                {
                    "variant": variant.name,
                    "open_unix": open_unix,
                    "t_ns": int(times[j]),
                    "sec_into": round(float(sec[j]), 3),
                    "action": d.action.value,
                    "side": "yes" if d.buy_yes else "no",
                    "entry_px": d.entry_px,
                    "gap": round(float(d.gap or 0.0), 4),
                    "touch_size": float(leg.asz[li]),
                    "k_bid": kq.bid,
                    "k_ask": kq.ask,
                    "k_bsz": float(k.bsz[ki[j]]),
                    "k_asz": float(k.asz[ki[j]]),
                    "y_bid": yq.bid,
                    "y_ask": yq.ask,
                    "n_bid": nq.bid,
                    "n_ask": nq.ask,
                    "enter": d.action is Action.ENTER,
                }  # fmt: skip
            )
            break
    return out, stats


def _grid(k: Leg, y: Leg, n: Leg, open_unix: int, cl: tuple[np.ndarray, np.ndarray] | None):
    o = open_unix * NS
    ends = o + (np.arange(WINDOW_S) + 1) * NS - 1
    ki, kv = _at(k, ends)
    yi, yv = _at(y, ends)
    ni, nv = _at(n, ends)
    ok = kv & yv & nv
    secs = np.nonzero(ok)[0]
    cols = {
        "open_unix": np.full(len(secs), open_unix, dtype=np.int64),
        "sec": secs.astype(np.int16),
        "k_bid": k.bid[ki[ok]] / PX, "k_ask": k.ask[ki[ok]] / PX,
        "y_bid": y.bid[yi[ok]] / PX, "y_ask": y.ask[yi[ok]] / PX,
        "n_bid": n.bid[ni[ok]] / PX, "n_ask": n.ask[ni[ok]] / PX,
        "y_asz": y.asz[yi[ok]], "n_asz": n.asz[ni[ok]],
    }  # fmt: skip
    if cl is not None and len(cl[0]):
        j = np.searchsorted(cl[0], ends[ok], side="right") - 1
        cols["s"] = np.where(j >= 0, cl[1][np.maximum(j, 0)], np.nan)
    else:
        cols["s"] = np.full(len(secs), np.nan)
    return cols


def _chainlink_window(cl: tuple[np.ndarray, np.ndarray] | None, open_unix: int) -> dict[str, Any]:
    if cl is None or not len(cl[0]):
        return {}
    t, px = cl
    o, c = open_unix * NS, (open_unix + WINDOW_S) * NS
    i_open = np.searchsorted(t, o, side="right") - 1
    i_close = np.searchsorted(t, c, side="right") - 1
    last60 = (t >= c - 60 * NS) & (t < c)
    return {
        "cl_open": float(px[i_open]) if i_open >= 0 else None,
        "cl_close_last": float(px[i_close]) if i_close >= 0 else None,
        "cl_twap60": float(px[last60].mean()) if last60.any() else None,
    }


def _poly_trades(day: str, root: Path | None, tokens: set[str], mono: np.ndarray, off: np.ndarray):
    """Polymarket trades on ``tokens``, receive time mapped to wall through the BBO rows."""
    rows: dict[str, list[tuple[int, float, float, str]]] = {}
    for stream in ("Polymarket_CLOB_Trade", "Polymarket_CLOB_No_Trade"):
        if not reader.path(stream, day, root).exists():
            continue
        for r in reader.iter_poly_trades(stream, day, root):
            if r.asset_id in tokens:
                rows.setdefault(r.asset_id, []).append((r.ts_received_ns, r.price, r.size, r.side))
    out = {}
    for token, trades in rows.items():
        recv = np.array([x[0] for x in trades], dtype=np.int64)
        wall = recv + np.interp(recv, mono, off).astype(np.int64)
        order = np.argsort(wall, kind="stable")
        out[token] = (
            wall[order],
            np.array([x[1] for x in trades])[order],
            np.array([x[2] for x in trades])[order],
            np.array([x[3] for x in trades])[order],
        )
    return out


def run_day(
    day: str,
    out_dir: Path,
    root: Path | None,
    resolutions: dict[tuple[str, int], dict[str, Any]],
    shift_ms: dict[str, float],
) -> dict[str, Any]:
    import pandas as pd

    started = time.time()
    timings: dict[str, float] = {}
    missing = [
        st for st in ("Polymarket_CLOB_Yes_BBO", "Polymarket_CLOB_No_BBO", "Kalshi_Yes_BBO")
        if not reader.path(st, day, root).exists()
    ]  # fmt: skip
    if missing:
        return {"day": day, "skipped": f"missing {', '.join(missing)}"}
    windows = list(reader.iter_windows(day, root))
    if not windows:
        return {"day": day, "skipped": "no windows"}
    yes_tokens = [w.token_yes for w in windows]
    no_tokens = [w.token_no for w in windows]
    t0 = time.time()
    yes = reader.poly_bbo_series("Polymarket_CLOB_Yes_BBO", day, yes_tokens, root, extra=("recv",))
    no = reader.poly_bbo_series("Polymarket_CLOB_No_BBO", day, no_tokens, root)
    timings["poly"] = time.time() - t0
    t0 = time.time()
    aids = list(reader.HELIX_15M)
    ky = reader.kalshi_quote_series("Kalshi_Yes_BBO", day, aids, root, extra=("seq",))
    timings["kalshi"] = time.time() - t0
    t0 = time.time()
    spot = {reader.CHAINLINK_SPOT[a]: a for a in reader.ASSETS}
    chainlink = {spot[aid]: v for aid, v in reader.chainlink_prices(day, list(spot), root).items()}
    order = np.argsort(yes.cols["recv"])[:: max(1, len(yes) // 100_000)]
    mono = yes.cols["recv"][order]
    off = (yes.cols["t"] - yes.cols["recv"])[order].astype(np.float64)
    trades = _poly_trades(day, root, set(yes_tokens) | set(no_tokens), mono, off)
    timings["trades_chainlink"] = time.time() - t0

    shift_k = round(shift_ms.get("k", 0) * 1e6)
    shift_p = round(shift_ms.get("p", 0) * 1e6)
    by_asset = {aid: i for i, aid in enumerate(aids)}
    crossings: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    grids: list[dict[str, np.ndarray]] = []
    slices: list[dict[str, Any]] = []
    t0 = time.time()
    for idx, w in enumerate(windows):
        asset = reader.HELIX_15M[w.asset]
        o, c = w.open_unix * NS, (w.open_unix + WINDOW_S) * NS
        k_rows = ky.rows(by_asset[w.asset])
        kt, kseq = ky.cols["t"][k_rows], ky.cols["seq"][k_rows]
        start = reader.kalshi_contract_start(kt, kseq, o)
        k_all = Leg.of(ky, slice(k_rows.start + start, k_rows.stop), shift_k)
        k = k_all.clip(o, c)
        y = Leg.of(yes, yes.rows(idx), shift_p).clip(o, c)
        n = Leg.of(no, no.rows(idx), shift_p).clip(o, c)
        found, stats = scan_window(k, y, n, w.open_unix)
        res = resolutions.get((asset, w.open_unix), {})
        window_rows.append(
            {
                "asset": asset,
                "open_unix": w.open_unix,
                "strike": w.strike if w.strike == w.strike else None,
                "poly_outcome": res.get("poly_outcome"),
                "kalshi_switch_ms": (int(k_all.t[0]) - o) / 1e6 if len(k_all.t) else None,
                "k_rows": len(k.t),
                "y_rows": len(y.t),
                "n_rows": len(n.t),
            }
            | stats
            | _chainlink_window(chainlink.get(asset), w.open_unix)
        )
        g = _grid(k, y, n, w.open_unix, chainlink.get(asset))
        g["asset"] = np.full(len(g["sec"]), asset)
        grids.append(g)
        seen: set[int] = set()
        for f in found:
            f["asset"] = asset
            crossings.append(f)
            t_x = f["t_ns"]
            if t_x in seen:
                continue
            seen.add(t_x)
            lo, hi = t_x - SLICE_BEFORE_NS, t_x + SLICE_AFTER_NS
            for leg_name, leg in (("k", k), ("y", y), ("n", n)):
                part = leg.clip(lo, hi)
                for j in range(len(part.t)):
                    slices.append(
                        {"asset": asset, "open_unix": w.open_unix, "t_cross": t_x,
                         "kind": f"bbo_{leg_name}", "dt_ms": (int(part.t[j]) - t_x) / 1e6,
                         "bid": part.bid[j] / PX, "ask": part.ask[j] / PX,
                         "bsz": float(part.bsz[j]), "asz": float(part.asz[j]),
                         "trade_px": None, "trade_sz": None, "trade_side": None}
                    )  # fmt: skip
            for leg_name, token in (("y", w.token_yes), ("n", w.token_no)):
                tr = trades.get(token)
                if tr is None:
                    continue
                a, b = np.searchsorted(tr[0], lo), np.searchsorted(tr[0], hi)
                for j in range(a, b):
                    slices.append(
                        {"asset": asset, "open_unix": w.open_unix, "t_cross": t_x,
                         "kind": f"trade_{leg_name}", "dt_ms": (int(tr[0][j]) - t_x) / 1e6,
                         "bid": None, "ask": None, "bsz": None, "asz": None,
                         "trade_px": float(tr[1][j]), "trade_sz": float(tr[2][j]),
                         "trade_side": str(tr[3][j])}
                    )  # fmt: skip
    timings["scan"] = time.time() - t0

    day_dir = out_dir / day
    tmp = out_dir / f".{day}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    pd.DataFrame(crossings).to_parquet(tmp / "crossings.parquet", index=False)
    pd.DataFrame(window_rows).to_parquet(tmp / "windows.parquet", index=False)
    pd.DataFrame(slices).to_parquet(tmp / "slices.parquet", index=False)
    grid = {c: np.concatenate([g[c] for g in grids]) for c in grids[0]} if grids else {}
    pd.DataFrame(grid).to_parquet(tmp / "grid.parquet", index=False)
    summary = {
        "day": day,
        "windows": len(windows),
        "crossings": len(crossings),
        "slices_rows": len(slices),
        "grid_rows": int(len(grid.get("sec", []))),
        "rows": {"poly_yes_raw": yes.raw_rows, "poly_yes": len(yes), "poly_no": len(no),
                 "kalshi_raw": ky.raw_rows, "kalshi": len(ky)},
        "shift_ms": shift_ms,
        "timings_s": {k: round(v, 1) for k, v in timings.items()},
        "seconds": round(time.time() - started, 1),
        "variants": {v.name: asdict(v.params) for v in VARIANTS},
    }  # fmt: skip
    (tmp / "summary.json").write_text(json.dumps(summary, indent=1))
    shutil.rmtree(day_dir, ignore_errors=True)
    os.replace(tmp, day_dir)
    return summary


def _free_gb(p: Path) -> float:
    return shutil.disk_usage(p).free / 1e9


def _parse_shift(text: str) -> dict[str, float]:
    out = {}
    for part in filter(None, text.split(",")):
        venue, _, value = part.partition("=")
        out[venue.strip()] = float(value)
    return out


def _days(spec: str | None, root: Path | None) -> list[str]:
    available = reader.days("Polymarket_Window", root)
    if not spec:
        return available
    if "-" in spec:
        lo, hi = spec.split("-", 1)
        return [d for d in available if lo <= d <= hi]
    return [d for d in spec.split(",") if d in available]


def _load_resolutions(path: Path) -> dict[tuple[str, int], dict[str, Any]]:
    import pandas as pd

    if not path.is_file():
        raise SystemExit(f"{path} is missing: run python -m research.helix.resolutions first")
    df = pd.read_parquet(path)
    return {(r["asset"], int(r["open_unix"])): r for r in df.to_dict("records")}


def _main() -> None:
    parser = argparse.ArgumentParser(prog="research.helix_replay")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--root", type=Path, default=None, help="tape dir (default HELIX_TAPE_DIR)")
    run.add_argument("--days", help="YYYYMMDD-YYYYMMDD or a comma list (default: all)")
    run.add_argument("--out", type=Path, default=Path("research/data/helix"))
    run.add_argument("--resolutions", type=Path, default=Path("research/data/resolutions.parquet"))
    run.add_argument("--jobs", type=int, default=3, help="days in parallel (~3 GB each)")
    run.add_argument("--shift-ms", default="", help="per-venue delay, e.g. k=9,p=-4")
    run.add_argument("--force", action="store_true", help="redo days already written")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    resolutions = _load_resolutions(args.resolutions)
    shift = _parse_shift(args.shift_ms)
    todo = [
        d for d in _days(args.days, args.root)
        if args.force or not (args.out / d / "summary.json").is_file()
    ]  # fmt: skip
    print(f"{len(todo)} days to run", flush=True)
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        pending = {}
        queue = list(todo)
        while queue or pending:
            while queue and len(pending) < args.jobs:
                if _free_gb(args.out) < MIN_FREE_GB:
                    raise SystemExit(f"under {MIN_FREE_GB} GB free: stopping (PLAN 3.3)")
                day = queue.pop(0)
                fut = pool.submit(run_day, day, args.out, args.root, resolutions, shift)
                pending[fut] = day
            done = next(as_completed(pending))
            day = pending.pop(done)
            try:
                s = done.result()
                keys = ("day", "skipped", "crossings", "seconds", "timings_s")
                print(json.dumps({k: s[k] for k in keys if k in s}), flush=True)
            except Exception as exc:  # noqa: BLE001 - one bad day must not stop the batch
                print(json.dumps({"day": day, "error": f"{type(exc).__name__}: {exc}"}), flush=True)


if __name__ == "__main__":
    _main()
