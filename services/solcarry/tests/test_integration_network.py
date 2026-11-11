"""Live end-to-end fetch of a small slice from each source. Run with ``pytest -m network``."""

from __future__ import annotations

from datetime import date

import pytest

from solcarry.config import Config
from solcarry.http import Http
from solcarry.sources.binance_vision import BinanceVision, datasets_for
from solcarry.sources.defillama import DefiLlama
from solcarry.sources.fred import Fred
from solcarry.sources.jupiter import Jupiter
from solcarry.sources.velocity import Velocity

pytestmark = pytest.mark.network


def test_fetch_one_month_each(paths):
    cfg = Config.model_validate({"run": {"start": "2025-01-01", "end": "2025-01-31"}})
    with Http(cfg.http) as http:
        bv = BinanceVision(cfg, http, paths.raw)
        ds = {d.name: d for d in datasets_for(cfg, [])}
        assert bv.sync(ds["funding"], today=date(2026, 11, 11))
        assert len(bv.load(ds["funding"])) > 50
        v = Velocity(cfg, http, paths.raw)
        assert v.fetch_day(date(2026, 10, 1), date(2026, 11, 11)) > 0
        assert DefiLlama(cfg, http, paths.raw).sync()["jitosol"] > 100
        assert Fred(cfg, http, paths.raw).sync() > 1000
        assert Jupiter(cfg, http).quote(cfg.depth.mints["jitoSOL"], 1.0) is not None
