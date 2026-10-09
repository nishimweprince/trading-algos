from __future__ import annotations

import hashlib
import io
import zipfile

import httpx
import pandas as pd
import pytest
from conftest import DAY, HOUR, kline_zip, klines, ms

from zone_trend.data import vision
from zone_trend.data.parse import read_archive
from zone_trend.data.store import load, month_meta
from zone_trend.data.sync import needs_build, sync, sync_series

NS = 'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'


def listing(keys: list[str], prefixes: list[str] = (), truncated=False, marker=None) -> str:
    body = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys)
    body += "".join(f"<CommonPrefixes><Prefix>{p}</Prefix></CommonPrefixes>" for p in prefixes)
    nm = f"<NextMarker>{marker}</NextMarker>" if marker else ""
    return f"<ListBucketResult {NS}><IsTruncated>{str(truncated).lower()}</IsTruncated>{nm}{body}</ListBucketResult>"  # noqa: E501


class FakeVision:
    """Serves an S3 listing and checksummed zips from an in-memory key -> bytes map."""

    def __init__(self, files: dict[str, bytes], bad_checksum: set[str] = frozenset()):
        self.files = files
        self.bad = bad_checksum
        self.downloads: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("s3"):
            pfx = request.url.params["prefix"]
            keys, prefixes = [], set()
            for key in self.files:
                if key.startswith(pfx):
                    rest = key[len(pfx) :]
                    if "/" in rest:
                        prefixes.add(pfx + rest.split("/", 1)[0] + "/")
                    else:
                        keys += [key, key + ".CHECKSUM"]
            return httpx.Response(200, text=listing(sorted(keys), sorted(prefixes)))
        key = request.url.path.lstrip("/")
        if key.endswith(".CHECKSUM"):
            name = key.removesuffix(".CHECKSUM")
            digest = "0" * 64 if name in self.bad else hashlib.sha256(self.files[name]).hexdigest()
            return httpx.Response(200, text=f"{digest}  {name.rsplit('/', 1)[-1]}")
        if key not in self.files:
            return httpx.Response(404)
        self.downloads.append(key)
        return httpx.Response(200, content=self.files[key])

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def mkey(dataset, symbol, interval, month):
    tail = f"{interval}/" if interval else ""
    name = f"{symbol}-{interval or dataset}-{month}.zip"
    return f"data/futures/um/monthly/{dataset}/{symbol}/{tail}{name}"


def dkey(dataset, symbol, interval, day):
    return f"data/futures/um/daily/{dataset}/{symbol}/{interval}/{symbol}-{interval}-{day}.zip"


def test_parse_listing_returns_prefixes_and_marker():
    keys, prefixes, marker = vision.parse_listing(listing(["a.zip"], ["p/"], True, "m"))
    assert (keys, prefixes, marker) == (["a.zip"], ["p/"], "m")
    assert vision.parse_listing(listing([], [], True))[2] is None


def test_prefix_requires_interval_for_klines_only():
    assert vision.prefix("klines", "BTCUSDT", "1h", "monthly").endswith("klines/BTCUSDT/1h/")
    assert vision.prefix("fundingRate", "BTCUSDT", None, "monthly").endswith("BTCUSDT/")
    with pytest.raises(ValueError):
        vision.prefix("klines", "BTCUSDT", None, "monthly")


def test_list_symbols_includes_every_directory():
    fake = FakeVision({mkey("klines", s, "1d", "2024-01"): b"x" for s in ("BTCUSDT", "LUNAUSDT")})
    with fake.client() as client:
        assert vision.list_symbols(client) == ["BTCUSDT", "LUNAUSDT"]


def test_plan_uses_daily_files_only_where_monthly_is_missing():
    files = {
        mkey("klines", "BTCUSDT", "1h", "2024-01"): b"",
        dkey("klines", "BTCUSDT", "1h", "2024-01-31"): b"",  # superseded by the monthly
        dkey("klines", "BTCUSDT", "1h", "2024-02-01"): b"",
        dkey("klines", "BTCUSDT", "1h", "2024-02-02"): b"",
        mkey("klines", "BTCUSDT", "1h", "2023-12"): b"",  # before start
    }
    with FakeVision(files).client() as client:
        plans = vision.plan_months(client, "klines", "BTCUSDT", "1h", "2024-01")
    assert [(p.month, p.source, len(p.keys)) for p in plans] == [
        ("2024-01", "monthly", 1),
        ("2024-02", "daily", 2),
    ]


