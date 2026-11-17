"""Runner order/sync/kill behaviour against FakeHub on synthetic data (no history needed)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from conftest import research_minutes, synthetic_m1
from fakehub import FakeHub

from walkforward_patterns import config as cfgmod
from walkforward_patterns.engine import Entry, Signal
from walkforward_patterns.journal import Journal
from walkforward_patterns.live import Runner
from walkforward_patterns.replay import DEFAULT_CONFIG
from walkforward_patterns.settings import Settings


@pytest.fixture
def setup(tmp_path):
    full = cfgmod.load(DEFAULT_CONFIG)
    cfg = replace(full, strategies=tuple(s for s in full.strategies if s.symbol == "XAUUSD"))
    m1 = synthetic_m1(days=40, start="2024-01-07 18:00")
    t = research_minutes(m1.index)
    hub = FakeHub.from_research({"XAUUSD": (t, m1.o.values, m1.h.values, m1.l.values, m1.c.values)})
    now = datetime(2024, 2, 7, 15, 0, 2, tzinfo=UTC)  # a Wednesday
    hub.advance(now)
    s = Settings(WFP_DB=str(tmp_path / "j.sqlite3"), WFP_BAR_WAIT_SECONDS=0, WFP_WARMUP_DAYS=60, WFP_HUB_API_KEY="x")
    j = Journal(s.db_path)
    r = Runner(s, cfg, hub, j)
    r.warmup(now)
    return r, hub, j, cfg, now


def _entry(cfg, sid, side, t):
    st = next(s for s in cfg.strategies if s.id == sid)
    sig = Signal("patterns", 15, 5, "double_bottom", side, t - 15, t, 1.0, 0.0, {60: side, 240: side})
    return Entry(st, side, t, 0.0, sig)


def test_refuses_empty_lots(tmp_path):
    full = cfgmod.load(DEFAULT_CONFIG)
    bad = replace(full, strategies=(replace(full.strategies[0], lots=None),))
    with pytest.raises(SystemExit):
        Runner(Settings(WFP_DB=str(tmp_path / "x.db"), WFP_HUB_API_KEY="x"), bad, None, Journal(tmp_path / "x.db"))


def test_entry_sl_exit_and_one_position(setup):
    r, hub, j, cfg, now = setup
    T = r.markets["XAUUSD"].last_t + 1
    r._enter(_entry(cfg, "S01", 1, T), hub.quote("XAUUSD"))
    (row,) = j.open_trades()
    assert row["status"] == "open" and hub.orders[-1]["source"] == "wf_s01"
    assert not r.is_flat("S01") and r.is_flat("S02")
    pos = hub.positions_[row["position_id"]]
    hub._close(pos, pos.sl, now)  # broker stop-out
    r.sync()
    (closed,) = j.closed_trades("S01")
    assert closed["exit_reason"] == "SL" and closed["r_price"] == pytest.approx(-1.0)
    assert closed["net_usd"] == pytest.approx(closed["gross_usd"] - 0.25)
    assert r.is_flat("S01")


def test_rejected_and_unknown_orders(setup):
    r, hub, j, cfg, now = setup
    T = r.markets["XAUUSD"].last_t + 1
    hub.fail_next_order = "rejected"
    r._enter(_entry(cfg, "S02", 1, T), hub.quote("XAUUSD"))
    assert j.open_trades() == [] and r.is_flat("S02")
    # unknown outcome, but the order reached the broker: sync adopts the position by label
    hub.fail_next_order = "unknown"
    r._enter(_entry(cfg, "S03", 1, T), hub.quote("XAUUSD"))
    assert j.open_trades()[0]["status"] == "pending" and not r.is_flat("S03")
    hub.market_order(source="wf_s03", instrument="XAUUSD", side=1, lots=0.1, sl_distance=5, tp_distance=10, digits=2)
    r.sync()
    (row,) = j.open_trades()
    assert row["status"] == "open" and row["position_id"] is not None


def test_restart_adopts_unknown_labelled_position(setup):
    r, hub, j, cfg, now = setup
    hub.market_order(source="wf_s05", instrument="XAUUSD", side=-1, lots=0.1, sl_distance=10, tp_distance=20,
                     digits=2)
    hub.market_order(source="someone_else", instrument="XAUUSD", side=1, lots=0.1, sl_distance=10, tp_distance=20,
                     digits=2)
    r.sync()
    rows = j.open_trades()
    assert [x["strategy"] for x in rows] == ["S05"] and not r.is_flat("S05")
    assert rows[0]["id"] in r.timers
    r.sync()
    assert len(j.open_trades()) == 1  # never duplicated


def test_time_stop_closes_at_market(setup):
    r, hub, j, cfg, now = setup
    T = r.markets["XAUUSD"].last_t + 1
    r._enter(_entry(cfg, "S04", -1, T), hub.quote("XAUUSD"))
    (row,) = j.open_trades()
    r.timers[row["id"]].j -= 2880  # pretend 48h of market minutes passed
    r._time_exits("XAUUSD", T + 1)
    r.sync()
    (closed,) = j.closed_trades("S04")
    assert closed["exit_reason"] == "TIME"


def test_kill_rule_disables_after_60_losing(setup):
    r, hub, j, cfg, now = setup
    for i in range(60):
        j.new_trade(strategy="S10", symbol="XAUUSD", side=-1, status="closed", r_net=-0.1 if i % 2 else 0.05,
                    exit_utc=(now + timedelta(minutes=i)).isoformat(), entry_minute=0)
    r._kill_rule("S10")
    assert "S10" in j.disabled()
    T = r.markets["XAUUSD"].last_t + 1
    r._enter(_entry(cfg, "S10", -1, T), hub.quote("XAUUSD"))
    assert j.open_trades() == []


def test_kill_rule_keeps_positive_strategy(setup):
    r, hub, j, cfg, now = setup
    for _ in range(59):
        j.new_trade(strategy="S15", symbol="XAUUSD", side=1, status="closed", r_net=-0.5,
                    exit_utc=now.isoformat(), entry_minute=0)
    r._kill_rule("S15")
    assert "S15" not in j.disabled()  # under 60 trades


def test_ftmo_breach_is_record_only(setup):
    r, hub, j, cfg, now = setup
    j.new_trade(strategy="S19", symbol="XAUUSD", side=1, status="closed", net_usd=-6000.0, r_net=-1,
                exit_utc=now.isoformat(), entry_minute=0)
    r._ftmo_check()
    assert any(e["kind"] == "ftmo" for e in j.events())
    T = r.markets["XAUUSD"].last_t + 1
    r._enter(_entry(cfg, "S19", 1, T), hub.quote("XAUUSD"))
    assert j.open_trades()  # still trades


def test_ticks_run_on_synthetic_feed(setup):
    r, hub, j, cfg, now = setup
    t = now
    for _ in range(600):
        t += timedelta(minutes=1)
        hub.advance(t)
        r.tick(t)
    assert r.markets["XAUUSD"].last_t is not None
    assert np.isfinite(r.last_close["XAUUSD"])
