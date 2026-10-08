"""The rule scorer and rule models: no training, ungated, the model's slot in the hot path."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ofi_scalper_service.alerts import Alerts
from ofi_scalper_service.execution_bridge import DEFAULT_FILTERS, ExecutionBridge
from ofi_scalper_service.model import ModelError, load_model, write_rules_model
from ofi_scalper_service.risk import RiskLimits, RiskState
from ofi_scalper_service.rules import DEFAULT_POLICY, RuleParams, RuleScorer
from ofi_scalper_service.rules import main as rules_main
from tests.conftest import RecordingNotifier, make_settings

T0 = 1_790_000_000_000_000_000
GRID = 100_000_000
S = 1_000_000_000
GATE_ON = {"on": True, "threshold_mult": 1.0, "max_inventory_mult": 1.0, "reasons": []}
PARAMS = RuleParams(halflife_s=60, warmup_s=10, z_entry=3, hold_ms=500)


def row(t: int, ofi: float, **overrides: Any) -> dict[str, Any]:
    return {
        "symbol": "BTCUSDT",
        "t_ns": t,
        "trigger": "grid",
        "mid": 60000.05,
        "best_bid": 60000.0,
        "best_ask": 60000.1,
        "ofi_int_5000ms": ofi,
        "imbalance": 0.5,
        "spread_ticks": 1,
        "tfi_5000ms": 0.0,
        **overrides,
    }


def warm(scorer: RuleScorer, seconds: float = 20) -> int:
    """Alternating +-1 for ``seconds`` of grid: mean ~0, std ~1. Returns the next time."""
    t = T0
    for i in range(int(seconds * S / GRID)):
        assert scorer.score(row(t, 1.0 if i % 2 else -1.0)) is not None
        t += GRID
    return t


def test_no_score_until_warm_and_none_when_a_feature_is_missing() -> None:
    scorer = RuleScorer(PARAMS)
    first = scorer.score(row(T0, 50.0, imbalance=0.9, tfi_5000ms=1.0))
    assert first is not None and first.none == 1.0  # not warm: never fires
    assert scorer.score({**row(T0 + GRID, 1.0), "imbalance": None}) is None


def test_fires_up_with_confirmation_and_holds_then_releases() -> None:
    scorer = RuleScorer(PARAMS)
    t = warm(scorer)
    fired = scorer.score(row(t, 10.0, imbalance=0.7, tfi_5000ms=2.0))
    assert fired is not None and (fired.up, fired.down) == (1.0, 0.0)
    held = scorer.score(row(t + GRID, 0.0))  # signal gone, still inside hold_ms
    assert held is not None and held.up == 1.0
    released = scorer.score(row(t + 6 * GRID, 0.0))
    assert released is not None and released.none == 1.0
    assert scorer.fired == {"up": 1}


@pytest.mark.parametrize(
    "overrides",
    [
        {"imbalance": 0.5},  # the touch does not agree
        {"tfi_5000ms": -1.0},  # trade flow does not agree
        {"spread_ticks": 5},  # spread too wide
    ],
)
def test_confirmations_block_a_firing(overrides: dict[str, Any]) -> None:
    scorer = RuleScorer(PARAMS)
    t = warm(scorer)
    values = {"imbalance": 0.7, "tfi_5000ms": 2.0, **overrides}
    scores = scorer.score(row(t, 10.0, **values))
    assert scores is not None and scores.none == 1.0


def test_down_replaces_a_held_up_at_once() -> None:
    scorer = RuleScorer(PARAMS)
    t = warm(scorer)
    scorer.score(row(t, 10.0, imbalance=0.7, tfi_5000ms=2.0))
    down = scorer.score(row(t + GRID, -10.0, imbalance=0.3, tfi_5000ms=-2.0))
    assert down is not None and (down.up, down.down) == (0.0, 1.0)


def test_burst_samples_do_not_move_the_statistics() -> None:
    scorer = RuleScorer(PARAMS)
    t = warm(scorer)
    before = (scorer.states["BTCUSDT"].mean, scorer.states["BTCUSDT"].var)
    scorer.score(row(t, 500.0, trigger="burst"))
    assert (scorer.states["BTCUSDT"].mean, scorer.states["BTCUSDT"].var) == before


def test_params_reject_nonsense() -> None:
    with pytest.raises(ValueError, match="imbalance_min"):
        RuleParams(imbalance_min=0.2)
    with pytest.raises(ValueError, match="unknown rules fields"):
        RuleParams.from_dict({"z": 3})


# --- rule models -------------------------------------------------------------------------


def write(tmp_path: Path, version: str = "rules-v1", **rules: Any) -> Path:
    return write_rules_model(
        tmp_path, version, rules={**PARAMS.as_dict(), **rules}, policy=dict(DEFAULT_POLICY)
    )


def test_a_rule_model_is_ungated_and_refused_where_a_pass_is_required(tmp_path: Path) -> None:
    write(tmp_path)
    with pytest.raises(ModelError, match="ungated rules model.*OFI_ALLOW_UNGATED"):
        load_model(tmp_path, "rules-v1")
    model = load_model(tmp_path, "rules-v1", require_pass=False)
    assert model.kind == "rules" and model.ungated
    assert isinstance(model.scorer, RuleScorer)
    summary = model.summary()
    assert summary["kind"] == "rules" and summary["ungated"] is True
    assert summary["policy"]["barrier_bp"] == DEFAULT_POLICY["barrier_bp"]


def test_a_tampered_rule_model_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path)
    rules = json.loads((path / "rules.json").read_text())
    (path / "rules.json").write_text(json.dumps({**rules, "z_entry": 0.5}))
    with pytest.raises(ModelError, match="sha256"):
        load_model(tmp_path, "rules-v1", require_pass=False)


def test_manifest_features_must_be_the_rules_inputs(tmp_path: Path) -> None:
    path = write(tmp_path)
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["features"] = ["imbalance"]
    (path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ModelError, match="rule's inputs"):
        load_model(tmp_path, "rules-v1", require_pass=False)


def test_rules_cli_writes_a_loadable_model(tmp_path: Path, capsys) -> None:
    args = ["--model-dir", str(tmp_path), "--z-entry", "4", "--barrier-bp", "15"]
    assert rules_main(args) == 0
    model = load_model(tmp_path, "rules-v1", require_pass=False)
    assert model.policy.barrier_bp == 15 and model.scorer.params.z_entry == 4  # type: ignore[union-attr]
    assert rules_main(args) == 1  # never overwrites
    assert "exists" in capsys.readouterr().err


def test_a_rule_model_trades_through_the_shadow_bridge(tmp_path: Path) -> None:
    write(tmp_path / "models")
    model = load_model(tmp_path / "models", "rules-v1", require_pass=False)
    settings = make_settings(
        tmp_path,
        OFI_EXECUTION_MODE="shadow",
        OFI_MODEL_VERSION="rules-v1",
        OFI_ALLOW_UNGATED="true",
        OFI_ORDER_NOTIONAL_USD=180,
    )
    bridge = ExecutionBridge(
        settings,
        risk=RiskState(RiskLimits.from_settings(settings)),
        alerts=Alerts(RecordingNotifier()),
        model=model,
        filters=dict(DEFAULT_FILTERS),
        state_dir=tmp_path / "state",
        fees=lambda _s: (2.0, 5.0),
    )
    t = T0
    for i in range(200):
        bridge.on_sample(row(t, 1.0 if i % 2 else -1.0), GATE_ON)
        t += GRID
    bridge.on_sample(row(t, 10.0, imbalance=0.7, tfi_5000ms=2.0), GATE_ON)
    (signal,) = bridge.book.recent_signals
    assert signal["action"] == "enter" and signal["side"] == "buy"
    assert signal["ungated"] is True and signal["model_version"] == "rules-v1"
    assert signal["edge_bp"] == pytest.approx(DEFAULT_POLICY["barrier_bp"])
    assert bridge.status()["ungated"] is True


def test_real_money_never_loads_an_ungated_model(tmp_path: Path) -> None:
    from ofi_scalper_service.app import load_pinned_model
    from ofi_scalper_service.config import MICRO_LIVE_ACK, ExecutionMode

    write(tmp_path / "models")
    settings = make_settings(
        tmp_path,
        OFI_EXECUTION_MODE="micro_live",
        OFI_MICRO_LIVE_ACK=MICRO_LIVE_ACK,
        EXECUTION_API_KEY="gateway-key-at-least-16",
        OFI_MODEL_VERSION="rules-v1",
        OFI_MODEL_DIR=str(tmp_path / "models"),
        OFI_ORDER_NOTIONAL_USD=25,
        OFI_MAX_POSITION_NOTIONAL_USD=30,
        OFI_MAX_TOTAL_NOTIONAL_USD=30,
        OFI_DAILY_LOSS_LIMIT_USD=3,
        OFI_MANUAL_APPROVAL_NOTIONAL_USD=30,
    )
    # Even if the flag got past config (it cannot), the loader still demands a pass.
    forced = settings.model_copy(update={"allow_ungated": True})
    assert forced.execution_mode is ExecutionMode.MICRO_LIVE
    with pytest.raises(ModelError, match="ungated"):
        load_pinned_model(forced)
