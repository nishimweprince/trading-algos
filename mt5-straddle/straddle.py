#!/usr/bin/env python3
"""
MT5 news straddle across one or two accounts (Windows, MetaTrader5 Python API).

Default layout: account 1 places a BUY STOP above price, account 2 places a
SELL STOP below price, a few seconds before the release time set in .env (GMT).
When one leg fills, the other account's pending order is cancelled (OCO), and
the filled position is managed with breakeven + trailing stop, an optional
max hold time, and automatic cancellation of unfilled orders after expiry.

Each account runs in its own process, attached to its own MT5 terminal install
(the MetaTrader5 package can only talk to one terminal per process).

Usage (from the mt5-straddle folder):
    py straddle.py --check        connect, print account + sizing, no orders
    py straddle.py --in 60        test run: release = now + 60 s (use on demo)
    py straddle.py                real run at EXECUTION_TIME_GMT from .env
    py straddle.py --account 1    run only account 1
"""
from __future__ import annotations

import argparse
import logging
import math
import multiprocessing as mp
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# .env loading (no external dependency)
# --------------------------------------------------------------------------

def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if val[:1] in ("'", '"') and val[1:].find(val[0]) >= 0:
            val = val[1 : 1 + val[1:].find(val[0])]
        else:
            # strip trailing inline comment: "value   # comment"
            for sep in (" #", "\t#"):
                if sep in val:
                    val = val.split(sep, 1)[0].strip()
        os.environ.setdefault(key, val)


def _get(key: str, default=None):
    v = os.environ.get(key)
    if v is None or v.strip() == "":
        return default
    return v.strip()


def _f(key: str, default: float) -> float:
    return float(_get(key, default))


def _i(key: str, default: int) -> int:
    return int(float(_get(key, default)))


def _b(key: str, default: bool) -> bool:
    return str(_get(key, default)).strip().lower() in ("1", "true", "yes", "y", "on")


def parse_gmt(text: str, now: datetime | None = None) -> datetime:
    """'2026-11-10 13:30:00', '2026-11-10T13:30Z', '13:30' (today, GMT)."""
    s = text.strip().upper()
    for suffix in ("GMT", "UTC", "Z"):
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
    s = s.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(s, fmt).time()
            today = (now or datetime.now(timezone.utc)).date()
            return datetime.combine(today, t, tzinfo=timezone.utc)
        except ValueError:
            pass
    raise ValueError(f"cannot parse EXECUTION_TIME_GMT={text!r} (use 'YYYY-MM-DD HH:MM:SS')")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

@dataclass
class Settings:
    release: datetime
    arm_seconds_before: float
    entry_distance_points: int
    stop_loss_points: int
    take_profit_points: int
    risk_percent: float
    lot_size: float
    commission_per_lot: float
    sizing_slippage_points: int
    max_margin_pct: float
    max_spread_points: int
    pending_expiry_seconds: float
    oco: bool
    trail_enabled: bool
    trail_start_points: int
    trail_distance_points: int
    trail_step_points: int
    breakeven_trigger_points: int
    breakeven_offset_points: int
    max_hold_seconds: float
    close_on_exit: bool
    deviation_points: int
    magic: int
    comment: str
    poll_ms: int
    modify_min_interval_ms: int
    dry_run: bool
    log_dir: Path


@dataclass
class Account:
    idx: int
    enabled: bool
    login: int
    password: str
    server: str
    terminal_path: str
    portable: bool
    symbol: str
    side: str  # BUY | SELL | BOTH
    risk_percent: float
    lot_size: float
    commission_per_lot: float


