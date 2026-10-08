"""PLAN phases 1 and 2: uncensored gaps, shadow variants, depth and markouts, the
small fixes, sharded Polymarket reconnects, the gzip-child recorder, discovery."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import (
    OPEN,
    at,
    ctl,
    kalshi_delta,
    kalshi_snapshot,
    make_engine,
    make_settings,
    poly_book,
    poly_change,
    setup_window,
    window,
)
from pydantic import ValidationError

from poly_kalshi_service.book import Quote
from poly_kalshi_service.build import config_hash
from poly_kalshi_service.decision import Action, StrategyParams, decide
from poly_kalshi_service.engine import Engine
from poly_kalshi_service.feeds import PolyFeed, PolyShards
from poly_kalshi_service.ledger import Ledger, Position
from poly_kalshi_service.recorder import Recorder
from poly_kalshi_service.recordings import iter_files, read_lines
from poly_kalshi_service.results import paper_results
from poly_kalshi_service.shadow import Shadow, parse_variants, shadow_table
from poly_kalshi_service.trades import read_journal
from poly_kalshi_service.windows import Discovery

P10 = StrategyParams(gap_threshold=0.10)


def _journal(tmp_path: Path, kind: str) -> list[dict[str, Any]]:
    return [r for r in read_journal(tmp_path / "journal") if r["type"] == kind]


def _kalshi_mid(engine: Engine, w, sec: float, bid: float, seq: int) -> None:
    """Re-snapshot Kalshi YES at ``bid`` / ``bid + 0.02``."""
    engine.handle_line(
        at(w.open_unix, sec),
        "k",
        kalshi_snapshot(w.kalshi_ticker, [(bid, 500)], [(round(1 - bid - 0.02, 4), 500)], seq=seq),
    )


def _roll(engine: Engine, w) -> None:
    engine.handle_line(at(w.open_unix, 900.5), "k", kalshi_delta("other", "no", 0.05, 1))


# --- 1.1 uncensored gap ------------------------------------------------------------


def test_gap_is_tracked_past_the_crossing_for_every_threshold(tmp_path: Path) -> None:
    engine = make_engine(tmp_path, params=P10)
    w = window()
    setup_window(engine, w)  # Polymarket YES mid 0.45
    _kalshi_mid(engine, w, 10, 0.50, 2)  # gap 0.06 (before entry_early anyway)
    _kalshi_mid(engine, w, 30, 0.55, 3)  # gap 0.11: crosses 0.08 and 0.10, enters
    _kalshi_mid(engine, w, 200, 0.60, 4)  # gap 0.16: past the crossing, still measured
    _kalshi_mid(engine, w, 870, 0.90, 5)  # after entry_late: not counted
    _roll(engine, w)
    (decision,) = _journal(tmp_path, "decision")
    assert decision["sec_into"] == 30
    (rec,) = [r for r in _journal(tmp_path, "window") if r["open_unix"] == OPEN]
    assert rec["max_abs_gap"] == pytest.approx(0.11)  # censored at the crossing
    assert rec["max_abs_gap_full"] == pytest.approx(0.16)
    assert rec["first_cross_s"] == {"0.08": 30, "0.10": 30, "0.12": 200, "0.15": 200, "0.20": None}
    assert rec["gap_at_cross_side"]["0.15"] == "yes"


# --- 1.2 shadow variants -------------------------------------------------------------


def test_kalshi_spread_filter_is_off_by_default_and_never_consumes() -> None:
    k = Quote(True, 0.60, 0.70)  # spread 0.10, mid 0.65
    y = Quote(True, 0.44, 0.46)
    n = Quote(True, 0.54, 0.56)
    assert decide(k, y, n, P10, 100, False).action is Action.ENTER
    wide = StrategyParams(gap_threshold=0.10, max_kalshi_spread=0.05)
    d = decide(k, y, n, wide, 100, False)
    assert (d.action, d.reason, d.consumes_window) == (Action.NONE, "kalshi_spread", False)


def test_config_hash_ignores_unset_optional_params() -> None:
    import hashlib

    base = config_hash(StrategyParams(gap_threshold=0.10), 150)
    # What the hash was before max_kalshi_spread existed: epoch-3 configs keep their id.
    before = {"gap_threshold": 0.10, "max_entry_px": 0.60, "notional_usd": 50.0,
              "entry_early_s": 20, "entry_late_s": 60, "paper_latency_ms": 150}  # fmt: skip
    old = hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()[:10]
    assert base == old
    assert config_hash(StrategyParams(gap_threshold=0.10, max_kalshi_spread=None), 150) == base
    assert config_hash(StrategyParams(gap_threshold=0.10, max_kalshi_spread=0.05), 150) != base


def test_parse_variants_keeps_live_values_and_rejects_bad_names() -> None:
    (v,) = parse_variants('[{"name": "t15", "gap": 0.15}]', P10)
    assert v.params.gap_threshold == 0.15 and v.params.max_entry_px == P10.max_entry_px
    assert parse_variants("", P10) == ()
    for bad in ('[{"name": "live"}]', '[{"name": "a"}, {"name": "a"}]', '[{"name": "x", "y": 1}]'):
        with pytest.raises(ValueError):
            parse_variants(bad, P10)


def test_settings_validate_shadow_variants(tmp_path: Path) -> None:
    assert len(make_settings(tmp_path).shadow_variants) == 17
    with pytest.raises(ValidationError, match="PK_SHADOW_VARIANTS"):
        make_settings(tmp_path, PK_SHADOW_VARIANTS='{"not": "a list"}')


def test_shadow_variants_decide_alongside_and_settle_at_the_touch(tmp_path: Path) -> None:
    variants = parse_variants(
        json.dumps(
            [
                {"name": "t20", "gap": 0.20},
                {"name": "t30", "gap": 0.30},
                {"name": "cap40", "cap": 0.40},
            ]
        ),
        P10,
    )
    engine = make_engine(tmp_path, params=P10)
    engine.shadow = Shadow(variants, P10)
    w = window()
    setup_window(engine, w)  # gap 0.26, YES ask 0.46 with 60 + 80 shares
    engine.handle_line(at(OPEN, 20), "k", kalshi_delta(w.kalshi_ticker, "no", 0.05, 1))
    _roll(engine, w)
    shadows = {r["variant"]: r for r in _journal(tmp_path, "shadow")}
    assert set(shadows) == {"live", "t20", "t30", "cap40"}
    assert shadows["t20"]["action"] == "enter" and shadows["t20"]["entry_px"] == 0.46
    assert shadows["t20"]["depth_at_limit"] == 60  # the 0.47 level is above the limit
    assert shadows["t30"]["action"] == "none"
    assert shadows["cap40"]["action"] == "skip"
    (live,) = _journal(tmp_path, "decision")
    assert (shadows["live"]["action"], shadows["live"]["entry_px"]) == (
        live["action"],
        live["entry_px"],
    )
    engine.handle_line(
        at(OPEN, 1200),
        "c",
        ctl(
            {
                "type": "resolution",
                "venue": "poly",
                "asset": "BTC",
                "open_unix": OPEN,
                "outcome": "yes",
            }
        ),
    )
    settles = {r["variant"]: r for r in _journal(tmp_path, "shadow_settle")}
    assert set(settles) == {"live", "t20", "cap40"}
    expected = 100 * (1 - 0.46 - 0.07 * 0.46 * 0.54)
    assert settles["t20"]["net_cents_touch"] == pytest.approx(expected, abs=1e-3)
    table = shadow_table(list(read_journal(tmp_path / "journal")))
    assert list(table)[0] == "live"
    assert table["t20"]["n"] == 1 and table["t20"]["win_rate"] == 1.0
    assert table["cap40"]["n"] == 0 and table["cap40"]["crossings"] == 1  # a skip is no trade
    assert table["t30"]["windows"] == 1 and table["t30"]["crossings"] == 0


# --- 1.3 depth and markouts --------------------------------------------------------


def test_depth_at_limit_and_markouts_for_fills_and_no_fills(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)  # gap 0.20 default: setup's 0.26 crosses
    w = window()
    setup_window(engine, w)
    t = at(OPEN, 20)
    engine.handle_line(t, "k", kalshi_delta(w.kalshi_ticker, "no", 0.05, 1))
    (decision,) = _journal(tmp_path, "decision")
    assert decision["depth_at_limit"] == 60
    engine.handle_line(t + 200_000_000, "k", kalshi_delta(w.kalshi_ticker, "no", 0.05, 1))
    (fill,) = _journal(tmp_path, "fill")
    assert fill["depth_at_limit"] == 60 and fill["shares"] == 60
    # The market moves: YES 0.50 / 0.52 by +1 s, then 0.40 / 0.42 by +5 s.
    engine.handle_line(t + 900_000_000, "p", poly_book(w.yes_token, [(0.50, 10)], [(0.52, 10)]))
    engine.handle_line(t + 1_300_000_000, "k", kalshi_delta(w.kalshi_ticker, "no", 0.05, 1))
    engine.handle_line(t + 4_000_000_000, "p", poly_book(w.yes_token, [(0.40, 10)], [(0.42, 10)]))
    engine.handle_line(t + 5_300_000_000, "k", kalshi_delta(w.kalshi_ticker, "no", 0.05, 1))
    engine.handle_line(t + 30_300_000_000, "k", kalshi_delta(w.kalshi_ticker, "no", 0.05, 1))
    assert not _journal(tmp_path, "markout")  # not before the last exit horizon, +120 s
    engine.handle_line(t + 120_300_000_000, "k", kalshi_delta(w.kalshi_ticker, "no", 0.05, 1))
    (m,) = _journal(tmp_path, "markout")
    assert m["filled"] is True and m["ref_px"] == 0.46
    assert (m["mid_1s"], m["markout_1s"]) == (0.51, 5.0)
    assert (m["mid_5s"], m["markout_5s"]) == (0.41, -5.0)
    assert m["ask_30s"] == 0.42
    assert (m["bid_5s"], m["bid_size_5s"], m["bid_120s"]) == (0.40, 10, 0.40)
    assert m["exit_gap_s"] is None  # Kalshi mid 0.71 against 0.41: never closed
    res = paper_results(read_journal(tmp_path / "journal"))
    assert res["execution"]["markouts"]["fills"]["5s"]["median"] == -5.0
    assert res["execution"]["markouts"]["no_fills"]["n"] == 0
    assert res["trades"][0]["markout_5s"] == -5.0 and res["trades"][0]["depth_at_limit"] == 60


def test_a_no_fill_is_marked_out_against_its_limit(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    w = window()
    setup_window(engine, w)
    t = at(OPEN, 20)
    engine.handle_line(t, "k", kalshi_delta(w.kalshi_ticker, "no", 0.05, 1))
    engine.handle_line(t + 50_000_000, "p", poly_book(w.yes_token, [(0.50, 10)], [(0.52, 10)]))
    for dt in (0.2, 1.3, 5.3, 30.3, 60.3, 120.3):
        engine.handle_line(t + int(dt * 1e9), "k", kalshi_delta(w.kalshi_ticker, "no", 0.05, 1))
    (m,) = _journal(tmp_path, "markout")
    assert m["filled"] is False and m["ref_px"] == 0.46 and m["markout_1s"] == 5.0


def test_markout_records_when_the_gap_closes_and_the_bid_then(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    w = window()
    setup_window(engine, w)  # Kalshi mid 0.71, Polymarket YES 0.44 / 0.46
    t = at(OPEN, 20)
    engine.handle_line(t, "k", kalshi_delta(w.kalshi_ticker, "no", 0.05, 1))
    engine.handle_line(t + 200_000_000, "k", kalshi_delta(w.kalshi_ticker, "no", 0.05, 1))
    # Polymarket converges up to 0.60 / 0.62: still a gap of 0.10.
    engine.handle_line(t + 1_000_000_000, "p", poly_book(w.yes_token, [(0.60, 30)], [(0.62, 30)]))
    # Kalshi comes down to mid 0.63 at +3 s: the gap is 0.02. Like the horizons, the
    # close is read on the next event (an unrelated NO-book tick), as the books stood.
    _kalshi_mid(engine, w, 23.2, 0.62, 2)
    engine.handle_line(t + 3_250_000_000, "p", poly_book(w.no_token, [(0.38, 10)], [(0.40, 10)]))
    for dt in (5.3, 30.3, 60.3, 120.3):
        engine.handle_line(t + int(dt * 1e9), "p", poly_book(w.yes_token, [(0.55, 5)], [(0.57, 5)]))
    (m,) = _journal(tmp_path, "markout")
    assert m["exit_gap_s"] == pytest.approx(3.05, abs=0.01)
    assert (m["exit_gap_bid"], m["exit_gap_bid_size"]) == (0.60, 30)
    # +5 s is read on the 5.3 s event, before it lands; +30 s after it.
    assert (m["bid_5s"], m["bid_size_5s"], m["bid_30s"], m["bid_size_30s"]) == (0.60, 30, 0.55, 5)


def test_shadow_table_clusters_by_window_and_splits_us_night() -> None:
    night = 1791357300  # 07:15 UTC
    day = night + 12 * 3600  # 19:15 UTC
    rows = [
        {"type": "shadow", "variant": "live", "action": "enter", "open_unix": o}
        for o in (night, night, day)
    ] + [
        {"type": "shadow_settle", "variant": "live", "action": "enter", "open_unix": o,
         "win": w, "entry_px": 0.4, "net_cents_touch": n}
        for o, w, n in ((night, 0.0, -40.0), (night, 0.0, -40.0), (day, 1.0, 58.0))
    ]  # fmt: skip
    t = shadow_table(rows)["live"]
    assert t["n"] == 3 and t["entry_windows"] == 2
    # Two identical losses in one window are one observation, not two.
    assert t["se_cluster_cents"] > t["se_cents"]
    assert t["us_night"] == {"n": 2, "win_rate": 0.0, "net_cents": -40.0}
    assert t["us_day"] == {"n": 1, "win_rate": 1.0, "net_cents": 58.0}


# --- 1.4 small fixes --------------------------------------------------------------


def test_oldest_open_age_is_never_negative() -> None:
    ledger = Ledger(start_cash=1000, cash=1000)
    entry = at(OPEN, 100)
    ledger.buy(Position("BTC", OPEN, "yes", 10, 4.6, 0.1, entry))
    during = ledger.snapshot(at(OPEN, 300))
    assert during["oldest_open_age_s"] == 0 and during["oldest_entry_age_s"] == 200
    after = ledger.snapshot(at(OPEN, 960))
    assert after["oldest_open_age_s"] == 60


def test_daily_pnl_counts_on_the_settle_day(tmp_path: Path) -> None:
    day_end = 1_791_331_200  # 2026-10-07 00:00 UTC, a window grid point
    w_open = day_end - 900  # the day's last window
    records = [
        {"type": "decision", "asset": "BTC", "open_unix": w_open, "t_ns": at(w_open, 30),
         "action": "enter", "side": "yes"},
        {"type": "fill", "asset": "BTC", "open_unix": w_open, "t_ns": at(w_open, 30), "shares": 10,
         "ordered": 10, "vwap": 0.5, "cost": 5.0, "fee": 0.1, "touch_px": 0.5, "side": "yes"},
        {"type": "settle", "asset": "BTC", "open_unix": w_open, "t_ns": at(w_open, 1140),
         "payout": 10.0, "pnl": 4.9, "outcome": "yes"},
    ]  # fmt: skip
    daily = {d["day"]: d for d in paper_results(records)["daily"]}
    assert daily["2026-10-06"]["filled"] == 1 and daily["2026-10-06"]["pnl"] == 0
    assert daily["2026-10-07"]["settled"] == 1 and daily["2026-10-07"]["pnl"] == 4.9


async def test_kalshi_discovery_retries_429_and_reuses_one_series_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    import poly_kalshi_service.windows as windows_mod

    monkeypatch.setattr(windows_mod, "BACKOFF_429_S", 0.0)
    calls: list[httpx.Request] = []

    def iso(u: int) -> str:
        return datetime.fromtimestamp(u, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429)
        markets = [
            {"ticker": f"KXBTC15M-{o}", "open_time": iso(o), "close_time": iso(o + 900)}
            for o in (OPEN, OPEN + 900)
        ]
        return httpx.Response(200, json={"markets": markets})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    discovery = Discovery(http, gamma_url="https://g.test", kalshi_rest_url="https://k.test/v2")
    first = await discovery.kalshi_market("BTC", OPEN)
    second = await discovery.kalshi_market("BTC", OPEN + 900)
    await http.aclose()
    assert first["ticker"] == f"KXBTC15M-{OPEN}" and second["ticker"] == f"KXBTC15M-{OPEN + 900}"
    assert len(calls) == 2 and discovery.retries_429 == 1  # one 429, one query for both


def test_bbo_check_is_sampled(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    w = window()
    setup_window(engine, w)
    engine.bbo_check_every = 3
    for i in range(7):
        change = poly_change(w.yes_token, 0.43, 5 + i, "BUY", best_bid="0.44", best_ask="0.46")
        engine.handle_line(at(OPEN, 1 + i / 10), "p", change)
    assert engine.counters["bbo_checks_pc"] == 3  # the 1st, 4th and 7th


# --- 2.1 sharded Polymarket sockets -------------------------------------------------


def test_a_shard_reconnect_drops_only_its_own_books(tmp_path: Path) -> None:
    engine = make_engine(tmp_path, assets=("BTC", "ETH"))
    btc, eth = window("BTC"), window("ETH")
    setup_window(engine, btc)
    setup_window(engine, eth)
    engine.handle_line(
        at(OPEN, 1),
        "c",
        ctl(
            {"type": "reconnect", "venue": "p", "shard": 1, "tokens": [eth.yes_token, eth.no_token]}
        ),
    )
    assert engine.quotes("BTC")[1].valid
    assert not engine.quotes("ETH")[1].valid
    assert engine.quotes("ETH")[0].valid  # Kalshi untouched
    # An old tape's reconnect (no tokens) still drops every Polymarket book.
    engine.handle_line(at(OPEN, 2), "c", ctl({"type": "reconnect", "venue": "p"}))
    assert not engine.quotes("BTC")[1].valid


async def test_poly_shards_spread_assets_and_name_their_tokens() -> None:
    lines: list[tuple[str, str]] = []
    shards = PolyShards("wss://x", lambda v, t: lines.append((v, t)), ("BTC", "ETH", "SOL"), 2)
    assert len(shards.shards) == 2 and shards.shard_of == {"BTC": 0, "ETH": 1, "SOL": 0}
    await shards.set_wanted_groups({"BTC": {"b1", "b2"}, "ETH": {"e1"}, "SOL": {"s1"}})
    assert shards.shards[0].wanted == {"b1", "b2", "s1"}
    assert shards.shards[1].wanted == {"e1"}
    assert shards.shards[1]._reconnect_fields() == {"shard": 1, "tokens": ["e1"]}
    assert PolyFeed("wss://x", lambda v, t: None)._reconnect_fields() == {}
    assert len(PolyShards("wss://x", lambda v, t: None, ("BTC", "ETH"), 0).shards) == 2


# --- 2.2 recorder through a gzip child ----------------------------------------------


@pytest.mark.parametrize("gzip_bin", ["auto", None])
def test_recorder_output_reads_back_with_either_compressor(tmp_path: Path, gzip_bin) -> None:
    recorder = Recorder(tmp_path, host_tag="test", gzip_bin=gzip_bin)
    recorder.session_ns = 7
    recorder.start()
    lines = [(at(OPEN, s), "p", json.dumps({"n": s})) for s in range(200)]
    for recv_ns, venue, text in lines:
        recorder.write(venue, recv_ns, text)
    recorder.stop()
    assert recorder.stats()["compressor"] == ("gzip-child" if gzip_bin else "thread")
    files = iter_files(tmp_path)
    assert len(files) == 1
    assert list(read_lines(files)) == lines
    with gzip.open(files[0], "rt") as handle:  # a standard gzip file
        assert handle.readline().startswith(str(lines[0][0]))


def test_limit_offset_raises_the_order_limit_not_the_decision(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    engine.limit_offset = 0.02
    w = window()
    setup_window(engine, w)  # YES asks 0.46 x 60, 0.47 x 80; the touch is 0.46
    t = at(OPEN, 20)
    engine.handle_line(t, "k", kalshi_delta(w.kalshi_ticker, "no", 0.05, 1))
    engine.handle_line(t + 200_000_000, "k", kalshi_delta(w.kalshi_ticker, "no", 0.05, 1))
    (decision,) = [r for r in read_journal(tmp_path / "journal") if r["type"] == "decision"]
    (fill,) = [r for r in read_journal(tmp_path / "journal") if r["type"] == "fill"]
    assert decision["entry_px"] == 0.46 and decision["contracts"] == 108
    assert fill["limit_px"] == 0.48 and fill["shares"] == 108  # 60 at 0.46, 48 at 0.47
    assert fill["depth_at_limit"] == 140
    assert engine.limit_px(0.59) == 0.60  # never above the cap


def test_poly_spread_filter_waits_without_consuming() -> None:
    k = Quote(True, 0.70, 0.72)
    wide = Quote(True, 0.30, 0.46)  # mid 0.38, spread 0.16
    n = Quote(True, 0.54, 0.70)
    params = StrategyParams(gap_threshold=0.10, max_poly_spread=0.10)
    d = decide(k, wide, n, params, 100, False)
    assert (d.action, d.reason, d.consumes_window) == (Action.NONE, "poly_spread", False)
    tight = Quote(True, 0.44, 0.46)
    assert decide(k, tight, n, params, 100, False).action is Action.ENTER
