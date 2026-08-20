from __future__ import annotations

import json
from pathlib import Path

import pytest

from main import parse_args, run
from models import Candle, EngineParams, Timeframe
from research.s5_resolver_bias import (
    S5_EXECUTABLE_TIERS,
    render_s5_markdown,
    run_s5_resolver_bias,
)
from sessions import build_windows

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "xauusd_m15.jsonl"
EXPORTS = [
    FIXTURES / f"session-hedging-XAUUSD-{timeframe}.csv"
    for timeframe in ("M15", "H1", "H4")
]


def _candles() -> list[Candle]:
    return [
        Candle.model_validate_json(line)
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _params() -> EngineParams:
    return EngineParams.model_validate(
        {
            "orb_minutes": 60,
            "entry_delay_minutes": 15,
            "time_exit_mode": "max_age",
            "max_age_hours": 24,
            "one_open_per_session": False,
            "max_concurrent_structures": 0,
            "max_open_risk_pct": 0,
        }
    )


def _report():
    return run_s5_resolver_bias(
        _candles(),
        build_windows(["tokyo", "london", "new_york"], {}),
        _params(),
        [],
        symbol="XAUUSD",
        timeframe=Timeframe.M15,
        source="local",
    )


@pytest.fixture(scope="module")
def report():
    return _report()


def test_every_resolver_tier_is_reported_and_executable_tiers_run(report) -> None:
    assert [(tier["tier"], tier["intrabar_mode"]) for tier in report["tiers"]] == [
        *((index, mode.value) for index, mode in S5_EXECUTABLE_TIERS),
        (4, "tick"),
    ]
    assert all(tier["status"] == "executed" for tier in report["tiers"][:4])
    assert report["tiers"][4]["status"] == "interface_only_unavailable"


def test_every_executable_tier_shares_configuration_and_fingerprint(report) -> None:
    assert len(report["candle_set_sha256"]) == 64
    assert "intrabar_mode" not in report["shared_params"]
    assert report["bar_count"] == len(_candles())
    assert all(tier["structures_total"] is not None for tier in report["tiers"][:4])


def test_partial_or_absent_m1_never_mixes_chronology(report) -> None:
    assert report["m1_coverage"]["status"] == "absent"
    assert report["m1_coverage"]["subpath_used"] is False
    for tier in report["tiers"][2:4]:
        assert tier["m1_subpath_used"] is False
        assert tier["fallback"] == "pessimistic_same_bar_no_subpath"
    assert report["tiers"][1]["gross_pips"] == report["tiers"][2]["gross_pips"]
    assert report["tiers"][1]["gross_pips"] == report["tiers"][3]["gross_pips"]


def test_changed_structures_and_paired_deltas_are_complete(report) -> None:
    baseline = report["tiers"][0]
    assert baseline["changed_structure_count"] == 0
    for tier in report["tiers"][:4]:
        assert tier["changed_structure_count"] == len(tier["changed_structures"])
        assert set(tier["delta_vs_tier_0"]) == {
            "gross_pips",
            "net_pips",
            "gross_r",
            "net_r",
        }
        assert tier["gross_pips"] - baseline["gross_pips"] == pytest.approx(
            tier["delta_vs_tier_0"]["gross_pips"]
        )
        assert tier["net_r"] - baseline["net_r"] == pytest.approx(
            tier["delta_vs_tier_0"]["net_r"]
        )


def test_rerun_and_markdown_are_deterministic(report) -> None:
    again = _report()
    assert json.dumps(again, sort_keys=True) == json.dumps(report, sort_keys=True)
    assert render_s5_markdown(again) == render_s5_markdown(report)
    markdown = render_s5_markdown(report)
    assert "Every changed structure" in markdown
    assert "M15 10.6%, H1 11.2%, H4 5.1%" in markdown


def test_cli_writes_s5_artifacts(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "data" / "candles" / "XAUUSD"
    target.mkdir(parents=True)
    (target / "M15.jsonl").write_bytes(FIXTURE.read_bytes())
    (tmp_path / ".env.s5test").write_text(
        "SYMBOL=XAUUSD\nTIMEFRAME=M15\nTRADING_SESSIONS=new_york\n"
        "DATA_DIR=data\nLOGS_DIR=logs\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    args = parse_args(["--run-s5-resolver-bias"])
    assert args.run_s5_resolver_bias is True

    with pytest.raises(SystemExit) as exit_info:
        run(["--profile", "s5test", "--run-s5-resolver-bias", "--output-dir", "out"])
    assert exit_info.value.code == 0
    written = json.loads((tmp_path / "out" / "s5-resolver-bias.json").read_text())
    assert written["study"] == "s5_resolver_ladder_bias"
    assert len(written["tiers"]) == 5
    assert (tmp_path / "out" / "s5-resolver-bias.md").exists()


@pytest.mark.skipif(
    not all(path.is_file() for path in EXPORTS),
    reason="S5 M15/H1/H4 export calibration fixtures are absent from tests/fixtures",
)
def test_s5_export_same_bar_rates_match_section_0() -> None:
    """Fixture-dependent calibration stays explicit; no replacement data is synthesized."""
    pytest.fail("Implement the export parser when the three named fixtures are supplied")