def load_settings(release_override: datetime | None) -> tuple[Settings, list[Account]]:
    release = release_override or parse_gmt(_get("EXECUTION_TIME_GMT", "") or "")
    s = Settings(
        release=release,
        arm_seconds_before=_f("ARM_SECONDS_BEFORE", 15),
        entry_distance_points=_i("ENTRY_DISTANCE_POINTS", 100),
        stop_loss_points=_i("STOP_LOSS_POINTS", 150),
        take_profit_points=_i("TAKE_PROFIT_POINTS", 0),
        risk_percent=_f("RISK_PERCENT", 1.0),
        lot_size=_f("LOT_SIZE", 0),
        commission_per_lot=_f("COMMISSION_PER_LOT", 7.0),
        sizing_slippage_points=_i("SIZING_SLIPPAGE_POINTS", 50),
        max_margin_pct=_f("MAX_MARGIN_PCT", 50),
        max_spread_points=_i("MAX_SPREAD_POINTS", 30),
        pending_expiry_seconds=_f("PENDING_EXPIRY_SECONDS", 120),
        oco=_b("OCO", True),
        trail_enabled=_b("TRAIL_ENABLED", True),
        trail_start_points=_i("TRAIL_START_POINTS", 100),
        trail_distance_points=_i("TRAIL_DISTANCE_POINTS", 80),
        trail_step_points=_i("TRAIL_STEP_POINTS", 10),
        breakeven_trigger_points=_i("BREAKEVEN_TRIGGER_POINTS", 60),
        breakeven_offset_points=_i("BREAKEVEN_OFFSET_POINTS", 10),
        max_hold_seconds=_f("MAX_HOLD_SECONDS", 900),
        close_on_exit=_b("CLOSE_ON_EXIT", False),
        deviation_points=_i("DEVIATION_POINTS", 30),
        magic=_i("MAGIC", 20261110),
        comment=_get("ORDER_COMMENT", "news-straddle")[:31],
        poll_ms=_i("POLL_MS", 100),
        modify_min_interval_ms=_i("MODIFY_MIN_INTERVAL_MS", 250),
        dry_run=_b("DRY_RUN", True),
        log_dir=HERE / _get("LOG_DIR", "logs"),
    )
    default_symbol = _get("SYMBOL", "EURUSD")
    accounts = []
    for n, default_side in ((1, "BUY"), (2, "SELL")):
        p = f"ACC{n}_"
        side = _get(p + "SIDE", default_side).upper()
        if side not in ("BUY", "SELL", "BOTH"):
            raise ValueError(f"{p}SIDE must be BUY, SELL or BOTH (got {side})")
        accounts.append(Account(
            idx=n,
            enabled=_b(p + "ENABLED", True),
            login=_i(p + "LOGIN", 0),
            password=_get(p + "PASSWORD", "") or "",
            server=_get(p + "SERVER", "") or "",
            terminal_path=_get(p + "TERMINAL_PATH", "") or "",
            portable=_b(p + "PORTABLE", False),
            symbol=_get(p + "SYMBOL", default_symbol),
            side=side,
            risk_percent=_f(p + "RISK_PERCENT", s.risk_percent),
            lot_size=_f(p + "LOT_SIZE", s.lot_size),
            commission_per_lot=_f(p + "COMMISSION_PER_LOT", s.commission_per_lot),
        ))
    return s, accounts


# --------------------------------------------------------------------------
# Pure helpers (unit-tested in test_straddle.py)
# --------------------------------------------------------------------------

def floor_to_step(volume: float, step: float) -> float:
    if step <= 0:
        return volume
    v = math.floor(volume / step + 1e-9) * step
    decimals = max(0, -int(math.floor(math.log10(step))))
    return round(v, decimals)


def round_to_tick(price: float, tick: float, digits: int) -> float:
    if tick > 0:
        price = round(price / tick) * tick
    return round(price, digits)


def trail_target(is_buy: bool, open_price: float, exit_price: float, current_sl: float,
                 point: float, *, trail_enabled: bool, start: int, distance: int, step: int,
                 be_trigger: int, be_offset: int) -> float | None:
    """New stop-loss price for a position, or None if the stop should not move.

    exit_price is the price the position would close at (bid for a buy, ask for a sell).
    The stop only ever moves in the position's favour, by at least `step` points.
    """
    profit_pts = (exit_price - open_price) / point if is_buy else (open_price - exit_price) / point
    profit_pts = round(profit_pts, 6)  # 1.10060 - 1.10000 = 59.9999... points otherwise
    eps = 1e-6 * point
    sign = 1 if is_buy else -1
    candidates = []
    if be_trigger > 0 and profit_pts >= be_trigger:
        candidates.append(open_price + sign * be_offset * point)
    if trail_enabled and profit_pts >= start:
        candidates.append(exit_price - sign * distance * point)
    if not candidates:
        return None
    target = max(candidates) if is_buy else min(candidates)
    if current_sl == 0:
        return target
    if is_buy and target >= current_sl + step * point - eps:
        return target
    if not is_buy and target <= current_sl - step * point + eps:
        return target
    return None


