from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from ipda.candles import Candle
from ipda.ipda_model import NY
from ta_contracts.signals import SignalRequest
from test_ipda_model import DAY, KZ, PRE, _bar, _local
from test_service import FakeMt5Client, FakeNotifier, _freeze, _service, _settings

IPDA_ENV = {
    "STRATEGY": "ipda",
    "DATA_LOOKBACK": 1440,
    "PIP_SIZE": 0.1,
    "PRICE_DIGITS": 2,
    "TRADING_SESSIONS": "",
}


def _daily() -> list[Candle]:
    """D1 bars for the 21 trading days before DAY; each starts 17:00 NY the evening before."""
    out = []
    for i in range(21, 0, -1):
        key = DAY - timedelta(days=i)
        start = datetime.combine(key - timedelta(days=1), datetime.min.time(), tzinfo=NY)
        start = start.replace(hour=17).astimezone(UTC)
        high, low = (2010.0, 1990.0) if i == 1 else (2100.0, 1900.0)
        out.append(Candle(start, 2000.0, high, low, 2000.0, 0.0))
    return out


def _minutes(kz_bars: int) -> list[Candle]:
    feed = []
    for hh in (20, 21):
        s = _local(DAY - timedelta(days=1), hh, 0).astimezone(UTC)
        feed.append(Candle(s, 1996.0, 1998.0, 1994.0, 1996.0, 0.0))
    for hh, mm, *ohlc in PRE + KZ[:kz_bars]:
        feed += _bar(_local(DAY, hh, mm), *ohlc)
    return feed


class IpdaDataClient:
    def __init__(self) -> None:
        self.minutes: list[Candle] = []
        self.daily_calls = 0

    async def fetch_minute_candles(self, quote: str) -> list[Candle]:
        return self.minutes

    async def fetch_daily_candles(self, quote: str, count: int) -> list[Candle]:
        self.daily_calls += 1
        return _daily()[-count:]


async def test_ipda_sends_one_limit_order_after_warm_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, **IPDA_ENV)
    data, mt5 = IpdaDataClient(), FakeMt5Client()
    svc = _service(tmp_path, settings, data, mt5, FakeNotifier())  # type: ignore[arg-type]
    _freeze(monkeypatch, _local(DAY, 2, 15).astimezone(UTC) + timedelta(seconds=20))

    data.minutes = _minutes(4)  # warm-up poll: history only
    await svc.tick()
    assert mt5.submitted == []
    assert data.daily_calls == 1

    data.minutes = _minutes(5)  # the 02:12 bar closes: sweep + shift + FVG
    await svc.tick()
    assert len(mt5.submitted) == 1
    payload: dict[str, Any] = mt5.submitted[0]
    assert payload["execution_type"] == "limit"
    assert payload["direction"] == "buy"
    assert payload["entry_price"] == "1993.00"
    assert payload["stop_loss"] == "1988.00"
    assert payload["take_profit"] == "2003.00"
    assert payload["expires_at"] == _local(DAY, 5, 0).astimezone(UTC).isoformat()
    assert "stop_loss_distance" not in payload
    SignalRequest.model_validate(payload)  # the execution service's own contract

    await svc.tick()  # same minutes again: nothing new, no re-seed
    assert len(mt5.submitted) == 1
    assert data.daily_calls == 1


async def test_ipda_drops_setups_found_during_warm_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, **IPDA_ENV)
    data, mt5 = IpdaDataClient(), FakeMt5Client()
    svc = _service(tmp_path, settings, data, mt5, FakeNotifier())  # type: ignore[arg-type]
    _freeze(monkeypatch, _local(DAY, 2, 30).astimezone(UTC))
    data.minutes = _minutes(5)  # the setup is already in the warm-up history
    await svc.tick()
    await svc.tick()
    assert mt5.submitted == []


def test_ipda_requires_a_day_of_lookback(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="DATA_LOOKBACK"):
        _settings(tmp_path, **{**IPDA_ENV, "DATA_LOOKBACK": 600})


def test_ipda_is_exempt_from_hard_targets_but_reversal_is_not(tmp_path: Path) -> None:
    _settings(tmp_path, **{**IPDA_ENV, "USE_HARD_TARGETS": False})
    with pytest.raises(ValueError, match="USE_HARD_TARGETS"):
        _settings(tmp_path, USE_HARD_TARGETS=False)


def test_unknown_killzone_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="IPDA_KILLZONES"):
        _settings(tmp_path, **{**IPDA_ENV, "IPDA_KILLZONES": "london,asia"})
