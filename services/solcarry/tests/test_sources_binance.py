from __future__ import annotations

import hashlib

import httpx
import pytest
import respx

from solcarry.http import Http
from solcarry.sources.binance_vision import (
    FUNDING_COLS,
    KLINE_COLS,
    BinanceVision,
    ChecksumError,
    Dataset,
    parse_s3_listing,
    quarterly_expiry,
    read_zip_csv,
)

from .conftest import make_zip

S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
BASE = "https://data.binance.vision/data"
FUND_CSV = (
    "calc_time,funding_interval_hours,last_funding_rate\n"
    "1704067200000,8,0.0001\n1704096000000,8,-0.0002\n"
)


def test_parse_listing(fixtures):
    keys, common, trunc, nxt = parse_s3_listing((fixtures / "s3_listing.xml").read_text())
    assert keys == []
    assert "data/futures/cm/monthly/klines/SOLUSD_240927/" in common
    assert not trunc and nxt is None


@respx.mock
def test_discover_quarterlies(cfg, paths, fixtures):
    respx.get(S3).mock(
        return_value=httpx.Response(200, text=(fixtures / "s3_listing.xml").read_text())
    )
    bv = BinanceVision(cfg, Http(cfg.http), paths.raw)
    assert bv.discover_quarterlies() == ["SOLUSD_240927", "SOLUSD_241227"]


def test_expiry():
    ts = quarterly_expiry("SOLUSD_250328", 8)
    assert ts.isoformat() == "2025-03-28T08:00:00+00:00"


@respx.mock
def test_download_good_checksum(cfg, paths):
    z = make_zip("SOLUSDT-fundingRate-2024-01.csv", FUND_CSV)
    rel = "futures/um/monthly/fundingRate/SOLUSDT/SOLUSDT-fundingRate-2024-01.zip"
    respx.get(f"{BASE}/{rel}").mock(return_value=httpx.Response(200, content=z))
    respx.get(f"{BASE}/{rel}.CHECKSUM").mock(
        return_value=httpx.Response(200, text=f"{hashlib.sha256(z).hexdigest()}  x.zip\n")
    )
    bv = BinanceVision(cfg, Http(cfg.http), paths.raw)
    got, rows = bv.download("data/" + rel)
    assert got == rel and rows == 3
    assert bv.cache.has(rel)
    # idempotent: second call makes no request
    respx.reset()
    assert bv.download("data/" + rel)[0] == rel
    df = bv.load(Dataset("funding", "futures/um/monthly/fundingRate/SOLUSDT/", None, FUNDING_COLS))
    assert list(df.columns) == FUNDING_COLS and len(df) == 2


@respx.mock
def test_download_bad_checksum(cfg, paths):
    z = make_zip("a.csv", FUND_CSV)
    rel = "futures/um/monthly/fundingRate/SOLUSDT/SOLUSDT-fundingRate-2024-02.zip"
    respx.get(f"{BASE}/{rel}").mock(return_value=httpx.Response(200, content=z))
    respx.get(f"{BASE}/{rel}.CHECKSUM").mock(
        return_value=httpx.Response(200, text="deadbeef  a.zip")
    )
    bv = BinanceVision(cfg, Http(cfg.http), paths.raw)
    with pytest.raises(ChecksumError):
        bv.download("data/" + rel)


@respx.mock
def test_404_is_gap(cfg, paths):
    rel = "spot/monthly/klines/SOLUSDT/1h/SOLUSDT-1h-2099-01.zip"
    respx.get(f"{BASE}/{rel}").mock(return_value=httpx.Response(404))
    bv = BinanceVision(cfg, Http(cfg.http), paths.raw)
    assert bv.download("data/" + rel) == (rel, None)
    assert bv.cache.is_gap(rel)


def test_read_zip_headerless_klines():
    text = "1704067200000,100,101,99,100.5,10,1704070799999,1000,5,5,500,0\n"
    df = read_zip_csv(make_zip("k.csv", text), KLINE_COLS)
    assert list(df.columns) == KLINE_COLS
    assert df["close"].iloc[0] == 100.5
