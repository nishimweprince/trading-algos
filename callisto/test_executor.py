"""Executor tests against the plugin's wire-level FakeCTraderServer (no network).

The fake broker answers auth, symbol, spot and reconcile requests, fills market orders
(ORDER_ACCEPTED as the correlated reply, then an uncorrelated ORDER_FILLED), acknowledges
SL amendments and closes positions, so the whole order path runs over real framing.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import executor as X
import pytest
from config import CFG
from ta_plugin_ctrader import proto as P
from ta_plugin_ctrader._generated import OpenApiModelMessages_pb2 as M
from ta_plugin_ctrader.testing import FakeCTraderServer

ACC = 49581317
SYM = 41


class Broker:
    """Scripted cTrader: one gold symbol, fills market orders at the current ask/bid."""

    def __init__(self):
        self.srv = FakeCTraderServer()
        self.bid, self.ask = 4000.00, 4000.30
        self.next_pos = 1000
        self.positions: dict[int, P.ProtoOAPosition] = {}
        self.orders: list[P.ProtoOANewOrderReq] = []
        self.amends: list[P.ProtoOAAmendPositionSLTPReq] = []
        s = self.srv
        s.reply_with("ProtoOAApplicationAuthReq", P.ProtoOAApplicationAuthRes())
        s.reply_with("ProtoOAAccountAuthReq", P.ProtoOAAccountAuthRes(ctidTraderAccountId=ACC))
        s.reply_with("ProtoOASymbolsListReq", P.ProtoOASymbolsListRes(
            ctidTraderAccountId=ACC,
            symbol=[M.ProtoOALightSymbol(symbolId=7, symbolName="EURUSD"),
                    M.ProtoOALightSymbol(symbolId=SYM, symbolName="XAUUSD")]))
        s.reply_with("ProtoOASymbolByIdReq", P.ProtoOASymbolByIdRes(
            ctidTraderAccountId=ACC,
            symbol=[P.ProtoOASymbol(symbolId=SYM, digits=2, pipPosition=1, lotSize=10000,
                                    minVolume=100, stepVolume=100, maxVolume=10_000_000)]))
        s.respond("ProtoOASubscribeSpotsReq", self._subscribe)
        s.respond("ProtoOAReconcileReq", lambda r, _m: P.ProtoOAReconcileRes(
            ctidTraderAccountId=ACC, position=list(self.positions.values())))
        s.reply_with("ProtoOATraderReq", P.ProtoOATraderRes(
            ctidTraderAccountId=ACC, trader=M.ProtoOATrader(
                ctidTraderAccountId=ACC, balance=1_000_000, depositAssetId=1, moneyDigits=2,
                leverageInCents=10000, brokerName="ftmo", traderLogin=17249135)))
        s.respond("ProtoOANewOrderReq", self._new_order)
        s.respond("ProtoOAAmendPositionSLTPReq", self._amend)
        s.respond("ProtoOAClosePositionReq", self._close)

    def spot(self, bid, ask):
        self.bid, self.ask = bid, ask
        self.srv.push(P.ProtoOASpotEvent(ctidTraderAccountId=ACC, symbolId=SYM,
                                         bid=int(round(bid * 1e5)), ask=int(round(ask * 1e5))))

    def _subscribe(self, _r, _m):
        asyncio.get_running_loop().call_soon(self.spot, self.bid, self.ask)
        return P.ProtoOASubscribeSpotsRes(ctidTraderAccountId=ACC)

    def _order_msg(self, req, status):
        return M.ProtoOAOrder(orderId=self.next_pos, orderType=req.orderType, orderStatus=status,
                              clientOrderId=req.clientOrderId, tradeData=M.ProtoOATradeData(
                                  symbolId=req.symbolId, volume=req.volume,
                                  tradeSide=req.tradeSide, label=req.label))

    def _new_order(self, req, mid):
        self.orders.append(req)
        pid = self.next_pos
        self.next_pos += 1
        buy = req.tradeSide == M.ProtoOATradeSide.Value("BUY")
        px = self.ask if buy else self.bid
        sgn = 1 if buy else -1
        pos = M.ProtoOAPosition(
            positionId=pid, positionStatus=M.ProtoOAPositionStatus.Value("POSITION_STATUS_OPEN"),
            swap=0, price=px, stopLoss=round(px - sgn * req.relativeStopLoss / 1e5, 2),
            takeProfit=round(px + sgn * req.relativeTakeProfit / 1e5, 2),
            tradeData=M.ProtoOATradeData(symbolId=req.symbolId, volume=req.volume,
                                         tradeSide=req.tradeSide, label=req.label,
                                         comment=req.comment,
                                         openTimestamp=1_763_200_000_000))
        self.positions[pid] = pos
        filled = P.ProtoOAExecutionEvent(
            ctidTraderAccountId=ACC, executionType=M.ProtoOAExecutionType.Value("ORDER_FILLED"),
            position=pos,
            order=self._order_msg(req, M.ProtoOAOrderStatus.Value("ORDER_STATUS_FILLED")))
        asyncio.get_running_loop().call_soon(self.srv.push, filled, mid)
        return P.ProtoOAExecutionEvent(
            ctidTraderAccountId=ACC, executionType=M.ProtoOAExecutionType.Value("ORDER_ACCEPTED"),
            order=self._order_msg(req, M.ProtoOAOrderStatus.Value("ORDER_STATUS_ACCEPTED")))

    def _amend(self, req, _mid):
        self.amends.append(req)
        pos = self.positions[req.positionId]
        pos.stopLoss = req.stopLoss
        return P.ProtoOAExecutionEvent(
            ctidTraderAccountId=ACC, executionType=M.ProtoOAExecutionType.Value("ORDER_REPLACED"),
            position=pos)

    def _close(self, req, mid):
        pos = self.positions.pop(req.positionId)
        closed = M.ProtoOAPosition()
        closed.CopyFrom(pos)
        closed.positionStatus = M.ProtoOAPositionStatus.Value("POSITION_STATUS_CLOSED")
        ev = P.ProtoOAExecutionEvent(
            ctidTraderAccountId=ACC, executionType=M.ProtoOAExecutionType.Value("ORDER_FILLED"),
            position=closed)
        asyncio.get_running_loop().call_soon(self.srv.push, ev, mid)
        return P.ProtoOAExecutionEvent(
            ctidTraderAccountId=ACC, executionType=M.ProtoOAExecutionType.Value("ORDER_ACCEPTED"))


def settings(**kw) -> X.ExecSettings:
    s = X.ExecSettings(client_id="cid", client_secret="sec", access_token="tok",
                       refresh_token="ref", account_id=ACC, is_live=False,
                       execution_enabled=True, live_trading_enabled=False, lots=0.01,
                       max_open=1, max_signal_age_s=180, tm_events="", tm_enabled=True,
                       tm_lots=0.2, tm_max_open=1, tm_max_age_s=300, tm_max_slip_pips=50,
                       tm_follow_close=True)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


@pytest.fixture(autouse=True)
def _tmp_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(X, "EXEC_LOG", tmp_path / "executions.jsonl")


async def make(tmp_path, **kw):
    b = Broker()
    s = settings(**kw)
    ex = X.Executor(s, connector=b.srv.connector(),
                    tokens=X.TokenCache(s, path=tmp_path / "tokens.json"))
    await ex.connect()
    return b, ex


def signal(side="BUY", age_s=5, post_id=1):
    return {"event": "signal", "ticker": "XAUUSD", "direction": side, "post_id": post_id,
            "posted_at": (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()}


async def test_signal_places_market_order_with_sl_tp(tmp_path):
    b, ex = await make(tmp_path)
    out = await ex.on_signal(signal("BUY"))
    assert out["event"] == "placed"
    o = b.orders[0]
    assert o.ctidTraderAccountId == ACC and o.symbolId == SYM and o.volume == 100
    assert M.ProtoOATradeSide.Name(o.tradeSide) == "BUY"
    assert M.ProtoOAOrderType.Name(o.orderType) == "MARKET"
    pip = CFG.pip("XAUUSD")
    assert o.relativeStopLoss == round(CFG.sl_pips * pip * 1e5)
    assert o.relativeTakeProfit == round(CFG.tp_pips * pip * 1e5)
    assert o.label == "callisto"
    m = out["position"]
    assert m["entry"] == 4000.30 and m["stop_loss"] == round(4000.30 - CFG.sl_pips * pip, 2)
    assert len(ex.managed) == 1
    await ex.close()


async def test_dry_run_sends_nothing(tmp_path):
    b, ex = await make(tmp_path, execution_enabled=False)
    out = await ex.on_signal(signal("SELL"))
    assert out["event"] == "would_place" and "dry run" in out["reason"]
    assert b.orders == [] and out["volume"] == 100
    await ex.close()


async def test_live_account_needs_live_flag(tmp_path):
    b, ex = await make(tmp_path, is_live=True)
    out = await ex.on_signal(signal())
    assert out["event"] == "would_place" and "LIVE_TRADING_ENABLED" in out["reason"]
    assert b.orders == []
    await ex.close()


async def test_stale_signal_and_position_cap(tmp_path):
    b, ex = await make(tmp_path)
    assert (await ex.on_signal(signal(age_s=600)))["event"] == "skipped"
    assert (await ex.on_signal(signal(post_id=1)))["event"] == "placed"
    capped = await ex.on_signal(signal(post_id=2))
    assert capped["event"] == "skipped" and "max 1" in capped["reason"]
    assert len(b.orders) == 1
    await ex.close()


async def test_trailing_moves_stop_in_steps(tmp_path, monkeypatch):
    monkeypatch.setattr(X, "CFG", replace(CFG, trail_start_pips=50.0, trail_distance_pips=30.0,
                                          trail_step_pips=10.0))
    b, ex = await make(tmp_path)
    await ex.on_signal(signal("BUY"))          # entry 4000.30
    pid = next(iter(ex.managed))
    b.spot(4004.00, 4004.30)                   # +37 pips: below start, no amend
    await asyncio.sleep(0.05)
    assert b.amends == []
    b.spot(4006.00, 4006.30)                   # +57 pips: stop -> 4006.00 - 3.00
    await asyncio.sleep(0.05)
    assert [a.stopLoss for a in b.amends] == [4003.00]
    b.spot(4006.50, 4006.80)                   # only 5 pips better: below step
    await asyncio.sleep(0.05)
    assert len(b.amends) == 1
    b.spot(4007.20, 4007.50)                   # 12 pips better than last stop basis
    await asyncio.sleep(0.05)
    assert [a.stopLoss for a in b.amends] == [4003.00, 4004.20]
    assert ex.managed[pid].stop_loss == 4004.20
    await ex.close()


async def test_close_and_reconcile(tmp_path):
    b, ex = await make(tmp_path)
    out = await ex.on_signal(signal("SELL"))
    m = ex.managed[out["position"]["position_id"]]
    await ex.close_position(m)
    assert ex.managed == {} and b.positions == {}
    await ex.on_signal(signal("BUY", post_id=3))
    await ex.close()
    # a restarted executor picks the open Callisto position back up for trailing
    s = settings()
    ex2 = X.Executor(s, connector=b.srv.connector(),
                     tokens=X.TokenCache(s, path=tmp_path / "t2.json"))
    await ex2.connect()
    await ex2.reconcile()
    assert len(ex2.managed) == 1 and next(iter(ex2.managed.values())).side == "BUY"
    await ex2.close()


async def test_order_error_surfaces(tmp_path):
    b, ex = await make(tmp_path)
    b.srv.respond("ProtoOANewOrderReq", lambda r, mid: P.ProtoOAOrderErrorEvent(
        ctidTraderAccountId=ACC, errorCode="NOT_ENOUGH_MONEY", description="margin"))
    out = await ex.on_signal(signal())
    assert out["event"] == "order_failed" and "NOT_ENOUGH_MONEY" in out["error"]
    await ex.close()


async def test_expired_token_is_refreshed(tmp_path):
    b = Broker()
    calls = {"n": 0}

    def auth(_r, _m):
        calls["n"] += 1
        if calls["n"] == 1:
            return P.ProtoOAErrorRes(errorCode="CH_ACCESS_TOKEN_INVALID", description="expired")
        return P.ProtoOAAccountAuthRes(ctidTraderAccountId=ACC)

    b.srv.respond("ProtoOAAccountAuthReq", auth)
    b.srv.reply_with("ProtoOARefreshTokenReq", P.ProtoOARefreshTokenRes(
        accessToken="new-access", refreshToken="new-refresh", tokenType="bearer",
        expiresIn=2_628_000))
    s = settings()
    tok = X.TokenCache(s, path=tmp_path / "tokens.json")
    ex = X.Executor(s, connector=b.srv.connector(), tokens=tok)
    await ex.connect()
    assert tok.access == "new-access" and tok.refresh == "new-refresh"
    assert (tmp_path / "tokens.json").stat().st_mode & 0o777 == 0o600
    await ex.close()


# -- Tickmind source ---------------------------------------------------------------------
def tm(status="active", entry=4000.0, stop=4013.0, target=3981.0, side="SELL", age_s=20,
       sid="tg-1", kind="update"):
    return {"seen_utc": datetime.now(UTC).isoformat(), "kind": kind, "id": sid,
            "posted_unix": time.time() - age_s, "pair": "XAUUSD", "side": side,
            "entry": entry, "stop": stop, "target": target, "status": status, "result": None}


async def test_tickmind_waits_for_levels_then_uses_its_stop_and_target(tmp_path):
    b, ex = await make(tmp_path)
    decided: set[str] = set()
    assert await ex.on_tickmind(tm(entry=0, stop=0, target=0, kind="new"), decided) is None
    assert b.orders == []
    out = await ex.on_tickmind(tm(), decided)              # SELL at bid 4000.00
    assert out["event"] == "placed" and out["source"] == "tickmind"
    o = b.orders[0]
    assert o.label == "tickmind" and o.comment == "tickmind tg-1"
    assert o.volume == 2000                                  # 0.2 lots
    assert o.relativeStopLoss == round(13.0 * 1e5)           # 4013 - 4000
    assert o.relativeTakeProfit == round(19.0 * 1e5)         # 4000 - 3981
    assert await ex.on_tickmind(tm(), decided) is None       # decided once
    assert len(b.orders) == 1
    await ex.close()


async def test_tickmind_guards(tmp_path):
    b, ex = await make(tmp_path)
    d: set[str] = set()
    stale = await ex.on_tickmind(tm(sid="a", age_s=900), d)
    assert stale["event"] == "skipped" and "old" in stale["reason"]
    late = await ex.on_tickmind(tm(sid="b", entry=4008.0, stop=4020.0, target=3990.0), d)
    assert late["event"] == "skipped" and "pips from entry" in late["reason"]   # +80 pips
    beyond = await ex.on_tickmind(tm(sid="c", entry=4002.0, stop=4013.0, target=4001.0), d)
    assert beyond["event"] == "skipped" and "beyond" in beyond["reason"]
    assert b.orders == []
    await ex.close()


async def test_tickmind_switch_is_independent(tmp_path):
    b, ex = await make(tmp_path, tm_enabled=False, execution_enabled=True)
    out = await ex.on_tickmind(tm(), set())
    assert out["event"] == "would_place" and "TICKMIND_EXECUTION_ENABLED" in out["reason"]
    assert (await ex.on_signal(signal("BUY")))["event"] == "placed"   # callisto unaffected
    await ex.close()


async def test_sources_have_separate_caps_and_only_callisto_trails(tmp_path, monkeypatch):
    monkeypatch.setattr(X, "CFG", replace(CFG, trail_start_pips=50.0, trail_distance_pips=30.0,
                                          trail_step_pips=10.0))
    b, ex = await make(tmp_path)
    assert (await ex.on_tickmind(tm(side="BUY", entry=4000.3, stop=3990.0, target=4020.0),
                                 set()))["event"] == "placed"
    assert (await ex.on_signal(signal("BUY")))["event"] == "placed"   # own cap
    b.spot(4007.00, 4007.30)                                           # +67 pips
    await asyncio.sleep(0.05)
    labels = {m.position_id: m.label for m in ex.managed.values()}
    assert [labels[a.positionId] for a in b.amends] == ["callisto"]
    await ex.close()


async def test_tickmind_close_is_followed_and_ids_survive_restart(tmp_path):
    b, ex = await make(tmp_path)
    await ex.on_tickmind(tm(sid="tg-9"), set())
    await ex.close()
    s = settings()
    ex2 = X.Executor(s, connector=b.srv.connector(),
                     tokens=X.TokenCache(s, path=tmp_path / "t2.json"))
    await ex2.connect()
    await ex2.reconcile()
    (m,) = ex2.managed.values()
    assert m.label == "tickmind" and m.post_id == "tg-9"
    await ex2.on_tickmind(tm(sid="tg-9", status="closed"), set())
    assert ex2.managed == {} and b.positions == {}
    await ex2.close()


def test_follower_reads_complete_lines_from_offset(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text('{"a": 1}\n')
    f = X.Follower(p, None)                 # first start: from the end
    assert f.read() == []
    with p.open("a") as fh:
        fh.write('{"b": "é"}\n{"c": 3')     # second line incomplete
    assert f.read() == [{"b": "é"}]
    with p.open("a") as fh:
        fh.write('}\n')
    assert f.read() == [{"c": 3}]
    g = X.Follower(p, f.offset)            # restart from the persisted offset
    assert g.read() == []
