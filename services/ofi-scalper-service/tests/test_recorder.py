from __future__ import annotations

import gzip
import json
from pathlib import Path

from ta_plugin_binance_futures.testing import agg_trade_frame, depth_frame

from ofi_scalper_service.gapcheck import check_file
from ofi_scalper_service.recorder import Recorder, symbol_and_stream

HOUR_NS = 3600 * 1_000_000_000
T0 = 1_790_000_000 * 1_000_000_000  # 2026-09-21T14:13:20Z


def snapshot_frame(symbol: str, last_update_id: int) -> str:
    return json.dumps(
        {
            "stream": f"{symbol.lower()}@depthSnapshot",
            "data": {"lastUpdateId": last_update_id, "bids": [], "asks": []},
        },
        separators=(",", ":"),
    )


def test_symbol_and_stream() -> None:
    assert symbol_and_stream(depth_frame("BTCUSDT", 1, 2, 0)) == ("BTCUSDT", "depth@0ms")
    assert symbol_and_stream(snapshot_frame("ETHUSDT", 5)) == ("ETHUSDT", "depthSnapshot")
    assert symbol_and_stream('{"e":"serverShutdown"}') == ("_misc", "unknown")


def test_round_trip_rotation_and_manifest(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path, host_tag="test")
    recorder.start()
    frames = [
        (T0, depth_frame("BTCUSDT", 1, 2, 0)),
        (T0 + 1, agg_trade_frame("BTCUSDT", 10, "100", "1", False)),
        (T0 + 2, depth_frame("ETHUSDT", 5, 6, 4)),
        (T0 + HOUR_NS, depth_frame("BTCUSDT", 3, 4, 2)),  # next hour
    ]
    for recv_ns, text in frames:
        recorder.write("public", recv_ns, text)
    recorder.note_gap("BTCUSDT")
    recorder.stop()

    files = sorted(tmp_path.rglob("*.gz"))
    assert [f.name for f in files] == [
        "BTCUSDT_20260921_14.gz",
        "BTCUSDT_20260921_15.gz",
        "ETHUSDT_20260921_14.gz",
    ]
    with gzip.open(files[0], "rt") as handle:
        lines = handle.read().splitlines()
    assert lines[0] == f"{T0} {frames[0][1]}"
    assert len(lines) == 2
    manifest = json.loads(Path(str(files[0]) + ".json").read_text())
    assert manifest["lines"] == 2 and manifest["host"] == "test"
    assert manifest["streams"] == {"depth@0ms": 1, "aggTrade": 1}
    assert recorder.stats()["lines_written"] == 4
    assert recorder.errors == 0


def test_restart_within_hour_appends(tmp_path: Path) -> None:
    for offset in (0, 10):
        recorder = Recorder(tmp_path, host_tag="test")
        recorder.start()
        recorder.write("public", T0 + offset, depth_frame("BTCUSDT", offset, offset + 1, 0))
        recorder.stop()
    (path,) = tmp_path.rglob("*.gz")
    with gzip.open(path, "rt") as handle:
        assert len(handle.read().splitlines()) == 2
    manifest = json.loads(Path(str(path) + ".json").read_text())
    assert manifest["lines"] == 2 and manifest["sessions"] == 2


def write_gz(path: Path, lines: list[tuple[int, str]]) -> Path:
    with gzip.open(path, "wt") as handle:
        for recv_ns, text in lines:
            handle.write(f"{recv_ns} {text}\n")
    return path


def test_gapcheck_clean_file(tmp_path: Path) -> None:
    path = write_gz(
        tmp_path / "ok.gz",
        [
            (1, depth_frame("BTCUSDT", 1, 5, 0)),
            (2, depth_frame("BTCUSDT", 6, 9, 5)),
            (3, agg_trade_frame("BTCUSDT", 1, "1", "1", False)),
            (4, agg_trade_frame("BTCUSDT", 2, "1", "1", True)),
        ],
    )
    report = check_file(path)
    assert report["ok"] and report["depth_breaks"] == 0 and report["agg_trade_gaps"] == 0


def test_gapcheck_recovered_and_unrecovered_breaks(tmp_path: Path) -> None:
    recovered = write_gz(
        tmp_path / "recovered.gz",
        [
            (1, depth_frame("BTCUSDT", 1, 5, 0)),
            (2, depth_frame("BTCUSDT", 10, 12, 9)),  # break
            (3, snapshot_frame("BTCUSDT", 14)),
            (4, depth_frame("BTCUSDT", 13, 15, 12)),  # bridges 14
            (5, depth_frame("BTCUSDT", 16, 18, 15)),
        ],
    )
    report = check_file(recovered)
    assert report["depth_breaks"] == 1 and report["unrecovered_breaks"] == 0 and report["ok"]

    broken = write_gz(
        tmp_path / "broken.gz",
        [(1, depth_frame("BTCUSDT", 1, 5, 0)), (2, depth_frame("BTCUSDT", 10, 12, 9))],
    )
    assert not check_file(broken)["ok"]


def test_gapcheck_backwards_time_and_trade_gap(tmp_path: Path) -> None:
    path = write_gz(
        tmp_path / "bad.gz",
        [
            (5, agg_trade_frame("BTCUSDT", 1, "1", "1", False)),
            (4, agg_trade_frame("BTCUSDT", 3, "1", "1", False)),
        ],
    )
    report = check_file(path)
    assert report["backwards"] == 1 and report["agg_trade_gaps"] == 1 and not report["ok"]


def test_gapcheck_partial_streams_count_skips_but_pass(tmp_path: Path) -> None:
    from ta_plugin_binance_futures.testing import partial_depth_frame

    path = write_gz(
        tmp_path / "partial.gz",
        [
            (1, partial_depth_frame("BTCUSDT", 10, 9, [("1", "1")], [("2", "1")])),
            (2, partial_depth_frame("BTCUSDT", 11, 10, [("1", "1")], [("2", "1")])),
            (3, partial_depth_frame("BTCUSDT", 20, 15, [("1", "1")], [("2", "1")])),
        ],
    )
    report = check_file(path)
    assert report["partial_snapshots"] == 3 and report["partial_skipped"] == 1
    assert report["depth_breaks"] == 0 and report["ok"]


def test_gapcheck_reports_a_truncated_file_without_failing(tmp_path: Path) -> None:
    whole = write_gz(
        tmp_path / "whole.gz",
        [(n, agg_trade_frame("BTCUSDT", n, "1", "1", False)) for n in range(1, 200)],
    )
    cut = tmp_path / "cut.gz"
    cut.write_bytes(whole.read_bytes()[:-40])  # an hour still being written, or a crash
    report = check_file(cut)
    assert report["truncated"] and report["ok"]
    assert 0 < report["lines"] <= 199
    assert check_file(whole)["truncated"] is False
