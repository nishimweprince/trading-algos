from __future__ import annotations

import json

from typer.testing import CliRunner

from solcarry.cli import app
from solcarry.config import Config, canonical_json
from solcarry.lock import check_lock, preregistration_hash, write_lock


def test_hash_stable_across_key_order():
    assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})
    c1 = Config()
    c2 = Config.model_validate(
        {"thresholds": dict(reversed(list(c1.thresholds.model_dump().items())))}
    )
    assert preregistration_hash(c1) == preregistration_hash(c2)


def test_lock_roundtrip_and_mismatch(tmp_path):
    cfg = Config()
    lf = tmp_path / "gate1.lock"
    assert check_lock(cfg, lf).state == "MISSING"
    write_lock(cfg, lf)
    assert check_lock(cfg, lf).ok
    cfg.thresholds.excess_median_min = 0.0
    st = check_lock(cfg, lf)
    assert not st.ok and st.state == "MISMATCH"


def test_non_prereg_change_keeps_lock(tmp_path):
    cfg = Config()
    lf = tmp_path / "gate1.lock"
    write_lock(cfg, lf)
    cfg.costs.coinm_fee_bps = 99
    assert check_lock(cfg, lf).ok


def _project(tmp_path):
    (tmp_path / "config").mkdir()
    cfgp = tmp_path / "config" / "gate1.yaml"
    cfgp.write_text("variants: [D1-BASIS]\n")
    return cfgp


def test_gate_exit_3_without_lock(tmp_path):
    cfgp = _project(tmp_path)
    r = CliRunner().invoke(app, ["--config", str(cfgp), "gate"])
    assert r.exit_code != 0  # no analysis run yet -> error, but lock is checked first below


def test_gate_lock_and_unlocked_stamp(tmp_path):
    cfgp = _project(tmp_path)
    run = tmp_path / "data" / "results" / "20260101T000000Z-abc"
    run.mkdir(parents=True)
    summ = {
        "variants": ["D1-BASIS"],
        "gate_alpha": 0.99,
        "summaries": {
            "D1-BASIS": {
                "variant": "D1-BASIS",
                "kind": "D",
                "excess": {"median": 0.1, "p5": 0.05},
                "share_negative": 0.0,
                "sample_days": 500,
                "uses_lst": False,
                "flags": [],
            }
        },
    }
    (run / "summaries.json").write_text(json.dumps(summ))
    runner = CliRunner()
    r = runner.invoke(app, ["--config", str(cfgp), "gate"])
    assert r.exit_code == 3
    r = runner.invoke(app, ["--config", str(cfgp), "gate", "--unlocked"])
    assert r.exit_code == 0, r.output
    g = json.loads((run / "gate.json").read_text())
    assert g["unlocked"] is True and g["result"] == "CONTINUE"
    r = runner.invoke(app, ["--config", str(cfgp), "lock"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["--config", str(cfgp), "gate"])
    assert r.exit_code == 0
    assert json.loads((run / "gate.json").read_text())["unlocked"] is False
