"""The Helix tape path on a synthetic ``.hlxz``: reader against recv3, the Kalshi
contract switch, resolutions, the footer scan, and the replay's window scan."""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

zstd = pytest.importorskip("zstandard")

from poly_kalshi_service.book import Quote  # noqa: E402
from poly_kalshi_service.decision import StrategyParams, decide  # noqa: E402
from research.helix import inventory, reader, recv3  # noqa: E402
from research.helix_replay import Leg, Variant, scan_window  # noqa: E402

NS = 1_000_000_000
OPEN = 1_789_862_400
TOKEN_A = "21526639888734313115391373365824865053958089617297749853672342685426162050348"
# Shares TOKEN_A's first 8 characters: the lookup must compare all 24 key bytes.
TOKEN_B = "21526639" + "1" * 69


def _container(path: Path, blocks: list[tuple[int, int, list[bytes]]], footer: bool = True) -> None:
    """``blocks``: (tag, record_size, records). Writes FileHeader, blocks, footer index."""
    body = bytearray(recv3._FILE_HDR.pack(recv3._FILE_MAGIC, 1, 32, 0, 0, 0, 1 if footer else 0, 0))
    index = []
    for tag, rsz, records in blocks:
        raw = b"".join(records)
        comp = zstd.ZstdCompressor().compress(raw)
        off = len(body)
        body += recv3._BLK_HDR.pack(recv3._BLK_MAGIC, tag, 0, rsz, len(records), len(comp),
                                    len(raw), 0, 0)  # fmt: skip
        body += comp
        index.append(recv3._IDX_ENT.pack(off, len(comp), len(records), tag, b"\0\0\0", 0, 0))
    if footer:
        ix = b"".join(index)
        body += ix + recv3._TRAILER.pack(recv3._FOOT_MAGIC, len(index), len(ix), 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(body))


def _poly(
    wall_ns: int, token: str, bid: float, ask: float, bsz: float = 10, asz: float = 20
) -> bytes:
    e8 = lambda x: round(x * 1e8)  # noqa: E731
    return recv3.POLY_BBO.pack(wall_ns - 5, wall_ns, 0, e8(bid), e8(bsz), e8(ask), e8(asz),
                               b"cond", token.encode(), 0)  # fmt: skip


def _quote(wall_ns: int, aid: int, bid: float, ask: float, seq: int) -> bytes:
    return recv3.QUOTE_V3.pack(bid, ask, 5.0, 6.0, wall_ns - 5, wall_ns, seq, 31, 1, aid, 1)


def test_poly_series_matches_recv3_and_dedups_repeats(tmp_path: Path) -> None:
    t = OPEN * NS
    records = [
        _poly(t + 3 * NS, TOKEN_A, 0.50, 0.51),
        _poly(t + 1 * NS, TOKEN_A, 0.49, 0.51),  # block order is not time order
        _poly(t + 2 * NS, TOKEN_B, 0.30, 0.32),
        _poly(t + 4 * NS, TOKEN_A, 0.50, 0.51),  # repeats the touch: dropped
        _poly(t + 901 * NS, TOKEN_A, 0.50, 0.51),  # repeats, but a new 900 s window: kept
    ]
    _container(tmp_path / "Polymarket_CLOB_Yes_BBO" / "20260920.hlxz", [(27, 224, records)])
    typed = list(recv3.iter_poly_bbo(str(tmp_path / "Polymarket_CLOB_Yes_BBO" / "20260920.hlxz")))
    assert len(typed) == 5 and typed[0].ask_px == 0.51
    s = reader.poly_bbo_series("Polymarket_CLOB_Yes_BBO", "20260920", [TOKEN_A, TOKEN_B], tmp_path)
    assert s.raw_rows == 5
    a = s.rows(0)
    assert list(s.cols["t"][a]) == [t + 1 * NS, t + 3 * NS, t + 901 * NS]
    assert list(s.cols["bid"][a]) == [4900, 5000, 5000] and s.cols["ask"][a][0] == 5100
    assert s.cols["asz"][a][0] == 20.0
    assert list(s.cols["bid"][s.rows(1)]) == [3000]
    raw = reader.poly_bbo_series(
        "Polymarket_CLOB_Yes_BBO", "20260920", [TOKEN_A], tmp_path, dedup=False
    )
    assert len(raw) == 4  # TOKEN_B's row is not asked for


