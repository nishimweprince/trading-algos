"""Model directories for tests, and a backend whose output the test controls."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ofi_scalper_service.model import LoadedModel, load_model, write_model

FEATURES = ["ofi_int_1000ms", "imbalance", "microprice_minus_mid_bp", "spread_bp"]
POLICY = {
    "horizon_s": 5,
    "threshold": 0.6,
    "barrier_bp": 8,
    "buffer_bp": 1,
    "entry_timeout_ms": 1000,
    "time_stop_mult": 2,
}
IDENTITY = {"x": [0.0, 1.0], "y": [0.0, 1.0]}


class Dial:
    """predict() returns whatever the test last set: [down, none, up]."""

    def __init__(self) -> None:
        self.probs: Sequence[float] = (0.1, 0.8, 0.1)
        self.rows: list[list[float]] = []

    def set(self, up: float = 0.1, down: float = 0.1) -> None:
        self.probs = (down, 1 - up - down, up)

    def backend(self, _path: Path, classes: list[str]) -> tuple[Any, dict[str, int]]:
        def predict(row: list[float]) -> Sequence[float]:
            self.rows.append(row)
            return self.probs

        return predict, {name: index for index, name in enumerate(classes)}


def make_model_dir(
    root: Path,
    version: str = "v1",
    *,
    passed: bool = True,
    binding: bool = False,
    features: list[str] | None = None,
    policy: dict[str, Any] | None = None,
    model_bytes: bytes = b"not a real catboost file",
    engine: dict[str, Any] | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    source = root / f"{version}.cbm.src"
    source.write_bytes(model_bytes)
    return write_model(
        root,
        version,
        model_file=source,
        features=features or FEATURES,
        policy=policy or POLICY,
        calibrator={"down": IDENTITY, "none": IDENTITY, "up": IDENTITY},
        engine=engine or {"pca_weights": [1.0] + [0.0] * 9},
        gates={"passed": passed, "binding": binding},
        extra_files={"strategy.md": "# test strategy\n"},
    )


def dial_model(root: Path, dial: Dial, **kwargs: Any) -> LoadedModel:
    make_model_dir(root, **kwargs)
    return load_model(root, kwargs.get("version", "v1"), backend=dial.backend)
