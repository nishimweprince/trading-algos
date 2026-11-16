"""SPEC §8.2: full-history replay of all 20 strategies against reference/trades.

    WF_DATA=".../Walk Forward/reference/data" WF_REFERENCE=".../Walk Forward/reference/trades" uv run pytest -m parity
"""

from pathlib import Path

import pytest
from conftest import DATA, REFERENCE

from walkforward_patterns import config as cfgmod
from walkforward_patterns.parity import compare
from walkforward_patterns.replay import DEFAULT_CONFIG, replay_symbol, write_trades

pytestmark = [pytest.mark.parity, pytest.mark.skipif(not (DATA and REFERENCE), reason="WF_DATA/WF_REFERENCE unset")]


@pytest.fixture(scope="module")
def replayed(tmp_path_factory):
    cfg = cfgmod.load(DEFAULT_CONFIG)
    out = tmp_path_factory.mktemp("replay")
    for sym in cfg.symbols:
        trades, _ = replay_symbol(sym, Path(DATA), cfg, progress=False)
        for sid, rows in trades.items():
            write_trades(out / f"{sid}_trades.csv", rows)
    return cfg, out


@pytest.mark.parametrize("sid", [f"S{i:02d}" for i in range(1, 21)])
def test_strategy_parity(replayed, sid):
    _, out = replayed
    r = compare(out, Path(REFERENCE), sid)
    assert r.ok, r