def test_kalshi_switch_starts_at_the_seq_restart() -> None:
    o = OPEN * NS
    t = np.array([o - NS, o + NS // 10, o + 2 * NS, o + 3 * NS])
    seq = np.array([40_000, 40_001, 1, 2])  # the old contract outlives the open, then restarts
    assert reader.kalshi_contract_start(t, seq, o) == 2
    busy = np.array([290_000, 290_005, 1_363, 1_364])
    assert reader.kalshi_contract_start(t, busy, o) == 2
    assert reader.kalshi_contract_start(t, np.array([5, 6, 7, 8]), o) == 1  # no break: the open


def test_kalshi_series_and_resolutions_and_footer_scan(tmp_path: Path) -> None:
    t = OPEN * NS
    quotes = [_quote(t + k * NS, 1, 0.40, 0.42, k + 1) for k in range(3)]
    quotes.append(_quote(t, 3, 0.70, 0.71, 1))
    _container(tmp_path / "Kalshi_Yes_BBO" / "20260920.hlxz", [(43, 64, quotes)])
    k = reader.kalshi_quote_series("Kalshi_Yes_BBO", "20260920", [1, 3], tmp_path, dedup=False)
    assert len(k.cols["t"][k.rows(0)]) == 3 and list(k.cols["ask"][k.rows(1)]) == [7100]
    res = struct.pack("<QQ64sdBBH4x", t + 904 * NS, OPEN, b"abc", 81187.86, 0xFF, 1, 1)
    window = recv3.POLY_WINDOW.pack(t, OPEN, b"abc", TOKEN_A.encode(), TOKEN_B.encode(), 81000.0, 1)
    path = tmp_path / "Polymarket_Window" / "20260920.hlxz"
    _container(path, [(46, 256, [window, window]), (47, 96, [res])])
    (r,) = reader.iter_resolutions("20260920", tmp_path)
    assert (r.open_unix, r.close_px, r.asset, r.condition_id) == (OPEN, 81187.86, 1, "abc")
    assert len(list(reader.iter_windows("20260920", tmp_path))) == 1  # the repeat is dropped
    scan = inventory.scan_headers(path)
    assert scan["records"] == 3 and scan["subfiles"] == 1 and scan["tags"] == {46: 2, 47: 1}
    unindexed = tmp_path / "x.hlxz"
    _container(unindexed, [(46, 256, [window])], footer=False)
    assert inventory.scan_headers(unindexed)["records"] == 1  # walked, not read from a footer


def _leg(rows: list[tuple[float, float, float]]) -> Leg:
    t = np.array([int((OPEN + s) * NS) for s, _b, _a in rows], dtype=np.int64)
    bid = np.array([round(b * 1e4) for _s, b, _a in rows], dtype=np.int16)
    ask = np.array([round(a * 1e4) for _s, _b, a in rows], dtype=np.int16)
    size = np.full(len(rows), 50.0, dtype=np.float32)
    return Leg(t, bid, ask, size, size)


def test_scan_window_takes_decides_first_consuming_tick() -> None:
    k = _leg([(1, 0.50, 0.52), (30, 0.60, 0.62), (40, 0.70, 0.72)])
    y = _leg([(1, 0.49, 0.51), (50, 0.40, 0.41)])
    n = _leg([(1, 0.49, 0.51)])
    base = StrategyParams(gap_threshold=0.10)
    variants = (Variant("base", base), Variant("t20", StrategyParams(gap_threshold=0.20)))
    found, stats = scan_window(k, y, n, OPEN, variants)
    by = {f["variant"]: f for f in found}
    # Gap 0.11 at 30 s (0.61 - 0.50): base buys YES at 0.51.
    assert by["base"]["sec_into"] == 30 and by["base"]["entry_px"] == 0.51
    assert by["base"]["side"] == "yes" and by["base"]["touch_size"] == 50.0
    # 0.20 is reached at 50 s (0.71 - 0.405 = 0.305), first at 40 s (0.71 - 0.50 = 0.21).
    assert by["t20"]["sec_into"] == 40
    expected = decide(Quote(True, 0.60, 0.62), Quote(True, 0.49, 0.51), Quote(True, 0.49, 0.51),
                      base, 30.0, False)  # fmt: skip
    assert (by["base"]["action"], by["base"]["entry_px"]) == (
        expected.action.value,
        expected.entry_px,
    )
    assert stats["first_cross_s_10"] == 30 and stats["first_cross_side_10"] == "yes"
    assert stats["max_abs_gap_full"] == pytest.approx(0.305)
    empty = Leg(
        *(np.array([], dtype=d) for d in (np.int64, np.int16, np.int16, np.float32, np.float32))
    )
    assert scan_window(empty, y, n, OPEN, variants)[0] == []  # a leg with no rows never fires
