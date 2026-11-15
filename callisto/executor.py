"""cTrader execution for Callisto and Tickmind signals (Open API, protobuf over TLS).

One process, one connection, one token store for both sources: cTrader rotates the refresh
token on every refresh, so two processes on the same account would lock each other out.
Positions are labelled `callisto` or `tickmind`; each source has its own switch, size and cap.

Tickmind (TICKMIND_EVENTS_PATH = tickmind/state/events.jsonl): a signal is traded once, when it
is `active` and its entry/stop/target are known, if it was posted within
TICKMIND_MAX_SIGNAL_AGE_SECONDS and price is within TICKMIND_MAX_ENTRY_SLIP_PIPS of its entry.
The order uses Tickmind's own stop and target (as distances from the fill), no trailing.
When Tickmind marks the signal closed, the position is closed too (TICKMIND_FOLLOW_CLOSE).

Reads the monitor's `signal` events from data/events.jsonl and turns each into a market
order with stop loss and take profit, then trails the stop client-side with the same rule
as the backtest (TRAIL_START / TRAIL_DISTANCE / TRAIL_STEP pips). Uses the repo's cTrader
protocol client (plugins/ctrader).

Safety
- EXECUTION_ENABLED=false (default): every order is built and logged as `would_place`,
  nothing is sent.
- A live account (Spotware `isLive`) also needs LIVE_TRADING_ENABLED=true.
- Only signals posted within MAX_SIGNAL_AGE_SECONDS are traded; a restart never replays old
  ones. MAX_OPEN_POSITIONS caps concurrent Callisto positions.

Commands
  python executor.py check                 auth, account, symbol, a live quote, the order a
                                           sample signal would produce; sends no order
  python executor.py test-order --side BUY place the smallest order with SL/TP, wait for the
                                           fill, close it (live needs --confirm-live too)
  python executor.py run                   the service: follow events.jsonl and trade

Env (callisto/.env)
  CTRADER_CLIENT_ID, CTRADER_CLIENT_SECRET   Open API application (openapi.ctrader.com)
  CTRADER_ACCESS_TOKEN, CTRADER_REFRESH_TOKEN
  CTRADER_ACCOUNT_NUMBER=17249135            broker login; resolved to the ctid account id
  CTRADER_ACCOUNT_ID                         ctid account id (skips the lookup)
  CTRADER_SYMBOLS={"XAUUSD": "XAUUSD"}       signal ticker -> broker symbol name
  EXECUTION_ENABLED=false  LIVE_TRADING_ENABLED=false
  LOTS=0.2  MAX_OPEN_POSITIONS=1  MAX_SIGNAL_AGE_SECONDS=180
  TICKMIND_EVENTS_PATH=/home/ec2-user/tickmind/state/events.jsonl
  TICKMIND_EXECUTION_ENABLED=false  TICKMIND_LOTS=0.2  TICKMIND_MAX_OPEN_POSITIONS=1
  TICKMIND_MAX_SIGNAL_AGE_SECONDS=300  TICKMIND_MAX_ENTRY_SLIP_PIPS=50  TICKMIND_FOLLOW_CLOSE=true
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import requests
from config import CFG
from google.protobuf.message import Message
from ta_plugin_ctrader.errors import CTraderError
from ta_plugin_ctrader.proto import (
    ProtoOAAccountAuthReq,
    ProtoOAAmendPositionSLTPReq,
    ProtoOAApplicationAuthReq,
    ProtoOAClosePositionReq,
    ProtoOAExecutionEvent,
    ProtoOAExecutionType,
    ProtoOANewOrderReq,
    ProtoOAOrderErrorEvent,
    ProtoOAOrderType,
    ProtoOAPositionStatus,
    ProtoOAReconcileReq,
    ProtoOARefreshTokenReq,
    ProtoOASpotEvent,
    ProtoOASubscribeSpotsReq,
    ProtoOASymbolByIdReq,
    ProtoOASymbolsListReq,
    ProtoOATraderReq,
    ProtoOATradeSide,
)
from ta_plugin_ctrader.protocol import CTraderProtocolClient, tls_connector

HOSTS = {"demo": "demo.ctraderapi.com", "live": "live.ctraderapi.com"}
PRICE_SCALE = 100_000  # spot prices and relative SL/TP are in 1/100000 of a price unit
LABEL = "callisto"            # Callisto positions (trailed)
TM_LABEL = "tickmind"         # Tickmind positions (its own stop/target, no trailing)
LABELS = (LABEL, TM_LABEL)
EXEC_LOG = CFG.data_dir / "executions.jsonl"
EXEC_STATE = CFG.data_dir / "executor_state.json"
TOKEN_CACHE = CFG.data_dir / "ctrader_tokens.json"
EXEC_HEARTBEAT = CFG.data_dir / "executor_heartbeat.json"


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name, "").strip().lower()
    return default if not v else v in ("1", "true", "yes", "on")


@dataclass
class ExecSettings:
    client_id: str = os.getenv("CTRADER_CLIENT_ID", "")
    client_secret: str = os.getenv("CTRADER_CLIENT_SECRET", "")
    access_token: str = os.getenv("CTRADER_ACCESS_TOKEN", "")
    refresh_token: str = os.getenv("CTRADER_REFRESH_TOKEN", "")
    account_number: int = int(os.getenv("CTRADER_ACCOUNT_NUMBER") or 0)
    account_id: int = int(os.getenv("CTRADER_ACCOUNT_ID") or 0)
    is_live: bool | None = None
    symbols: dict = field(default_factory=lambda: json.loads(
        os.getenv("CTRADER_SYMBOLS") or '{"XAUUSD": "XAUUSD"}'))
    execution_enabled: bool = _env_bool("EXECUTION_ENABLED")
    live_trading_enabled: bool = _env_bool("LIVE_TRADING_ENABLED")
    lots: float = float(os.getenv("LOTS") or 0.2)
    max_open: int = int(os.getenv("MAX_OPEN_POSITIONS") or 1)
    max_signal_age_s: float = float(os.getenv("MAX_SIGNAL_AGE_SECONDS") or 180)
    # Tickmind source (tickmind/watch_signals.py writes state/events.jsonl)
    tm_events: str = os.getenv("TICKMIND_EVENTS_PATH", "")
    tm_enabled: bool = _env_bool("TICKMIND_EXECUTION_ENABLED")
    tm_lots: float = float(os.getenv("TICKMIND_LOTS") or 0.2)
    tm_max_open: int = int(os.getenv("TICKMIND_MAX_OPEN_POSITIONS") or 1)
    tm_max_age_s: float = float(os.getenv("TICKMIND_MAX_SIGNAL_AGE_SECONDS") or 300)
    tm_max_slip_pips: float = float(os.getenv("TICKMIND_MAX_ENTRY_SLIP_PIPS") or 50)
    tm_follow_close: bool = _env_bool("TICKMIND_FOLLOW_CLOSE", True)

    def missing(self) -> list[str]:
        need = {"CTRADER_CLIENT_ID": self.client_id, "CTRADER_CLIENT_SECRET": self.client_secret,
                "CTRADER_ACCESS_TOKEN": self.access_token}
        out = [k for k, v in need.items() if not v]
        if not (self.account_id or self.account_number):
            out.append("CTRADER_ACCOUNT_NUMBER")
        return out


@dataclass
class SymbolSpec:
    name: str
    symbol_id: int
    digits: int
    lot_size: int      # protocol volume units (0.01 of a unit) per lot
    min_volume: int
    step_volume: int
    max_volume: int

    def volume_for(self, lots: float) -> int:
        raw = int(round(lots * self.lot_size))
        step = max(self.step_volume, 1)
        vol = max(self.min_volume, (raw // step) * step)
        return min(vol, self.max_volume) if self.max_volume else vol


@dataclass
class Managed:
    """A position this executor opened (label callisto or tickmind)."""
    position_id: int
    symbol_id: int
    side: str
    entry: float
    volume: int
    stop_loss: float | None
    take_profit: float | None
    best: float
    post_id: int | str | None = None   # Callisto post id or Tickmind signal id
    opened: str = ""
    label: str = LABEL


def log(ev: dict, quiet: bool = False) -> dict:
    ev = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), **ev}
    with EXEC_LOG.open("a") as f:
        f.write(json.dumps(ev, default=str) + "\n")
    if not quiet:
        print(json.dumps(ev, default=str), flush=True)
    return ev


def resolve_account(s: ExecSettings) -> None:
    """Account number -> ctid id and live flag, through Spotware's REST account list."""
    if s.account_id and s.is_live is not None:
        return
    r = requests.get("https://api.spotware.com/connect/tradingaccounts",
                     params={"access_token": s.access_token}, timeout=20)
    r.raise_for_status()
    data = r.json()
    accounts = data.get("data", data) if isinstance(data, dict) else data
    for a in accounts:
        if (s.account_id and a.get("accountId") == s.account_id) or (
                s.account_number and a.get("accountNumber") == s.account_number):
            s.account_id = int(a["accountId"])
            s.is_live = bool(a.get("live"))
            return
    raise SystemExit(f"account {s.account_number or s.account_id} is not authorised by this "
                     "access token")


