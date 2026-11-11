"""Capital needed for a target income per quarter: target / (R / 4)."""

from __future__ import annotations

import math
from typing import Any

import pandas as pd


def required_capital(target_per_quarter: float, R_annual: float) -> float:
    if not math.isfinite(R_annual) or R_annual <= 0:
        return float("inf")
    return target_per_quarter / (R_annual / 4.0)


def sizing_table(summaries: list[dict[str, Any]], target: float) -> pd.DataFrame:
    rows = []
    for s in summaries:
        rm, rp = s.get("R_median", float("nan")), s.get("R_p25", float("nan"))
        rows.append(
            {
                "variant": s["variant"],
                "R_median": rm,
                "R_p25": rp,
                "capital_at_median": required_capital(target, rm),
                "capital_at_p25": required_capital(target, rp),
                "target_per_quarter_usd": target,
                "evidence_tier": "reconstructed",
            }
        )
    return pd.DataFrame(rows)
