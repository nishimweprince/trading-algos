from __future__ import annotations

import pytest
from fake_exchange import FakeExchange

from zone_trend.live.oms import Oms, client_id
from zone_trend.live.reconciler import Reconciler
from zone_trend.live.runner import kill
from zone_trend.persistence.repo import Repo
from zone_trend.risk.sizing import Filters, Size
from zone_trend.types import Side, Signal

SIG = Signal("a" * 40, "ETHUSDT", Side.LONG, "B", 0, 2000.0, 1960.0, 2060.0, 2120.0, 2, "z1")
SIZE = Size(qty=0.05, notional=100.0, tp1_qty=0.02, single_tp=False, risk_usd=2.0)


class Alerts:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def __call__(self, level, subject, lines):
        self.sent.append((level, subject))

    def levels(self, level):
        return [s for lv, s in self.sent if lv == level]


@pytest.fixture
def world(tmp_path):
    ex = FakeExchange({"ETHUSDT": 2000.0, "BTCUSDT": 60000.0, "XRPUSDT": 0.5})
    repo = Repo(f"sqlite:///{tmp_path / 'zt.db'}")
    alerts = Alerts()
    oms = Oms(ex, repo, alerts, leverage=5, fee_taker=0.0005, clock_ms=lambda: ex.now)
    oms.filters = {"ETHUSDT": Filters(0.01, 0.001, 0.001, 20.0)}
    ex.sink = oms.on_event
    return ex, repo, oms, alerts


def cid(leg):
    return client_id(SIG.id, leg)


async def test_entry_then_stop_and_targets_are_placed(world):
    ex, repo, oms, _ = world
    assert await oms.open(SIG, SIZE)
    await oms.drain()
    pos = repo.position(SIG.id)
    assert pos["status"] == "OPEN" and pos["entry_price"] == 2000.0 and pos["qty_open"] == 0.05
    stop, tp1, tp2 = ex.algos[cid("sl1")], ex.algos[cid("tp1")], ex.algos[cid("tp2")]
    assert stop["closePosition"] and stop["triggerPrice"] == 1960.0 and stop["side"] == "SELL"
    assert tp1["quantity"] == pytest.approx(0.02) and tp2["quantity"] == pytest.approx(0.03)
    # the stop goes out before the targets
    legs = [c for kind, c in ex.calls if kind == "new_algo"]
    assert legs[0] == cid("sl1")


async def test_stop_out_closes_and_cleans_up(world):
    ex, repo, oms, alerts = world
    await oms.open(SIG, SIZE)
    await oms.drain()
    ex.move("ETHUSDT", 1950.0)
    await oms.drain()
    pos = repo.position(SIG.id)
    assert pos["status"] == "CLOSED" and pos["exit_reason"] == "stop" and pos["pnl"] < 0
    assert ex.algos[cid("tp1")]["status"] == "CANCELED"
    assert ex.algos[cid("tp2")]["status"] == "CANCELED"
    assert ex.position["ETHUSDT"] == 0
    assert any("closed: stop" in s for s in alerts.levels("info"))


async def test_tp1_moves_stop_to_breakeven_place_then_cancel(world):
    ex, repo, oms, _ = world
    await oms.open(SIG, SIZE)
    await oms.drain()
    ex.move("ETHUSDT", 2061.0)
    await oms.drain()
    pos = repo.position(SIG.id)
    assert pos["tp1_done"] and pos["qty_open"] == pytest.approx(0.03)
    new_stop = ex.algos[cid("sl2")]
    assert not new_stop["closePosition"] and new_stop["quantity"] == pytest.approx(0.03)
    assert new_stop["triggerPrice"] == pytest.approx(2002.0)  # entry + round-trip fees
    order = [c for _, c in ex.calls]
    assert order.index(cid("sl2")) < order.index(cid("sl1"), order.index(cid("tp1")))  # placed
    assert ex.algos[cid("sl1")]["status"] == "CANCELED"
    ex.move("ETHUSDT", 2001.0)
    await oms.drain()
    pos = repo.position(SIG.id)
    assert pos["status"] == "CLOSED" and pos["pnl"] > 0


