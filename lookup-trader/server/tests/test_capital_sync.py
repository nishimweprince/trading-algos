from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from app.providers.base import Candle
from app.providers.capital import CapitalError, CapitalMarketDataClient, HttpResponse
from app.services.capital_sync import CapitalCandleConflict, CapitalCandleSync
from app.services.h4_resample import derive_h4
from app.utils.parquet import month_partition_path

FIXTURE = Path(__file__).parent / "fixtures" / "capital_hourly.json"


class RecordedTransport:
    def __init__(
        self,
        *,
        unauthorized_once: bool = False,
        corrected: bool = False,
        invalid_close_ask: bool = False,
    ):
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.calls = []
        self.unauthorized_once = unauthorized_once
        self.corrected = corrected
        self.invalid_close_ask = invalid_close_ask
        self.sessions = 0

    def __call__(self, method, url, params, headers, body):
        self.calls.append((method, url, params, headers, body))
        if url.endswith("/session"):
            self.sessions += 1
            assert headers == {"X-CAP-API-KEY": "api-key"}
            assert body == {
                "identifier": "user@example.com",
                "password": "custom-password",
                "encryptedPassword": False,
            }
            return HttpResponse(
                200,
                {"cst": f"cst-{self.sessions}", "x-security-token": f"security-{self.sessions}"},
                {},
            )
        if url.endswith("/time"):
            stamp = datetime(2026, 8, 5, 23, 1, 29, tzinfo=UTC).timestamp() * 1000
            return HttpResponse(200, {}, {"serverTime": stamp})
        if self.unauthorized_once and url.endswith("/markets"):
            self.unauthorized_once = False
            return HttpResponse(401, {}, {})
        assert headers["CST"].startswith("cst-")
        assert headers["X-SECURITY-TOKEN"].startswith("security-")
        if url.endswith("/ping"):
            return HttpResponse(200, {}, {"status": "OK"})
        if url.endswith("/markets"):
            return HttpResponse(200, {}, {"markets": self.fixture["markets"]})
        if "/prices/" in url:
            prices = json.loads(json.dumps(self.fixture["prices"]))
            if self.corrected:
                prices[1]["closePrice"]["bid"] += 0.25
            if self.invalid_close_ask:
                prices[1]["closePrice"]["ask"] = prices[1]["closePrice"]["bid"] - 0.34
            return HttpResponse(200, {}, {"prices": prices})
        raise AssertionError(url)


def _client(transport):
    return CapitalMarketDataClient(
        api_key="api-key",
        identifier="user@example.com",
        api_password="custom-password",
        transport=transport,
    )


def _histdata(root: Path) -> None:
    ts = pd.date_range("2026-08-05T18:00:00Z", periods=4, freq="h")
    frame = pd.DataFrame(
        {
            "ts": ts,
            "open": [2397.0, 2398.0, 2399.0, 2400.0],
            "high": [2398.0, 2399.0, 2400.0, 2402.0],
            "low": [2396.0, 2397.0, 2398.0, 2399.0],
            "close": [2397.5, 2398.5, 2399.5, 2401.0],
            "volume": [1.0] * 4,
        }
    )
    path = month_partition_path(root / "candles", "XAUUSD", "H1", 2026, 8)
    path.parent.mkdir(parents=True)
    frame.to_parquet(path, index=False)


def test_market_data_authentication_is_redacted_and_renews_once():
    transport = RecordedTransport(unauthorized_once=True)
    client = _client(transport)
    assert client.search_markets("Gold")[0]["epic"] == "GOLD"
    assert transport.sessions == 2
    public_methods = {name.lower() for name in dir(client) if not name.startswith("_")}
    assert not public_methods & {"positions", "orders", "confirm", "workingorders", "trade"}

    bad = RecordedTransport()
    bad.__call__ = lambda *args: HttpResponse(401, {}, {})  # pragma: no cover
    assert "api-key" not in str(CapitalError("Capital.com authentication failed"))


