from __future__ import annotations

import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "check_plugin_boundary.py"
SPEC = importlib.util.spec_from_file_location("check_plugin_boundary", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
boundary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(boundary)


def test_the_repository_respects_the_boundary() -> None:
    assert boundary.main() == 0


def test_direct_construction_and_sdk_imports_are_reported(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "services" / "demo" / "src" / "demo.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import MetaTrader5\n"
        "from ta_plugin_ctrader.gateway import CTraderGateway\n"
        "from ta_plugin_ctrader.settings import CTraderSettingsMixin\n"
        "gateway = CTraderGateway(settings)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(boundary, "ROOT", tmp_path)
    monkeypatch.setattr(boundary, "SCANNED", ["services/*/src"])

    found = list(boundary._violations(source))

    assert len(found) == 2
    assert "imports MetaTrader5" in found[0]
    assert "constructs CTraderGateway" in found[1]