# --------------------------------------------------------------------------
# Per-account trader
# --------------------------------------------------------------------------

class Abort(Exception):
    pass


class Trader:
    def __init__(self, s: Settings, a: Account, buy_filled, sell_filled, log: logging.Logger):
        self.s, self.a, self.log = s, a, log
        self.buy_filled, self.sell_filled = buy_filled, sell_filled
        self.mt5 = None
        self.sym = None
        self.placed: dict[int, dict] = {}      # order ticket -> {side, price, volume}
        self.fills: dict[int, dict] = {}       # position ticket -> {side, time, price}
        self.last_modify: dict[int, float] = {}

    # ---------- connection ----------
    def connect(self) -> None:
        import MetaTrader5 as mt5  # Windows only
        self.mt5 = mt5
        a = self.a
        kwargs = {"timeout": 60000}
        if a.login:
            kwargs.update(login=a.login, password=a.password, server=a.server)
        if a.portable:
            kwargs["portable"] = True
        ok = mt5.initialize(a.terminal_path, **kwargs) if a.terminal_path else mt5.initialize(**kwargs)
        if not ok:
            raise Abort(f"MT5 initialize failed: {mt5.last_error()} (check TERMINAL_PATH/LOGIN/SERVER)")
        info = mt5.account_info()
        term = mt5.terminal_info()
        if info is None or term is None:
            raise Abort(f"no account/terminal info: {mt5.last_error()}")
        if a.login and info.login != a.login:
            raise Abort(f"terminal is logged into {info.login}, expected {a.login}")
        mode = {0: "DEMO", 1: "CONTEST", 2: "REAL"}.get(info.trade_mode, str(info.trade_mode))
        hedging = info.margin_mode == mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
        self.log.info("connected login=%s server=%s mode=%s balance=%.2f equity=%.2f free=%.2f %s "
                      "leverage=1:%s %s", info.login, info.server, mode, info.balance, info.equity,
                      info.margin_free, info.currency, info.leverage,
                      "hedging" if hedging else "NETTING")
        if mode == "REAL":
            self.log.warning("*** REAL ACCOUNT *** DRY_RUN=%s", self.s.dry_run)
        if a.side == "BOTH" and not hedging:
            self.log.warning("SIDE=BOTH on a netting account: if both legs fill they net out")
        if not term.trade_allowed:
            raise Abort("Algo Trading is OFF in this terminal: click the 'Algo Trading' button")
        if not info.trade_allowed or not info.trade_expert:
            raise Abort("trading not allowed for this account / expert advisors (check terminal options)")
        if not mt5.symbol_select(a.symbol, True):
            raise Abort(f"symbol {a.symbol} not available: {mt5.last_error()} (broker suffix? set ACC{a.idx}_SYMBOL)")
        self.refresh_symbol()
        sym = self.sym
        self.log.info("symbol %s digits=%d point=%g tick_size=%g stops_level=%d freeze_level=%d "
                      "vol min/step/max=%g/%g/%g", a.symbol, sym.digits, sym.point,
                      sym.trade_tick_size, sym.trade_stops_level, sym.trade_freeze_level,
                      sym.volume_min, sym.volume_step, sym.volume_max)

    def refresh_symbol(self) -> None:
        self.sym = self.mt5.symbol_info(self.a.symbol)
        if self.sym is None:
            raise Abort(f"symbol_info({self.a.symbol}) failed: {self.mt5.last_error()}")

    def shutdown(self) -> None:
        if self.mt5:
            self.mt5.shutdown()

    # ---------- helpers ----------
    @property
    def point(self) -> float:
        return self.sym.point

    def price(self, p: float) -> float:
        return round_to_tick(p, self.sym.trade_tick_size or self.sym.point, self.sym.digits)

    def tick(self):
        t = self.mt5.symbol_info_tick(self.a.symbol)
        if t is None or t.bid <= 0 or t.ask <= 0:
            raise Abort(f"no quote for {self.a.symbol}: {self.mt5.last_error()}")
        return t

    def spread_points(self, t) -> float:
        return (t.ask - t.bid) / self.point

    def sides(self) -> list[str]:
        return ["BUY", "SELL"] if self.a.side == "BOTH" else [self.a.side]

    def market_fillings(self) -> list[int]:
        m = self.mt5
        fm = self.sym.filling_mode  # bit 1 = FOK, bit 2 = IOC
        out = []
        if fm & 2:
            out.append(m.ORDER_FILLING_IOC)
        if fm & 1:
            out.append(m.ORDER_FILLING_FOK)
        out.append(m.ORDER_FILLING_RETURN)
        return out

    def pending_fillings(self) -> list[int]:
        m = self.mt5
        return [m.ORDER_FILLING_RETURN] + [f for f in self.market_fillings() if f != m.ORDER_FILLING_RETURN]

    def send(self, req: dict, fillings: list[int], check_only: bool = False):
        """order_send (or order_check), trying filling modes until one is accepted."""
        m = self.mt5
        res = None
        for f in fillings:
            req["type_filling"] = f
            res = m.order_check(req) if check_only else m.order_send(req)
            if res is None:
                self.log.error("order_%s returned None: %s req=%s", "check" if check_only else "send",
                               m.last_error(), req)
                return None
            if res.retcode == m.TRADE_RETCODE_INVALID_FILL:
                continue
            return res
        return res

    def ok(self, res, check_only: bool = False) -> bool:
        m = self.mt5
        if res is None:
            return False
        if check_only:
            return res.retcode == 0
        return res.retcode in (m.TRADE_RETCODE_DONE, m.TRADE_RETCODE_PLACED, m.TRADE_RETCODE_DONE_PARTIAL)

    # ---------- sizing ----------
    def volume_for(self, is_buy: bool, entry: float, sl_points: int) -> float:
        m, a, s, sym = self.mt5, self.a, self.s, self.sym
        acct = m.account_info()
        otype = m.ORDER_TYPE_BUY if is_buy else m.ORDER_TYPE_SELL
        if a.lot_size > 0:
            vol = a.lot_size
            self.log.info("sizing: fixed LOT_SIZE=%g", vol)
        else:
            capital = min(acct.balance, acct.equity)
            risk_money = capital * a.risk_percent / 100.0
            dist = (sl_points + s.sizing_slippage_points) * self.point
            exit_px = entry - dist if is_buy else entry + dist
            pnl = m.order_calc_profit(otype, a.symbol, 1.0, entry, exit_px)
            if pnl is None or pnl >= 0:
                raise Abort(f"order_calc_profit failed: {pnl} {m.last_error()}")
            per_lot = abs(pnl) + a.commission_per_lot
            vol = risk_money / per_lot
            self.log.info("sizing: risk %.2f%% of %.2f = %.2f %s; loss/lot at SL+slip (%d pts) = %.2f "
                          "(incl. commission %.2f) -> %.4f lots", a.risk_percent, capital, risk_money,
                          acct.currency, sl_points + s.sizing_slippage_points, per_lot,
                          a.commission_per_lot, vol)
        margin_1 = m.order_calc_margin(otype, a.symbol, 1.0, entry)
        if margin_1 and margin_1 > 0 and s.max_margin_pct > 0:
            cap = acct.margin_free * s.max_margin_pct / 100.0 / margin_1
            if vol > cap:
                self.log.warning("margin cap: %.4f lots -> %.4f (MAX_MARGIN_PCT=%g%% of free margin %.2f, "
                                 "margin/lot %.2f)", vol, cap, s.max_margin_pct, acct.margin_free, margin_1)
                vol = cap
        vol = min(floor_to_step(vol, sym.volume_step), sym.volume_max)
        if vol < sym.volume_min:
            raise Abort(f"volume {vol} below broker minimum {sym.volume_min}: raise RISK_PERCENT or "
                        f"reduce STOP_LOSS_POINTS")
        return vol

    # ---------- placement ----------
    def build_pending(self, side: str, t) -> tuple[dict, int]:
        m, s, sym = self.mt5, self.s, self.sym
        is_buy = side == "BUY"
        min_dist = sym.trade_stops_level + 1 if sym.trade_stops_level > 0 else 0
        entry_pts = max(s.entry_distance_points, min_dist)
        sl_pts = max(s.stop_loss_points, min_dist)
        tp_pts = max(s.take_profit_points, min_dist) if s.take_profit_points > 0 else 0
        if entry_pts != s.entry_distance_points or sl_pts != s.stop_loss_points:
            self.log.warning("broker stops level %d pts: entry %d pts, SL %d pts",
                             sym.trade_stops_level, entry_pts, sl_pts)
        if is_buy:
            price = self.price(t.ask + entry_pts * self.point)
            sl = self.price(price - sl_pts * self.point)
            tp = self.price(price + tp_pts * self.point) if tp_pts else 0.0
            otype = m.ORDER_TYPE_BUY_STOP
        else:
            price = self.price(t.bid - entry_pts * self.point)
            sl = self.price(price + sl_pts * self.point)
            tp = self.price(price - tp_pts * self.point) if tp_pts else 0.0
            otype = m.ORDER_TYPE_SELL_STOP
        vol = self.volume_for(is_buy, price, sl_pts)
        req = {
            "action": m.TRADE_ACTION_PENDING,
            "symbol": self.a.symbol,
            "volume": vol,
            "type": otype,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": s.deviation_points,
            "magic": s.magic,
            "comment": s.comment,
            "type_time": m.ORDER_TIME_GTC,
        }
        return req, sl_pts

    def place_all(self) -> bool:
        placed_any = False
        for side in self.sides():
            for attempt in range(1, 4):
                t = self.tick()
                req, _ = self.build_pending(side, t)
                self.log.info("%s %s STOP %.2f lots @ %s  SL %s  TP %s  (bid %s ask %s, spread %.0f pts)",
                              "CHECK" if self.s.dry_run else "PLACE", side, req["volume"], req["price"],
                              req["sl"], req["tp"] or "-", t.bid, t.ask, self.spread_points(t))
                res = self.send(req, self.pending_fillings(), check_only=self.s.dry_run)
                if self.ok(res, check_only=self.s.dry_run):
                    if self.s.dry_run:
                        self.log.info("DRY_RUN: order_check OK (margin %.2f, free after %.2f) - no order sent",
                                      res.margin, res.margin_free)
                    else:
                        self.placed[res.order] = {"side": side, "price": req["price"], "volume": req["volume"]}
                        self.log.info("placed %s order #%s", side, res.order)
                    placed_any = True
                    break
                self.log.error("%s leg rejected (attempt %d): retcode=%s %s", side, attempt,
                               getattr(res, "retcode", None), getattr(res, "comment", self.mt5.last_error()))
                time.sleep(0.2)
        return placed_any

    # ---------- order / position management ----------
    def my_orders(self):
        return [o for o in (self.mt5.orders_get(symbol=self.a.symbol) or ()) if o.magic == self.s.magic]

    def my_positions(self):
        return [p for p in (self.mt5.positions_get(symbol=self.a.symbol) or ()) if p.magic == self.s.magic]

    def cancel(self, o, reason: str) -> None:
        m = self.mt5
        res = m.order_send({"action": m.TRADE_ACTION_REMOVE, "order": o.ticket})
        if self.ok(res):
            self.log.info("cancelled order #%s (%s)", o.ticket, reason)
        else:
            self.log.error("cancel #%s failed: %s %s", o.ticket, getattr(res, "retcode", None),
                           getattr(res, "comment", m.last_error()))

    def close(self, p, reason: str) -> None:
        m = self.mt5
        t = self.tick()
        is_buy = p.type == m.POSITION_TYPE_BUY
        req = {
            "action": m.TRADE_ACTION_DEAL,
            "symbol": p.symbol,
            "volume": p.volume,
            "type": m.ORDER_TYPE_SELL if is_buy else m.ORDER_TYPE_BUY,
            "position": p.ticket,
            "price": t.bid if is_buy else t.ask,
            "deviation": self.s.deviation_points,
            "magic": self.s.magic,
            "comment": "straddle close",
            "type_time": m.ORDER_TIME_GTC,
        }
        res = self.send(req, self.market_fillings())
        if self.ok(res):
            self.log.info("closed position #%s (%s) at %s", p.ticket, reason, res.price)
        else:
            self.log.error("close #%s failed: %s %s", p.ticket, getattr(res, "retcode", None),
                           getattr(res, "comment", m.last_error()))

    def manage(self, p, t) -> None:
        m, s = self.mt5, self.s
        is_buy = p.type == m.POSITION_TYPE_BUY
        exit_px = t.bid if is_buy else t.ask
        target = trail_target(is_buy, p.price_open, exit_px, p.sl, self.point,
                              trail_enabled=s.trail_enabled, start=s.trail_start_points,
                              distance=s.trail_distance_points, step=s.trail_step_points,
                              be_trigger=s.breakeven_trigger_points, be_offset=s.breakeven_offset_points)
        if target is None:
            return
        now = time.monotonic()
        if now - self.last_modify.get(p.ticket, 0) < s.modify_min_interval_ms / 1000:
            return
        # keep the new stop outside the broker's stops/freeze distance
        gap = (max(self.sym.trade_stops_level, self.sym.trade_freeze_level) + 1) * self.point
        target = min(target, exit_px - gap) if is_buy else max(target, exit_px + gap)
        target = self.price(target)
        if p.sl and ((is_buy and target <= p.sl) or (not is_buy and target >= p.sl)):
            return
        self.last_modify[p.ticket] = now
        res = m.order_send({"action": m.TRADE_ACTION_SLTP, "symbol": p.symbol, "position": p.ticket,
                            "sl": target, "tp": p.tp, "magic": s.magic})
        if self.ok(res):
            self.log.info("SL #%s %s -> %s (price %s, +%.0f pts)", p.ticket, p.sl or "-", target, exit_px,
                          ((exit_px - p.price_open) if is_buy else (p.price_open - exit_px)) / self.point)
        else:
            self.log.warning("SL modify #%s to %s failed: %s %s (will retry)", p.ticket, target,
                             getattr(res, "retcode", None), getattr(res, "comment", m.last_error()))
            self.refresh_symbol()

    def monitor(self) -> None:
        m, s = self.mt5, self.s
        expiry = s.release + timedelta(seconds=s.pending_expiry_seconds)
        empty_loops = 0
        self.log.info("monitoring (pending expiry %s GMT, OCO=%s, trail=%s, BE=%s, max hold=%ss)",
                      expiry.strftime("%H:%M:%S"), s.oco, s.trail_enabled,
                      s.breakeven_trigger_points or "off", s.max_hold_seconds or "off")
        while True:
            now = utcnow()
            orders = self.my_orders()
            positions = self.my_positions()

            for p in positions:
                if p.ticket not in self.fills:
                    is_buy = p.type == m.POSITION_TYPE_BUY
                    side = "BUY" if is_buy else "SELL"
                    self.fills[p.ticket] = {"side": side, "time": now, "price": p.price_open}
                    (self.buy_filled if is_buy else self.sell_filled).set()
                    want = self.placed.get(p.identifier, {}).get("price")
                    slip = ""
                    if want:
                        slip_pts = ((p.price_open - want) if is_buy else (want - p.price_open)) / self.point
                        slip = f", slippage {slip_pts:+.0f} pts vs {want}"
                    self.log.info("FILLED %s #%s %.2f lots @ %s SL %s%s", side, p.ticket, p.volume,
                                  p.price_open, p.sl, slip)

            for o in orders:
                is_buy_order = o.type in (m.ORDER_TYPE_BUY_STOP, m.ORDER_TYPE_BUY_LIMIT, m.ORDER_TYPE_BUY)
                if s.oco and (self.sell_filled.is_set() if is_buy_order else self.buy_filled.is_set()):
                    self.cancel(o, "OCO: other leg filled")
                elif now >= expiry:
                    self.cancel(o, "expired")

            if positions:
                t = self.tick()
                for p in positions:
                    held = (now - self.fills[p.ticket]["time"]).total_seconds()
                    if s.max_hold_seconds > 0 and held >= s.max_hold_seconds:
                        self.close(p, f"max hold {s.max_hold_seconds:.0f}s")
                    else:
                        self.manage(p, t)

            if not orders and not positions:
                empty_loops += 1
                if empty_loops >= 10:
                    break
            else:
                empty_loops = 0
            time.sleep(s.poll_ms / 1000)

    def report(self) -> None:
        if not self.fills:
            self.log.info("done: no leg filled")
            return
        m = self.mt5
        now = datetime.now()
        deals = m.history_deals_get(now - timedelta(days=3), now + timedelta(days=3)) or ()
        total = 0.0
        for d in deals:
            if d.position_id in self.fills:
                total += d.profit + d.commission + d.swap + getattr(d, "fee", 0.0)
        self.log.info("done: %d position(s), net P&L incl. commission %.2f %s", len(self.fills), total,
                      m.account_info().currency)

    def cleanup_on_exit(self) -> None:
        try:
            for o in self.my_orders():
                self.cancel(o, "script stopped")
            if self.s.close_on_exit:
                for p in self.my_positions():
                    self.close(p, "script stopped (CLOSE_ON_EXIT)")
            elif self.my_positions():
                self.log.warning("open positions left with their SL/TP in place (no more trailing)")
        except Exception as e:  # noqa: BLE001
            self.log.error("cleanup failed: %s", e)

    # ---------- flows ----------
    def wait_until(self, when: datetime, label: str) -> None:
        last_log = 0.0
        while True:
            left = (when - utcnow()).total_seconds()
            if left <= 0:
                return
            if time.monotonic() - last_log >= (60 if left > 60 else 10):
                self.log.info("%s in %.0fs (%s GMT)", label, left, when.strftime("%Y-%m-%d %H:%M:%S"))
                last_log = time.monotonic()
            time.sleep(min(left, 0.5 if left > 2 else 0.02))

    def check(self) -> None:
        t = self.tick()
        self.log.info("quote bid %s ask %s spread %.0f pts; release %s GMT (in %.0fs)", t.bid, t.ask,
                      self.spread_points(t), self.s.release.strftime("%Y-%m-%d %H:%M:%S"),
                      (self.s.release - utcnow()).total_seconds())
        for side in self.sides():
            req, _ = self.build_pending(side, t)
            res = self.send(req, self.pending_fillings(), check_only=True)
            self.log.info("%s STOP %.2f lots @ %s SL %s -> order_check %s (%s)", side, req["volume"],
                          req["price"], req["sl"], "OK" if self.ok(res, True) else "REJECTED",
                          getattr(res, "comment", ""))

    def run(self) -> None:
        s = self.s
        if utcnow() >= s.release:
            raise Abort(f"release time {s.release:%Y-%m-%d %H:%M:%S} GMT has already passed")
        arm_at = s.release - timedelta(seconds=s.arm_seconds_before)
        self.wait_until(arm_at, "arming")
        if not self.mt5.terminal_info().connected:
            raise Abort("terminal lost connection to the broker")
        self.refresh_symbol()
        # spread filter: wait for an acceptable spread, but never place after the release
        while True:
            t = self.tick()
            sp = self.spread_points(t)
            if s.max_spread_points <= 0 or sp <= s.max_spread_points:
                break
            if utcnow() >= s.release - timedelta(seconds=1):
                raise Abort(f"spread {sp:.0f} pts > MAX_SPREAD_POINTS {s.max_spread_points}; no orders placed")
            time.sleep(0.1)
        if not self.place_all():
            raise Abort("no leg could be placed")
        if s.dry_run:
            self.log.info("DRY_RUN complete. Set DRY_RUN=false to trade.")
            return
        self.monitor()
        self.report()


