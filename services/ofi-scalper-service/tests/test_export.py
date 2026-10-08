"""ofi-export: one JSON of performance, trades, signal counts and state, minus recordings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ofi_scalper_service.export import build_export, run
from tests.conftest import API_KEY, make_settings

DAY_A = "2026-10-07"
DAY_B = "2026-10-08"


def _trade(net: float | None = None, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "cycle_id": "c1",
        "mode": "testnet",
        "symbol": "BTCUSDT",
        "side": "buy",
        "p": 0.7,
        "entry_qty_ordered": "0.001",
        "filled": net is not None,
        "fees_usd": 0.1,
        "funding_usd": 0.0,
        "exit_reason": "take_profit",
        "rtt_ms": {},
        "adverse_bp": {},
    }
    if net is not None:
        record.update(net_usd=net, net_bp=8.0)
    record.update(overrides)
    return record


def _signal(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "symbol": "BTCUSDT",
        "side": "buy",
        "p": 0.7,
        "threshold": 0.5,
        "action": "enter",
        "at": f"{DAY_B}T10:25:21.100000+00:00",
        "mode": "testnet",
    }
    record.update(overrides)
    return record


def _write_jsonl(path: Path, records: list[dict[str, Any]], extra: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r) + "\n" for r in records) + extra
    path.write_text(text)


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    state = tmp_path / "state" / "dev"
    _write_jsonl(state / "trades" / f"{DAY_A}.jsonl", [_trade(1.5), _trade(0.5)])
    _write_jsonl(state / "trades" / f"{DAY_B}.jsonl", [_trade()])
    _write_jsonl(state / "signals" / f"{DAY_A}.jsonl", [_signal()])
    _write_jsonl(
        state / "signals" / f"{DAY_B}.jsonl", [_signal(), _signal(side="sell")], extra='{"torn"\n'
    )
    _write_jsonl(state / "shadow" / "trades" / f"{DAY_B}.jsonl", [_trade(2.0)])
    _write_jsonl(state / "shadow" / "signals" / f"{DAY_B}.jsonl", [_signal()])
    (state / "daily").mkdir(parents=True, exist_ok=True)
    (state / "daily" / f"{DAY_A}.json").write_text(json.dumps({"date": DAY_A, "ok": True}))
    (state / "bridge.json").write_text(json.dumps({"schema": 1, "policies": {}}))
    (state / "drills.json").write_text(json.dumps({"kill_switch": {"result": "passed"}}))
    return state


@pytest.fixture
def model_dir(tmp_path: Path) -> Path:
    models = tmp_path / "models"
    version = models / "rules-v9"
    version.mkdir(parents=True)
    (version / "manifest.json").write_text(json.dumps({"version": "rules-v9"}))
    (version / "rules.json").write_text(json.dumps({"z_entry": 4}))
    (version / "model.cbm").write_bytes(b"\x00binary")  # binaries never land in JSON
    return models


def test_full_document(tmp_path: Path, state_dir: Path, model_dir: Path) -> None:
    latency = tmp_path / "latency.json"
    latency.write_text(json.dumps({"host": "test"}))
    settings = make_settings(
        tmp_path,
        OFI_STATE_DIR=str(state_dir),
        OFI_MODEL_DIR=str(model_dir),
        OFI_MODEL_VERSION="rules-v9",
        OFI_RECORD_DIR=str(tmp_path / "raw-missing"),  # must never be touched
    )
    document = build_export(settings, "dev", latency_path=latency)

    assert document["meta"]["profile"] == "dev"
    assert document["meta"]["host_tag"] == "test"
    assert document["meta"]["model_version"] == "rules-v9"
    assert document["meta"]["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert document["meta"]["days"] == [DAY_A, DAY_B]
    assert document["meta"]["unparsed_lines"] == 1  # the torn line, skipped not fatal

    overall = document["performance"]["overall"]
    assert overall["signals"] == 3 and overall["trades"] == 2
    assert overall["net_usd"] == 2.0
    assert set(document["performance"]["by_day"]) == {DAY_A, DAY_B}
    assert document["performance"]["by_symbol"]["BTCUSDT"]["trades"] == 2
    assert len(document["trades"]) == 3
    # Signals are events: counted, not listed (unless asked for).
    assert "signals" not in document and document["meta"]["signals_included"] is False
    counts = document["signal_counts"]
    assert counts["total"] == 3 and counts["by_action"] == {"enter": 3}
    assert counts["by_side"] == {"buy": 2, "sell": 1}
    assert counts["by_day"] == {DAY_A: {"enter": 1}, DAY_B: {"enter": 2}}
    assert counts["by_symbol_action"] == {"BTCUSDT": {"enter": 3}}

    assert document["shadow"]["performance"]["overall"]["net_usd"] == 2.0
    assert document["shadow"]["signal_counts"]["total"] == 1 and "signals" not in document["shadow"]
    assert document["daily"][DAY_A]["ok"] is True
    assert document["bridge"]["schema"] == 1
    assert document["drills"]["kill_switch"]["result"] == "passed"
    assert document["latency"]["host"] == "test"
    assert document["model"]["found"] is True
    assert set(document["model"]["files"]) == {"manifest.json", "rules.json"}

    assert document["meta"]["recordings_included"] is False
    assert {"runs", "candidates", "selections", "models", "selection_files"} <= set(
        document["research"]
    )

    dumped = json.dumps(document)
    assert API_KEY not in dumped  # no secrets travel with the export
    assert "raw-missing" not in dumped
    assert str(tmp_path) not in dumped  # no filesystem paths either


def test_raw_signals_on_request_and_full_selection_grids(
    tmp_path: Path, state_dir: Path, model_dir: Path
) -> None:
    research = tmp_path / "research"
    run_dir = research / "candidates" / "shallow-9"
    run_dir.mkdir(parents=True)
    grid = [
        {"name": f"h300-t0.{i}", "trades": i, "candidate": str(run_dir / "h300")} for i in range(12)
    ]
    (run_dir / "selection.json").write_text(
        json.dumps(
            {"chosen": None, "grid": grid, "diagnostics": [{"name": "h300-t0.45", "gap_max": 0.96}]}
        )
    )
    settings = make_settings(
        tmp_path,
        OFI_STATE_DIR=str(state_dir),
        OFI_RESEARCH_DIR=str(research),
        OFI_MODEL_DIR=str(model_dir),
    )
    document = build_export(settings, "dev", include_signals=True, latency_path=False)
    assert len(document["signals"]) == 3 and len(document["shadow"]["signals"]) == 1
    full = document["research"]["selection_files"]["shallow-9"]
    assert (
        len(full["grid"]) == 12 and full["diagnostics"][0]["gap_max"] == 0.96
    )  # the page keeps 10
    assert full["grid"][0]["candidate"] == "h300"  # the absolute path is reduced to its name
    assert str(tmp_path) not in json.dumps(document)


def test_days_and_shadow_filters(state_dir: Path, model_dir: Path, tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        OFI_STATE_DIR=str(state_dir),
        OFI_MODEL_DIR=str(model_dir),
        OFI_MODEL_VERSION="rules-v9",
    )
    one = build_export(settings, "dev", days=1, latency_path=False)
    assert one["meta"]["days"] == [DAY_B]
    assert one["performance"]["overall"]["signals"] == 2
    assert one["daily"] == {}

    plain = build_export(settings, "dev", include_shadow=False, latency_path=False)
    assert plain["shadow"] is None
    assert plain["meta"]["unparsed_lines"] == 1


def test_empty_state_exports_cleanly(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, OFI_STATE_DIR=str(tmp_path / "empty"))
    document = build_export(settings, "dev", latency_path=False)
    assert document["performance"]["overall"]["trades"] == 0
    assert document["signal_counts"]["total"] == 0 and document["trades"] == []
    assert document["shadow"] is None and document["model"] is None
    assert document["bridge"] is None and document["meta"]["days"] == []


def test_run_writes_file(
    tmp_path: Path, state_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    import ofi_scalper_service.export as export_mod

    settings = make_settings(tmp_path, OFI_STATE_DIR=str(state_dir))
    monkeypatch.setattr(export_mod, "load_settings", lambda profile: settings)
    out = tmp_path / "nested" / "export.json"
    with pytest.raises(SystemExit) as exit:
        run(["--profile", "dev", "--out", str(out), "--days", "1"])
    assert exit.value.code == 0
    written = json.loads(out.read_text())
    assert written["meta"]["profile"] == "dev" and written["meta"]["days"] == [DAY_B]
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["wrote"] == str(out) and receipt["signals"] == 2


def test_run_rejects_zero_days(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as exit:
        run(["--profile", "dev", "--days", "0"])
    assert exit.value.code == 2
