from __future__ import annotations

import json
from pathlib import Path

from conftest import OPEN, at

from poly_kalshi_service.recorder import Recorder
from poly_kalshi_service.recordings import iter_files, read_lines


def _record(root: Path, lines: list[tuple[int, str, str]], session_ns: int) -> None:
    recorder = Recorder(root, host_tag="test")
    recorder.session_ns = session_ns
    recorder.start()
    for recv_ns, venue, text in lines:
        recorder.write(venue, recv_ns, text)
    recorder.stop()


def test_restart_within_the_hour_starts_a_new_file_and_a_crash_tail_is_kept(
    tmp_path: Path,
) -> None:
    first = [(at(OPEN, s), "p", f'{{"n":{s}}}') for s in range(10)]
    second = [(at(OPEN, 100 + s), "k", f'{{"n":{100 + s}}}') for s in range(5)]
    _record(tmp_path, first, session_ns=1)
    _record(tmp_path, second, session_ns=2)
    files = iter_files(tmp_path)
    assert [p.name.rsplit("_", 1)[1] for p in files] == [
        "00000000000000000001.gz",
        "00000000000000000002.gz",
    ]
    manifest = json.loads(files[0].with_name(files[0].name + ".json").read_text())
    assert manifest["lines"] == 10 and manifest["streams"] == {"p": 10}

    # Simulate a crash in the first session: its gzip stream loses its end.
    data = files[0].read_bytes()
    files[0].write_bytes(data[:-12])
    stats: dict[str, int] = {}
    lines = list(read_lines(files, stats))
    assert stats["truncated"] == 1
    assert stats["out_of_order"] == 0
    # The second session is intact; the first keeps what was before the break.
    assert [v for _, v, _ in lines][-5:] == ["k"] * 5
    assert 0 < sum(1 for _, v, _ in lines if v == "p") <= 10


def test_iter_files_filters_by_hour(tmp_path: Path) -> None:
    _record(tmp_path, [(at(OPEN, 0), "p", "{}"), (at(OPEN, 3600), "p", "{}")], session_ns=1)
    files = iter_files(tmp_path)
    assert len(files) == 2
    first_hour = files[0].name[3:14]
    assert iter_files(tmp_path, start=first_hour, end=first_hour) == files[:1]
    assert iter_files(tmp_path, start=files[1].name[3:14]) == files[1:]


def test_frames_with_raw_newlines_stay_one_line(tmp_path: Path) -> None:
    """Polymarket's subscribe-time ``book`` arrays end with a newline (2026-10-06)."""
    from conftest import make_engine, make_settings

    from poly_kalshi_service.alerts import Alerts
    from poly_kalshi_service.runtime import Runtime

    recorder = Recorder(tmp_path / "raw", host_tag="test")
    runtime = Runtime(
        make_settings(tmp_path),
        engine=make_engine(),
        discovery=None,  # type: ignore[arg-type]
        kalshi=None,
        poly=None,
        alerts=Alerts(None),
        recorder=recorder,
        clock_ns=lambda: at(OPEN, 1),
    )
    recorder.start()
    runtime.ingest("p", '[{"event_type":"book",\n"bids":[]}]\n')
    runtime.ingest("p", "{}")
    recorder.stop()
    stats: dict[str, int] = {}
    lines = list(read_lines(iter_files(tmp_path / "raw"), stats))
    assert stats["bad"] == 0
    assert [json.loads(text) for _, _, text in lines] == [
        [{"event_type": "book", "bids": []}],
        {},
    ]
