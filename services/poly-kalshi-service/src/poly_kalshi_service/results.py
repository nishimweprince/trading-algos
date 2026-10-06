"""Paper results from the journal: per-day table, equity curve, trades, breakdowns.

``signal`` is the first thing to read when the curve goes negative. Over
settled trades it compares three numbers:

- ``win_rate``: how often the leg we bought won;
- ``kalshi_implied``: the probability Kalshi's mid gave that leg at entry;
- ``avg_price``: what we paid.

If win rate tracks Kalshi and sits above the price, the signal works and any
loss is costs or noise. If win rate tracks the price, Kalshi carries no
information over Polymarket any more: the edge is gone. If win rate is far
*below* the price, suspect a bug (leg mapping, settlement) before the market.

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


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def _kalshi_implied(decision: dict[str, Any], side: str) -> float | None:
    k = decision.get("kalshi") or {}
    if k.get("bid") is None or k.get("ask") is None:
        return None
    mid = (k["bid"] + k["ask"]) / 2
    return mid if side == "yes" else 1 - mid


def _group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    implied = [r["implied"] for r in rows if r["implied"] is not None]
    return {
        "n": len(rows),
        "win_rate": statistics.fmean(r["win"] for r in rows) if rows else None,
        "avg_price": statistics.fmean(r["vwap"] for r in rows) if rows else None,
        "kalshi_implied": statistics.fmean(implied) if implied else None,
        "net_cents": statistics.fmean(r["cents"] for r in rows) if rows else None,
        "pnl": round(sum(r["pnl"] for r in rows), 2),
    }


SEC_BUCKETS = ((0, 60), (60, 180), (180, 420), (420, 900))


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

    settled_rows = []
    for key, s in settles.items():
        f, d = fills.get(key), decisions.get(key, {})
        if f is None or f["shares"] <= 0:
            continue
        settled_rows.append(
            {
                "asset": key[0],
                "sec": d.get("sec_into") or 0.0,
                "win": 1.0 if s["payout"] > 0 else 0.0,
                "vwap": f["vwap"],
                "implied": _kalshi_implied(d, f["side"]),
                "cents": 100 * (s["payout"] - f["cost"] - f["fee"]) / f["shares"],
                "pnl": s["pnl"],
            }
        )
    by_asset = {
        asset: _group([r for r in settled_rows if r["asset"] == asset])
        for asset in sorted({r["asset"] for r in settled_rows})
    }
    by_sec = {
        f"{lo}-{hi}s": _group([r for r in settled_rows if lo <= r["sec"] < hi])
        for lo, hi in SEC_BUCKETS
    }
    filled = [f for f in fills.values() if f["shares"] > 0]
    ages_k = [d["age_ms"]["kalshi"] for d in decisions.values()
              if (d.get("age_ms") or {}).get("kalshi") is not None]  # fmt: skip
    execution = {
        "orders": len(fills),
        "filled": len(filled),
        "fill_rate": len(filled) / len(fills) if fills else None,
        "avg_fill_ratio": statistics.fmean(f["shares"] / f["ordered"] for f in filled)
        if filled
        else None,
        "avg_slippage_cents": statistics.fmean(100 * (f["vwap"] - f["touch_px"]) for f in filled)
        if filled
        else None,
        "avg_fee_cents_per_share": statistics.fmean(100 * f["fee"] / f["shares"] for f in filled)
        if filled
        else None,
        "order_latency_ms_p50": _pct(
            [f["latency_ms"] for f in fills.values() if f.get("latency_ms") is not None], 0.5
        ),  # fmt: skip
        "kalshi_age_ms_p50": _pct(ages_k, 0.5),
        "kalshi_age_ms_p90": _pct(ages_k, 0.9),
    }
    return {
        "daily": daily[::-1],
        "curve": curve,
        "trades": trades[::-1][:trades_limit],
        "signal": _group(settled_rows),
        "by_asset": by_asset,
        "by_sec": by_sec,
        "execution": execution,
    }
