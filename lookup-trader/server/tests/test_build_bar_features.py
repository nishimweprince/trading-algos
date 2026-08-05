"""The feature builder's bar-selection logic.

`_bars_to_build` decides what a run actually recomputes, so a bug here is either
a silent no-op or a full rebuild the operator did not ask for.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_bar_features.py"
_spec = importlib.util.spec_from_file_location("build_bar_features", _SCRIPT)
bbf = importlib.util.module_from_spec(_spec)
sys.modules["build_bar_features"] = bbf
_spec.loader.exec_module(bbf)


def _candles(n: int = 20) -> pd.DataFrame:
    """Bars as `_load_candles` hands them over: tz-aware UTC, like the store."""
    return pd.DataFrame(
        {
            "ts": pd.date_range("2026-05-01", periods=n, freq="h", tz="UTC"),
            "open": 1.0,
            "high": 1.1,
            "low": 0.9,
            "close": 1.05,
            "volume": 10.0,
        }
    )


def _empty_index() -> pd.DataFrame:
    return pd.DataFrame(columns=["ts", "bar_feature_version", "complete"])


@pytest.mark.parametrize(
    "since,label",
    [
        (datetime(2026, 5, 1, 10), "naive, as --from 2026-05-01T10:00 parses"),
        (datetime(2026, 5, 1, 10, tzinfo=timezone.utc), "already UTC-aware"),
    ],
)
def test_from_filters_rather_than_raising(since, label):
    """The candle column is tz-aware UTC; a naive bound used to raise on compare."""
    candles = _candles(20)

    targets = bbf._bars_to_build(candles, _empty_index(), min_warmup=1, since=since, rebuild=False)

    assert targets == list(range(10, 20)), label


def test_a_naive_from_is_read_as_utc_not_local():
    """Every date in this project is UTC. Reading `--from` in the machine's zone
    would make the same command select different bars on different machines."""
    candles = _candles(20)

    targets = bbf._bars_to_build(
        candles,
        _empty_index(),
        min_warmup=1,
        since=datetime(2026, 5, 1, 10),
        rebuild=False,
    )

    assert candles["ts"].iloc[targets[0]] == pd.Timestamp("2026-05-01 10:00", tz="UTC")


def test_an_offset_from_is_converted_not_stripped():
    """10:00-04:00 is 14:00 UTC — dropping the offset would select four hours early."""
    candles = _candles(20)

    targets = bbf._bars_to_build(
        candles,
        _empty_index(),
        min_warmup=1,
        since=datetime.fromisoformat("2026-05-01T10:00:00-04:00"),
        rebuild=False,
    )

    assert candles["ts"].iloc[targets[0]] == pd.Timestamp("2026-05-01 14:00", tz="UTC")


def test_no_from_leaves_every_eligible_bar():
    candles = _candles(20)

    targets = bbf._bars_to_build(candles, _empty_index(), min_warmup=1, since=None, rebuild=False)

    assert targets == list(range(20))


def test_from_composes_with_the_warmup_floor():
    """Whichever bound is later wins; `--from` cannot pull in unwarmed bars."""
    candles = _candles(20)

    targets = bbf._bars_to_build(
        candles,
        _empty_index(),
        min_warmup=15,
        since=datetime(2026, 5, 1, 5),
        rebuild=False,
    )

    assert targets == list(range(14, 20))
