"""Callisto and Tickmind execution through the shared cTrader execution service.

This process holds NO broker credentials. Orders, stop amendments, closes, positions and
quotes all go over HTTP to services/execution-service (cTrader `production` profile), which
owns the single cTrader connection and OAuth token store for every strategy on the host.
Its own fuses (TRADING_ENABLED, LIVE_TRADING_ENABLED, ALLOWED_ORDER_SOURCES, MAX_VOLUME_LOTS)
apply on top of the switches here.

Sources
- Callisto (data/events.jsonl, `signal` events from monitor.py): market order with
  SL_PIPS / TP_PIPS distances, then a client-side trailing stop (TRAIL_START / DISTANCE /
  STEP pips) amended through the service. Skipped when price already ran more than
  MAX_ENTRY_SLIP_PIPS past the channel's stated entry.
- Tickmind (TICKMIND_EVENTS_PATH = tickmind/state/events.jsonl): traded once, when `active`
  with entry/stop/target known, if posted within TICKMIND_MAX_SIGNAL_AGE_SECONDS and price is
  within TICKMIND_MAX_ENTRY_SLIP_PIPS of its entry. Uses Tickmind's own stop and target (as
  distances from the current price), no trailing; closed when Tickmind closes the signal.

The service's position list carries no strategy label, so this process keeps its own map of
the positions it opened (data/positions.json) and reconciles it against the account.

Commands
  python executor.py check                 service health, account, quote, sample order
  python executor.py test-order --side BUY smallest order with SL/TP, then close it
                                           (a live account needs --confirm-live)
  python executor.py run                   the service loop

Env (callisto/.env)
  EXECUTION_URL=http://127.0.0.1:8010   EXECUTION_API_KEY=...   EXECUTION_ACCOUNT=ftmo
  INSTRUMENT_MAP={"XAUUSD": "XAUUSD"}   signal ticker -> service instrument (canonical)
  EXECUTION_ENABLED=false  LOTS=0.2  MAX_OPEN_POSITIONS=1  MAX_SIGNAL_AGE_SECONDS=180
  MAX_ENTRY_SLIP_PIPS=80
  TICKMIND_EVENTS_PATH=  TICKMIND_EXECUTION_ENABLED=false  TICKMIND_LOTS=0.2
  TICKMIND_MAX_OPEN_POSITIONS=1  TICKMIND_MAX_SIGNAL_AGE_SECONDS=300
  TICKMIND_MAX_ENTRY_SLIP_PIPS=50  TICKMIND_FOLLOW_CLOSE=true
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
from typing import Any
from uuid import uuid4

import httpx
from config import CFG

LABEL = "callisto"            # Callisto positions (trailed)
TM_LABEL = "tickmind"         # Tickmind positions (its own stop/target, no trailing)
LABELS = (LABEL, TM_LABEL)
EXEC_LOG = CFG.data_dir / "executions.jsonl"
EXEC_STATE = CFG.data_dir / "executor_state.json"
POSITIONS = CFG.data_dir / "positions.json"
EXEC_HEARTBEAT = CFG.data_dir / "executor_heartbeat.json"
DONE_STATES = {"succeeded", "partial_failure", "rejected", "unknown"}


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name, "").strip().lower()
    return default if not v else v in ("1", "true", "yes", "on")


@dataclass
class ExecSettings:
    url: str = os.getenv("EXECUTION_URL", "http://127.0.0.1:8010")
    api_key: str = os.getenv("EXECUTION_API_KEY", "")
    account: str = os.getenv("EXECUTION_ACCOUNT", "")
    instruments: dict = field(default_factory=lambda: json.loads(
        os.getenv("INSTRUMENT_MAP") or '{"XAUUSD": "XAUUSD"}'))
    execution_enabled: bool = _env_bool("EXECUTION_ENABLED")
    lots: float = float(os.getenv("LOTS") or 0.2)
    max_open: int = int(os.getenv("MAX_OPEN_POSITIONS") or 1)
    max_signal_age_s: float = float(os.getenv("MAX_SIGNAL_AGE_SECONDS") or 180)
    # Skip a Callisto signal when price has already run this many pips past the channel's
    # stated entry in the signal's direction (0 = off). 80 kept 51/64 month trades and the
    # best per-trade result in the backtest (RESULTS.md); 50 halves the trades.
    max_entry_slip_pips: float = float(os.getenv("MAX_ENTRY_SLIP_PIPS") or 80)
    tm_events: str = os.getenv("TICKMIND_EVENTS_PATH", "")
    tm_enabled: bool = _env_bool("TICKMIND_EXECUTION_ENABLED")
    tm_lots: float = float(os.getenv("TICKMIND_LOTS") or 0.2)
    tm_max_open: int = int(os.getenv("TICKMIND_MAX_OPEN_POSITIONS") or 1)
    tm_max_age_s: float = float(os.getenv("TICKMIND_MAX_SIGNAL_AGE_SECONDS") or 300)
    tm_max_slip_pips: float = float(os.getenv("TICKMIND_MAX_ENTRY_SLIP_PIPS") or 50)
    tm_follow_close: bool = _env_bool("TICKMIND_FOLLOW_CLOSE", True)

    def missing(self) -> list[str]:
        return [k for k, v in {"EXECUTION_API_KEY": self.api_key,
                               "EXECUTION_ACCOUNT": self.account}.items() if not v]


@dataclass
class Managed:
    """A position this executor opened."""
    position_id: int
    instrument: str
    side: str
    entry: float
    volume_lots: float
    stop_loss: float | None
    take_profit: float | None
    best: float
    label: str = LABEL
    post_id: int | str | None = None
    opened: str = ""


class ServiceError(RuntimeError):
    def __init__(self, code: str, message: str = "", status: int | None = None):
        super().__init__(f"{code}: {message}" if message else code)
        self.code, self.status = code, status


def log(ev: dict, quiet: bool = False) -> dict:
    ev = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), **ev}
    with EXEC_LOG.open("a") as f:
        f.write(json.dumps(ev, default=str) + "\n")
    if not quiet:
        print(json.dumps(ev, default=str), flush=True)
    return ev


class ExecClient:
    """The execution service's HTTP API, scoped to one account alias."""

    def __init__(self, s: ExecSettings, transport: httpx.AsyncBaseTransport | None = None):
        self.s = s
        self.http = httpx.AsyncClient(base_url=s.url.rstrip("/"), timeout=20,
                                      headers={"X-API-Key": s.api_key}, transport=transport)
        self.ok_at: float | None = None
        self.last_error: str | None = None

    async def close(self):
        await self.http.aclose()

    async def _call(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = await self.http.request(method, path, **kw)
        except httpx.HTTPError as e:
            self.last_error = f"transport: {e!r}"
            raise ServiceError("transport_error", repr(e)) from e
        if r.status_code < 500 or r.status_code == 503:
            self.ok_at = time.time()
            self.last_error = None
        return r

    @staticmethod
    def _error(r: httpx.Response) -> ServiceError:
        try:
            e = r.json().get("error") or {}
            return ServiceError(e.get("code", f"http_{r.status_code}"), e.get("message", ""),
                                r.status_code)
        except ValueError:
            return ServiceError(f"http_{r.status_code}", r.text[:200], r.status_code)

    async def readiness(self) -> dict:
        r = await self._call("GET", "/health/trading-ready")
        try:
            body = r.json()
        except ValueError:
            body = {}
        return {"ready": r.status_code == 200, **body}

    async def account_status(self) -> dict:
        r = await self._call("GET", "/v1/accounts")
        if r.status_code != 200:
            raise self._error(r)
        for a in r.json().get("accounts", []):
            if a.get("alias") == self.s.account:
                return a
        raise ServiceError("account_not_found", f"{self.s.account} is not served")

    async def quote(self, instrument: str) -> tuple[float, float]:
        r = await self._call("GET", f"/v1/accounts/{self.s.account}/quote",
                             params={"instrument": instrument})
        if r.status_code != 200:
            raise self._error(r)
        q = r.json()
        if q.get("bid") is None or q.get("ask") is None:
            raise ServiceError("quote_one_sided", f"{instrument} has no bid/ask")
        return float(q["bid"]), float(q["ask"])

    async def positions(self) -> list[dict]:
        r = await self._call("GET", f"/v1/accounts/{self.s.account}/positions")
        if r.status_code != 200:
            raise self._error(r)
        return r.json()

    async def operate(self, path: str, body: dict, wait_s: float = 30) -> dict:
        """POST an operation; a 202 is polled until it settles. The service keys every
        operation by operation_id, so a retry with the same id is idempotent."""
        r = await self._call("POST", path, json=body)
        if r.status_code not in (201, 202):
            raise self._error(r)
        op = r.json()
        end = time.monotonic() + wait_s
        while op.get("state") not in DONE_STATES and time.monotonic() < end:
            await asyncio.sleep(0.5)
            r = await self._call("GET", f"/v1/operations/{op['operation_id']}")
            if r.status_code == 200:
                op = r.json()
        return op

    @staticmethod
    def _base(source: str) -> dict:
        return {"operation_id": str(uuid4()), "source": source,
                "occurred_at": datetime.now(UTC).isoformat()}

    async def place(self, source: str, instrument: str, side: str, lots: float,
                    sl_distance: float, tp_distance: float, note: str) -> dict:
        return await self.operate("/v1/orders", {
            **self._base(source), "instrument": instrument, "execution_type": "market",
            "direction": side.lower(), "note": note[:500],
            "targets": [{"account": self.s.account, "volume_lots": f"{lots:g}"}],
            "stop_loss_distance": f"{sl_distance:.5f}",
            "take_profit_distance": f"{tp_distance:.5f}"})

    async def protect(self, source: str, position_id: int, stop_loss: float | None,
                      take_profit: float | None) -> dict:
        target: dict[str, Any] = {"account": self.s.account, "position_id": position_id}
        if stop_loss is not None:
            target["stop_loss"] = f"{stop_loss:.5f}"
        if take_profit is not None:
            target["take_profit"] = f"{take_profit:.5f}"
        return await self.operate("/v1/positions/protection",
                                  {**self._base(source), "targets": [target]})

    async def close_position(self, source: str, position_id: int, lots: float) -> dict:
        return await self.operate("/v1/positions/close", {
            **self._base(source), "targets": [{"account": self.s.account,
                                               "position_id": position_id,
                                               "volume_lots": f"{lots:g}"}]})


def _settled_target(op: dict) -> dict:
    """The single target of a settled operation, or raise with its error."""
    t = (op.get("targets") or [{}])[0]
    if op.get("state") != "succeeded":
        raise ServiceError(t.get("error_code") or f"operation_{op.get('state')}",
                           t.get("error_message") or "")
    return t


class Executor:
    def __init__(self, s: ExecSettings, client: ExecClient | None = None,
                 positions_path: Path = POSITIONS):
        self.s = s
        self.svc = client or ExecClient(s)
        self.positions_path = positions_path
        self.managed: dict[int, Managed] = {}
        self.quotes: dict[str, tuple[float, float]] = {}
        self.is_live: bool | None = None
        if positions_path.exists():
            try:
                for d in json.loads(positions_path.read_text()):
                    self.managed[int(d["position_id"])] = Managed(**d)
            except (ValueError, TypeError, KeyError):
                pass

    def save_positions(self):
        self.positions_path.write_text(json.dumps([asdict(m) for m in self.managed.values()]))

    def instrument(self, ticker: str | None) -> str:
        t = (ticker or "XAUUSD").upper()
        return self.s.instruments.get(t, t)

    async def start(self):
        status = await self.svc.account_status()
        self.is_live = bool(status.get("is_live"))
        await self.reconcile()

    async def reconcile(self):
        """Forget remembered positions the account no longer has; sync SL/TP of the rest."""
        open_ = {int(p["position_id"]): p for p in await self.svc.positions()}
        for pid in list(self.managed):
            if pid not in open_:
                m = self.managed.pop(pid)
                log({"event": "position_closed", "source": m.label, "post_id": m.post_id,
                     "position_id": pid, "entry": m.entry, "side": m.side,
                     "last_quote": self.quotes.get(m.instrument)})
            else:
                p, m = open_[pid], self.managed[pid]
                if p.get("stop_loss"):
                    m.stop_loss = float(p["stop_loss"])
                if p.get("take_profit"):
                    m.take_profit = float(p["take_profit"])
        self.save_positions()

    async def quote(self, instrument: str) -> tuple[float, float]:
        q = await self.svc.quote(instrument)
        self.quotes[instrument] = q
        return q

    def can_send(self, source: str) -> tuple[bool, str]:
        if source == TM_LABEL and not self.s.tm_enabled:
            return False, "TICKMIND_EXECUTION_ENABLED is false (dry run)"
        if source == LABEL and not self.s.execution_enabled:
            return False, "EXECUTION_ENABLED is false (dry run)"
        return True, ""

    async def open_position(self, label: str, post_id, instrument: str, side: str,
                            lots: float, sl_dist: float, tp_dist: float, note: str) -> Managed:
        t = _settled_target(await self.svc.place(label, instrument, side, lots, sl_dist,
                                                 tp_dist, note))
        if not t.get("position_id"):
            raise ServiceError("no_position", "the order settled without a position id")
        entry = float(t.get("execution_price") or 0)
        sgn = 1 if side == "BUY" else -1
        m = Managed(position_id=int(t["position_id"]), instrument=instrument, side=side,
                    entry=entry, volume_lots=float(t.get("executed_volume_lots") or lots),
                    stop_loss=round(entry - sgn * sl_dist, 2) if entry else None,
                    take_profit=round(entry + sgn * tp_dist, 2) if entry else None,
                    best=entry, label=label, post_id=post_id,
                    opened=datetime.now(UTC).isoformat(timespec="seconds"))
        self.managed[m.position_id] = m
        self.save_positions()
        return m

    # -- trailing (Callisto only) ---------------------------------------------------------
    async def trail(self):
        if CFG.trail_start_pips <= 0:
            return
        for m in [m for m in self.managed.values() if m.label == LABEL and m.entry]:
            q = self.quotes.get(m.instrument)
            if q is None:
                continue
            bid, ask = q
            buy = m.side == "BUY"
            px = bid if buy else ask   # price the position would close at
            pip = CFG.pip(m.instrument)
            m.best = max(m.best, px) if buy else min(m.best, px)
            sgn = 1 if buy else -1
            if sgn * (m.best - m.entry) / pip < CFG.trail_start_pips:
                continue
            new_sl = round(m.best - sgn * CFG.trail_distance_pips * pip, 2)
            cur = m.stop_loss if m.stop_loss is not None else m.entry - sgn * 1e9
            if sgn * (new_sl - cur) / pip >= CFG.trail_step_pips:
                await self._amend(m, new_sl)

    async def _amend(self, m: Managed, new_sl: float):
        if not self.s.execution_enabled:
            m.stop_loss = new_sl
            log({"event": "would_trail", "source": LABEL, "position_id": m.position_id,
                 "stop_loss": new_sl})
            return
        try:
            _settled_target(await self.svc.protect(LABEL, m.position_id, new_sl,
                                                   m.take_profit))
            old, m.stop_loss = m.stop_loss, new_sl
            self.save_positions()
            log({"event": "trailed", "source": LABEL, "position_id": m.position_id,
                 "from": old, "to": new_sl, "best": m.best})
        except Exception as e:  # noqa: BLE001
            log({"event": "trail_error", "source": LABEL, "position_id": m.position_id,
                 "error": repr(e)})

    # -- Callisto ---------------------------------------------------------------------------
    async def on_signal(self, sig: dict) -> dict:
        posted = datetime.fromisoformat(sig["posted_at"])
        age = (datetime.now(UTC) - posted).total_seconds()
        base = {"source": LABEL, "post_id": sig.get("post_id"), "ticker": sig.get("ticker"),
                "side": sig.get("direction"), "signal_age_s": round(age)}
        if age > self.s.max_signal_age_s:
            return log({"event": "skipped", "reason": f"signal {age:.0f}s old", **base})
        side = sig.get("direction")
        if side not in ("BUY", "SELL"):
            return log({"event": "skipped", "reason": "no direction", **base})
        mine = [m for m in self.managed.values() if m.label == LABEL]
        if len(mine) >= self.s.max_open:
            return log({"event": "skipped", "reason": f"{len(mine)} open (max {self.s.max_open})",
                        **base})
        instrument = self.instrument(sig.get("ticker"))
        pip = CFG.pip(instrument)
        try:
            bid, ask = await self.quote(instrument)
        except ServiceError as e:
            return log({"event": "skipped", "reason": str(e), **base})
        px = ask if side == "BUY" else bid
        base["price"] = px
        entry = sig.get("entry")
        if entry and self.s.max_entry_slip_pips > 0:
            sgn = 1 if side == "BUY" else -1
            # >0: price already ran in the signal's favour; we would be chasing
            slip = sgn * (px - float(entry)) / pip
            base.update(entry=entry, slip_pips=round(slip, 1))
            if slip > self.s.max_entry_slip_pips:
                return log({"event": "skipped", "reason": f"price {slip:+.0f} pips past the "
                            f"entry (max {self.s.max_entry_slip_pips:g})", **base})
        detail = {"instrument": instrument, "lots": self.s.lots, "sl_pips": CFG.sl_pips,
                  "tp_pips": CFG.tp_pips}
        ok, why = self.can_send(LABEL)
        if not ok:
            return log({"event": "would_place", "reason": why, **base, **detail})
        try:
            m = await self.open_position(LABEL, sig.get("post_id"), instrument, side,
                                         self.s.lots, CFG.sl_pips * pip, CFG.tp_pips * pip,
                                         f"callisto post {sig.get('post_id')}")
        except Exception as e:  # noqa: BLE001
            return log({"event": "order_failed", "error": repr(e), **base, **detail})
        return log({"event": "placed", **base, **detail, "position": asdict(m)})

    # -- Tickmind ---------------------------------------------------------------------------
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
                    _settled_target(await self.svc.close_position(TM_LABEL, m.position_id,
                                                                  m.volume_lots))
                    self.managed.pop(m.position_id, None)
                    self.save_positions()
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
        instrument = self.instrument(ev.get("pair"))
        try:
            bid, ask = await self.quote(instrument)
        except ServiceError as e:
            return log({"event": "skipped", "reason": str(e), **base})
        pip = CFG.pip(instrument)
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
        detail = {"instrument": instrument, "lots": self.s.tm_lots,
                  "sl_pips": round(sl_dist / pip, 1), "tp_pips": round(tp_dist / pip, 1)}
        ok, why = self.can_send(TM_LABEL)
        if not ok:
            return log({"event": "would_place", "reason": why, **base, **detail})
        try:
            m = await self.open_position(TM_LABEL, sid, instrument, side, self.s.tm_lots,
                                         sl_dist, tp_dist, f"tickmind {sid}")
        except Exception as e:  # noqa: BLE001
            return log({"event": "order_failed", "error": repr(e), **base, **detail})
        return log({"event": "placed", **base, **detail, "position": asdict(m)})


# -- event-log follower -------------------------------------------------------------------
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


def _load_state() -> dict:
    try:
        return json.loads(EXEC_STATE.read_text())
    except (OSError, ValueError):
        return {}


def _write_hb(ex: Executor, started: str, error: str | None = None):
    ok_at = ex.svc.ok_at
    EXEC_HEARTBEAT.write_text(json.dumps({
        "started": started, "last_beat": datetime.now(UTC).isoformat(timespec="seconds"),
        "connected": ok_at is not None and time.time() - ok_at < 60,
        "service": ex.s.url, "account": ex.s.account,
        "environment": None if ex.is_live is None else ("live" if ex.is_live else "demo"),
        "execution_enabled": ex.s.execution_enabled, "lots": ex.s.lots,
        "sources": {
            LABEL: {"enabled": ex.s.execution_enabled, "lots": ex.s.lots,
                    "max_open": ex.s.max_open, "sl_pips": CFG.sl_pips, "tp_pips": CFG.tp_pips,
                    "max_entry_slip_pips": ex.s.max_entry_slip_pips,
                    "trail": [CFG.trail_start_pips, CFG.trail_distance_pips,
                              CFG.trail_step_pips]},
            TM_LABEL: {"enabled": ex.s.tm_enabled, "watching": bool(ex.s.tm_events),
                       "lots": ex.s.tm_lots, "max_open": ex.s.tm_max_open,
                       "max_age_s": ex.s.tm_max_age_s,
                       "max_slip_pips": ex.s.tm_max_slip_pips,
                       "follow_close": ex.s.tm_follow_close}},
        "open_positions": [asdict(m) for m in ex.managed.values()],
        "quotes": ex.quotes, "last_error": error or ex.svc.last_error}))


async def run_service(s: ExecSettings):
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
    ex = Executor(s)
    backoff = 5
    while True:
        try:
            await ex.start()
            log({"event": "executor_ready", "service": s.url, "account": s.account,
                 "environment": "live" if ex.is_live else "demo",
                 "callisto_enabled": s.execution_enabled,
                 "tickmind_enabled": s.tm_enabled if tickmind else None,
                 "managed": len(ex.managed)})
            backoff = 5
            tick = 0
            while True:
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
                if any(m.label == LABEL for m in ex.managed.values()):
                    for inst in {m.instrument for m in ex.managed.values()}:
                        try:
                            await ex.quote(inst)
                        except ServiceError:
                            pass
                    await ex.trail()
                if ex.managed and tick % 10 == 0:
                    await ex.reconcile()
                if tick % 30 == 0:
                    await ex.svc.account_status()   # service liveness for the heartbeat
                _write_hb(ex, started)
                tick += 1
                await asyncio.sleep(1)
        except Exception as e:  # noqa: BLE001 - service down: back off and retry
            log({"event": "executor_error", "error": repr(e)})
            _write_hb(ex, started, repr(e))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)


