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

**Days.** Entries, fills and no-fills count on the day of the decision; settled
trades, wins and P&L count on the day they settled (UTC), the day cash moved.
That is the ledger's ``realised_today`` basis too, so the two agree.

**Markouts** (``markout`` records): the cheap leg's mid 1, 5 and 30 s after a
fill or a no-fill, against the price paid (or the limit), in ¢/share. Fills
next to no-fills is the direct read of adverse selection.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from .decision import WINDOW_S
from .engine import MARKOUT_S
from .shadow import shadow_table

__all__ = ["SETTLE_DELAY_DEFAULT_S", "open_trade_etas", "paper_results"]

CURVE_POINTS = 400  # enough for a sparkline; older points are thinned
# Expected seconds from window close to the Polymarket resolution when the
# journal holds no measured delay yet (Oct 2026 median was ~234 s).
SETTLE_DELAY_DEFAULT_S = 240.0


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


def _quality(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Trade quality over settled trades in time order."""
    pnl = [r["pnl"] for r in rows]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p <= 0]
    streak = longest = 0
    for p in pnl:
        streak = streak + 1 if p <= 0 else 0
        longest = max(longest, streak)
    fees = sum(r["fee"] for r in rows)
    gross = sum(pnl) + fees
    return {
        "n": len(pnl),
        "wins": len(wins),
        "losses": len(losses),
        "avg_win": statistics.fmean(wins) if wins else None,
        "avg_loss": statistics.fmean(losses) if losses else None,
        "profit_factor": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else None,
        "expectancy": statistics.fmean(pnl) if pnl else None,
        "largest_win": max(wins) if wins else None,
        "largest_loss": min(losses) if losses else None,
        "longest_losing_streak": longest,
        "gross_pnl": round(gross, 2),
        "fees": round(fees, 2),
        "fees_pct_of_gross": 100 * fees / gross if gross > 0 else None,
    }


def _drawdown(pnl: list[float]) -> dict[str, float]:
    """Max and current drawdown ($) of cumulative settled P&L, from a zero start."""
    peak = running = worst = 0.0
    for p in pnl:
        running += p
        peak = max(peak, running)
        worst = max(worst, peak - running)
    return {"max": round(worst, 2), "current": round(peak - running, 2)}


def open_trade_etas(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Open paper trades with settlement ETAs, for the status page.

    A trade is open when its fill holds shares and no settle row exists for
    the same window. ETA is window close plus the median measured Polymarket
    resolution delay (``SETTLE_DELAY_DEFAULT_S`` until the journal holds a
    sample). Kalshi's outcome is included when already recorded: an early
    hint only, since the venues can disagree. Pure journal read, like
    ``paper_results``: showing it costs the trading process nothing.
    """
    fills: dict[tuple[str | None, int | None], dict[str, Any]] = {}
    settled: set[tuple[str | None, int | None]] = set()
    kalshi_outcome: dict[tuple[str | None, int | None], str] = {}
    delays: list[float] = []
    for r in records:
        kind = r.get("type")
        if kind == "fill" and (r.get("shares") or 0) > 0:
            fills[(r.get("asset"), r.get("open_unix"))] = r
        elif kind == "settle":
            settled.add((r.get("asset"), r.get("open_unix")))
        elif kind == "resolution":
            if r.get("venue") == "poly" and r.get("delay_s") is not None:
                delays.append(r["delay_s"])
            elif r.get("venue") == "kalshi" and r.get("outcome"):
                kalshi_outcome[(r.get("asset"), r.get("open_unix"))] = r["outcome"]
    basis = statistics.median(delays) if delays else SETTLE_DELAY_DEFAULT_S
    trades = []
    for (asset, open_unix), f in fills.items():
        if (asset, open_unix) in settled or open_unix is None:
            continue
        close_unix = open_unix + WINDOW_S
        trades.append(
            {
                "asset": asset,
                "side": f["side"],
                "shares": f["shares"],
                "vwap": f["vwap"],
                "cost": f.get("cost"),
                "open_unix": open_unix,
                "close_unix": close_unix,
                "eta_unix": close_unix + basis,
                "kalshi_outcome": kalshi_outcome.get((asset, open_unix)),
            }
        )
    trades.sort(key=lambda t: t["eta_unix"])
    return {
        "trades": trades,
        "settle_delay_s": basis,
        "delay_samples": len(delays),
        "delay_default": not delays,
    }


def _markout_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"n": len(rows)}
    for h in MARKOUT_S:
        values = [r[f"markout_{h}s"] for r in rows if r.get(f"markout_{h}s") is not None]
        out[f"{h}s"] = {
            "n": len(values),
            "mean": statistics.fmean(values) if values else None,
            "median": statistics.median(values) if values else None,
        }
    return out


