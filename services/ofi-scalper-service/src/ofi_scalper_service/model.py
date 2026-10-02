"""A pinned, hash-verified model: CatBoost, its calibrator, and its policy values.

A model lives in ``<OFI_MODEL_DIR>/<version>/``::

    manifest.json     version, horizon, classes, features, policy, sha256 of every file
    model.cbm         CatBoost multiclass model
    calibrator.json   isotonic knots per class, fit on validation folds only
    engine.json       PCA weights and microprice table for the feature engine
    gates.json        the research harness's verdict for this version
    ...               any other listed file (SHAP summary, strategy.md) is verified too

Loading refuses on any hash mismatch, on a feature the engine does not
produce, and (unless asked not to) on a ``gates.json`` that did not pass. The
research harness writes ``gates.json``; nothing here decides whether a model
passed.
"""

from __future__ import annotations

import hashlib
import json
import time
from bisect import bisect_right
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .policy import PolicyParams, Scores
from .state_engine import LEVELS, feature_names

__all__ = [
    "REQUIRED_FILES",
    "Calibrator",
    "GatesVerdict",
    "LoadedModel",
    "ModelError",
    "Scorer",
    "engine_overrides",
    "load_model",
    "write_model",
]

REQUIRED_FILES = ("model.cbm", "calibrator.json", "engine.json", "gates.json")
CLASSES = ("down", "none", "up")
LATENCY_RESERVOIR = 2000

Predict = Callable[[list[float]], Sequence[float]]