# -- CLI ----------------------------------------------------------------------------------
async def cmd_check(s: ExecSettings):
    ex = Executor(s)
    try:
        print("service:", await ex.svc.readiness())
        print("account:", await ex.svc.account_status())
        inst = ex.instrument("XAUUSD")
        try:
            bid, ask = await ex.quote(inst)
            print(f"quote {inst}: bid {bid} ask {ask}")
        except ServiceError as e:
            print("quote:", e)
        pip = CFG.pip(inst)
        print("sample callisto order (not sent):", {
            "instrument": inst, "lots": s.lots, "stop_loss_distance": CFG.sl_pips * pip,
            "take_profit_distance": CFG.tp_pips * pip})
        print("open positions:", len(await ex.svc.positions()),
              f"(remembered here: {len(ex.managed)})")
        print("callisto would send:", ex.can_send(LABEL), "| tickmind would send:",
              ex.can_send(TM_LABEL), "| tickmind events:", s.tm_events or None)
    finally:
        await ex.svc.close()


async def cmd_test_order(s: ExecSettings, side: str, confirm_live: bool, hold: float):
    test_path = CFG.data_dir / "test_positions.json"
    ex = Executor(s, positions_path=test_path)
    try:
        status = await ex.svc.account_status()
        if status.get("is_live") and not confirm_live:
            raise SystemExit("account is LIVE: pass --confirm-live")
        inst = ex.instrument("XAUUSD")
        bid, ask = await ex.quote(inst)
        pip = CFG.pip(inst)
        print(f"placing {side} {inst} 0.01 lots at ~{ask if side == 'BUY' else bid}, "
              f"SL {CFG.sl_pips:g}p TP {CFG.tp_pips:g}p via {s.url} ({s.account})")
        m = await ex.open_position(LABEL, "test", inst, side, 0.01, CFG.sl_pips * pip,
                                   CFG.tp_pips * pip, "callisto connectivity test")
        log({"event": "test_filled", "position": asdict(m)})
        await asyncio.sleep(hold)
        _settled_target(await ex.svc.close_position(LABEL, m.position_id, m.volume_lots))
        log({"event": "test_closed", "position_id": m.position_id})
        print("order placing works: filled and closed")
    finally:
        test_path.unlink(missing_ok=True)
        await ex.svc.close()


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
        except ServiceError as e:
            print(f"execution service rejected the test order: {e}")
            return 1
    else:
        asyncio.run(run_service(s))


if __name__ == "__main__":
    sys.exit(main())
