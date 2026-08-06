from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from app.config import settings
from app.providers.base import Candle, CandlePage
from app.providers.oanda import OandaV20Provider
from app.services.data_health import data_model_health
from app.services.oanda_sync import (
    CandleConflictError,
    OandaCandleSync,
    SyncLockedError,
    single_writer_lock,
    storage_to_oanda,
)

FIXTURE = Path(__file__).parent / "fixtures" / "oanda_h1_pages.json"
UTC = UTC


def _recording_transport():
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    calls: list[tuple[str, dict[str, str]]] = []
    pages = iter(recorded["pages"])

    def transport(url: str, params: dict[str, str], headers: dict[str, str]):
        calls.append((url, params))
        assert headers["Authorization"] == "Bearer test-token"
        if url.endswith("/instruments"):
            return {"instruments": recorded["instruments"]}
        return next(pages, {"candles": []})

    return transport, calls


def test_oanda_normalizes_symbol_utc_and_rejects_incomplete_candles():
    transport, calls = _recording_transport()
    provider = OandaV20Provider(
        token="test-token", account_id="test-account", transport=transport
    )
    provider.validate_instrument(storage_to_oanda("XAUUSD"))
    page = provider.fetch_candles_page(
        "XAU_USD",
        "H1",
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 8, 1, 4, tzinfo=UTC),
    )

    assert len(page.candles) == 1
    assert page.candles[0].ts == datetime(2026, 8, 1, 1, tzinfo=UTC)
    assert page.candles[0].close == pytest.approx(2401.8)
    candle_call = calls[-1]
    assert candle_call[0].endswith("/v3/instruments/XAU_USD/candles")
    assert candle_call[1]["price"] == "M"
    assert candle_call[1]["alignmentTimezone"] == "UTC"
    assert candle_call[1]["dailyAlignment"] == "0"


def test_recorded_pages_are_backfilled_until_end(tmp_path):
    transport, calls = _recording_transport()
    provider = OandaV20Provider(
        token="test-token", account_id="test-account", transport=transport
    )
    result = OandaCandleSync(provider, tmp_path / "candles").sync(
        symbol="XAUUSD",
        timeframe="H1",
        start=datetime(2026, 8, 1, tzinfo=UTC),
        end=datetime(2026, 8, 1, 4, tzinfo=UTC),
        dry_run=True,
    )
    assert result.fetched == 2
    assert len([url for url, _ in calls if url.endswith("/candles")]) == 3


class FakeProvider:
    name = "oanda_v20"

    def __init__(self, close_offset: float = 0) -> None:
        self.close_offset = close_offset

    def validate_instrument(self, instrument: str) -> None:
        assert instrument == "XAU_USD"

    def fetch_candles_page(self, instrument, timeframe, start, end):
        base = datetime(2026, 8, 1, 1, tzinfo=UTC)
        candles = []
        for index in range(2):
            open_ = 2400.0 + index
            close = 2400.5 + index + self.close_offset
            candles.append(
                Candle(
                    ts=base + timedelta(hours=index),
                    open=open_,
                    high=max(open_, close) + 1,
                    low=min(open_, close) - 1,
                    close=close,
                    volume=100 + index,
                    provider=self.name,
                    source_instrument=instrument,
                )
            )
        return CandlePage(tuple(candles), None)


def test_monthly_upsert_is_idempotent_and_conflicts_are_quarantined(tmp_path):
    root = tmp_path / "candles"
    sync = OandaCandleSync(FakeProvider(), root)
    kwargs = {
        "symbol": "XAUUSD",
        "timeframe": "H1",
        "start": datetime(2026, 8, 1, tzinfo=UTC),
        "end": datetime(2026, 8, 1, 4, tzinfo=UTC),
    }
    first = sync.sync(**kwargs)
    second = sync.sync(**kwargs)
    assert first.published == 2
    assert second.published == 0
    assert second.identical_overlaps == 2

    candle_path = next(root.glob("**/month=*/part-*.parquet"))
    assert len(pd.read_parquet(candle_path)) == 2
    provenance_path = next((tmp_path / "candle_sources").glob("**/month=*/part-*.parquet"))
    assert set(pd.read_parquet(provenance_path)["provider"]) == {"oanda_v20"}

    with pytest.raises(CandleConflictError) as caught:
        OandaCandleSync(FakeProvider(close_offset=0.25), root).sync(**kwargs)
    assert caught.value.quarantine_path.exists()
    assert len(pd.read_parquet(candle_path)) == 2


def test_single_writer_lock_rejects_contention(tmp_path):
    lock = tmp_path / ".sync.lock"
    with ExitStack() as stack:
        stack.enter_context(single_writer_lock(lock))
        with pytest.raises(SyncLockedError):
            stack.enter_context(single_writer_lock(lock))


def test_data_model_health_reports_missing_and_ready_states(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    model_root = data_dir / "models" / "outcome"
    monkeypatch.setattr(settings, "data_dir", data_dir)
    monkeypatch.setattr(settings, "outcome_artifact_root", model_root)
    monkeypatch.setattr(settings, "outcome_artifact_version", "health-test")

    empty = data_model_health(datetime(2026, 8, 1, 2, tzinfo=UTC))
    assert empty["status"] == "degraded"
    assert empty["model"]["status"] == "missing"
    assert empty["tag_parity"]["status"] == "unavailable"

    candle_path = (
        data_dir
        / "candles"
        / "symbol=XAUUSD"
        / "timeframe=H1"
        / "year=2026"
        / "month=08"
        / "part-000.parquet"
    )
    candle_path.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "ts": [pd.Timestamp("2026-08-01T01:00:00Z")],
            "open": [2400.0],
            "high": [2401.0],
            "low": [2399.0],
            "close": [2400.5],
            "volume": [100.0],
        }
    ).to_parquet(candle_path, index=False)

    artifact = model_root / "health-test"
    artifact.mkdir(parents=True)
    (artifact / "model.joblib").write_bytes(b"recorded-model")
    (artifact / "metadata.json").write_text(
        '{"artifact_version":"health-test","outcome_feature_version":"1"}',
        encoding="utf-8",
    )
    (artifact / "metrics.json").write_text("{}", encoding="utf-8")
    (artifact / "dataset_manifest.json").write_text("{}", encoding="utf-8")

    ready = data_model_health(datetime(2026, 8, 1, 2, tzinfo=UTC))
    assert ready["status"] == "ok"
    assert ready["candles"]["latest_complete_candle"] == "2026-08-01T01:00:00+00:00"
    assert ready["candles"]["lag_seconds"] == 3600
    assert ready["model"]["status"] == "ready"
