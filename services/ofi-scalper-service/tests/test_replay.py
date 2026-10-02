"""Replay parity: recordings replayed through the live code reproduce the live samples."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ta_plugin_binance_futures.testing import (
    FakeFuturesStream,
    agg_trade_frame,
    depth_frame,
    mark_price_frame,
)

from ofi_scalper_service.sample_log import SampleLog
from research.replay import compare_samples, find_session, load_samples, replay_files
from tests.conftest import until

HOUR_NS = 3600 * 1_000_000_000


class StepClock:
    """Each read advances time, so a burst of fake frames spans many grid steps."""

    def __init__(self, start: int, step_ns: int) -> None:
        self.now = start
        self.step = step_ns

    def __call__(self) -> int:
        self.now += self.step
        return self.now


def market_frames(n: int = 60) -> dict:
    public = [depth_frame("BTCUSDT", 95, 99, 94), depth_frame("ETHUSDT", 499, 501, 498)]
    last_btc, last_eth = 99, 501
    for i in range(n):
        bid = f"{60000.0 - (i % 3) * 0.1:.2f}"
        public.append(
            depth_frame(
                "BTCUSDT", last_btc + 1, last_btc + 2, last_btc, bids=[(bid, f"{1 + i % 5}")]
            )
        )
        last_btc += 2
        if i % 2 == 0:
            ask = f"{2500.01 + (i % 4) * 0.01:.2f}"
            public.append(
                depth_frame("ETHUSDT", last_eth + 1, last_eth + 3, last_eth, asks=[(ask, "7")])
            )
            last_eth += 3
    market = [mark_price_frame("BTCUSDT", "60000.05", "0.0001", 1_790_003_600_000)]
    for i in range(n // 2):
        market.append(agg_trade_frame("BTCUSDT", i + 1, "60000.10", f"0.{i % 9 + 1}", i % 3 == 0))
    return {"public": [public], "market": [market]}


def all_files(record_dir: Path) -> list[Path]:
    return sorted(record_dir.rglob("*.gz"))


async def run_live(build, tmp_path: Path, start_ns: int):
    runtime = build(FakeFuturesStream(market_frames()), record=True, OFI_BURST_TRADES=2)
    clock = StepClock(start_ns, 30_000_000)  # 30 ms per read
    runtime.streams._clock_ns = clock
    runtime._clock_ns = clock
    runtime.sample_log = SampleLog(tmp_path / "samples")
    runtime.on_sample = runtime.sample_log.write
    await runtime.start()
    await until(
        lambda: (
            runtime.counts["AggTrade"] == 30
            and runtime.counts["DepthUpdate"] >= 90
            and all(s.verified for s in runtime.streams.syncs.values())
        ),
        seconds=5,
    )
    await runtime.close()  # flushes the recorder and the sample log
    return runtime


async def test_replay_reproduces_every_live_sample_exactly(build, tmp_path: Path) -> None:
    runtime = await run_live(build, tmp_path, 1_790_000_000_000_000_000)
    record_dir = runtime.settings.record_dir
    session = find_session(record_dir, datetime(2100, 1, 1, tzinfo=UTC))
    assert session is not None and session["tick_sizes"]["BTCUSDT"] == 0.1

    replayed, replayer = replay_files(all_files(record_dir), session=session, burst_trades=2)
    live = [s for p in sorted((tmp_path / "samples").rglob("*.gz")) for s in load_samples(p)]
    last = max(r["t_ns"] for r in replayed)
    report = compare_samples([s for s in live if s["t_ns"] <= last], replayed)

    assert report["mismatched"] == 0, report["examples"]
    assert report["only_live"] == 0 and report["only_replayed"] == 0
    assert report["matched"] > 20
    triggers = {r["trigger"] for r in replayed}
    assert triggers == {"grid", "burst"}  # both sampling paths are covered
    assert replayer.checkpoint_report["mismatches"] == 0


async def test_any_hour_replays_from_its_checkpoint(build, tmp_path: Path) -> None:
    # Start 1.5 s before an hour boundary so the session spans two hour files.
    boundary = (1_790_000_000_000_000_000 // HOUR_NS + 1) * HOUR_NS
    runtime = await run_live(build, tmp_path, boundary - 1_500_000_000)
    record_dir = runtime.settings.record_dir
    second_hour = [p for p in all_files(record_dir) if p.name.endswith(_hour_suffix(boundary))]
    assert any("BTCUSDT" in p.name for p in second_hour)
    session = find_session(record_dir, datetime(2100, 1, 1, tzinfo=UTC))

    # Only the later hour's files: no REST snapshot in them, just the checkpoint.
    replayed, replayer = replay_files(second_hour, session=session, burst_trades=2)
    assert all(sync.verified for sync in replayer.streams.syncs.values())
    for symbol, sync in replayer.streams.syncs.items():
        live_sync = runtime.streams.syncs[symbol]
        assert sync.last_final_id == live_sync.last_final_id
        assert sync.book.bids == live_sync.book.bids and sync.book.asks == live_sync.book.asks

    # Book-only features (no history windows) already match the live ones.
    live = {
        (s["symbol"], s["t_ns"], s["trigger"]): s
        for p in (tmp_path / "samples").rglob("*.gz")
        for s in load_samples(p)
    }
    compared = 0
    for row in replayed:
        twin = live.get((row["symbol"], row["t_ns"], row["trigger"]))
        if twin is None or row["mid"] is None:
            continue
        for name in (
            "mid",
            "imbalance",
            "spread_ticks",
            "microprice_minus_mid_bp",
            "depth_bid_5bp",
        ):
            assert row[name] == twin[name], (name, row["t_ns"])
        compared += 1
    assert compared > 5


def _hour_suffix(t_ns: int) -> str:
    stamp = datetime.fromtimestamp(t_ns / 1e9, UTC)
    return f"_{stamp.strftime('%Y%m%d_%H')}.gz"


def test_book_features_never_see_an_update_received_at_or_after_the_sample() -> None:
    """Regression: the update that triggers a grid sample was applied to the book
    before sampling, so depth bands at t included an update received after t."""
    from ta_plugin_binance_futures.testing import partial_depth_frame

    from research.replay import ReplayConfig, Replayer

    replayer = Replayer(
        ReplayConfig(symbols=("BTCUSDT",), tick_sizes={"BTCUSDT": 0.1}, book_mode="partial")
    )
    s = 1_000_000_000
    thin = partial_depth_frame("BTCUSDT", 10, 9, [("100.0", "1")], [("100.1", "1")])
    thick = partial_depth_frame("BTCUSDT", 11, 10, [("100.0", "9")], [("100.1", "1")])
    replayer.feed(5 * s + 50_000_000, thin)  # anchors the grid; next sample at 5.1 s
    replayer.feed(5 * s + 250_000_000, thick)  # received after the 5.1 s and 5.2 s samples
    grid = [r for r in replayer.samples if r["trigger"] == "grid"]
    assert [r["t_ns"] for r in grid] == [5 * s + 100_000_000, 5 * s + 200_000_000]
    for row in grid:
        assert row["depth_bid_5bp"] == 100.0 * 1  # the thin book, not the thick one
        assert row["imbalance"] == 0.5


def test_load_samples_keeps_rows_before_a_cut(tmp_path: Path) -> None:
    import gzip
    import json

    path = tmp_path / "samples_10.jsonl.gz"
    with gzip.open(path, "wt") as handle:
        for i in range(500):
            handle.write(json.dumps({"symbol": "BTCUSDT", "t_ns": i, "trigger": "grid"}) + "\n")
    cut = tmp_path / "cut.jsonl.gz"
    cut.write_bytes(path.read_bytes()[:-30])  # still being written / crashed
    rows = load_samples(cut)
    assert 0 < len(rows) <= 500 and rows[0]["t_ns"] == 0


async def test_replay_restarts_with_the_process(build, tmp_path: Path) -> None:
    """Two live sessions in one recording (a deploy): replay drops all state at
    the second session line, exactly as the restarted process had none."""
    first = await run_live(build, tmp_path, 1_790_000_000_000_000_000)
    await run_live(build, tmp_path, 1_790_000_000_000_000_000 + 60 * 1_000_000_000)
    record_dir = first.settings.record_dir
    replayed, replayer = replay_files(all_files(record_dir), burst_trades=2)
    live = [s for p in sorted((tmp_path / "samples").rglob("*.gz")) for s in load_samples(p)]
    report = compare_samples(
        [s for s in live if s["t_ns"] <= max(r["t_ns"] for r in replayed)], replayed
    )
    assert replayer.sessions == 2
    assert report["mismatched"] == 0, report["examples"]
    assert report["only_live"] == 0 and report["matched"] > 40


async def test_parity_holds_with_a_models_engine_fits(build, tmp_path: Path) -> None:
    from tests.model_fixture import Dial, dial_model

    model = dial_model(
        tmp_path / "models",
        Dial(),
        engine={"pca_weights": [0.5, 0.3, 0.2] + [0.0] * 7, "microprice_table": [[5, 1, 0.3]]},
    )
    runtime = build(FakeFuturesStream(market_frames()), record=True, OFI_BURST_TRADES=2)
    runtime.model = model  # init_engine reads its engine.json at boot
    clock = StepClock(1_790_000_000_000_000_000, 30_000_000)
    runtime.streams._clock_ns = clock
    runtime._clock_ns = clock
    runtime.sample_log = SampleLog(tmp_path / "samples")
    runtime.on_sample = runtime.sample_log.write
    await runtime.start()
    await until(lambda: runtime.counts["AggTrade"] == 30 and runtime.counts["DepthUpdate"] >= 90)
    await runtime.close()
    record_dir = runtime.settings.record_dir
    session = find_session(record_dir, datetime(2100, 1, 1, tzinfo=UTC))
    live = [s for p in sorted((tmp_path / "samples").rglob("*.gz")) for s in load_samples(p)]

    replayed, _ = replay_files(all_files(record_dir), session=session, burst_trades=2, model=model)
    last = max(r["t_ns"] for r in replayed)
    report = compare_samples([s for s in live if s["t_ns"] <= last], replayed)
    assert report["mismatched"] == 0 and report["matched"] > 20, report["examples"]
    # Without the model's fits the integrated OFI differs: the fits really are applied.
    plain, _ = replay_files(all_files(record_dir), session=session, burst_trades=2)
    assert compare_samples(replayed, plain)["mismatched"] > 0


async def test_shadow_replay_is_deterministic(build, tmp_path: Path) -> None:
    from ofi_scalper_service.risk import RiskLimits
    from research.replay import ShadowConfig
    from tests.model_fixture import Dial, dial_model

    runtime = await run_live(build, tmp_path, 1_790_000_000_000_000_000)
    record_dir = runtime.settings.record_dir
    session = find_session(record_dir, datetime(2100, 1, 1, tzinfo=UTC))
    dial = Dial()
    dial.set(up=0.9, down=0.02)
    model = dial_model(tmp_path / "models", dial)

    def run() -> tuple[list[dict], list[dict]]:
        shadow = ShadowConfig(
            limits=RiskLimits(1_000, 1_500, 100, 50, 800, 60_000, 0.5),
            gate={
                "funding_blackout_minutes": 0,
                "spread_pctl": None,
                "vol_1m_bp": None,
                "liq_burst_usd": None,
            },
        )
        replay_files(
            all_files(record_dir), session=session, burst_trades=2, model=model, shadow=shadow
        )
        return list(shadow.book.recent_trades), list(shadow.book.recent_signals)

    trades, signals = run()
    again = run()
    assert signals and trades
    assert (trades, signals) == again
    assert {s["action"].split(":")[0] for s in signals} <= {"enter", "skip"}
    assert all(t["mode"] == "shadow" and t["model_version"] == "v1" for t in trades)
