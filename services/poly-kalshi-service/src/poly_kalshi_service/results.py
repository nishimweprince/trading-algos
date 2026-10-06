"""Paper results from the journal: per-day table, equity curve, recent trades.

Read by ``pk-status`` straight from ``<state_dir>/journal`` on disk, so showing
results costs the trading process nothing. Pure Python, like ``gates``.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

__all__ = ["paper_results"]

CURVE_POINTS = 400  # enough for a sparkline; older points are thinned


def _day(t_ns: int) -> str:
    return datetime.fromtimestamp(t_ns / 1e9, UTC).strftime("%Y-%m-%d")


def paper_results(records: Iterable[dict[str, Any]], trades_limit: int = 50) -> dict[str, Any]:
    decisions: dict[tuple[str, int], dict[str, Any]] = {}
    fills: dict[tuple[str, int], dict[str, Any]] = {}
    settles: dict[tuple[str, int], dict[str, Any]] = {}
    for r in records:
        key = (r.get("asset"), r.get("open_unix"))
        kind = r.get("type")
        if kind == "decision":
            decisions[key] = r
        elif kind == "fill":
            fills[key] = r
        elif kind == "settle":
            settles[key] = r

    days: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"enter": 0, "skip": 0, "blocked": 0, "filled": 0, "no_fill": 0, "settled": 0,
                 "wins": 0, "vwaps": [], "cents": [], "pnl": 0.0, "fees": 0.0}
    )  # fmt: skip
    for key, d in decisions.items():
        day = days[_day(d["t_ns"])]
        if d["action"] == "skip":
            day["skip"] += 1
        elif d.get("blocked"):
            day["blocked"] += 1
        else:
            day["enter"] += 1
        f = fills.get(key)
        if f is None:
            continue
        if f["shares"] <= 0:
            day["no_fill"] += 1
            continue
        day["filled"] += 1
        day["vwaps"].append(f["vwap"])
        day["fees"] += f["fee"]
        s = settles.get(key)
        if s is not None:
            day["settled"] += 1
            day["wins"] += 1 if s["payout"] > 0 else 0
            day["pnl"] += s["pnl"]
            day["cents"].append(100 * (s["payout"] - f["cost"] - f["fee"]) / f["shares"])

    daily = []
    cumulative = 0.0
    for name in sorted(days):
        d = days[name]
        cumulative += d["pnl"]
        daily.append(
            {
                "day": name,
                "enter": d["enter"],
                "skip": d["skip"],
                "blocked": d["blocked"],
                "filled": d["filled"],
                "no_fill": d["no_fill"],
                "settled": d["settled"],
                "win_rate": d["wins"] / d["settled"] if d["settled"] else None,
                "avg_vwap": statistics.fmean(d["vwaps"]) if d["vwaps"] else None,
                "net_cents": statistics.fmean(d["cents"]) if d["cents"] else None,
                "fees": round(d["fees"], 2),
                "pnl": round(d["pnl"], 2),
                "cumulative": round(cumulative, 2),
            }
        )

    trades = []
    for key, f in fills.items():
        if f["shares"] <= 0:
            continue
        d, s = decisions.get(key, {}), settles.get(key)
        trades.append(
            {
                "asset": key[0],
                "open_unix": key[1],
                "t_ns": f["t_ns"],
                "side": f["side"],
                "gap": d.get("gap"),
                "sec_into": d.get("sec_into"),
                "touch_px": f["touch_px"],
                "vwap": f["vwap"],
                "shares": f["shares"],
                "ordered": f["ordered"],
                "fee": f["fee"],
                "outcome": None if s is None else s["outcome"],
                "pnl": None if s is None else s["pnl"],
            }
        )
    trades.sort(key=lambda t: t["t_ns"])

    curve = []
    running = 0.0
    for t in trades:
        if t["pnl"] is not None:
            running += t["pnl"]
            curve.append([t["t_ns"] // 1_000_000_000, round(running, 2)])
    if len(curve) > CURVE_POINTS:
        step = len(curve) / CURVE_POINTS
        curve = [curve[int(i * step)] for i in range(CURVE_POINTS - 1)] + [curve[-1]]

    return {"daily": daily[::-1], "curve": curve, "trades": trades[::-1][:trades_limit]}
