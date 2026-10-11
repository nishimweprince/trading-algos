"""ofi-go-live: one checked switch for shadow trading, reversible."""

from __future__ import annotations

from pathlib import Path

import pytest

from ofi_scalper_service import go_live
from ofi_scalper_service.go_live import apply_env, plan, read_env

ENV = """\
API_KEY=test-api-key-at-least-16
BINANCE_FUTURES_SYMBOLS=BTCUSDT,ETHUSDT
OFI_MAX_POSITION_NOTIONAL_USD=500
OFI_MAX_TOTAL_NOTIONAL_USD=800
OFI_DAILY_LOSS_LIMIT_USD=50
OFI_MANUAL_APPROVAL_NOTIONAL_USD=400
OFI_RECORD_ENABLED=false
# a demo-era host: testnet plus the mirror
OFI_EXECUTION_MODE=testnet
OFI_SHADOW_MIRROR=true
EXECUTION_API_KEY=gateway-key-at-least-16
EXECUTION_URL=http://127.0.0.1:9
"""


def test_apply_env_sets_comments_out_and_appends() -> None:
    text = "A=1\n# B=old\nB=2\nB=3\nC=keep\n"
    out = apply_env(text, {"A": "9", "B": None, "D": "new"})
    lines = out.splitlines()
    assert "A=9" in lines and "C=keep" in lines and "# B=old" in lines
    assert "# B=2  # ofi-go-live off" in lines and "B=3" not in lines  # duplicate dropped
    assert lines[-1] == "D=new" and lines[-2].startswith("# ofi-go-live ")
    assert read_env_text(out) == {"A": "9", "C": "keep", "D": "new"}


def read_env_text(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


def test_plan_for_each_switch() -> None:
    on = plan("on", version="rules-v2", ungated=True)
    assert on == {
        "OFI_EXECUTION_MODE": "shadow",
        "OFI_MODEL_VERSION": "rules-v2",
        "OFI_ALLOW_UNGATED": "true",
        "OFI_SHADOW_MIRROR": None,
    }
    assert plan("on", version="v7", ungated=False)["OFI_ALLOW_UNGATED"] == "false"
    off = plan("off", version="", ungated=False)
    assert off["OFI_EXECUTION_MODE"] == "off" and off["OFI_MODEL_VERSION"] is None


@pytest.fixture
def service_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".env.t").write_text(ENV)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_on_moves_a_testnet_host_to_shadow_then_off_reverts(service_dir: Path, capsys) -> None:
    assert go_live.main(["on", "--profile", "t"]) == 0
    env = read_env(service_dir / ".env.t")
    assert env["OFI_EXECUTION_MODE"] == "shadow" and "OFI_SHADOW_MIRROR" not in env
    assert env["OFI_MODEL_VERSION"] == "rules-v5" and env["OFI_ALLOW_UNGATED"] == "true"
    assert (service_dir / "models" / "rules-v5" / "manifest.json").is_file()
    assert list(service_dir.glob(".env.t.bak-*"))
    out = capsys.readouterr().out
    assert "mode shadow" in out and "rules-v5 (rules, ungated)" in out

    assert go_live.main(["off", "--profile", "t"]) == 0
    env = read_env(service_dir / ".env.t")
    assert "OFI_MODEL_VERSION" not in env and env["OFI_EXECUTION_MODE"] == "off"


def test_a_missing_model_changes_nothing(service_dir: Path) -> None:
    before = (service_dir / ".env.t").read_text()
    with pytest.raises(SystemExit, match="no model"):
        go_live.main(["on", "--profile", "t", "--version", "v404"])
    assert (service_dir / ".env.t").read_text() == before


def test_a_change_that_does_not_load_is_put_back(service_dir: Path, capsys) -> None:
    from tests.model_fixture import make_model_dir

    # Gates passed, but model.cbm is not a CatBoost file: the service could not load it.
    make_model_dir(service_dir / "models", "v8", passed=True)
    before = (service_dir / ".env.t").read_text()
    assert go_live.main(["on", "--profile", "t", "--version", "v8"]) == 1
    assert (service_dir / ".env.t").read_text() == before
    assert "refused" in capsys.readouterr().err