# --------------------------------------------------------------------------
# Process entry points
# --------------------------------------------------------------------------

def make_logger(s: Settings, a: Account) -> logging.Logger:
    s.log_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(f"ACC{a.idx}")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter(f"%(asctime)s.%(msecs)03d GMT [ACC{a.idx}] %(levelname)s %(message)s",
                            "%H:%M:%S")
    fmt.converter = time.gmtime
    fh = logging.FileHandler(s.log_dir / f"straddle_{s.release:%Y%m%d_%H%M}_acc{a.idx}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    return log


def worker(s: Settings, a: Account, mode: str, buy_filled, sell_filled) -> None:
    log = make_logger(s, a)
    trader = Trader(s, a, buy_filled, sell_filled, log)
    code = 0
    try:
        trader.connect()
        if mode == "check":
            trader.check()
        else:
            trader.run()
    except KeyboardInterrupt:
        log.warning("interrupted (Ctrl+C)")
        if trader.mt5:
            trader.cleanup_on_exit()
        code = 130
    except Abort as e:
        log.error("ABORT: %s", e)
        if trader.mt5 and mode != "check":
            trader.cleanup_on_exit()
        code = 2
    except Exception:  # noqa: BLE001
        log.exception("unexpected error")
        if trader.mt5 and mode != "check":
            trader.cleanup_on_exit()
        code = 1
    finally:
        trader.shutdown()
    sys.exit(code)


def main() -> int:
    ap = argparse.ArgumentParser(description="MT5 two-account news straddle")
    ap.add_argument("--env", default=str(HERE / ".env"), help="path to .env (default: next to this script)")
    ap.add_argument("--check", action="store_true", help="connect, show sizing, order_check only")
    ap.add_argument("--in", dest="in_seconds", type=float, help="override release time: now + N seconds")
    ap.add_argument("--account", type=int, choices=(1, 2), help="run only this account")
    args = ap.parse_args()

    load_env(Path(args.env))
    override = utcnow() + timedelta(seconds=args.in_seconds) if args.in_seconds else None
    if args.check and override is None and not _get("EXECUTION_TIME_GMT"):
        override = utcnow() + timedelta(hours=1)
    try:
        s, accounts = load_settings(override)
    except ValueError as e:
        print(f"config error: {e}")
        return 2
    accounts = [a for a in accounts if a.enabled and (args.account is None or a.idx == args.account)]
    if not accounts:
        print("no enabled accounts")
        return 2
    paths = [a.terminal_path.lower() for a in accounts]
    if len(accounts) > 1 and (len(set(paths)) < len(paths) or "" in paths):
        print("each account needs its own MT5 terminal: set different ACC1_TERMINAL_PATH and ACC2_TERMINAL_PATH")
        return 2

    print(f"release {s.release:%Y-%m-%d %H:%M:%S} GMT | now {utcnow():%H:%M:%S} GMT | "
          f"accounts {[f'{a.idx}:{a.side} {a.symbol}' for a in accounts]} | "
          f"{'CHECK' if args.check else ('DRY_RUN' if s.dry_run else 'LIVE')}")

    buy_filled, sell_filled = mp.Event(), mp.Event()
    mode = "check" if args.check else "run"
    procs = [mp.Process(target=worker, args=(s, a, mode, buy_filled, sell_filled), name=f"acc{a.idx}")
             for a in accounts]
    for p in procs:
        p.start()
    try:
        for p in procs:
            p.join()
    except KeyboardInterrupt:
        print("Ctrl+C: waiting for workers to cancel pending orders...")
        for p in procs:
            p.join(timeout=30)
    codes = {p.name: p.exitcode for p in procs}
    print(f"finished: {codes}")
    return 0 if all(c == 0 for c in codes.values()) else 1


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
