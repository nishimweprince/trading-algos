"""Read-only projection of the committed S7 research artifact.

This is not an interactive backtest and not a broker fact. The committed JSON stores
path-level minima, not calendar-day equity series; ``worst_simulated_path_*`` is the
minimum terminal path result across the 2,000 simulations.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import (
    EntryMode,
    PercentileDistribution,
    ResearchSimulationLabel,
    S7BreachDays,
    S7ModePropPanel,
    S7ResearchArtifact,
)

# parents[3] is the service root: research/ -> backtesting_service/ -> src/ -> root.
# It was parents[2] before the package gained a level.
DEFAULT_S7_PATH = (
    Path(__file__).resolve().parents[3] / "reports" / "research" / "s7-propguard-monte-carlo.json"
)

S7_CAVEATS = [
    "Research simulation only; not interactive-backtest output and not broker facts.",
    "Committed S7 stores path-level minima, not calendar-day series.",
    "Zero breach rates are harness output under 0.1% equity risk per R, not a survivability claim.",
    "Empirical libraries contain only 4 to 15 clusters per mode; M1 is partial; "
    "costs and tails are modeled.",
    "Minimum free margin is a risk-budget proxy (equity percent minus concurrent "
    "structures times MAX_PAIR_RISK_PCT), not broker margin.",
    "Headroom path is the minimum-free-margin distribution across simulated paths.",
]


def _distribution(raw: dict[str, Any]) -> PercentileDistribution:
    return PercentileDistribution.model_validate(raw)


def _breach_map(raw: dict[str, Any]) -> dict[str, S7BreachDays]:
    return {key: S7BreachDays.model_validate(value) for key, value in raw.items()}


def project_s7_research_artifact(payload: dict[str, Any]) -> S7ResearchArtifact:
    """Project the committed S7 JSON into the typed research-only prop panel."""
    if payload.get("study") != "s7_propguard_monte_carlo":
        raise ValueError("S7 research artifact study field mismatch")
    modes: list[S7ModePropPanel] = []
    for row in payload["modes"]:
        simulation = row["simulation"]
        net_pips = simulation["path_net_pips_distribution"]
        gross_pips = simulation["path_gross_pips_distribution"]
        net_r = simulation["path_net_r_distribution"]
        gross_r = simulation["path_gross_r_distribution"]
        free_margin = _distribution(simulation["minimum_free_margin_pct_distribution"])
        modes.append(
            S7ModePropPanel(
                entry_mode=EntryMode(row["entry_mode"]),
                complete_structure_count=row["complete_structure_count"],
                cluster_count=row["cluster_count"],
                worst_simulated_path_gross_pips=gross_pips["min"],
                worst_simulated_path_net_pips=net_pips["min"],
                worst_simulated_path_gross_r=gross_r["min"],
                worst_simulated_path_net_r=net_r["min"],
                daily_breach_days=_breach_map(simulation["daily_limit_breaches"]),
                total_breach_days=_breach_map(simulation["total_limit_breaches"]),
                minimum_free_margin_pct_distribution=free_margin,
                headroom_path=free_margin,
            )
        )
    return S7ResearchArtifact(
        source=ResearchSimulationLabel(caveats=list(S7_CAVEATS)),
        study="s7_propguard_monte_carlo",
        seed=int(payload["seed"]),
        simulation_count_per_mode=int(payload["simulation_count_per_mode"]),
        horizon_days=int(payload["horizon_days"]),
        candle_set_sha256=str(payload["candle_set_sha256"]),
        bar_count=int(payload["bar_count"]),
        modes=modes,
    )


def load_s7_research_artifact(path: Path | None = None) -> S7ResearchArtifact:
    artifact_path = path or DEFAULT_S7_PATH
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    return project_s7_research_artifact(payload)
