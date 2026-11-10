"""Tests for the pure helpers. Run: py -m pytest test_straddle.py  (or python3 test_straddle.py)"""
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from straddle import floor_to_step, load_env, parse_gmt, round_to_tick, trail_target

P = 0.00001  # 5-digit EURUSD point
KW = dict(trail_enabled=True, start=100, distance=80, step=10, be_trigger=60, be_offset=10)


def test_parse_gmt():
    assert parse_gmt("2026-11-10 13:30:00") == datetime(2026, 11, 10, 13, 30, tzinfo=timezone.utc)
    assert parse_gmt("2026-11-10T13:30Z") == datetime(2026, 11, 10, 13, 30, tzinfo=timezone.utc)
    now = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
    assert parse_gmt("13:30 GMT", now) == datetime(2026, 11, 10, 13, 30, tzinfo=timezone.utc)


def test_floor_to_step():
    assert floor_to_step(1.237, 0.01) == 1.23
    assert floor_to_step(0.30, 0.01) == 0.30
    assert floor_to_step(2.99, 1) == 2
    assert floor_to_step(0.15, 0.1) == 0.1


def test_round_to_tick():
    assert round_to_tick(1.081234, 0.00001, 5) == 1.08123
    assert round_to_tick(2650.127, 0.05, 2) == 2650.15


def test_trail_buy():
    o = 1.10000
    assert trail_target(True, o, o + 50 * P, o - 150 * P, P, **KW) is None          # not yet
    assert abs(trail_target(True, o, o + 60 * P, o - 150 * P, P, **KW) - (o + 10 * P)) < 1e-9  # BE
    assert abs(trail_target(True, o, o + 200 * P, o + 10 * P, P, **KW) - (o + 120 * P)) < 1e-9  # trail
    assert trail_target(True, o, o + 205 * P, o + 120 * P, P, **KW) is None         # < step
    assert trail_target(True, o, o + 150 * P, o + 120 * P, P, **KW) is None         # never loosens


def test_trail_sell():
    o = 1.10000
    assert abs(trail_target(False, o, o - 60 * P, o + 150 * P, P, **KW) - (o - 10 * P)) < 1e-9
    assert abs(trail_target(False, o, o - 200 * P, o - 10 * P, P, **KW) - (o - 120 * P)) < 1e-9
    assert trail_target(False, o, o - 150 * P, o - 120 * P, P, **KW) is None


def test_load_env():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / ".env"
        f.write_text('ZZ_A=1  # c\nZZ_B="p#ss word"\nZZ_C=C:\\MT5\\terminal64.exe\n# ZZ_D=x\n')
        load_env(f)
        assert os.environ["ZZ_A"] == "1"
        assert os.environ["ZZ_B"] == "p#ss word"
        assert os.environ["ZZ_C"] == "C:\\MT5\\terminal64.exe"
        assert "ZZ_D" not in os.environ


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
