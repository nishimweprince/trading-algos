from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from research.collect.vision import coverage, download, parse_listing

NS = 'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'


def page(keys: list[str], truncated: bool, marker: str | None = None) -> str:
    contents = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys)
    next_marker = f"<NextMarker>{marker}</NextMarker>" if marker else ""
    return (
        f"<ListBucketResult {NS}><IsTruncated>{str(truncated).lower()}</IsTruncated>"
        f"{next_marker}{contents}</ListBucketResult>"
    )


def test_parse_listing_skips_checksums_and_follows_marker() -> None:
    keys, marker = parse_listing(
        page(["a/X-aggTrades-2026-09-01.zip", "a/X-aggTrades-2026-09-01.zip.CHECKSUM"], True, "m")
    )
    assert keys == ["a/X-aggTrades-2026-09-01.zip"] and marker == "m"
    assert parse_listing(page([], False)) == ([], None)


def test_coverage_pages_through_listing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        prefix = request.url.params["prefix"]
        if "bookTicker" in prefix:
            if "marker" not in request.url.params:
                return httpx.Response(
                    200, text=page([prefix + "B-bookTicker-2023-05-16.zip"], True, "x")
                )
            return httpx.Response(200, text=page([prefix + "B-bookTicker-2024-03-30.zip"], False))
        return httpx.Response(200, text=page([], False))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = coverage(client, ["BTCUSDT"])
    assert report["bookTicker"]["BTCUSDT"] == {
        "first": "2023-05-16",
        "last": "2024-03-30",
        "files": 2,
    }
    assert report["aggTrades"]["BTCUSDT"] is None


def test_download_verifies_checksum(tmp_path: Path) -> None:
    body = b"zipbytes"
    good = hashlib.sha256(body).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".CHECKSUM"):
            name = request.url.path.rsplit("/", 1)[-1].removesuffix(".CHECKSUM")
            digest = good if "2026-09-01" in name else "0" * 64
            return httpx.Response(200, text=f"{digest}  {name}")
        if "2026-09-03" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, content=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        path = download(client, "aggTrades", "BTCUSDT", "2026-09-01", tmp_path)
        assert path is not None and path.read_bytes() == body
        assert download(client, "aggTrades", "BTCUSDT", "2026-09-03", tmp_path) is None
        with pytest.raises(ValueError, match="checksum"):
            download(client, "aggTrades", "BTCUSDT", "2026-09-02", tmp_path)
