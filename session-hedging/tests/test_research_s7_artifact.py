from __future__ import annotations

from pathlib import Path

from models import S7ResearchArtifact
from research.s7_artifact import load_s7_research_artifact, project_s7_research_artifact

ROOT = Path(__file__).parents[1]
S7_PATH = ROOT / "reports" / "research" / "s7-propguard-monte-carlo.json"


def test_s7_artifact_is_typed_research_simulation_not_backtest_or_broker() -> None:
    artifact = load_s7_research_artifact(S7_PATH)
    assert isinstance(artifact, S7ResearchArtifact)
    assert artifact.source.kind == "research_simulation"
    assert artifact.source.not_interactive_backtest is True
    assert artifact.source.not_broker_fact is True
    assert artifact.seed == 20260820
    assert artifact.simulation_count_per_mode == 2000
    assert len(artifact.modes) == 4
    for mode in artifact.modes:
        assert mode.worst_simulated_path_net_pips <= mode.worst_simulated_path_gross_pips or (
            mode.worst_simulated_path_net_pips < 0
        )
        assert "3" in mode.daily_breach_days
        assert "5" in mode.daily_breach_days
        assert "6" in mode.total_breach_days
        assert "10" in mode.total_breach_days
        assert mode.headroom_path == mode.minimum_free_margin_pct_distribution
        assert mode.minimum_free_margin_pct_distribution.p01 <= (
            mode.minimum_free_margin_pct_distribution.p50
        )


def test_s7_projection_uses_path_minima_as_worst_simulated_path() -> None:
    import json

    raw = json.loads(S7_PATH.read_text(encoding="utf-8"))
    projected = project_s7_research_artifact(raw)
    hedge = next(mode for mode in projected.modes if mode.entry_mode.value == "hedge_pair")
    simulation = next(row for row in raw["modes"] if row["entry_mode"] == "hedge_pair")[
        "simulation"
    ]
    assert hedge.worst_simulated_path_net_r == simulation["path_net_r_distribution"]["min"]
    assert hedge.worst_simulated_path_net_pips == simulation["path_net_pips_distribution"]["min"]
    assert any("not broker" in caveat.lower() for caveat in projected.source.caveats)
    assert any("not a survivability claim" in caveat for caveat in projected.source.caveats)
