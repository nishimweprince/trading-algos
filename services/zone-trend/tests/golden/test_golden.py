"""Hand-labelled episodes (see ``zt episodes``): the core must agree with you.

Labels live in ``tests/golden/labels.yaml``; only entries with
``expected_signal`` filled in are checked. Skipped until labels and synced
data exist.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from zone_trend.config import load_config
from zone_trend.core.replay import replay
from zone_trend.data.store import DataPaths, load

LABELS = Path(__file__).with_name("labels.yaml")


def _labelled() -> list[dict]:
    if not LABELS.exists():
        return []
    rows = yaml.safe_load(LABELS.read_text()) or []
    return [r for r in rows if r.get("expected_signal") in ("yes", "no", True, False)]


@pytest.mark.data
@pytest.mark.parametrize("label", _labelled() or [None], ids=lambda r: r["id"] if r else "none")
def test_core_matches_label(label):
    if label is None:
        pytest.skip("no labelled episodes yet: run `zt episodes` and fill labels.yaml")
    paths = DataPaths.from_env()
    sym = label["symbol"]
    if not paths.months("klines", sym, "1h"):
        pytest.skip(f"{sym} not synced")
    t = int(label["bar_time"])
    zone_id = label["id"].rsplit("-", 1)[-1]
    h1, h4, d1 = (load(paths, "klines", sym, tf, end_ms=t + 1) for tf in ("1h", "4h", "1d"))
    took = False
    for _, step in replay(sym, h1, h4, d1, load_config()):
        if step.bar.close_time == t:
            took = any(s.zone_id == zone_id for s in step.result.signals)
    expected = label["expected_signal"] in ("yes", True)
    when = pd.Timestamp(t + 1, unit="ms", tz="UTC")
    assert took == expected, f"{sym} {when}: core {'took' if took else 'skipped'} it"