class TokenCache:
    """cTrader rotates the refresh token on every refresh; keep the newest pair on disk."""

    def __init__(self, s: ExecSettings, path: Path = TOKEN_CACHE):
        self.path = path
        self.access, self.refresh = s.access_token, s.refresh_token
        if path.exists():
            try:
                d = json.loads(path.read_text())
                self.access, self.refresh = d["access_token"], d["refresh_token"]
            except (ValueError, KeyError):
                pass

    def save(self, access: str, refresh: str, expires_in: int | None):
        self.access, self.refresh = access, refresh
        self.path.write_text(json.dumps({
            "access_token": access, "refresh_token": refresh,
            "expires_at": time.time() + (expires_in or 0), "saved": time.time()}))
        os.chmod(self.path, 0o600)


class Executor:
    def __init__(self, s: ExecSettings, connector=None, tokens: TokenCache | None = None):
        self.s = s
        self.tokens = tokens or TokenCache(s)
        self._connector = connector
        self.client: CTraderProtocolClient | None = None
        self.symbols: dict[str, SymbolSpec] = {}
        self.quotes: dict[int, tuple[float | None, float | None]] = {}
        self.managed: dict[int, Managed] = {}
        self.fills: dict[str, asyncio.Future] = {}   # clientOrderId -> filled position
        self.closes: dict[int, asyncio.Future] = {}  # positionId -> closed
        self._amending: set[int] = set()

    # -- connection -----------------------------------------------------------------------
    @property
    def environment(self) -> str:
        return "live" if self.s.is_live else "demo"

    async def connect(self):
        connector = self._connector or tls_connector(HOSTS[self.environment], 5035)
        self.client = CTraderProtocolClient(connector, on_event=self._on_event,
                                            request_timeout=15.0, heartbeat_interval=5.0)
        await self.client.connect()
        await self.client.request(ProtoOAApplicationAuthReq(
            clientId=self.s.client_id, clientSecret=self.s.client_secret))
        try:
            await self._account_auth()
        except CTraderError as e:
            if "TOKEN" not in e.error_code.upper() or not self.tokens.refresh:
                raise
            await self.refresh_token()
            await self._account_auth()

    async def _account_auth(self):
        await self.client.request(ProtoOAAccountAuthReq(
            ctidTraderAccountId=self.s.account_id, accessToken=self.tokens.access))

    async def refresh_token(self):
        res = await self.client.request(ProtoOARefreshTokenReq(refreshToken=self.tokens.refresh))
        self.tokens.save(res.accessToken, res.refreshToken, getattr(res, "expiresIn", None))
        log({"event": "token_refreshed"})

    async def close(self):
        if self.client:
            await self.client.close()

    async def trader(self) -> dict:
        res = await self.client.request(ProtoOATraderReq(ctidTraderAccountId=self.s.account_id))
        t = res.trader
        money = 10 ** (t.moneyDigits if t.HasField("moneyDigits") else 2)
        return {"balance": t.balance / money, "leverage": t.leverageInCents / 100,
                "broker": t.brokerName, "login": t.traderLogin, "live": self.s.is_live}

    async def symbol(self, ticker: str) -> SymbolSpec:
        name = self.s.symbols.get(ticker.upper(), ticker.upper())
        if name in self.symbols:
            return self.symbols[name]
        lst = await self.client.request(ProtoOASymbolsListReq(
            ctidTraderAccountId=self.s.account_id))
        match = [x for x in lst.symbol if x.symbolName.upper() == name.upper()]
        if not match:
            match = [x for x in lst.symbol
                     if x.symbolName.upper().split(".")[0] == name.upper()]
        if not match:
            raise CTraderError("SYMBOL_NOT_FOUND", f"{name} is not offered on this account")
        full = await self.client.request(ProtoOASymbolByIdReq(
            ctidTraderAccountId=self.s.account_id, symbolId=[match[0].symbolId]))
        x = full.symbol[0]
        spec = SymbolSpec(name=match[0].symbolName, symbol_id=x.symbolId, digits=x.digits,
                          lot_size=x.lotSize, min_volume=x.minVolume,
                          step_volume=x.stepVolume, max_volume=x.maxVolume)
        self.symbols[name] = spec
        await self.client.request(ProtoOASubscribeSpotsReq(
            ctidTraderAccountId=self.s.account_id, symbolId=[spec.symbol_id]))
        return spec

    async def quote(self, spec: SymbolSpec, timeout: float = 10) -> tuple[float, float]:  # noqa: ASYNC109
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            bid, ask = self.quotes.get(spec.symbol_id, (None, None))
            if bid and ask:
                return bid, ask
            await asyncio.sleep(0.1)
        raise CTraderError("NO_QUOTE", f"no spot price for {spec.name} within {timeout}s "
                           "(market closed?)")

    # -- orders ---------------------------------------------------------------------------
    def build_order(self, spec: SymbolSpec, side: str, client_order_id: str,
                    comment: str = "", *, lots: float | None = None,
                    sl_distance: float | None = None, tp_distance: float | None = None,
                    label: str = LABEL) -> ProtoOANewOrderReq:
        """Market order; SL/TP are price distances from the fill (default: env pips)."""
        pip = CFG.pip(spec.name)
        sl = sl_distance if sl_distance is not None else CFG.sl_pips * pip
        tp = tp_distance if tp_distance is not None else CFG.tp_pips * pip
        return ProtoOANewOrderReq(
            ctidTraderAccountId=self.s.account_id, symbolId=spec.symbol_id,
            orderType=ProtoOAOrderType.Value("MARKET"),
            tradeSide=ProtoOATradeSide.Value(side),
            volume=spec.volume_for(self.s.lots if lots is None else lots),
            relativeStopLoss=int(round(sl * PRICE_SCALE)),
            relativeTakeProfit=int(round(tp * PRICE_SCALE)),
            label=label, comment=comment[:100], clientOrderId=client_order_id)

    def can_send(self, source: str = LABEL) -> tuple[bool, str]:
        if source == TM_LABEL and not self.s.tm_enabled:
            return False, "TICKMIND_EXECUTION_ENABLED is false (dry run)"
        if source == LABEL and not self.s.execution_enabled:
            return False, "EXECUTION_ENABLED is false (dry run)"
        if self.s.is_live and not self.s.live_trading_enabled:
            return False, "live account and LIVE_TRADING_ENABLED is false"
        return True, ""

    async def place(self, order: ProtoOANewOrderReq, fill_timeout: float = 20) -> Managed:
        fut = asyncio.get_running_loop().create_future()
        self.fills[order.clientOrderId] = fut
        try:
            first = await self.client.request(order)
            self._on_event(first)
            return await asyncio.wait_for(fut, fill_timeout)
        finally:
            self.fills.pop(order.clientOrderId, None)

    async def close_position(self, m: Managed, timeout: float = 20) -> None:  # noqa: ASYNC109
        fut = asyncio.get_running_loop().create_future()
        self.closes[m.position_id] = fut
        try:
            res = await self.client.request(ProtoOAClosePositionReq(
                ctidTraderAccountId=self.s.account_id, positionId=m.position_id,
                volume=m.volume))
            self._on_event(res)
            await asyncio.wait_for(fut, timeout)
        finally:
            self.closes.pop(m.position_id, None)

    async def reconcile(self):
        res = await self.client.request(ProtoOAReconcileReq(ctidTraderAccountId=self.s.account_id))
        for p in res.position:
            if p.tradeData.label in LABELS and p.positionId not in self.managed:
                self.managed[p.positionId] = self._managed_from(p)
        return res

    # -- events ---------------------------------------------------------------------------
    def _managed_from(self, p, post_id: int | None = None) -> Managed:
        side = ProtoOATradeSide.Name(p.tradeData.tradeSide)
        if post_id is None and p.tradeData.comment:
            # comments are "callisto post <id>" / "tickmind <id>": recover the id after restart
            last = p.tradeData.comment.split()[-1]
            post_id = int(last) if last.isdigit() else last
        return Managed(position_id=p.positionId, symbol_id=p.tradeData.symbolId, side=side,
                       entry=p.price, volume=p.tradeData.volume,
                       stop_loss=p.stopLoss if p.HasField("stopLoss") else None,
                       take_profit=p.takeProfit if p.HasField("takeProfit") else None,
                       best=p.price, post_id=post_id, label=p.tradeData.label or LABEL,
                       opened=datetime.fromtimestamp(p.tradeData.openTimestamp / 1000, UTC)
                       .isoformat(timespec="seconds"))

    def _on_event(self, msg: Message):
        if isinstance(msg, ProtoOASpotEvent):
            bid, ask = self.quotes.get(msg.symbolId, (None, None))
            if msg.HasField("bid"):
                bid = msg.bid / PRICE_SCALE
            if msg.HasField("ask"):
                ask = msg.ask / PRICE_SCALE
            self.quotes[msg.symbolId] = (bid, ask)
            self._trail(msg.symbolId)
        elif isinstance(msg, ProtoOAExecutionEvent):
            et = ProtoOAExecutionType.Name(msg.executionType)
            pos = msg.position if msg.HasField("position") else None
            coid = msg.order.clientOrderId if msg.HasField("order") else ""
            if et == "ORDER_FILLED" and pos is not None:
                closed = pos.positionStatus == ProtoOAPositionStatus.Value("POSITION_STATUS_CLOSED")
                if closed:
                    m = self.managed.pop(pos.positionId, None)
                    deal = msg.deal if msg.HasField("deal") else None
                    log({"event": "position_closed", "position_id": pos.positionId,
                         "price": deal.executionPrice if deal else None,
                         "entry": m.entry if m else None})
                    f = self.closes.get(pos.positionId)
                    if f and not f.done():
                        f.set_result(True)
                elif pos.tradeData.label in LABELS:
                    m = self._managed_from(pos)
                    self.managed[pos.positionId] = m
                    f = self.fills.get(coid)
                    if f and not f.done():
                        f.set_result(m)
            elif et in ("ORDER_REJECTED", "ORDER_CANCELLED", "ORDER_EXPIRED"):
                f = self.fills.get(coid)
                if f and not f.done():
                    f.set_exception(CTraderError(et, msg.errorCode or None))
        elif isinstance(msg, ProtoOAOrderErrorEvent):
            log({"event": "order_error", "code": msg.errorCode, "description": msg.description})
            for f in list(self.fills.values()):
                if not f.done():
                    f.set_exception(CTraderError(msg.errorCode, msg.description or None))

    def _trail(self, symbol_id: int):
        if CFG.trail_start_pips <= 0:
            return
        bid, ask = self.quotes.get(symbol_id, (None, None))
        for m in list(self.managed.values()):
            if (m.symbol_id != symbol_id or m.position_id in self._amending
                    or m.label != LABEL):
                continue
            spec = next((s for s in self.symbols.values() if s.symbol_id == symbol_id), None)
            pip = CFG.pip(spec.name if spec else "XAUUSD")
            buy = m.side == "BUY"
            px = bid if buy else ask   # price the position would close at
            if px is None:
                continue
            m.best = max(m.best, px) if buy else min(m.best, px)
            sgn = 1 if buy else -1
            if sgn * (m.best - m.entry) / pip < CFG.trail_start_pips:
                continue
            new_sl = m.best - sgn * CFG.trail_distance_pips * pip
            if spec:
                new_sl = round(new_sl, spec.digits)
            cur = m.stop_loss if m.stop_loss is not None else m.entry - sgn * 1e9
            if sgn * (new_sl - cur) / pip >= CFG.trail_step_pips:
                self._amending.add(m.position_id)
                asyncio.get_running_loop().create_task(self._amend(m, new_sl))

    async def _amend(self, m: Managed, new_sl: float):
        try:
            kw = {"ctidTraderAccountId": self.s.account_id, "positionId": m.position_id,
                  "stopLoss": new_sl}
            if m.take_profit is not None:
                kw["takeProfit"] = m.take_profit
            if not self.s.execution_enabled:
                m.stop_loss = new_sl
                log({"event": "would_trail", "position_id": m.position_id, "stop_loss": new_sl})
                return
            await self.client.request(ProtoOAAmendPositionSLTPReq(**kw))
            old, m.stop_loss = m.stop_loss, new_sl
            log({"event": "trailed", "position_id": m.position_id, "from": old, "to": new_sl,
                 "best": m.best})
        except Exception as e:  # noqa: BLE001
            log({"event": "trail_error", "position_id": m.position_id, "error": repr(e)})
        finally:
            self._amending.discard(m.position_id)

    # -- signals --------------------------------------------------------------------------
    async def on_signal(self, sig: dict) -> dict:
        posted = datetime.fromisoformat(sig["posted_at"])
        age = (datetime.now(UTC) - posted).total_seconds()
        base = {"source": LABEL, "post_id": sig.get("post_id"), "ticker": sig.get("ticker"),
                "side": sig.get("direction"), "signal_age_s": round(age)}
        if age > self.s.max_signal_age_s:
            return log({"event": "skipped", "reason": f"signal {age:.0f}s old", **base})
        if sig.get("direction") not in ("BUY", "SELL"):
            return log({"event": "skipped", "reason": "no direction", **base})
        mine = [m for m in self.managed.values() if m.label == LABEL]
        if len(mine) >= self.s.max_open:
            return log({"event": "skipped", "reason": f"{len(mine)} open (max {self.s.max_open})",
                        **base})
        try:
            spec = await self.symbol(sig["ticker"])
        except CTraderError as e:
            return log({"event": "skipped", "reason": str(e), **base})
        order = self.build_order(spec, sig["direction"], f"callisto-{sig.get('post_id')}",
                                 comment=f"callisto post {sig.get('post_id')}")
        detail = {"symbol": spec.name, "volume": order.volume, "lots": self.s.lots,
                  "sl_pips": CFG.sl_pips, "tp_pips": CFG.tp_pips,
                  "quote": self.quotes.get(spec.symbol_id)}
        ok, why = self.can_send(LABEL)
        if not ok:
            return log({"event": "would_place", "reason": why, **base, **detail})
        try:
            m = await self.place(order)
        except Exception as e:  # noqa: BLE001
            return log({"event": "order_failed", "error": repr(e), **base, **detail})
        m.post_id = sig.get("post_id")
        return log({"event": "placed", **base, **detail, "position": asdict(m)})

    async def on_tickmind(self, ev: dict, decided: set[str]) -> dict | None:
        """One Tickmind watcher event. A signal is decided once: when it is active and its
        levels are known (the first `new` often has zeros until the card is read)."""
        sid = str(ev.get("id"))
        status = ev.get("status")
        if status == "closed":
            if not self.s.tm_follow_close:
                return None
            for m in [m for m in self.managed.values()
                      if m.label == TM_LABEL and str(m.post_id) == sid]:
                if not self.s.tm_enabled:
                    log({"event": "would_close", "source": TM_LABEL, "post_id": sid,
                         "position_id": m.position_id, "reason": "tickmind closed the signal"})
                    continue
                try:
                    await self.close_position(m)
                    log({"event": "closed_with_signal", "source": TM_LABEL, "post_id": sid,
                         "position_id": m.position_id, "result_pips": ev.get("result")})
                except Exception as e:  # noqa: BLE001
                    log({"event": "close_failed", "source": TM_LABEL, "post_id": sid,
                         "position_id": m.position_id, "error": repr(e)})
            return None
        if status != "active" or sid in decided:
            return None
        entry, stop, target = (float(ev.get(k) or 0) for k in ("entry", "stop", "target"))
        if not (entry and stop and target):
            return None  # levels not known yet; wait for the update
        decided.add(sid)
        side = ev.get("side")
        age = time.time() - float(ev.get("posted_unix") or 0)
        base = {"source": TM_LABEL, "post_id": sid, "ticker": ev.get("pair"), "side": side,
                "signal_age_s": round(age), "entry": entry, "stop": stop, "target": target}
        if side not in ("BUY", "SELL"):
            return log({"event": "skipped", "reason": "no direction", **base})
        if age > self.s.tm_max_age_s:
            return log({"event": "skipped", "reason": f"signal {age:.0f}s old", **base})
        mine = [m for m in self.managed.values() if m.label == TM_LABEL]
        if len(mine) >= self.s.tm_max_open:
            return log({"event": "skipped",
                        "reason": f"{len(mine)} open (max {self.s.tm_max_open})", **base})
        try:
            spec = await self.symbol(ev.get("pair") or "XAUUSD")
            bid, ask = await self.quote(spec, timeout=5)
        except CTraderError as e:
            return log({"event": "skipped", "reason": str(e), **base})
        pip = CFG.pip(spec.name)
        sgn = 1 if side == "BUY" else -1
        px = ask if side == "BUY" else bid
        slip = sgn * (px - entry) / pip          # >0: price already ran in the signal's favour
        sl_dist, tp_dist = sgn * (px - stop), sgn * (target - px)
        base.update(price=px, slip_pips=round(slip, 1))
        if abs(slip) > self.s.tm_max_slip_pips:
            return log({"event": "skipped", "reason": f"price {slip:+.0f} pips from entry "
                        f"(max {self.s.tm_max_slip_pips:g})", **base})
        if sl_dist <= 0 or tp_dist <= 0:
            return log({"event": "skipped", "reason": "price already beyond stop or target",
                        **base})
        order = self.build_order(spec, side, f"tickmind-{sid}", comment=f"tickmind {sid}",
                                 lots=self.s.tm_lots, sl_distance=sl_dist, tp_distance=tp_dist,
                                 label=TM_LABEL)
        detail = {"symbol": spec.name, "volume": order.volume, "lots": self.s.tm_lots,
                  "sl_pips": round(sl_dist / pip, 1), "tp_pips": round(tp_dist / pip, 1)}
        ok, why = self.can_send(TM_LABEL)
        if not ok:
            return log({"event": "would_place", "reason": why, **base, **detail})
        try:
            m = await self.place(order)
        except Exception as e:  # noqa: BLE001
            return log({"event": "order_failed", "error": repr(e), **base, **detail})
        m.post_id = sid
        return log({"event": "placed", **base, **detail, "position": asdict(m)})


