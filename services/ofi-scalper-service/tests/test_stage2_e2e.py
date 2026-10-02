"""Stage 2 end to end on recorded fake frames: features -> train -> select -> gates."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("pandas")
pytest.importorskip("catboost")

from ta_plugin_binance_futures.testing import (  # noqa: E402
    FakeFuturesStream,
    agg_trade_frame,
    depth_frame,
    mark_price_frame,
)

from ofi_scalper_service.model import ModelError, load_model  # noqa: E402
from research.backtest import (  # noqa: E402
    DayJob,
    Latency,
    VariantSpec,
    aggregate,
    job_settings,
    run_days,
)
from research.cv import HoldoutUsed, Split  # noqa: E402
from research.gates import evaluate  # noqa: E402
from research.pipeline import eligible_days, ensure_features  # noqa: E402
from research.selection import select  # noqa: E402
from research.train import train_candidate  # noqa: E402
from tests.conftest import until  # noqa: E402
from tests.test_replay import StepClock, drained  # noqa: E402

DAYS = ["2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"]
# Dense, early-available features: the fake sessions last seconds, not hours.
FEATURES = [
    "ofi_l1_100ms",
    "ofi_int_100ms",
    "ofi_l1_1000ms",
    "ofi_int_1000ms",
    "imbalance",
    "microprice_minus_mid_bp",
    "spread_bp",
]
PATH = [0.0, 0.2, 0.4, 0.6, 0.4, 0.2, 0.0, -0.2, -0.4, -0.6, -0.4, -0.2]


def moving_frames(n: int = 150) -> dict:
    """A BTC touch that walks up and down, and trades at it."""
    public = [depth_frame("BTCUSDT", 95, 99, 94), depth_frame("ETHUSDT", 499, 501, 498)]
    market = [mark_price_frame("BTCUSDT", "60000.05", "0.0001", 1_790_003_600_000)]
    last, bid, ask = 99, 60000.0, 60000.1
    # Clear the snapshot's second levels so a moving touch never crosses them.
    public.append(
        depth_frame("BTCUSDT", 100, 101, 99, bids=[("59999.90", "0")], asks=[("60000.20", "0")])
    )
    last = 101
    for i in range(n):
        new_bid = round(60000.0 + PATH[i % len(PATH)], 1)
        new_ask = round(new_bid + 0.1, 1)
        bids = [(f"{bid:.2f}", "0")] if new_bid != bid else []
        asks = [(f"{ask:.2f}", "0")] if new_ask != ask else []
        bids.append((f"{new_bid:.2f}", f"{1 + i % 3}"))
        asks.append((f"{new_ask:.2f}", f"{1 + (i + 1) % 3}"))
        public.append(depth_frame("BTCUSDT", last + 1, last + 2, last, bids=bids, asks=asks))
        last += 2
        bid, ask = new_bid, new_ask
        price = new_bid if i % 2 else new_ask
        market.append(agg_trade_frame("BTCUSDT", i + 1, f"{price:.2f}", "0.4", i % 2 == 1))
    return {"public": [public], "market": [market]}


async def record_day(build, start_ns: int):
    frames = moving_frames()
    runtime = build(FakeFuturesStream(frames), record=True, OFI_BURST_TRADES=2)
    clock = StepClock(start_ns, 30_000_000)
    runtime.streams._clock_ns = clock
    runtime._clock_ns = clock
    await runtime.start()
    await until(lambda: drained(runtime, frames), seconds=10)
    await runtime.close()
    return runtime


async def test_stage2_end_to_end(build, tmp_path: Path) -> None:
    runtime = None
    for day in DAYS:
        start = datetime.fromisoformat(day).replace(hour=0, minute=10, tzinfo=UTC)
        runtime = await record_day(build, int(start.timestamp() * 1e9))
    stage2(tmp_path, runtime.settings)


def stage2(tmp_path: Path, settings) -> None:
    """Everything after recording: synchronous, as research runs."""
    record_dir = settings.record_dir
    features_dir = tmp_path / "features"
    written = ensure_features(record_dir, features_dir, DAYS, {"burst_trades": 2}, n_jobs=1)
    assert len(written) == 4 and all(Path(p).is_file() for p in written)

    # The fake days have hours missing, so the real eligibility check refuses them.
    kept, excluded = eligible_days(record_dir, DAYS, host_tag="test")
    assert kept == [] and "missing hour" in excluded[DAYS[0]]

    split = Split(
        "e2e",
        "week",
        ("P1", "P2", "P3"),
        ("P4",),
        {f"P{i + 1}": [day] for i, day in enumerate(DAYS)},
        False,
        "now",
    )
    candidate = train_candidate(
        split,
        features_dir=features_dir,
        out_dir=tmp_path / "candidates" / "h0.5",
        horizon_s=0.5,
        barrier_bp=0.02,  # tiny: the fake mids move by fractions of a basis point
        params={"iterations": 20, "depth": 2},
        max_train_rows=None,
        features=FEATURES,
    )
    for name in (
        "model.cbm",
        "calibrator.json",
        "engine.json",
        "cv_report.json",
        "shap_summary.json",
    ):
        assert (candidate.path / name).is_file(), name
    assert sorted(p.stem for p in candidate.oof_dir().glob("*.parquet")) == DAYS[1:3]
    cv = json.loads((candidate.path / "cv_report.json").read_text())
    assert [f["validation"] for f in cv["folds"] if "skipped" not in f] == ["P2", "P3"]

    # No fees, so a fraction-of-a-basis-point barrier can still clear the cost
    # test, and an order size above BTC's minimum notional.
    common = {
        **job_settings(settings),
        "maker_bp": 0.0,
        "taker_bp": 0.0,
        "order_notional_usd": 180.0,
    }
    latency = Latency(feed_ms=1.0, order_ms=2.0)
    selection = select(
        [candidate],
        split,
        record_dir=record_dir,
        common=common,
        latency=latency,
        out=tmp_path / "candidates",
        thresholds=(0.34, 0.5),
        min_trades=0,
    )
    assert selection["chosen"] is not None and len(selection["grid"]) == 2

    # The backtest reads the same samples the tables were scored from, and is
    # deterministic: twice, and across processes.
    spec = VariantSpec("v", str(candidate.path), "oof", 0.34, "queue", 1.0)
    jobs = [DayJob(day, str(record_dir), (spec,), latency, **common) for day in DAYS[1:3]]
    first = run_days(jobs, 1)
    assert sum(r["variants"]["v"]["scored"]["hits"] for r in first) > 0
    assert sum(len(r["variants"]["v"]["trades"]) for r in first) > 0  # cycles happened
    again = aggregate(run_days(jobs, 1), capital_usd=500)["v"]["trades"]
    parallel = aggregate(run_days(jobs, 2), capital_usd=500)["v"]["trades"]
    assert aggregate(first, capital_usd=500)["v"]["trades"] == again == parallel

    research_dir = tmp_path / "research"
    research_dir.mkdir()
    result = evaluate(
        split,
        selection,
        record_dir=record_dir,
        features_dir=features_dir,
        research_dir=research_dir,
        models_dir=tmp_path / "models",
        common=common,
        latency=latency,
        daily_loss_limit_usd=settings.daily_loss_limit_usd,
        version="e2e-v1",
    )
    assert result["evaluated"]
    held_out = json.loads((tmp_path / "models" / "e2e-v1" / "held_out_summary.json").read_text())
    assert list(held_out["base"]["daily_net_usd"]) == [DAYS[3]]  # held-out days only
    assert held_out["base"]["summary"]["entries"] > 0
    gates = json.loads((tmp_path / "models" / "e2e-v1" / "gates.json").read_text())
    assert gates["model_version"] == "e2e-v1" and gates["binding"] is False
    assert set(gates["gates"]) >= {"sharpe", "hit_rate", "t_stat", "pessimistic_queue_profitable"}
    assert gates["passed"] == all(g["passed"] for g in gates["gates"].values())
    loaded = load_model(tmp_path / "models", "e2e-v1", require_pass=False)
    assert loaded.policy.threshold == selection["chosen"]["threshold"]
    if gates["passed"]:
        assert (tmp_path / "models" / "e2e-v1" / "strategy.md").is_file()
    else:
        assert (research_dir / "REPORT.md").read_text().startswith("# e2e-v1: does not clear")
        with pytest.raises(ModelError, match="did not pass"):
            load_model(tmp_path / "models", "e2e-v1")

    with pytest.raises(HoldoutUsed):
        evaluate(
            split,
            selection,
            record_dir=record_dir,
            features_dir=features_dir,
            research_dir=research_dir,
            models_dir=tmp_path / "models",
            common=common,
            latency=latency,
            daily_loss_limit_usd=settings.daily_loss_limit_usd,
            version="e2e-v2",
        )
