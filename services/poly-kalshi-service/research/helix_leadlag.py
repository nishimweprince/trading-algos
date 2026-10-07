"""PLAN 4.2 on the Helix tape: who moves first, spot, Kalshi or Polymarket, at ~20 ms.

    python -m research.helix_leadlag --days 20260820,20260901 [--bin-ms 20]

Per asset and day, inside every window's entry period (20 s to 840 s), each
series is sampled at the end of every ``bin_ms`` bin and differenced:

- **spot**: Binance spot best bid/ask mid (``BinanceSpot_Book``, a 100 ms depth
  stream, so ~100 ms is the finest lead this can resolve; receive time is
  monotonic and is mapped to wall through the Polymarket rows);
- **chainlink**: the Chainlink reference price (the settlement source);
- **kalshi**: Kalshi YES mid, from the window's own contract (seq switch);
- **poly**: Polymarket YES mid of the window's token.

``xcorr[a→b]`` is corr(Δa(t), Δb(t + lag)) for lags of ±3 s: a peak at a
positive lag means a leads b by that much. ``incremental`` regresses
Polymarket's move over the next 500 ms on spot's and Kalshi's moves over the
past second: the R² with spot alone and with both, and Kalshi's t-stat. If
Kalshi adds nothing once spot is known, the gap signal is spot, late.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

from research.helix import reader, recv3

NS = 1_000_000_000
MAX_LAG_S = 3.0
SPOT_ID = {"BTC": 15, "ETH": 16, "SOL": 17, "XRP": 18, "BNB": 19, "DOGE": 20}


def _sample(t: np.ndarray, v: np.ndarray, ends: np.ndarray, lo: int) -> np.ndarray:
    """Value in force at each bin end; NaN before the first row at or after ``lo``."""
    i = np.searchsorted(t, ends, side="right") - 1
    out = np.where(i >= 0, v[np.maximum(i, 0)], np.nan)
    first = np.searchsorted(t, lo)
    return np.where(i >= first, out, np.nan)


def _spot_books(day: str, root: Path | None, mono: np.ndarray, off: np.ndarray):
    out: dict[int, list[tuple[int, float]]] = {aid: [] for aid in SPOT_ID.values()}
    p = reader.path("BinanceSpot_Book", day, root)
    if not p.exists():
        return {}
    for b in recv3.iter_book(str(p)):
        if b.asset_id in out and b.bids and b.asks:
            out[b.asset_id].append((b.ts_received_ns, (b.bids[0][0] + b.asks[0][0]) / 2))
    result = {}
    for aid, rows in out.items():
        if not rows:
            continue
        recv = np.array([r[0] for r in rows], dtype=np.int64)
        wall = recv + np.interp(recv, mono, off).astype(np.int64)
        order = np.argsort(wall, kind="stable")
        result[aid] = (wall[order], np.log(np.array([r[1] for r in rows]))[order])
    return result


def day_series(day: str, root: Path | None, bin_ms: int) -> dict[str, dict[str, np.ndarray]]:
    windows = list(reader.iter_windows(day, root))
    yes = reader.poly_bbo_series(
        "Polymarket_CLOB_Yes_BBO", day, [w.token_yes for w in windows], root, extra=("recv",)
    )
    aids = list(reader.HELIX_15M)
    ky = reader.kalshi_quote_series("Kalshi_Yes_BBO", day, aids, root, extra=("seq",))
    order = np.argsort(yes.cols["recv"])[:: max(1, len(yes) // 100_000)]
    mono = yes.cols["recv"][order]
    off = (yes.cols["t"] - yes.cols["recv"])[order].astype(np.float64)
    spot = _spot_books(day, root, mono, off)
    cl = reader.chainlink_prices(day, list(reader.CHAINLINK_SPOT.values()), root)
    step = bin_ms * 1_000_000
    out: dict[str, dict[str, list[np.ndarray]]] = {}
    for k, w in enumerate(windows):
        asset = reader.HELIX_15M[w.asset]
        o = w.open_unix * NS
        ends = np.arange(o + 20 * NS, o + 840 * NS, step) + step - 1
        sl = yes.rows(k)
        y = yes.cols
        valid = (y["bid"][sl] > 0) & (y["ask"][sl] > y["bid"][sl])
        p_mid = ((y["bid"][sl] + y["ask"][sl]) / (2 * reader.PX)).astype(float)
        poly = _sample(y["t"][sl][valid], p_mid[valid], ends, o)
        ks = ky.rows(aids.index(w.asset))
        kt, kseq = ky.cols["t"][ks], ky.cols["seq"][ks]
        start = reader.kalshi_contract_start(kt, kseq, o)
        kb, ka = ky.cols["bid"][ks][start:], ky.cols["ask"][ks][start:]
        kvalid = (kb > 0) & (ka > kb)
        k_mid = ((kb + ka) / (2 * reader.PX)).astype(float)
        kal = _sample(kt[start:][kvalid], k_mid[kvalid], ends, o)
        series = {"poly": poly, "kalshi": kal}
        if asset in SPOT_ID and SPOT_ID[asset] in spot:
            st, sv = spot[SPOT_ID[asset]]
            series["spot"] = _sample(st, sv, ends, o - 10 * NS)
        cid = reader.CHAINLINK_SPOT[asset]
        if cid in cl:
            ct, cv = cl[cid]
            series["chainlink"] = _sample(ct, np.log(cv), ends, o - 10 * NS)
        for name, values in series.items():
            d = np.diff(values, prepend=np.nan)
            out.setdefault(asset, {}).setdefault(name, []).append(np.nan_to_num(d))
    return {a: {n: np.concatenate(v) for n, v in s.items()} for a, s in out.items()}


def xcorr(a: np.ndarray, b: np.ndarray, max_lag: int) -> np.ndarray:
    """corr(a(t), b(t + lag)) for lag in [-max_lag, max_lag] bins, via FFT."""
    n = len(a)
    size = 1 << (2 * n - 1).bit_length()
    fa, fb = np.fft.rfft(a, size), np.fft.rfft(b, size)
    full = np.fft.irfft(np.conj(fa) * fb, size)
    lags = np.concatenate([full[size - max_lag :], full[: max_lag + 1]])
    denom = np.sqrt((a * a).sum() * (b * b).sum())
    return lags / denom if denom > 0 else lags * 0


def incremental(s: dict[str, np.ndarray], bin_ms: int) -> dict[str, Any] | None:
    if "spot" not in s:
        return None
    past, ahead = round(1000 / bin_ms), round(500 / bin_ms)

    def window_sum(x: np.ndarray, k: int) -> np.ndarray:
        c = np.concatenate([[0.0], np.cumsum(x)])
        return c[k:] - c[:-k]

    n = len(s["poly"])
    idx = np.arange(past, n - ahead, 5)  # every 5th bin: overlapping targets, fewer rows
    y = window_sum(s["poly"], ahead)[idx]
    xs = window_sum(s["spot"], past)[idx - past]
    xk = window_sum(s["kalshi"], past)[idx - past]
    mask = (y != 0) | (xs != 0) | (xk != 0)
    y, xs, xk = y[mask], xs[mask], xk[mask]
    if len(y) < 100:
        return None

    def fit(cols: list[np.ndarray]) -> tuple[float, np.ndarray, np.ndarray]:
        x = np.column_stack([np.ones(len(y)), *cols])
        beta, *_ = np.linalg.lstsq(x, y, rcond=None)
        resid = y - x @ beta
        r2 = 1 - resid.var() / y.var() if y.var() > 0 else 0.0
        cov = np.linalg.pinv(x.T @ x) * resid.var()
        return float(r2), beta, np.sqrt(np.diag(cov))

    r2_s, _, _ = fit([xs])
    r2_k, _, _ = fit([xk])
    r2_sk, beta, se = fit([xs, xk])
    return {
        "rows": int(len(y)),
        "r2_spot": round(r2_s, 4),
        "r2_kalshi": round(r2_k, 4),
        "r2_spot_kalshi": round(r2_sk, 4),
        "kalshi_t_given_spot": round(float(beta[2] / se[2]), 2) if se[2] > 0 else None,
    }


def _main() -> None:
    parser = argparse.ArgumentParser(prog="research.helix_leadlag")
    parser.add_argument("--days", required=True, help="comma list of YYYYMMDD")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--bin-ms", type=int, default=20)
    parser.add_argument("--out", type=Path, default=Path("research/data/helix_leadlag.json"))
    args = parser.parse_args()
    max_lag = round(MAX_LAG_S * 1000 / args.bin_ms)
    lags_ms = (np.arange(-max_lag, max_lag + 1) * args.bin_ms).tolist()
    pairs = (("spot", "poly"), ("spot", "kalshi"), ("kalshi", "poly"), ("chainlink", "poly"),
             ("spot", "chainlink"))  # fmt: skip
    acc: dict[str, dict[str, np.ndarray]] = {}
    inc: dict[str, list[dict[str, Any]]] = {}
    for day in args.days.split(","):
        series = day_series(day, args.root, args.bin_ms)
        for asset, s in series.items():
            for a, b in pairs:
                if a in s and b in s:
                    c = xcorr(s[a], s[b], max_lag)
                    key = f"{a}->{b}"
                    acc.setdefault(asset, {})[key] = acc.get(asset, {}).get(key, 0) + c
            r = incremental(s, args.bin_ms)
            if r is not None:
                inc.setdefault(asset, []).append(r | {"day": day})
        print(day, "done", flush=True)
    ndays = len(args.days.split(","))
    result: dict[str, Any] = {"days": args.days.split(","), "bin_ms": args.bin_ms, "assets": {}}
    for asset, curves in acc.items():
        body: dict[str, Any] = {}
        for key, c in curves.items():
            c = c / ndays
            peak = int(np.argmax(c))
            body[key] = {
                "peak_lag_ms": lags_ms[peak],
                "peak_corr": round(float(c[peak]), 4),
                "corr_at_0": round(float(c[max_lag]), 4),
                "corr_at_150ms": round(float(c[max_lag + round(150 / args.bin_ms)]), 4),
                "share_of_mass_after_150ms": round(
                    float(c[max_lag + round(150 / args.bin_ms) :].clip(0).sum() / c.clip(0).sum()),
                    3,
                ),
            }
        body["incremental"] = inc.get(asset)
        result["assets"][asset] = body
    args.out.write_text(json.dumps(result, indent=1))
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    _main()