def test_download_rejects_a_checksum_mismatch(tmp_path):
    key = mkey("klines", "BTCUSDT", "1h", "2024-01")
    fake = FakeVision({key: b"zip"}, bad_checksum={key})
    with fake.client() as client, pytest.raises(vision.ChecksumError):
        vision.download(client, key, tmp_path)
    assert not (tmp_path / key).exists()


@pytest.mark.parametrize("header", [True, False])
def test_read_archive_handles_files_with_and_without_header(tmp_path, header):
    df = klines("2024-01-01", 5, HOUR)
    path = tmp_path / "a.zip"
    path.write_bytes(kline_zip(df, header))
    out = read_archive(path, "klines")
    assert out["open_time"].tolist() == df["open_time"].tolist()
    assert out["open_time"].dtype == "int64" and out["close"].dtype == "float64"
    assert str(out["open_dt"].dt.tz) == "UTC"


def test_read_archive_converts_microsecond_timestamps(tmp_path):
    df = klines("2025-02-01", 3, HOUR)
    df["open_time"] *= 1000
    df["close_time"] *= 1000
    path = tmp_path / "a.zip"
    path.write_bytes(kline_zip(df, True))
    assert read_archive(path, "klines")["open_time"].iloc[0] == ms("2025-02-01")


def test_read_funding_rounds_settlement_jitter(tmp_path):
    buf = io.BytesIO()
    csv = "calc_time,funding_interval_hours,last_funding_rate\n1748736000001,8,-0.00000582\n"
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("f.csv", csv)
    path = tmp_path / "f.zip"
    path.write_bytes(buf.getvalue())
    out = read_archive(path, "fundingRate")
    assert out["settle_time"].iloc[0] == 1748736000000
    assert out["last_funding_rate"].iloc[0] == pytest.approx(-0.00000582)


@pytest.mark.parametrize(
    "meta, source, n, expected",
    [
        (None, "monthly", 1, True),
        ({"source": "monthly", "files": 1}, "monthly", 1, False),
        ({"source": "daily", "files": 3}, "monthly", 1, True),
        ({"source": "daily", "files": 3}, "daily", 3, False),
        ({"source": "daily", "files": 3}, "daily", 4, True),
    ],
)
def test_needs_build(meta, source, n, expected):
    plan = vision.MonthPlan("2024-02", source, tuple(f"k{i}" for i in range(n)))
    assert needs_build(meta, plan) is expected


def test_sync_series_builds_once_then_upgrades_daily_months(paths):
    jan = klines("2024-01-01", 31 * 24, HOUR)
    feb1 = klines("2024-02-01", 24, HOUR)
    files = {
        mkey("klines", "BTCUSDT", "1h", "2024-01"): kline_zip(jan, False),
        dkey("klines", "BTCUSDT", "1h", "2024-02-01"): kline_zip(feb1, True),
    }
    fake = FakeVision(files)
    with fake.client() as client:
        first = sync_series(client, paths, "klines", "BTCUSDT", "1h", "2024-01")
        again = sync_series(client, paths, "klines", "BTCUSDT", "1h", "2024-01")
        assert first.built == ["2024-01", "2024-02"] and again.built == []

        feb = klines("2024-02-01", 29 * 24, HOUR)
        fake.files[mkey("klines", "BTCUSDT", "1h", "2024-02")] = kline_zip(feb, True)
        upgraded = sync_series(client, paths, "klines", "BTCUSDT", "1h", "2024-01")
    assert upgraded.built == ["2024-02"]
    assert month_meta(paths.month_file("klines", "BTCUSDT", "1h", "2024-02"))["source"] == "monthly"
    out = load(paths, "klines", "BTCUSDT", "1h")
    assert len(out) == (31 + 29) * 24 and out["open_time"].is_monotonic_increasing
    clipped = load(paths, "klines", "BTCUSDT", "1h", ms("2024-02-01"), ms("2024-02-01") + DAY)
    assert len(clipped) == 24


def test_sync_reports_failures_without_stopping(paths):
    good = mkey("klines", "BTCUSDT", "1d", "2024-01")
    bad = mkey("klines", "ETHUSDT", "1d", "2024-01")
    df = klines("2024-01-01", 31, DAY)
    fake = FakeVision({good: kline_zip(df, True), bad: kline_zip(df, True)}, bad_checksum={bad})
    results = sync(
        paths, ["BTCUSDT", "ETHUSDT"], {"klines": ("1d",)}, "2024-01", client_factory=fake.client
    )
    by_symbol = {r.symbol: r for r in results}
    assert by_symbol["BTCUSDT"].built == ["2024-01"] and by_symbol["BTCUSDT"].error is None
    assert "checksum" in by_symbol["ETHUSDT"].error
    assert isinstance(load(paths, "klines", "BTCUSDT", "1d"), pd.DataFrame)
