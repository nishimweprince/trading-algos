"""Closed-bar, schema-checked inference for the shadow outcome model."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from app.config import settings
from app.ml.outcome.artifact import load_artifact
from app.ml.outcome.features import CAUSAL_FEATURES, OUTCOME_FEATURE_VERSION
from app.ml.outcome.model import CLASS_ORDER
from app.ml.outcome.preprocessing import INPUT_FEATURES
from app.services.bar_features import context_half, tags_half
from app.services.export_bar_features import _encode_tags
from app.services.pips import spread_pips

OUTCOME_HORIZON_BARS = 24
OUTCOME_TARGET_ATR = 1.5
OUTCOME_STOP_ATR = 1.0


class OutcomeArtifactError(RuntimeError):
    """Base class for safe, operator-actionable inference failures."""

    code = "outcome_artifact_error"


class OutcomeArtifactUnavailable(OutcomeArtifactError):
    code = "outcome_artifact_absent"


class OutcomeArtifactIncompatible(OutcomeArtifactError):
    code = "outcome_artifact_incompatible"


@dataclass(frozen=True)
class OutcomeProbability:
    direction: Literal["long", "short"]
    side: Literal[1, -1]
    p_win: float
    p_loss: float
    p_timeout: float
    expected_gross_r: float
    estimated_spread_cost_r: float
    expected_net_r: float
    gross_break_even_p_win: float
    spread_adjusted_break_even_p_win: float
    edge_over_break_even: float


@dataclass(frozen=True)
class OutcomeContract:
    timeframe: Literal["H1"]
    horizon_bars: Literal[24]
    target_atr: Literal[1.5]
    stop_atr: Literal[1.0]
    spread_pips_assumed: float
    atr_at_signal: float


@dataclass(frozen=True)
class OutcomeInference:
    long: OutcomeProbability
    short: OutcomeProbability
    contract: OutcomeContract
    model_version: str
    artifact_version: str
    schema_sha256: str
    outcome_feature_version: str
    feature_version: str
    bar_feature_version: str
    status: Literal["pilot_shadow"] = "pilot_shadow"
    pilot: bool = True
    promoted: bool = False


def outcome_economics(
    p_win: float, p_loss: float, p_timeout: float, *, spread_cost_r: float
) -> dict[str, float]:
    """Interpret the fixed 1.5R/1R/24-bar probability contract economically."""
    reward_r = OUTCOME_TARGET_ATR / OUTCOME_STOP_ATR
    gross = reward_r * p_win - p_loss
    gross_break_even = 1.0 / (1.0 + reward_r)
    adjusted_break_even = min(
        max((1.0 + spread_cost_r - p_timeout) / (1.0 + reward_r), 0.0),
        1.0,
    )
    return {
        "expected_gross_r": gross,
        "estimated_spread_cost_r": spread_cost_r,
        "expected_net_r": gross - spread_cost_r,
        "gross_break_even_p_win": gross_break_even,
        "spread_adjusted_break_even_p_win": adjusted_break_even,
        "edge_over_break_even": p_win - adjusted_break_even,
    }


def build_input_features(
    window: pd.DataFrame,
    symbol: str,
    timeframe: str,
    pip_size: float,
    htf: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Build exactly ``INPUT_FEATURES`` from a window ending at a closed bar."""
    if window.empty:
        raise ValueError("Closed-bar window is empty")
    row = context_half(window, symbol, timeframe, pip_size, htf)
    tag_data = tags_half(window, float(row["atr_at_bar"] or 0.0))
    frame = pd.DataFrame(
        [
            {
                **{name: row.get(name) for name in CAUSAL_FEATURES},
                "bar_tags": tag_data["bar_tags"],
            }
        ]
    )
    encoded = _encode_tags(frame)
    sides = pd.concat(
        [encoded.assign(side=1), encoded.assign(side=-1)],
        ignore_index=True,
    )
    output = sides.loc[:, INPUT_FEATURES]
    # ATR remains operational metadata for cost analysis, but never crosses the
    # estimator input boundary.
    output.attrs["atr_at_signal"] = row.get("atr_at_signal")
    return output