async def test_trailing_moves_alternate_with_closeposition(world):
    ex, repo, oms, _ = world
    await oms.open(SIG, SIZE)
    await oms.drain()
    assert await oms.move_stop("ETHUSDT", 1980.0)  # sl1 closePosition -> sl2 reduce-only
    assert await oms.move_stop("ETHUSDT", 1990.0)  # -> sl3 closePosition again (no -4130)
    assert ex.algos[cid("sl3")]["closePosition"] and ex.algos[cid("sl3")]["status"] == "NEW"
    assert [ex.algos[cid(f"sl{i}")]["status"] for i in (1, 2)] == ["CANCELED", "CANCELED"]
    assert repo.position(SIG.id)["stop"] == 1990.0


async def test_ambiguous_entry_that_happened_is_not_resent(world):
    ex, repo, oms, _ = world
    ex.fail("new_order", "unknown", happened=True)
    assert await oms.open(SIG, SIZE)
    await oms.drain()
    assert [c for k, c in ex.calls if k == "new_order"] == [cid("entry")]
    assert repo.position(SIG.id)["status"] == "OPEN" and ex.position["ETHUSDT"] == 0.05


async def test_ambiguous_entry_that_never_arrived_is_resent_once(world):
    ex, repo, oms, _ = world
    ex.fail("new_order", "unknown", happened=False)
    assert await oms.open(SIG, SIZE)
    await oms.drain()
    assert [c for k, c in ex.calls if k == "new_order"] == [cid("entry"), cid("entry")]
    assert ex.position["ETHUSDT"] == pytest.approx(0.05)


async def test_rejected_entry_closes_the_row(world):
    ex, repo, oms, _ = world
    ex.fail("new_order", "rejected", code=-2019)
    assert not await oms.open(SIG, SIZE)
    pos = repo.position(SIG.id)
    assert pos["status"] == "CLOSED" and pos["exit_reason"] == "entry_rejected:-2019"


async def test_stop_failing_twice_flattens_and_alerts(world):
    ex, repo, oms, alerts = world
    ex.fail("new_algo", "rejected", code=-1111)
    ex.fail("new_algo", "rejected", code=-1111)
    await oms.open(SIG, SIZE)
    await oms.drain()
    assert ex.position["ETHUSDT"] == 0
    assert repo.position(SIG.id)["status"] == "CLOSED"
    assert any("stop failed twice" in s for s in alerts.levels("critical"))


async def test_stop_that_would_trigger_closes_at_market(world):
    ex, repo, oms, _ = world
    await oms.open(SIG, SIZE)
    await oms.drain()
    ex.price["ETHUSDT"] = 1970.0  # price fell below the new level without crossing the old stop
    await oms.move_stop("ETHUSDT", 1975.0)
    await oms.drain()
    assert ex.position["ETHUSDT"] == 0 and repo.position(SIG.id)["status"] == "CLOSED"


async def test_failed_market_close_keeps_the_stop(world):
    ex, repo, oms, alerts = world
    await oms.open(SIG, SIZE)
    await oms.drain()
    ex.fail("new_order", "rejected", code=-2022)
    await oms.close("ETHUSDT", "time")
    assert repo.position(SIG.id)["status"] == "OPEN"
    assert ex.algos[cid("sl1")]["status"] == "NEW"
    assert any("close failed" in s for s in alerts.levels("critical"))


async def test_time_exit_closes_then_cancels(world):
    ex, repo, oms, _ = world
    await oms.open(SIG, SIZE)
    await oms.drain()
    await oms.close("ETHUSDT", "time")
    await oms.drain()
    pos = repo.position(SIG.id)
    assert pos["status"] == "CLOSED" and pos["exit_reason"] == "time"
    assert all(a["status"] == "CANCELED" for a in ex.algos.values())


# -- reconciler --------------------------------------------------------------------------


