from __future__ import annotations

from typing import Literal

RecommendationVerdict = Literal[
    "buy",
    "sell",
    "lean_long",
    "lean_short",
    "wait",
    "insufficient_data",
]


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
    resolved_count: int | None = None,
    confidence_low_r: float | None = None,
    independent_periods: int | None = None,
    min_periods: int | None = None,
    policy_version: str = "empirical-evidence-v2",
) -> dict:
    caveats: list[str] = []
    evidence_count = resolved_count if resolved_count is not None else decided

    if (
        level_used == "no_signal"
        or evidence_count < min_samples
        or (
            min_periods is not None
            and independent_periods is not None
            and independent_periods < min_periods
        )
    ):
        return {
            "verdict": "insufficient_data",
            "headline": "Insufficient data",
            "rationale": "Not enough resolved history to trust this yet.",
            "caveats": caveats,
            "policy_version": policy_version,
        }

    if overlap_ratio is not None and overlap_ratio > 0.4:
        caveats.append("Sample may be overstated — overlapping holding windows.")
    if setup_delta is not None and abs(setup_delta) <= 0.01:
        caveats.append("Your setup isn't beating the context prior.")
    if effective_n is not None and decided > 0 and effective_n < decided / 3:
        caveats.append(f"Only ~{round(effective_n)} independent bars behind this.")

    if expectancy_r is not None and expectancy_r <= 0:
        return {
            "verdict": "wait",
            "headline": "Wait",
            "rationale": "Estimated expectancy is not positive after assumed costs.",
            "caveats": caveats,
            "policy_version": policy_version,
        }

    # Automatic base rates provide a block-bootstrap lower bound in R. Manual
    # comparisons retain their Wilson probability interval until their thinner
    # occurrence data can support the same resampling contract.
    favorable = expectancy_r is not None and expectancy_r > 0
    if confidence_low_r is not None:
        favorable = favorable and confidence_low_r > 0
    else:
        favorable = favorable and wilson_low is not None and wilson_low >= break_even_win_rate

    if favorable:
        direction = "buying" if side == 1 else "selling"
        return {
            "verdict": "buy" if side == 1 else "sell",
            "headline": "Buy" if side == 1 else "Sell",
            "rationale": (
                "Historical net expectancy remains positive across the 95% confidence range."
                if confidence_low_r is not None
                else f"Past bars in this context slightly favor {direction}."
            ),
            "caveats": caveats,
            "policy_version": policy_version,
        }

    if expectancy_r is not None and expectancy_r > 0:
        return {
            "verdict": "lean_long" if side == 1 else "lean_short",
            "headline": "Lean long" if side == 1 else "Lean short",
            "rationale": "Historical expectancy is positive, but uncertainty still crosses zero.",
            "caveats": caveats,
            "policy_version": policy_version,
        }

    return {
        "verdict": "wait",
        "headline": "Wait",
        "rationale": "Mixed — the edge is too small to act on from history alone.",
        "caveats": caveats,
        "policy_version": policy_version,
    }


def verdict_rank(verdict: RecommendationVerdict) -> int:
    if verdict in ("buy", "sell"):
        return 3
    if verdict in ("lean_long", "lean_short"):
        return 2
    if verdict == "wait":
        return 1
    return 0
