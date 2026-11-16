"""Executor tests against a fake execution service (httpx.MockTransport, no network).

The fake implements the slice of services/execution-service the executor uses: accounts,
quote, positions, /v1/orders, /v1/positions/protection, /v1/positions/close and
/v1/operations/{id}, with fills at the current bid/ask.
"""
from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import executor as X
import httpx
import pytest
from config import CFG


class FakeService:
    def __init__(self, is_live=False, bid=4000.00, ask=4000.30, pending_first=False):
        self.is_live, self.bid, self.ask = is_live, bid, ask
        self.pending_first = pending_first
        self.positions: dict[int, dict] = {}
        self.ops: dict[str, dict] = {}
        self.requests: list[tuple[str, str, dict | None]] = []
        self.reject_orders: str | None = None
        self.next_pid = 1000

    def _op(self, body, action, target, state="succeeded"):
        op = {"operation_id": body["operation_id"], "action": action, "state": state,
              "targets": [target], "created_at": body["occurred_at"],
              "updated_at": body["occurred_at"]}
        self.ops[body["operation_id"]] = op
        return op

    def handler(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        self.requests.append((req.method, req.url.path, body))
        assert req.headers.get("X-API-Key") == "k" * 32
        p = req.url.path
        if p == "/v1/accounts":
            return httpx.Response(200, json={"accounts": [
                {"alias": "ftmo", "is_live": self.is_live, "ready": True}]})
        if p == "/health/trading-ready":
            return httpx.Response(200, json={"status": "ready", "details": {}})
        if p == "/v1/accounts/ftmo/quote":
            assert req.url.params["instrument"] == "XAUUSD"
            return httpx.Response(200, json={"symbol": "XAUUSD", "bid": self.bid,
                                             "ask": self.ask, "price": self.bid})
        if p == "/v1/accounts/ftmo/positions":
            return httpx.Response(200, json=list(self.positions.values()))
        if p.startswith("/v1/operations/"):
            op = self.ops[p.rsplit("/", 1)[1]]
            op["state"] = "succeeded"
            return httpx.Response(200, json=op)
        if p == "/v1/orders":
            UUID(body["operation_id"])
            assert body["execution_type"] == "market"
            assert body["source"] in ("callisto", "tickmind")
            if self.reject_orders:
                return httpx.Response(201, json=self._op(body, "place_order", {
                    "account": "ftmo", "state": "rejected", "error_code": self.reject_orders,
                    "error_message": "rejected by broker", "updated_at": body["occurred_at"]},
                    state="rejected"))
            t = body["targets"][0]
            buy = body["direction"] == "buy"
            px = self.ask if buy else self.bid
            sgn = 1 if buy else -1
            pid = self.next_pid
            self.next_pid += 1
            self.positions[pid] = {
                "account": "ftmo", "position_id": pid, "instrument": body["instrument"],
                "volume_lots": t["volume_lots"], "direction": body["direction"], "price": px,
                "stop_loss": round(px - sgn * float(body["stop_loss_distance"]), 2),
                "take_profit": round(px + sgn * float(body["take_profit_distance"]), 2)}
            target = {"account": "ftmo", "state": "filled", "position_id": pid,
                      "execution_price": str(px), "executed_volume_lots": t["volume_lots"],
                      "updated_at": body["occurred_at"]}
            if self.pending_first:
                return httpx.Response(202, json=self._op(body, "place_order", target,
                                                         state="pending"))
            return httpx.Response(201, json=self._op(body, "place_order", target))
        if p == "/v1/positions/protection":
            t = body["targets"][0]
            self.positions[t["position_id"]]["stop_loss"] = float(t["stop_loss"])
            return httpx.Response(201, json=self._op(body, "amend_position", {
                "account": "ftmo", "state": "amended", "position_id": t["position_id"],
                "updated_at": body["occurred_at"]}))
        if p == "/v1/positions/close":
            t = body["targets"][0]
            self.positions.pop(t["position_id"])
            return httpx.Response(201, json=self._op(body, "close_position", {
                "account": "ftmo", "state": "filled", "position_id": t["position_id"],
                "updated_at": body["occurred_at"]}))
        return httpx.Response(404, json={"error": {"code": "not_found", "message": p}})

    def calls(self, path: str) -> list[dict]:
        return [b for m, p, b in self.requests if m == "POST" and p == path]


def settings(**kw) -> X.ExecSettings:
    s = X.ExecSettings(url="http://exec.test", api_key="k" * 32, account="ftmo",
                       instruments={"XAUUSD": "XAUUSD"}, execution_enabled=True, lots=0.1,
                       max_open=1, max_signal_age_s=180, max_entry_slip_pips=80,
                       tm_events="", tm_enabled=True, tm_lots=0.1, tm_max_open=1,
                       tm_max_age_s=300, tm_max_slip_pips=50, tm_follow_close=True)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


@pytest.fixture(autouse=True)
def _tmp_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(X, "EXEC_LOG", tmp_path / "executions.jsonl")


async def make(tmp_path, svc: FakeService | None = None, **kw):
    svc = svc or FakeService()
    s = settings(**kw)
    client = X.ExecClient(s, transport=httpx.MockTransport(svc.handler))
    ex = X.Executor(s, client, positions_path=tmp_path / "positions.json")
    await ex.start()
    return svc, ex


def signal(side="BUY", age_s=5, post_id=1, entry=None):
    return {"event": "signal", "ticker": "XAUUSD", "direction": side, "post_id": post_id,
            "entry": entry,
            "posted_at": (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()}


async def test_signal_places_market_order_through_the_service(tmp_path):
    svc, ex = await make(tmp_path)
    out = await ex.on_signal(signal("BUY"))
    assert out["event"] == "placed"
    (o,) = svc.calls("/v1/orders")
    pip = CFG.pip("XAUUSD")
    assert o["source"] == "callisto" and o["direction"] == "buy" and o["instrument"] == "XAUUSD"
    assert o["targets"] == [{"account": "ftmo", "volume_lots": "0.1"}]
    assert float(o["stop_loss_distance"]) == pytest.approx(CFG.sl_pips * pip)
    assert float(o["take_profit_distance"]) == pytest.approx(CFG.tp_pips * pip)
    m = out["position"]
    assert m["entry"] == 4000.30 and m["stop_loss"] == round(4000.30 - CFG.sl_pips * pip, 2)
    assert json.loads((tmp_path / "positions.json").read_text())[0]["label"] == "callisto"
    await ex.svc.close()


async def test_pending_operation_is_polled_until_settled(tmp_path):
    svc, ex = await make(tmp_path, FakeService(pending_first=True))
    out = await ex.on_signal(signal("SELL"))
    assert out["event"] == "placed" and out["position"]["entry"] == 4000.00
    assert any(p.startswith("/v1/operations/") for _, p, _ in svc.requests)
    await ex.svc.close()


async def test_dry_run_sends_nothing(tmp_path):
    svc, ex = await make(tmp_path, execution_enabled=False)
    out = await ex.on_signal(signal("SELL"))
    assert out["event"] == "would_place" and "dry run" in out["reason"]
    assert svc.calls("/v1/orders") == []
    await ex.svc.close()


async def test_stale_signal_and_position_cap(tmp_path):
    svc, ex = await make(tmp_path)
    assert (await ex.on_signal(signal(age_s=600)))["event"] == "skipped"
    assert (await ex.on_signal(signal(post_id=1)))["event"] == "placed"
    capped = await ex.on_signal(signal(post_id=2))
    assert capped["event"] == "skipped" and "max 1" in capped["reason"]
    assert len(svc.calls("/v1/orders")) == 1
    await ex.svc.close()


async def test_broker_rejection_surfaces(tmp_path):
    svc = FakeService()
    svc.reject_orders = "NOT_ENOUGH_MONEY"
    svc, ex = await make(tmp_path, svc)
    out = await ex.on_signal(signal())
    assert out["event"] == "order_failed" and "NOT_ENOUGH_MONEY" in out["error"]
    assert ex.managed == {}
    await ex.svc.close()


async def test_callisto_skips_chased_entries(tmp_path):
    svc, ex = await make(tmp_path)                       # bid 4000.00 / ask 4000.30
    chased = await ex.on_signal(signal("BUY", entry=3990.80, post_id=1))
    assert chased["event"] == "skipped" and "past the entry" in chased["reason"]
    assert chased["slip_pips"] == 95.0
    ok = await ex.on_signal(signal("SELL", entry=4000.50, post_id=2))
    assert ok["event"] == "placed" and ok["slip_pips"] == 5.0
    assert len(svc.calls("/v1/orders")) == 1
    await ex.svc.close()


async def test_callisto_price_against_signal_is_not_chasing(tmp_path):
    svc, ex = await make(tmp_path)
    out = await ex.on_signal(signal("BUY", entry=4010.30))
    assert out["event"] == "placed" and out["slip_pips"] == -100.0
    await ex.svc.close()


async def test_trailing_moves_stop_in_steps(tmp_path, monkeypatch):
    monkeypatch.setattr(X, "CFG", replace(CFG, trail_start_pips=50.0, trail_distance_pips=30.0,
                                          trail_step_pips=10.0))
    svc, ex = await make(tmp_path)
    await ex.on_signal(signal("BUY"))          # entry 4000.30

    async def tick(bid):
        svc.bid, svc.ask = bid, bid + 0.30
        await ex.quote("XAUUSD")
        await ex.trail()

    await tick(4004.00)                        # +37 pips: below start
    assert svc.calls("/v1/positions/protection") == []
    await tick(4006.00)                        # +57 pips: stop -> 4006.00 - 3.00
    await tick(4006.50)                        # only 5 pips better: below step
    await tick(4007.20)                        # 12 pips better
    amends = [float(b["targets"][0]["stop_loss"]) for b in
              svc.calls("/v1/positions/protection")]
    assert amends == [4003.00, 4004.20]
    assert next(iter(ex.managed.values())).stop_loss == 4004.20
    await ex.svc.close()


async def test_closed_positions_are_forgotten_and_restart_resumes(tmp_path):
    svc, ex = await make(tmp_path)
    await ex.on_signal(signal("SELL", post_id=7))
    await ex.svc.close()
    s = settings()
    ex2 = X.Executor(s, X.ExecClient(s, transport=httpx.MockTransport(svc.handler)),
                     positions_path=tmp_path / "positions.json")
    await ex2.start()                         # restart: still open at the broker
    (m,) = ex2.managed.values()
    assert m.post_id == 7 and m.label == "callisto"
    svc.positions.clear()                     # stop loss hit at the broker
    await ex2.reconcile()
    assert ex2.managed == {}
    logged = [json.loads(line) for line in X.EXEC_LOG.read_text().splitlines()]
    assert logged[-1]["event"] == "position_closed" and logged[-1]["post_id"] == 7
    await ex2.svc.close()


# -- Tickmind source ---------------------------------------------------------------------
def tm(status="active", entry=4000.0, stop=4013.0, target=3981.0, side="SELL", age_s=20,
       sid="tg-1", kind="update"):
    return {"seen_utc": datetime.now(UTC).isoformat(), "kind": kind, "id": sid,
            "posted_unix": time.time() - age_s, "pair": "XAUUSD", "side": side,
            "entry": entry, "stop": stop, "target": target, "status": status, "result": None}


async def test_tickmind_waits_for_levels_then_uses_its_stop_and_target(tmp_path):
    svc, ex = await make(tmp_path)
    decided: set[str] = set()
    assert await ex.on_tickmind(tm(entry=0, stop=0, target=0, kind="new"), decided) is None
    out = await ex.on_tickmind(tm(), decided)              # SELL at bid 4000.00
    assert out["event"] == "placed" and out["source"] == "tickmind"
    (o,) = svc.calls("/v1/orders")
    assert o["source"] == "tickmind" and o["note"] == "tickmind tg-1"
    assert float(o["stop_loss_distance"]) == pytest.approx(13.0)
    assert float(o["take_profit_distance"]) == pytest.approx(19.0)
    assert await ex.on_tickmind(tm(), decided) is None       # decided once
    await ex.svc.close()


async def test_tickmind_guards(tmp_path):
    svc, ex = await make(tmp_path)
    d: set[str] = set()
    stale = await ex.on_tickmind(tm(sid="a", age_s=900), d)
    assert stale["event"] == "skipped" and "old" in stale["reason"]
    late = await ex.on_tickmind(tm(sid="b", entry=4008.0, stop=4020.0, target=3990.0), d)
    assert late["event"] == "skipped" and "pips from entry" in late["reason"]
    beyond = await ex.on_tickmind(tm(sid="c", entry=4002.0, stop=4013.0, target=4001.0), d)
    assert beyond["event"] == "skipped" and "beyond" in beyond["reason"]
    assert svc.calls("/v1/orders") == []
    await ex.svc.close()


async def test_sources_have_separate_switches_caps_and_only_callisto_trails(tmp_path,
                                                                             monkeypatch):
    monkeypatch.setattr(X, "CFG", replace(CFG, trail_start_pips=50.0, trail_distance_pips=30.0,
                                          trail_step_pips=10.0))
    svc, ex = await make(tmp_path, tm_enabled=False)
    off = await ex.on_tickmind(tm(sid="z"), set())
    assert off["event"] == "would_place" and "TICKMIND_EXECUTION_ENABLED" in off["reason"]
    ex.s.tm_enabled = True
    assert (await ex.on_tickmind(tm(side="BUY", entry=4000.3, stop=3990.0, target=4020.0),
                                 set()))["event"] == "placed"
    assert (await ex.on_signal(signal("BUY")))["event"] == "placed"   # own cap
    svc.bid, svc.ask = 4007.00, 4007.30
    await ex.quote("XAUUSD")
    await ex.trail()
    labels = {m.position_id: m.label for m in ex.managed.values()}
    assert [labels[b["targets"][0]["position_id"]]
            for b in svc.calls("/v1/positions/protection")] == ["callisto"]
    await ex.svc.close()


async def test_tickmind_close_is_followed(tmp_path):
    svc, ex = await make(tmp_path)
    await ex.on_tickmind(tm(sid="tg-9"), set())
    await ex.on_tickmind(tm(sid="tg-9", status="closed"), set())
    assert ex.managed == {} and svc.positions == {}
    (c,) = svc.calls("/v1/positions/close")
    assert c["source"] == "tickmind" and c["targets"][0]["volume_lots"] == "0.1"
    await ex.svc.close()


async def test_service_down_is_a_clean_error(tmp_path):
    def boom(_req):
        raise httpx.ConnectError("refused")

    s = settings()
    ex = X.Executor(s, X.ExecClient(s, transport=httpx.MockTransport(boom)),
                    positions_path=tmp_path / "p.json")
    with pytest.raises(X.ServiceError) as e:
        await ex.start()
    assert e.value.code == "transport_error"
    await ex.svc.close()


def test_no_broker_credentials_in_settings():
    names = set(X.ExecSettings.__dataclass_fields__)
    assert not any(w in n for n in names for w in ("token", "secret", "client_id"))


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
    assert X.Follower(p, f.offset).read() == []
