"""The live loop end to end: scripted Binance frames -> books -> features -> risk."""

from __future__ import annotations

import pytest
from ta_plugin_binance_futures.testing import (
    FakeBinanceFutures,
    FakeFuturesStream,
    agg_trade_frame,
    depth_frame,
    force_order_frame,
    mark_price_frame,
)

from ofi_scalper_service import runtime as runtime_module
from ofi_scalper_service.gapcheck import check_file
from tests.conftest import until

GRID = 100_000_000


def healthy_frames() -> dict:
    return {
        "public": [
            [
                depth_frame("BTCUSDT", 95, 99, 94),
                depth_frame("BTCUSDT", 100, 105, 99, bids=[("60000.00", "1.2")]),
                depth_frame("ETHUSDT", 499, 501, 498),
            ]
        ],
        "market": [
            [
                agg_trade_frame("BTCUSDT", 1, "60000.10", "0.5", False),
                agg_trade_frame("BTCUSDT", 2, "60000.00", "0.25", True),
                mark_price_frame("BTCUSDT", "60000.05", "0.0001", 1_790_003_600_000),
                force_order_frame("BTCUSDT", "SELL", "59990", "1"),
            ]
        ],
    }


def books_live(runtime) -> bool:
    return runtime.booted.is_set() and all(s.verified for s in runtime.streams.syncs.values())


async def test_features_flow_from_frames(build) -> None:
    runtime = build(FakeFuturesStream(healthy_frames()))
    await runtime.start()
    await until(lambda: books_live(runtime) and runtime.counts["AggTrade"] == 2)
    runtime._clock_ns.now += 3 * GRID  # let the quiet-market grid fire
    await until(lambda: "BTCUSDT" in runtime.latest)

    f = runtime.latest["BTCUSDT"]
    assert f["trigger"] == "grid"
    assert f["mid"] == pytest.approx(60000.05)
    # touch 60000.00 x1.2 / 60000.10 x1.5 (snapshot + replayed diff)
    assert f["imbalance"] == pytest.approx(1.2 / 2.7)
    assert f["spread_ticks"] == pytest.approx(1.0)
    assert f["tfi_1000ms"] == pytest.approx((0.5 - 0.25) / 0.75)
    assert f["funding_bucket"] == 0
    ready, details = runtime.readiness()
    assert ready, details
    assert runtime.risk.paused("BTCUSDT") == frozenset()
    status = runtime.status()
    assert status["counts"]["Liquidation"] == 1
    assert "depth" in status["feed_latency_ms_uncorrected"]


async def test_gap_pauses_symbol_alerts_and_recovers(build, fake: FakeBinanceFutures) -> None:
    fake.snapshot_ids["BTCUSDT"] = [100, 200]
    stream = FakeFuturesStream(
        {
            "public": [
                [
                    depth_frame("BTCUSDT", 100, 105, 99),
                    depth_frame("ETHUSDT", 499, 501, 498),
                    depth_frame("BTCUSDT", 150, 160, 149),  # gap
                    depth_frame("BTCUSDT", 161, 201, 160),
                ]
            ]
        }
    )
    runtime = build(stream)
    await runtime.start()
    await until(lambda: runtime.streams.syncs["BTCUSDT"].gaps == 1 and books_live(runtime))
    subjects = [subject for _, subject in runtime.alerts.sent]
    assert any("depth" in subject and "BTCUSDT" in subject for subject in subjects)
    assert runtime.counts["would_cancel_all"] >= 1
    assert "depth_resync" not in runtime.risk.paused("BTCUSDT")