def test_429_uses_bounded_backoff_and_rate_limit_stays_below_ten_per_second():
    class Clock:
        value = 0.0

        def monotonic(self):
            return self.value

        def sleep(self, seconds):
            self.value += seconds

    clock = Clock()
    attempts = 0
    call_times = []

    def transport(method, url, params, headers, body):
        nonlocal attempts
        call_times.append(clock.value)
        if url.endswith("/session"):
            return HttpResponse(200, {"cst": "cst", "x-security-token": "token"}, {})
        if url.endswith("/markets"):
            attempts += 1
            if attempts < 3:
                return HttpResponse(429, {}, {})
            return HttpResponse(200, {}, {"markets": [{"epic": "GOLD"}]})
        if url.endswith("/time"):
            return HttpResponse(200, {}, {"serverTime": 1_786_000_000_000})
        raise AssertionError(url)

    client = CapitalMarketDataClient(
        api_key="key",
        identifier="identifier",
        api_password="password",
        transport=transport,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert client.search_markets("Gold")[0]["epic"] == "GOLD"
    assert attempts == 3
    for _ in range(12):
        client.server_time()
    assert any(stamp >= 1.0 for stamp in call_times)
    assert (
        max(sum(start <= stamp < start + 1 for stamp in call_times) for start in set(call_times))
        <= 9
    )


def test_market_validation_and_closed_bid_normalization():
    transport = RecordedTransport()
    client = _client(transport)
    assert client.validate_market("GOLD")["instrument_name"] == "Gold"
    with pytest.raises(CapitalError, match="unavailable"):
        client.validate_market("NOT_GOLD")
    candles = client.fetch_closed_hourly(
        "GOLD",
        datetime(2026, 8, 5, 20, tzinfo=UTC),
        datetime(2026, 8, 5, 23, tzinfo=UTC),
        server_time=datetime(2026, 8, 5, 23, 1, 29, tzinfo=UTC),
    )
    assert [candle.ts.hour for candle in candles] == [21, 22]
    assert candles[0].close == 2401.0
    assert candles[0].spread == pytest.approx(0.3)
    price_call = next(call for call in transport.calls if "/prices/" in call[1])
    assert price_call[2]["resolution"] == "HOUR"
    assert int(price_call[2]["max"]) <= 1000


def test_invalid_close_ask_uses_intrabar_spread_without_rejecting_bid_candle():
    client = _client(RecordedTransport(invalid_close_ask=True))
    candles = client.fetch_closed_hourly(
        "GOLD",
        datetime(2026, 8, 5, 20, tzinfo=UTC),
        datetime(2026, 8, 5, 23, tzinfo=UTC),
        server_time=datetime(2026, 8, 5, 23, 1, 29, tzinfo=UTC),
    )
    fallback = next(candle for candle in candles if candle.ts.hour == 22)
    assert fallback.close == 2402.0
    assert fallback.spread == pytest.approx(0.3)
    assert fallback.spread_source == "intrabar_median_fallback"


def test_histdata_boundary_append_is_idempotent_and_corrected_live_bar_quarantines(tmp_path):
    _histdata(tmp_path)
    kwargs = {"symbol": "XAUUSD", "epic": "GOLD"}
    first = CapitalCandleSync(_client(RecordedTransport()), data_dir=tmp_path).sync(**kwargs)
    assert first.published == 1
    assert first.histdata_overlaps == 1
    assert first.histdata_mismatches == 0
    second = CapitalCandleSync(_client(RecordedTransport()), data_dir=tmp_path).sync(**kwargs)
    assert second.published == 0
    assert second.identical_overlaps == 1
    assert (tmp_path / "candle_sources" / "capital_boundary.json").exists()
    assert next((tmp_path / "candle_sources").glob("**/month=*/part-*.parquet")).exists()
    generation_reports = list((tmp_path / "reports" / "capital-sync").glob("*.json"))
    assert len(generation_reports) == 2
    persisted = json.loads(generation_reports[0].read_text(encoding="utf-8"))
    assert persisted["generation"]
    assert persisted["publication"]["request_status"] == "ok"

    with pytest.raises(ValueError, match="overlap changed OHLC"):
        CapitalCandleSync(_client(RecordedTransport(corrected=True)), data_dir=tmp_path).sync(
            **kwargs, dry_run=True
        )

    with pytest.raises(CapitalCandleConflict) as caught:
        CapitalCandleSync(_client(RecordedTransport(corrected=True)), data_dir=tmp_path).sync(
            **kwargs
        )
    assert caught.value.quarantine_path.exists()


def test_sync_rejects_provider_gap_before_publication(tmp_path):
    _histdata(tmp_path)

    class GapClient:
        environment = "demo"

        def validate_market(self, epic):
            return {"epic": epic}

        def server_time(self):
            return datetime(2026, 8, 6, 3, tzinfo=UTC)

        def fetch_closed_hourly(self, epic, start, end, **kwargs):
            return (
                Candle(
                    datetime(2026, 8, 5, 22, tzinfo=UTC),
                    2400.0,
                    2401.0,
                    2399.0,
                    2400.5,
                    1.0,
                    "capital",
                    epic,
                    0.3,
                ),
                Candle(
                    datetime(2026, 8, 6, 1, tzinfo=UTC),
                    2400.5,
                    2402.0,
                    2400.0,
                    2401.0,
                    1.0,
                    "capital",
                    epic,
                    0.3,
                ),
            )

    with pytest.raises(ValueError, match="market-open gap"):
        CapitalCandleSync(GapClient(), data_dir=tmp_path).sync(symbol="XAUUSD", epic="GOLD")
    assert not (tmp_path / "candle_sources" / "capital_boundary.json").exists()


def test_h4_is_deterministically_derived_from_h1():
    ts = pd.date_range("2026-08-05T01:00:00Z", periods=8, freq="h")
    frame = pd.DataFrame(
        {
            "ts": ts,
            "open": range(100, 108),
            "high": range(101, 109),
            "low": range(99, 107),
            "close": [value + 0.5 for value in range(100, 108)],
            "volume": [1.0] * 8,
        }
    )
    result = derive_h4(frame)
    assert list(result["ts"].dt.hour) == [4, 8]
    assert result.iloc[0]["open"] == 100
    assert result.iloc[0]["close"] == 103.5
    assert result.iloc[0]["volume"] == 4.0


def test_sync_pages_hourly_requests_at_capital_maximum(tmp_path):
    _histdata(tmp_path)
    latest = datetime(2026, 8, 5, 21, tzinfo=UTC)

    class PagingClient:
        environment = "demo"

        def __init__(self):
            self.pages = []
            self.epics = []

        def validate_market(self, epic):
            self.epics.append(epic)
            return {"epic": epic}

        def server_time(self):
            return latest + timedelta(hours=1200)

        def fetch_closed_hourly(self, epic, start, end, **kwargs):
            self.epics.append(epic)
            self.pages.append((start, end, kwargs["max_bars"]))
            return ()

    client = PagingClient()
    result = CapitalCandleSync(client, data_dir=tmp_path).sync(symbol="XAUUSD")
    assert result.fetched == 0
    assert result.symbol == "XAUUSD"
    assert result.epic == "GOLD"
    assert set(client.epics) == {"GOLD"}
    assert len(client.pages) == 2
    assert all(max_bars == 1000 for _, _, max_bars in client.pages)
