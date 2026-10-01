"""The real MT5 provider plugin behind the real app, with only the terminal faked."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from ta_contracts import MarketKind
from ta_plugin_mt5.market_data import MT5MarketData
from ta_plugin_mt5.terminal import TickSnapshot
from ta_plugin_mt5.testing import FakeMT5Adapter

from market_data_service.api import create_app
from market_data_service.config import MarketBinding
from tests.conftest import AUTH, build_settings

OFFSET = 7200  # broker server at UTC+2


def test_deriv_terminal_serves_utc_candles_and_quotes(tmp_path: Path) -> None:
    symbols = tmp_path / "symbols.json"
    symbols.write_text(json.dumps([{"quote": "Volatility 75 Index"}]), encoding="utf-8")
    settings = build_settings(
        tmp_path,
        SYMBOLS_FILE=symbols,
        MT5_SERVER_UTC_OFFSET_SECONDS=OFFSET,
        MT5_QUOTE_POLL_SECONDS=60,
        markets={MarketKind.DERIV: MarketBinding(provider="mt5")},
    )
    adapter = FakeMT5Adapter()
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    newest_closed_start = now - timedelta(minutes=2)
    adapter.rates = [
        {
            "time": int((newest_closed_start - timedelta(minutes=i)).timestamp()) + OFFSET,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 7,
        }
        for i in reversed(range(3))
    ] + [
        {  # the bar still forming
            "time": int(now.timestamp()) + OFFSET,
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "volume": 1,
        }
    ]
    adapter.tick = TickSnapshot(bid=100.1, ask=100.3, time=int(now.timestamp()) + OFFSET)
    app = create_app(settings, providers={"mt5": MT5MarketData(adapter, settings)})

    with TestClient(app) as client:
        candles = client.get(
            "/v1/deriv/candles",
            params={"symbol": "Volatility 75 Index", "timeframe": "M1", "count": 3},
            headers=AUTH,
        )
        quote = client.get("/v1/deriv/tick", params={"symbol": "Volatility 75 Index"}, headers=AUTH)
        ready = client.get("/health/ready")

    assert candles.status_code == 200, candles.text
    stamps = [datetime.fromisoformat(c["ts"]) for c in candles.json()["candles"]]
    assert stamps[-1] == newest_closed_start + timedelta(minutes=1)
    assert len(stamps) == 3
    assert quote.status_code == 200
    assert datetime.fromisoformat(quote.json()["ts"]) == now
    assert quote.json()["source_instrument"] == "Volatility 75 Index"
    assert ready.status_code == 200