# -- event-log follower -------------------------------------------------------------------
def _load_state() -> dict:
    try:
        return json.loads(EXEC_STATE.read_text())
    except (OSError, ValueError):
        return {}


def _write_hb(ex: Executor, started: str, error: str | None = None):
    EXEC_HEARTBEAT.write_text(json.dumps({
        "started": started, "last_beat": datetime.now(UTC).isoformat(timespec="seconds"),
        "connected": bool(ex.client and ex.client.is_connected),
        "environment": ex.environment, "account_id": ex.s.account_id,
        "account_number": ex.s.account_number, "execution_enabled": ex.s.execution_enabled,
        "live_trading_enabled": ex.s.live_trading_enabled, "lots": ex.s.lots,
        "sources": {
            LABEL: {"enabled": ex.s.execution_enabled, "lots": ex.s.lots,
                    "max_open": ex.s.max_open, "sl_pips": CFG.sl_pips, "tp_pips": CFG.tp_pips,
                    "trail": [CFG.trail_start_pips, CFG.trail_distance_pips,
                              CFG.trail_step_pips]},
            TM_LABEL: {"enabled": ex.s.tm_enabled, "watching": bool(ex.s.tm_events),
                       "lots": ex.s.tm_lots, "max_open": ex.s.tm_max_open,
                       "max_age_s": ex.s.tm_max_age_s,
                       "max_slip_pips": ex.s.tm_max_slip_pips,
                       "follow_close": ex.s.tm_follow_close}},
        "open_positions": [asdict(m) for m in ex.managed.values()],
        "quotes": {str(k): v for k, v in ex.quotes.items()}, "last_error": error}))