async def test_kill_file_halts_and_ack_needs_it_gone(build, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(runtime_module, "KILL_POLL_SECONDS", 0.01)
    runtime = build(FakeFuturesStream({}))
    await runtime.start()
    kill_file = runtime.settings.kill_file_path
    kill_file.parent.mkdir(parents=True, exist_ok=True)
    kill_file.touch()
    await until(lambda: runtime.risk.halted)
    assert runtime.risk.snapshot()["halt"]["source"] == "file"
    assert ("kill", "OFI KILL SWITCH") in runtime.alerts.sent
    assert runtime.counts["would_flatten"] == 1
    ok, why = runtime.ack("test")
    assert not ok and "remove" in why
    kill_file.unlink()
    assert runtime.ack("test")[0]
    assert not runtime.risk.halted


async def test_recording_is_gap_checked_clean(build) -> None:
    runtime = build(FakeFuturesStream(healthy_frames()), record=True)
    await runtime.start()
    await until(lambda: books_live(runtime) and runtime.counts["AggTrade"] == 2)
    await runtime.close()
    files = sorted(runtime.settings.record_dir.rglob("*.gz"))
    assert {f.name.split("_")[0] for f in files} == {"BTCUSDT", "ETHUSDT"}
    for path in files:
        report = check_file(path)
        assert report["ok"], report
    btc = next(f for f in files if f.name.startswith("BTCUSDT"))
    assert check_file(btc)["snapshots"] == 1


async def test_stale_book_pauses_on_grid(build) -> None:
    runtime = build(FakeFuturesStream(healthy_frames()))
    await runtime.start()
    await until(lambda: books_live(runtime))
    runtime._clock_ns.now += 10 * GRID  # 1 s with no depth update > 750 ms
    await until(lambda: "stale_book" in runtime.risk.paused("BTCUSDT"))


async def test_partial_mode_feeds_the_engine_from_snapshots(build, fake) -> None:
    from ta_plugin_binance_futures.testing import partial_depth_frame

    stream = FakeFuturesStream(
        {
            "public": [
                [
                    partial_depth_frame(
                        "BTCUSDT", 10, 9, [("60000.00", "1.0")], [("60000.10", "1.5")]
                    ),
                    partial_depth_frame("ETHUSDT", 50, 49, [("2500.00", "3")], [("2500.01", "4")]),
                    # bid queue 1.0 -> 3.0 at the same price: OFI +60000*3 - 60000*1
                    partial_depth_frame(
                        "BTCUSDT", 11, 10, [("60000.00", "3.0")], [("60000.10", "1.5")]
                    ),
                ]
            ],
            "market": [[agg_trade_frame("BTCUSDT", 1, "60000.10", "0.5", False)]],
        },
    )
    runtime = build(stream, BINANCE_FUTURES_BOOK_MODE="partial")
    await runtime.start()
    await until(
        lambda: (
            books_live(runtime)
            and runtime.counts["BookSnapshot"] == 3
            and runtime.counts["AggTrade"] == 1
        )
    )
    runtime._clock_ns.now += 3 * GRID
    await until(lambda: "BTCUSDT" in runtime.latest)
    f = runtime.latest["BTCUSDT"]
    assert f["imbalance"] == pytest.approx(3.0 / 4.5)
    assert f["ofi_l1_1000ms"] == pytest.approx(120_000.0)
    assert runtime.readiness()[0]
    assert runtime.streams.mode == "partial"
    assert not any(r.url.path == "/fapi/v1/depth" for r in fake.requests)


async def test_heartbeat_reports_rates_lag_and_books(build) -> None:
    runtime = build(FakeFuturesStream(healthy_frames()), record=True)
    await runtime.start()
    await until(lambda: books_live(runtime) and runtime.counts["AggTrade"] == 2)
    line = runtime.heartbeat()
    assert line["booted"] and line["ready"]
    assert line["books"] == {"BTCUSDT": "live", "ETHUSDT": "live"}
    assert line["events_per_s"]["AggTrade"] > 0
    assert "depth" in line["lag_ms"] and line["halted"] is False
    assert "disk_free_gb" in line["recorder"]
    assert runtime.status()["heartbeat"] == line
    # The next heartbeat reports rates since this one, not since start.
    assert runtime.heartbeat()["events_per_s"].get("AggTrade", 0) == 0


async def test_key_check_alerts_on_dangerous_permissions(build, fake) -> None:
    from pydantic import SecretStr
    from ta_plugin_binance_futures.account import AccountReader
    from ta_plugin_binance_futures.rest import FapiRest

    runtime = build(FakeFuturesStream({}))
    fake.key_restrictions["enableFutures"] = True
    config = runtime.settings.model_copy(
        update={
            "binance_futures_api_key": SecretStr("k" * 64),
            "binance_futures_api_secret": SecretStr("s" * 64),
        }
    )
    http = runtime.account._rest._http
    signed = FapiRest(config, http=http)
    runtime.account = AccountReader(signed, sapi=FapiRest(config, http=http))
    await runtime._check_key()
    assert runtime.key_check["status"] == "dangerous"
    assert runtime.key_check["dangerous"] == ["enableFutures"]
    assert ("key", "OFI: the read-only Binance key has extra permissions") in runtime.alerts.sent


async def test_key_check_skipped_without_key(build) -> None:
    runtime = build(FakeFuturesStream({}))
    await runtime._check_key()
    assert runtime.key_check["status"] == "skipped"


async def test_disk_pause_alerts_once_and_recovery_alerts(build) -> None:
    runtime = build(FakeFuturesStream({}), record=True)
    runtime.recorder.disk_paused = True
    runtime._check_disk_alert()
    runtime._check_disk_alert()
    subjects = [subject for _, subject in runtime.alerts.sent]
    assert subjects.count("OFI recorder stopped: disk nearly full") == 1
    runtime.recorder.disk_paused = False
    runtime._check_disk_alert()
    assert "OFI recorder resumed: disk space recovered" in [s for _, s in runtime.alerts.sent]
