"""The pre-registered gates, read from the journal (plan §Gates, manual §10).

Pure Python (no pandas) so the status page can show progress on the host.
``research.readout`` prints the same numbers.

Per entry, net ¢/share = payout/share - VWAP - fee/share, in cents. Bands are
the manual's eight entry-price bands. Day-t is the mean of daily net ¢/share
over its standard error.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

__all__ = ["BANDS", "band_of", "evaluate"]

BANDS = (
    (0.0, 0.3),
    (0.3, 0.4),
    (0.4, 0.5),
    (0.5, 0.6),
    (0.6, 0.7),
    (0.7, 0.8),
    (0.8, 0.9),
    (0.9, 1.0),
)
GATE_MIN_N = 300
GATE_MIN_DAY_T = 2.0
GATE_DOLLARS_PER_ENTRY = (4.4, 6.9)
DISAGREEMENT_EXPECTED = (0.03, 0.10)


def band_of(price: float) -> str:
    for lo, hi in BANDS:
        if lo <= price < hi:
            return f"[{lo:.1f},{hi:.1f})"
    return "[0.9,1.0)"


def _pctl(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def _day(open_unix: int) -> str:
    return datetime.fromtimestamp(open_unix, UTC).strftime("%Y-%m-%d")


def _t(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    sd = statistics.stdev(values)
    return None if sd == 0 else statistics.fmean(values) / (sd / math.sqrt(len(values)))


def evaluate(records: Iterable[dict[str, Any]], max_entry_px: float = 0.60) -> dict[str, Any]:
    fills: dict[tuple[str, int], dict[str, Any]] = {}
    settles: dict[tuple[str, int], dict[str, Any]] = {}
    decisions: dict[str, int] = defaultdict(int)
    no_fills = 0
    resolutions: dict[tuple[str, int], dict[str, str]] = defaultdict(dict)
    windows: dict[tuple[str, str], int] = defaultdict(int)
    for r in records:
        key = (r.get("asset"), r.get("open_unix"))
        kind = r.get("type")
        if kind == "decision":
            decisions[r["action"] if not r.get("blocked") else f"blocked_{r['blocked']}"] += 1
        elif kind == "fill":
            if r["shares"] > 0:
                fills[key] = r
            else:
                no_fills += 1
        elif kind == "settle":
            settles[key] = r
        elif kind == "resolution":
            resolutions[key][r["venue"]] = r["outcome"]
        elif kind == "window":
            windows[(r["asset"], _day(r["open_unix"]))] += 1

    vwaps = [f["vwap"] for f in fills.values()]
    settled = []
    for key, s in settles.items():
        f = fills.get(key)
        if f is None:
            continue
        per_share = 100 * ((s["payout"] - f["cost"] - f["fee"]) / f["shares"])
        settled.append((key, f, s, per_share))

    bands: dict[str, list[float]] = defaultdict(list)
    daily: dict[str, list[float]] = defaultdict(list)
    for key, f, _s, cents in settled:
        bands[band_of(f["vwap"])].append(cents)
        if f["vwap"] <= max_entry_px:
            daily[_day(key[1])].append(cents)
    day_means = [statistics.fmean(v) for _, v in sorted(daily.items())]
    in_band = [c for (_k, f, _s, c) in settled if f["vwap"] <= max_entry_px]
    pnl = [s["pnl"] for (_k, _f, s, _c) in settled]
    slip = [f["vwap"] - f["touch_px"] for f in fills.values()]
    fill_ratio = [f["shares"] / f["ordered"] for f in fills.values() if f.get("ordered")]

    both = [v for v in resolutions.values() if "poly" in v and "kalshi" in v]
    disagree = sum(1 for v in both if v["poly"] != v["kalshi"])

    day_t = _t(day_means)
    avg_vwap = statistics.fmean(vwaps) if vwaps else None
    p90 = _pctl(vwaps, 0.9)
    dollars = statistics.fmean(pnl) if pnl else None
    gates = {
        "fill_price": None
        if avg_vwap is None
        else (avg_vwap <= max_entry_px and (p90 or 0) <= max_entry_px),
        "day_t": None if len(in_band) < GATE_MIN_N or day_t is None else day_t >= GATE_MIN_DAY_T,
        "dollars_per_entry": None
        if len(pnl) < GATE_MIN_N or dollars is None
        else GATE_DOLLARS_PER_ENTRY[0] <= dollars,
    }
    return {
        "decisions": dict(decisions),
        "fills": len(fills),
        "no_fills": no_fills,
        "fill_rate": None if not (fills or no_fills) else len(fills) / (len(fills) + no_fills),
        "avg_fill_ratio": statistics.fmean(fill_ratio) if fill_ratio else None,
        "vwap": {"avg": avg_vwap, "p10": _pctl(vwaps, 0.1), "p50": _pctl(vwaps, 0.5), "p90": p90},
        "slippage_vs_touch": statistics.fmean(slip) if slip else None,
        "settled": len(settled),
        "net_cents_per_share": statistics.fmean([c for *_, c in settled]) if settled else None,
        "dollars_per_entry": dollars,
        "pnl_total": sum(pnl),
        "bands": {
            band_of(lo): {"n": len(bands.get(band_of(lo), [])), "net_cents": _mean(bands, lo)}
            for lo, _hi in BANDS
        },
        "in_band_n": len(in_band),
        "days": len(day_means),
        "days_positive": sum(1 for m in day_means if m > 0),
        "day_t": day_t,
        "venue_disagreement": None if not both else disagree / len(both),
        "resolved_windows": len(both),
        "windows_per_asset_day": {f"{a} {d}": n for (a, d), n in sorted(windows.items())},
        "gates": gates,
        "gate_min_n": GATE_MIN_N,
    }


def _mean(bands: dict[str, list[float]], lo: float) -> float | None:
    values = bands.get(band_of(lo), [])
    return statistics.fmean(values) if values else None