class Follower:
    """New complete lines of an append-only JSONL file, from a persisted byte offset."""

    def __init__(self, path: Path, offset: int | None):
        self.path = path
        # first start: begin at the end, never trade history
        self.offset = offset if offset is not None else (
            path.stat().st_size if path.exists() else 0)

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self.offset:      # file was truncated or replaced
            self.offset = 0
        if size == self.offset:
            return []
        with self.path.open("rb") as f:
            f.seek(self.offset)
            chunk = f.read()
        done = chunk.rfind(b"\n") + 1
        self.offset += done
        out = []
        for line in chunk[:done].splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


async def run_service(s: ExecSettings):
    resolve_account(s)
    started = datetime.now(UTC).isoformat(timespec="seconds")
    st = _load_state()
    callisto = Follower(CFG.data_dir / "events.jsonl", st.get("offset"))
    tickmind = Follower(Path(s.tm_events), st.get("tickmind_offset")) if s.tm_events else None
    decided: set[str] = set(st.get("tickmind_decided", []))

    def save_state():
        EXEC_STATE.write_text(json.dumps({
            "offset": callisto.offset,
            "tickmind_offset": tickmind.offset if tickmind else None,
            "tickmind_decided": sorted(decided)[-500:]}))

    save_state()
    backoff = 5
    while True:
        ex = Executor(s)
        try:
            await ex.connect()
            await ex.reconcile()
            for t in set(s.symbols):
                await ex.symbol(t)
            log({"event": "executor_ready", "environment": ex.environment,
                 "account_id": s.account_id, "callisto_enabled": s.execution_enabled,
                 "tickmind_enabled": s.tm_enabled if tickmind else None,
                 "live_trading_enabled": s.live_trading_enabled,
                 "managed": len(ex.managed)})
            backoff = 5
            while ex.client.is_connected:
                changed = False
                for ev in callisto.read():
                    changed = True
                    if ev.get("event") == "signal":
                        await ex.on_signal(ev)
                for ev in (tickmind.read() if tickmind else []):
                    changed = True
                    await ex.on_tickmind(ev, decided)
                if changed:
                    save_state()
                _write_hb(ex, started)
                await asyncio.sleep(1)
            raise ConnectionError("cTrader connection closed")
        except Exception as e:  # noqa: BLE001 - reconnect with backoff
            log({"event": "executor_error", "error": repr(e)})
            _write_hb(ex, started, repr(e))
            await ex.close()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)