def recon(world, symbols=("ETHUSDT", "BTCUSDT")):
    ex, repo, oms, alerts = world
    return Reconciler(ex, repo, oms, alerts, set(symbols), clock_ms=lambda: ex.now)


async def test_reconciler_replaces_a_missing_stop(world):
    ex, repo, oms, alerts = world
    await oms.open(SIG, SIZE)
    await oms.drain()
    ex.algos[cid("sl1")]["status"] = "CANCELED"  # vanished on the exchange
    repairs = await recon(world).run_once()
    assert any("stop replaced" in r for r in repairs)
    live = [a for a in ex.algos.values() if a["status"] == "NEW" and a["type"] == "STOP_MARKET"]
    assert len(live) == 1 and live[0]["triggerPrice"] == 1960.0
    assert any("unprotected" in s for s in alerts.levels("critical"))


async def test_reconciler_backfills_fills_the_stream_missed(world):
    ex, repo, oms, _ = world
    await oms.open(SIG, SIZE)
    await oms.drain()
    ex.stream_up = False
    ex.move("ETHUSDT", 1950.0)  # stopped out while the stream was down
    assert repo.position(SIG.id)["status"] == "OPEN"
    await recon(world).run_once()
    await oms.drain()
    pos = repo.position(SIG.id)
    assert pos["status"] == "CLOSED" and pos["pnl"] < 0


async def test_reconciler_resolves_an_unknown_entry(world):
    ex, repo, oms, _ = world
    ex.fail("new_order", "unknown", happened=True)
    ex.fail("query_order", "unknown")  # both of the OMS's queries fail too,
    ex.fail("query_order", "unknown")  # so the entry is left for the reconciler
    original = ex.query_order

    async def flaky(symbol, client_id):
        inj = ex._injected("query_order")
        if inj:
            from fake_exchange import Reply

            return Reply(inj[0], code=inj[1])
        return await original(symbol, client_id)

    ex.query_order = flaky
    ex.stream_up = False
    await oms.open(SIG, SIZE)
    assert repo.position(SIG.id)["status"] == "PENDING"
    ex.stream_up = True
    await recon(world).run_once()
    await oms.drain()
    assert repo.position(SIG.id)["status"] == "OPEN"
    assert any(a["type"] == "STOP_MARKET" and a["status"] == "NEW" for a in ex.algos.values())


async def test_reconciler_protects_unknown_positions_and_ignores_foreign_ones(world):
    ex, repo, oms, alerts = world
    ex.position["BTCUSDT"], ex.entry["BTCUSDT"] = 0.01, 60000.0
    ex.position["XRPUSDT"], ex.entry["XRPUSDT"] = 100.0, 0.5
    await recon(world).run_once()
    stops = [a for a in ex.algos.values() if a["symbol"] == "BTCUSDT"]
    assert len(stops) == 1 and stops[0]["triggerPrice"] == pytest.approx(56400.0)
    assert not [a for a in ex.algos.values() if a["symbol"] == "XRPUSDT"]
    assert any("not managed" in s for s in alerts.levels("warning"))


async def test_reconciler_cancels_orphan_orders(world):
    ex, repo, oms, _ = world
    await ex.new_algo(
        "ETHUSDT",
        "SELL",
        "STOP_MARKET",
        trigger_price=1000,
        client_id="zt-orphan-x",
        close_position=True,
    )
    await ex.new_algo(
        "ETHUSDT", "SELL", "STOP_MARKET", trigger_price=1000, client_id="manual-1", quantity=1
    )
    await recon(world).run_once()
    assert ex.algos["zt-orphan-x"]["status"] == "CANCELED"
    assert ex.algos["manual-1"]["status"] == "NEW"  # not ours: untouched


async def test_kill_flattens_cancels_and_halts(world):
    ex, repo, oms, alerts = world
    await oms.open(SIG, SIZE)
    await oms.drain()
    await kill(oms, repo, ex, alerts)
    assert ex.position["ETHUSDT"] == 0 and repo.get_state("halted") is True
    assert all(a["status"] != "NEW" for a in ex.algos.values())