class ModelError(RuntimeError):
    """The model cannot be used; the message says why."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class GatesVerdict:
    passed: bool
    binding: bool
    raw: dict[str, Any]


class Calibrator:
    """Isotonic maps per class as piecewise-linear knots, then renormalised."""

    def __init__(self, knots: dict[str, dict[str, list[float]]]) -> None:
        self.knots: dict[str, tuple[list[float], list[float]]] = {}
        for name in CLASSES:
            if name not in knots:
                raise ModelError(f"calibrator.json has no '{name}' class")
            entry = knots[name] if isinstance(knots[name], dict) else {}
            xs, ys = list(entry.get("x") or []), list(entry.get("y") or [])
            if len(xs) != len(ys) or len(xs) < 2 or xs != sorted(xs):
                raise ModelError(f"calibrator.json '{name}' needs >= 2 sorted x with matching y")
            self.knots[name] = (xs, ys)

    @staticmethod
    def _interp(x: float, xs: list[float], ys: list[float]) -> float:
        if x <= xs[0]:
            return ys[0]
        if x >= xs[-1]:
            return ys[-1]
        i = bisect_right(xs, x)
        x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
        return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)

    def __call__(self, raw: dict[str, float]) -> Scores:
        mapped = {name: max(self._interp(raw[name], *self.knots[name]), 0.0) for name in CLASSES}
        total = sum(mapped.values())
        if total <= 0:
            return Scores(up=0.0, down=0.0, none=1.0)
        return Scores(
            up=mapped["up"] / total, down=mapped["down"] / total, none=mapped["none"] / total
        )


@dataclass
class Scorer:
    """Feature sample -> calibrated ``Scores``; times itself."""

    features: list[str]
    class_index: dict[str, int]  # class name -> column of predict's output
    predict: Predict
    calibrator: Calibrator
    latency_us: deque[float] = field(default_factory=lambda: deque(maxlen=LATENCY_RESERVOIR))

    def score(self, sample: dict[str, Any]) -> Scores | None:
        """None when any feature is missing: no score, no trade."""
        row: list[float] = []
        for name in self.features:
            value = sample.get(name)
            if value is None:
                return None
            row.append(float(value))
        started = time.perf_counter()
        probs = self.predict(row)
        raw = {name: float(probs[index]) for name, index in self.class_index.items()}
        scores = self.calibrator(raw)
        self.latency_us.append((time.perf_counter() - started) * 1e6)
        return scores

    def latency(self) -> dict[str, float] | None:
        if not self.latency_us:
            return None
        ordered = sorted(self.latency_us)

        def pick(q: float) -> float:
            return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 1)

        return {"p50_us": pick(0.5), "p99_us": pick(0.99), "n": len(ordered)}


@dataclass
class LoadedModel:
    version: str
    path: Path
    manifest: dict[str, Any]
    policy: PolicyParams
    engine: dict[str, Any]
    gates: GatesVerdict
    scorer: Scorer

    def summary(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "horizon_s": self.policy.horizon_s,
            "policy": self.policy.as_dict(),
            "features": len(self.scorer.features),
            "gates": {"passed": self.gates.passed, "binding": self.gates.binding},
            "scoring_latency": self.scorer.latency(),
        }


def engine_overrides(engine: dict[str, Any]) -> dict[str, Any]:
    """``engine.json`` -> keyword arguments for ``state_engine.EngineConfig``."""
    out: dict[str, Any] = {}
    if "pca_weights" in engine:
        weights = tuple(float(w) for w in engine["pca_weights"])
        if len(weights) != LEVELS:
            raise ModelError(f"engine.json pca_weights needs {LEVELS} entries")
        out["pca_weights"] = weights
    if "microprice_table" in engine:
        out["microprice_table"] = {
            (int(bucket), int(spread)): float(adjust)
            for bucket, spread, adjust in engine["microprice_table"]
        }
    for name in ("imbalance_buckets", "spread_cap"):
        if name in engine:
            out[name] = int(engine[name])
    return out


def _catboost_backend(path: Path, classes: list[str]) -> tuple[Predict, dict[str, int]]:
    try:
        from catboost import CatBoostClassifier
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise ModelError(
            "catboost is not installed; sync with the model extra "
            "(uv sync --package ofi-scalper-service --extra model)"
        ) from exc
    model = CatBoostClassifier()
    model.load_model(str(path))
    order = [str(c) for c in model.classes_]
    missing = set(classes) - set(order)
    if missing:
        raise ModelError(f"model.cbm has no class(es) {sorted(missing)}; it has {order}")

    def predict(row: list[float]) -> Sequence[float]:
        return model.predict_proba([row])[0]

    return predict, {name: order.index(name) for name in classes}


Backend = Callable[[Path, list[str]], tuple[Predict, dict[str, int]]]


def load_model(
    model_dir: Path,
    version: str,
    *,
    require_pass: bool = True,
    backend: Backend = _catboost_backend,
) -> LoadedModel:
    path = (model_dir / version).expanduser()
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file():
        raise ModelError(f"no manifest at {manifest_path}")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("version") != version:
        raise ModelError(f"manifest version {manifest.get('version')!r} != {version!r}")

    files: dict[str, str] = manifest.get("files") or {}
    for name in REQUIRED_FILES:
        if name not in files:
            raise ModelError(f"manifest lists no {name}")
    for name, expected in files.items():
        target = path / name
        if not target.is_file():
            raise ModelError(f"{name} is listed in the manifest but missing")
        actual = _sha256(target)
        if actual != expected:
            raise ModelError(f"{name} sha256 {actual[:12]}… != manifest {str(expected)[:12]}…")

    features = list(manifest.get("features") or [])
    known = set(feature_names(cross=True))
    unknown = [name for name in features if name not in known]
    if not features or unknown:
        raise ModelError(f"features the engine does not produce: {unknown or 'none listed'}")
    if list(manifest.get("classes") or CLASSES) != list(CLASSES):
        raise ModelError(f"classes must be {list(CLASSES)}")

    try:
        policy = PolicyParams.from_dict(manifest.get("policy") or {})
    except (TypeError, ValueError) as exc:
        raise ModelError(f"manifest policy: {exc}") from exc
    if float(manifest.get("horizon_s", policy.horizon_s)) != policy.horizon_s:
        raise ModelError("manifest horizon_s differs from policy.horizon_s")

    gates_raw = json.loads((path / "gates.json").read_text())
    if gates_raw.get("model_version") != version:
        raise ModelError(f"gates.json is for {gates_raw.get('model_version')!r}, not {version!r}")
    gates = GatesVerdict(
        passed=gates_raw.get("passed") is True,
        binding=gates_raw.get("binding") is True,
        raw=gates_raw,
    )
    if require_pass and not gates.passed:
        raise ModelError(
            f"{version}: gates.json did not pass. Shadow and testnet need a model whose "
            "research gates passed (ofi-scalper-plan.md §1.7)."
        )

    engine = json.loads((path / "engine.json").read_text())
    engine_overrides(engine)  # validate now
    calibrator = Calibrator(json.loads((path / "calibrator.json").read_text()))
    predict, class_index = backend(path / "model.cbm", list(CLASSES))
    return LoadedModel(
        version=version,
        path=path,
        manifest=manifest,
        policy=policy,
        engine=engine,
        gates=gates,
        scorer=Scorer(features, class_index, predict, calibrator),
    )


def write_model(
    model_dir: Path,
    version: str,
    *,
    model_file: Path,
    features: list[str],
    policy: dict[str, Any],
    calibrator: dict[str, Any],
    engine: dict[str, Any],
    gates: dict[str, Any],
    extra_files: dict[str, str] | None = None,
    data: dict[str, Any] | None = None,
) -> Path:
    """Write a model directory with its manifest (research's train.py uses this)."""
    path = model_dir / version
    path.mkdir(parents=True, exist_ok=False)
    (path / "model.cbm").write_bytes(model_file.read_bytes())
    (path / "calibrator.json").write_text(json.dumps(calibrator, indent=2))
    (path / "engine.json").write_text(json.dumps(engine, indent=2))
    (path / "gates.json").write_text(json.dumps({"model_version": version, **gates}, indent=2))
    for name, text in (extra_files or {}).items():
        (path / name).write_text(text)
    names = [*REQUIRED_FILES, *(extra_files or {})]
    manifest = {
        "schema": 1,
        "version": version,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "horizon_s": policy["horizon_s"],
        "classes": list(CLASSES),
        "features": features,
        "policy": policy,
        "data": data or {},
        "files": {name: _sha256(path / name) for name in names},
    }
    (path / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return path
