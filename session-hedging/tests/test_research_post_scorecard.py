from __future__ import annotations

import json
from pathlib import Path

import pytest

from main import parse_args, run
from research.gate_scorecard import RESEARCH_STEMS
from research.post_s6_s7_scorecard import (
    POST_SCORECARD_STEM,
    build_post_s6_s7_scorecard,
    render_post_s6_s7_scorecard_markdown,
)

ROOT = Path(__file__).parents[1]
RESEARCH = ROOT / "reports" / "research"


@pytest.fixture(scope="module")
def post_scorecard() -> dict[str, object]:
    return build_post_s6_s7_scorecard(RESEARCH)


def test_post_scorecard_does_not_rewrite_the_original_blocking_artifact(post_scorecard) -> None:
    original = json.loads((RESEARCH / "phase3-gate-scorecard.json").read_text())
    assert post_scorecard["original_scorecard"] == "phase3-gate-scorecard.json"
    assert post_scorecard["overwrites_original_scorecard"] is False
    assert post_scorecard["study"] == "phase3_post_s6_s7_scorecard"
    assert original["study"] == "phase3_gate_scorecard"
    assert original["blocking_gate_verdicts"]["edge_reality"] == "not-yet-testable"
    assert post_scorecard["blocking_gate_verdicts"]["edge_reality"] == "fail"


def test_post_scorecard_fails_edge_and_retains_s7_caveats(post_scorecard) -> None:
    blocking = post_scorecard["blocking_gate_verdicts"]
    assert blocking == {
        "tp_rate_margin": "fail",
        "cost_headroom": "fail",
        "edge_reality": "fail",
    }
    assert post_scorecard["phase3_redesign_authorized"] is False
    assert post_scorecard["verdict_counts"] == {
        "pass": 1,
        "fail": 4,
        "not-yet-testable": 5,
    }
    prop = next(row for row in post_scorecard["gates"] if row["gate_id"] == "prop_survivability")
    assert prop["verdict"] == "not-yet-testable"
    assert "not a survivability claim" in prop["rationale"]
    caveats = post_scorecard["s6_s7_caveats"]
    assert caveats["edge_reality"] == "failed_descriptive_evidence"
    assert caveats["prop_survivability"] == "descriptive_inconclusive"
    assert caveats["s6_unseen_net_r"] < 0
    assert caveats["s6_pbo"] == 0.4


def test_post_scorecard_cli_leaves_the_original_file_in_place(tmp_path: Path, monkeypatch) -> None:
    args = parse_args(["--run-phase3-post-s6-s7-scorecard"])
    assert args.run_phase3_post_s6_s7_scorecard is True
    with pytest.raises(SystemExit):
        parse_args(["--run-phase3-gate-scorecard", "--run-phase3-post-s6-s7-scorecard"])

    research = tmp_path / "reports" / "research"
    research.mkdir(parents=True)
    for stem in (*RESEARCH_STEMS.values(), "s6-nested-walk-forward", "s7-propguard-monte-carlo"):
        (research / f"{stem}.json").write_bytes((RESEARCH / f"{stem}.json").read_bytes())
    original = json.loads((RESEARCH / "phase3-gate-scorecard.json").read_text())
    (research / "phase3-gate-scorecard.json").write_text(
        json.dumps(original, indent=2) + "\n", encoding="utf-8"
    )
    (tmp_path / ".env.postscore").write_text(
        "SYMBOL=XAUUSD\nTIMEFRAME=M15\nTRADING_SESSIONS=new_york\nDATA_DIR=data\nLOGS_DIR=logs\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        run(
            [
                "--profile",
                "postscore",
                "--run-phase3-post-s6-s7-scorecard",
                "--output-dir",
                "reports/research",
            ]
        )
    assert exit_info.value.code == 0

    written = json.loads((research / f"{POST_SCORECARD_STEM}.json").read_text())
    markdown = (research / f"{POST_SCORECARD_STEM}.md").read_text()
    leftover = json.loads((research / "phase3-gate-scorecard.json").read_text())
    assert leftover["blocking_gate_verdicts"]["edge_reality"] == "not-yet-testable"
    assert written["blocking_gate_verdicts"]["edge_reality"] == "fail"
    assert "does **not** overwrite" in markdown
    assert render_post_s6_s7_scorecard_markdown(written) == markdown
