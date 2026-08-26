from __future__ import annotations

import json
from pathlib import Path

import pytest

from backtesting_service.main import parse_args, run
from backtesting_service.research.gate_scorecard import (
    BLOCKING_GATES,
    RESEARCH_STEMS,
    build_phase3_gate_scorecard,
    render_phase3_gate_scorecard_markdown,
)

ROOT = Path(__file__).parents[1]
RESEARCH = ROOT / "reports" / "research"


@pytest.fixture(scope="module")
def scorecard() -> dict[str, object]:
    return build_phase3_gate_scorecard(RESEARCH)


def test_scorecard_has_one_row_for_every_section_9_gate(scorecard) -> None:
    assert scorecard["gate_count"] == 10
    assert [row["gate_id"] for row in scorecard["gates"]] == [
        "anchor_drift",
        "tp_rate_margin",
        "scale",
        "hedge_vs_synthetic",
        "cost_headroom",
        "rr",
        "lock",
        "holding_horizon",
        "edge_reality",
        "prop_survivability",
    ]
    for row in scorecard["gates"]:
        assert row["question"]
        assert row["artifact"]
        assert row["field"]
        assert row["measured_value"]
        assert row["interval"]
        assert row["verdict"] in {"pass", "fail", "not-yet-testable"}


def test_all_six_committed_surfaces_are_used_and_share_fingerprint(scorecard) -> None:
    artifacts = " ".join(row["artifact"] for row in scorecard["gates"])
    assert all(f"{stem}.json" in artifacts for stem in RESEARCH_STEMS.values())
    fingerprints = {
        json.loads((RESEARCH / f"{stem}.json").read_text())["candle_set_sha256"]
        for stem in RESEARCH_STEMS.values()
    }
    assert fingerprints == {scorecard["candle_set_sha256"]}


def test_blocking_gate_failure_prevents_redesign(scorecard) -> None:
    blocking = scorecard["blocking_gate_verdicts"]
    assert set(blocking) == BLOCKING_GATES
    assert blocking == {
        "tp_rate_margin": "fail",
        "cost_headroom": "fail",
        "edge_reality": "not-yet-testable",
    }
    assert scorecard["phase3_redesign_authorized"] is False
    assert scorecard["verdict_counts"] == {
        "pass": 1,
        "fail": 3,
        "not-yet-testable": 6,
    }


def test_scorecard_states_m1_and_data_limits(scorecard) -> None:
    assert scorecard["bar_count"] == 2000
    assert scorecard["m1_coverage"]["status"] == "partial"
    assert scorecard["m1_coverage"]["subpath_used"] is False
    assert scorecard["m1_coverage"]["subpath_fallback"] == "pessimistic_same_bar_no_subpath"
    assert scorecard["data_sufficiency"]["walk_forward_selection"] is False
    assert scorecard["data_sufficiency"]["prop_survivability_claim"] is False


def test_scorecard_rerun_and_render_are_deterministic(scorecard) -> None:
    again = build_phase3_gate_scorecard(RESEARCH)
    assert json.dumps(again, sort_keys=True) == json.dumps(scorecard, sort_keys=True)
    assert render_phase3_gate_scorecard_markdown(again) == render_phase3_gate_scorecard_markdown(
        scorecard
    )


def test_cli_exposes_and_writes_the_gate_scorecard(tmp_path: Path, monkeypatch) -> None:
    args = parse_args(["--run-phase3-gate-scorecard"])
    assert args.run_phase3_gate_scorecard is True
    assert args.run_s8_scale_sweep is False
    with pytest.raises(SystemExit):
        parse_args(["--run-phase3-gate-scorecard", "--run-s8-scale-sweep"])

    research = tmp_path / "reports" / "research"
    research.mkdir(parents=True)
    for stem in RESEARCH_STEMS.values():
        (research / f"{stem}.json").write_bytes((RESEARCH / f"{stem}.json").read_bytes())
    (tmp_path / ".env.gatetest").write_text(
        "SYMBOL=XAUUSD\nTIMEFRAME=M15\nTRADING_SESSIONS=new_york\nDATA_DIR=data\nLOGS_DIR=logs\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        run(
            [
                "--profile",
                "gatetest",
                "--run-phase3-gate-scorecard",
                "--output-dir",
                "reports/research",
            ]
        )
    assert exit_info.value.code == 0

    written = json.loads((research / "phase3-gate-scorecard.json").read_text())
    markdown = (research / "phase3-gate-scorecard.md").read_text()
    assert written["gate_count"] == 10
    assert written["phase3_redesign_authorized"] is False
    assert written["candle_set_sha256"] in markdown