# -- CLI checks ---------------------------------------------------------------------------
async def cmd_check(s: ExecSettings):
    resolve_account(s)
    ex = Executor(s)
    await ex.connect()
    try:
        print("account:", await ex.trader())
        spec = await ex.symbol("XAUUSD")
        print("symbol:", asdict(spec))
        try:
            bid, ask = await ex.quote(spec)
            print(f"quote: bid {bid} ask {ask}")
        except CTraderError as e:
            print("quote:", e)
        o = ex.build_order(spec, "BUY", "callisto-check")
        print("sample order (not sent):", {"volume": o.volume, "lots": s.lots,
              "relativeStopLoss": o.relativeStopLoss, "relativeTakeProfit": o.relativeTakeProfit})
        res = await ex.reconcile()
        print(f"open positions: {len(res.position)} (callisto: {len(ex.managed)}), "
              f"pending orders: {len(res.order)}")
        print("would send callisto orders:", ex.can_send(LABEL))
        print("tickmind source:", {"events": s.tm_events or None,
              "readable": bool(s.tm_events) and os.access(s.tm_events, os.R_OK),
              "lots": s.tm_lots, "max_open": s.tm_max_open, "max_age_s": s.tm_max_age_s,
              "max_slip_pips": s.tm_max_slip_pips, "follow_close": s.tm_follow_close},
              "would send:", ex.can_send(TM_LABEL))
    finally:
        await ex.close()


