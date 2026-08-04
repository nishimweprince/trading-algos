from __future__ import annotations

from typing import Literal

RecommendationVerdict = Literal["buy", "sell", "wait", "insufficient_data"]


def break_even_from_geometry(stop: float, target: float) -> float:
    if stop <= 0 or target <= 0:
        return 0.4
    return stop / (stop + target)


def derive_recommendation(
    *,
    side: int,
    expectancy_r: float | None,
    wilson_low: float | None,
    decided: int,
    min_samples: int,
    level_used: str | None,
    effective_n: float | None = None,
    overlap_ratio: float | None = None,
    break_even_win_rate: float,
    setup_delta: float | None = None,
) -> dict:
    caveats: list[str] = []

    if level_used == "no_signal" or decided < min_samples:
        return {
            "verdict": "insufficient_data",
            "headline": "Insufficient data",
            "rationale": "Not enough resolved history to trust this yet.",
            "caveats": caveats,
        }

    if overlap_ratio is not None and overlap_ratio > 0.4:
        caveats.append("Sample may be overstated — overlapping holding windows.")
    if setup_delta is not None and abs(setup_delta) <= 0.01:
        caveats.append("Your setup isn't beating the context prior.")
    if effective_n is not None and decided > 0 and effective_n < decided / 3:
        caveats.append(f"Only ~{round(effective_n)} independent bars behind this.")

    favorable = (
        expectancy_r is not None
        and expectancy_r > 0
        and wilson_low is not None
        and wilson_low >= break_even_win_rate
    )

    if expectancy_r is not None and expectancy_r <= 0:
        return {
            "verdict": "wait",
            "headline": "Wait",
            "rationale": "Past bars in this context don't support taking the trade.",
            "caveats": caveats,
        }

    if wilson_low is not None and wilson_low < break_even_win_rate:
        return {
            "verdict": "wait",
            "headline": "Wait",
            "rationale": "History doesn't clear the break-even bar after costs and uncertainty.",
            "caveats": caveats,
        }

    if favorable:
        direction = "buying" if side == 1 else "selling"
        return {
            "verdict": "buy" if side == 1 else "sell",
            "headline": "Buy" if side == 1 else "Sell",
            "rationale": f"Past bars in this context slightly favor {direction}.",
            "caveats": caveats,
        }

    return {
        "verdict": "wait",
        "headline": "Wait",
        "rationale": "Mixed — the edge is too small to act on from history alone.",
        "caveats": caveats,
    }


def verdict_rank(verdict: RecommendationVerdict) -> int:
    if verdict in ("buy", "sell"):
        return 2
    if verdict == "wait":
        return 1
    return 0