def _artifact_files(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        metadata = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
        manifest = json.loads((path / "dataset_manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OutcomeArtifactUnavailable(
            f"Outcome artifact {path.name!r} is not installed at {path}"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise OutcomeArtifactIncompatible(
            f"Outcome artifact metadata is unreadable: {exc}"
        ) from exc
    return metadata, manifest


def _validate_contract(
    metadata: dict[str, Any],
    manifest: dict[str, Any],
    artifact_version: str,
) -> None:
    checks = {
        "artifact_version": (metadata.get("artifact_version"), artifact_version),
        "input_features": (metadata.get("input_features"), list(INPUT_FEATURES)),
        "class_order": (metadata.get("class_order"), list(CLASS_ORDER)),
        "outcome_feature_version": (
            metadata.get("outcome_feature_version"),
            OUTCOME_FEATURE_VERSION,
        ),
        "schema_sha256": (metadata.get("schema_sha256"), manifest.get("schema_sha256")),
        "bar_feature_version": (
            manifest.get("bar_feature_version"),
            settings.bar_feature_version,
        ),
        "feature_version": (manifest.get("feature_version"), settings.feature_version),
    }
    mismatches = [
        f"{name}: artifact={actual!r}, live={expected!r}"
        for name, (actual, expected) in checks.items()
        if actual != expected
    ]
    if mismatches:
        raise OutcomeArtifactIncompatible(
            "Outcome artifact contract mismatch; " + "; ".join(mismatches)
        )


def infer_outcomes(
    window: pd.DataFrame,
    symbol: str,
    timeframe: str,
    pip_size: float,
    htf: dict[str, Any] | None = None,
    *,
    artifact_root: Path | None = None,
    artifact_version: str | None = None,
) -> OutcomeInference:
    """Return calibrated probabilities for both sides of one closed bar."""
    version = artifact_version or settings.outcome_artifact_version
    path = (artifact_root or settings.outcome_artifact_root) / version
    metadata, manifest = _artifact_files(path)
    _validate_contract(metadata, manifest, version)
    try:
        model, loaded_metadata = load_artifact(path, expected_version=version)
        features = build_input_features(window, symbol, timeframe, pip_size, htf)
        probabilities = np.asarray(model.predict_proba(features), dtype=float)
    except OutcomeArtifactError:
        raise
    except Exception as exc:
        raise OutcomeArtifactIncompatible(
            f"Outcome artifact cannot serve inference: {exc}"
        ) from exc
    if probabilities.shape != (2, 3) or not np.isfinite(probabilities).all():
        raise OutcomeArtifactIncompatible("Outcome model returned invalid probabilities")
    if not np.allclose(probabilities.sum(axis=1), 1.0, atol=1e-9):
        raise OutcomeArtifactIncompatible("Outcome model probabilities do not sum to one")

    atr = float(features.attrs.get("atr_at_signal") or 0.0)
    if not np.isfinite(atr) or atr <= 0:
        raise OutcomeArtifactIncompatible("Outcome model input has no valid ATR for cost analysis")
    assumed_spread_pips = float(spread_pips(symbol))
    spread_price = assumed_spread_pips * pip_size
    spread_cost_r = spread_price / (OUTCOME_STOP_ATR * atr)
    def result(index: int, direction: Literal["long", "short"], side: Literal[1, -1]):
        values = probabilities[index]
        p_win, p_loss, p_timeout = map(float, values)
        return OutcomeProbability(
            direction=direction,
            side=side,
            p_win=p_win,
            p_loss=p_loss,
            p_timeout=p_timeout,
            **outcome_economics(
                p_win,
                p_loss,
                p_timeout,
                spread_cost_r=spread_cost_r,
            ),
        )

    return OutcomeInference(
        long=result(0, "long", 1),
        short=result(1, "short", -1),
        contract=OutcomeContract(
            timeframe="H1",
            horizon_bars=OUTCOME_HORIZON_BARS,
            target_atr=OUTCOME_TARGET_ATR,
            stop_atr=OUTCOME_STOP_ATR,
            spread_pips_assumed=assumed_spread_pips,
            atr_at_signal=atr,
        ),
        model_version=f"outcome-v{loaded_metadata['outcome_feature_version']}",
        artifact_version=version,
        schema_sha256=str(loaded_metadata["schema_sha256"]),
        outcome_feature_version=OUTCOME_FEATURE_VERSION,
        feature_version=settings.feature_version,
        bar_feature_version=settings.bar_feature_version,
    )