def paper_results(records: Iterable[dict[str, Any]], trades_limit: int = 50) -> dict[str, Any]:
    records = list(records)
    decisions: dict[tuple[str, int], dict[str, Any]] = {}
    fills: dict[tuple[str, int], dict[str, Any]] = {}
    settles: dict[tuple[str, int], dict[str, Any]] = {}
    markouts: dict[tuple[str, int], dict[str, Any]] = {}
    windows: list[dict[str, Any]] = []
    for r in records:
        key = (r.get("asset"), r.get("open_unix"))
        kind = r.get("type")
        if kind == "markout":
            markouts[key] = r
        elif kind == "decision":
            decisions[key] = r
        elif kind == "fill":
            fills[key] = r
        elif kind == "settle":
            settles[key] = r
        elif kind == "window":
            windows.append(r)

    days: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"windows": 0, "enter": 0, "skip": 0, "blocked": 0, "filled": 0, "no_fill": 0,
                 "settled": 0, "wins": 0, "vwaps": [], "cents": [], "pnl": 0.0, "fees": 0.0}
    )  # fmt: skip
    for w in windows:
        days[_day(w["open_unix"] * 1_000_000_000)]["windows"] += 1
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
            # Settle day: older settle records carry no t_ns; their window's close stands in.
            settled_on = days[_day(s.get("t_ns") or (key[1] + WINDOW_S) * 1_000_000_000)]
            settled_on["settled"] += 1
            settled_on["wins"] += 1 if s["payout"] > 0 else 0
            settled_on["pnl"] += s["pnl"]
            settled_on["cents"].append(100 * (s["payout"] - f["cost"] - f["fee"]) / f["shares"])

    daily = []
    cumulative = 0.0
    for name in sorted(days):
        d = days[name]
        cumulative += d["pnl"]
        daily.append(
            {
                "day": name,
                "windows": d["windows"],
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
                "depth_at_limit": f.get("depth_at_limit"),
                **{
                    f"markout_{h}s": (markouts.get(key) or {}).get(f"markout_{h}s")
                    for h in MARKOUT_S
                },
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
                "fee": f["fee"],
                "t": f["t_ns"],
            }
        )
    settled_rows.sort(key=lambda r: r["t"])
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
        "markouts": {
            "fills": _markout_summary([m for m in markouts.values() if m.get("filled")]),
            "no_fills": _markout_summary([m for m in markouts.values() if not m.get("filled")]),
        },
    }
    evals = sum(w.get("evals", 0) for w in windows)
    invalid = sum((w.get("reasons") or {}).get("invalid_leg", 0) for w in windows)
    return {
        "quality": _quality(settled_rows),
        "drawdown": _drawdown([r["pnl"] for r in settled_rows]),
        "funnel": {
            "windows": len(windows),
            "crossings": len(decisions),
            "skipped_above_cap": sum(1 for d in decisions.values() if d["action"] == "skip"),
            "blocked": sum(
                1 for d in decisions.values() if d["action"] == "enter" and d.get("blocked")
            ),
            "orders": len(fills),
            "filled": sum(1 for f in fills.values() if f["shares"] > 0),
            "settled": len(settled_rows),
        },
        "data_quality": {
            "windows": len(windows),
            "unidentified_windows": sum(1 for w in windows if not w.get("identified", True)),
            "evals": evals,
            "invalid_leg_share": invalid / evals if evals else None,
        },
        "daily": daily[::-1],
        "daily_basis": "entries by decision day; settled, wins and P&L by settle day (UTC)",
        "shadow": shadow_table(records),
        "curve": curve,
        "trades": trades[::-1][:trades_limit],
        "signal": _group(settled_rows),
        "by_asset": by_asset,
        "by_sec": by_sec,
        "execution": execution,
    }