async def cmd_test_order(s: ExecSettings, side: str, confirm_live: bool, hold: float):
    resolve_account(s)
    if s.is_live and not (confirm_live and s.live_trading_enabled):
        raise SystemExit("account is LIVE: needs LIVE_TRADING_ENABLED=true and --confirm-live")
    ex = Executor(s)
    await ex.connect()
    try:
        spec = await ex.symbol("XAUUSD")
        bid, ask = await ex.quote(spec)
        s.lots = spec.min_volume / spec.lot_size  # smallest size the symbol allows
        order = ex.build_order(spec, side, f"callisto-test-{int(time.time())}",
                               comment="callisto connectivity test")
        print(f"placing {side} {spec.name} volume {order.volume} ({s.lots} lots) at "
              f"~{ask if side == 'BUY' else bid}, SL {CFG.sl_pips:g}p TP {CFG.tp_pips:g}p")
        m = await ex.place(order)
        log({"event": "test_filled", "position": asdict(m)})
        await asyncio.sleep(hold)
        await ex.close_position(m)
        log({"event": "test_closed", "position_id": m.position_id})
        print("order placing works: filled and closed")
    finally:
        await ex.close()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    t = sub.add_parser("test-order")
    t.add_argument("--side", choices=["BUY", "SELL"], default="BUY")
    t.add_argument("--confirm-live", action="store_true")
    t.add_argument("--hold", type=float, default=3.0, help="seconds to hold before closing")
    sub.add_parser("run")
    args = ap.parse_args()
    s = ExecSettings()
    if s.missing():
        raise SystemExit(f"missing in .env: {', '.join(s.missing())}")
    if args.cmd == "check":
        asyncio.run(cmd_check(s))
    elif args.cmd == "test-order":
        try:
            asyncio.run(cmd_test_order(s, args.side, args.confirm_live, args.hold))
        except CTraderError as e:
            hint = {"MARKET_CLOSED": "the order reached the broker; retry when the market opens",
                    "NOT_ENOUGH_MONEY": "free margin too low for the minimum size"}
            print(f"broker rejected the test order: {e}"
                  + (f" ({hint[e.error_code]})" if e.error_code in hint else ""))
            return 1
    else:
        asyncio.run(run_service(s))


if __name__ == "__main__":
    sys.exit(main())
