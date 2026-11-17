"""Live runner: broker M1 -> engine -> orders on the FTMO demo through the execution hub.

Each minute boundary T (UTC, a couple of seconds after :00):
  1. pull the latest closed M1 bid candles per symbol from the hub and feed the new ones to the engine;
  2. `signal_now(T)`: close the buckets that ended at T and resolve their signals with entry minute T;
  3. time stop / Friday flat for open positions (research rule: decided at the start of minute T);
  4. send market orders (SL/TP as distances, label = strategy source) for the entries;
  5. sync with the broker: positions that disappeared are closed from the hub's deal history (real exit price);
  6. kill rule, FTMO breach log (record only), status snapshot.
"""

from __future__ import annotations

import json
import logging
import time
from bisect import bisect_left
from collections import deque
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from . import config as cfgmod
from .clock import fmt, research_to_utc, trading_day, utc_to_research
from .engine import Entry, Market, Skip
from .exits import TimeExit
from .hub import Hub
from .journal import Journal
from .settings import CONTRACT, DIGITS, Settings

log = logging.getLogger("walkforward_patterns")
PRAGUE = ZoneInfo("Europe/Prague")  # FTMO's server day


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class Runner:
    def __init__(self, settings: Settings, cfg: cfgmod.Config, hub: Hub, journal: Journal, notifier=None):
        self.s = settings
        self.cfg = cfg
        self.hub = hub
        self.j = journal
        self.notifier = notifier
        self.by_id = {st.id: st for st in cfg.strategies}
        for st in cfg.strategies:
            if st.enabled and st.lots is None:
                raise SystemExit(f"{st.id}: lots is empty in {settings.config_path}; refusing to start (SPEC §1)")
        self.markets = {sym: Market(sym, list(cfg.strategies), cfg.cost[sym]) for sym in cfg.symbols}
        self.fed: dict[str, deque[int]] = {sym: deque(maxlen=20_000) for sym in cfg.symbols}
        self.timers: dict[int, TimeExit] = {}  # trade id -> timer (j in that symbol's M1 index)
        self.last_feed_ok: dict[str, float] = {}
        self.last_close: dict[str, float] = {}
        self.feed_alerted: set[str] = set()
        self.kill_min = int(cfg.defaults.get("kill_rule", {}).get("min_trades", 60))
        self.kill_below = float(cfg.defaults.get("kill_rule", {}).get("disable_if_cum_R_below", 0.0))

    # ----------------------------------------------------------------- utils
    def notify(self, subject: str, lines: list[str]) -> None:
        if self.notifier is not None:
            self.notifier.send(subject, lines)

    def is_flat(self, sid: str) -> bool:
        return not any(r["strategy"] == sid for r in self.j.open_trades())

    def _feed(self, sym: str, candles: list[dict], live: bool) -> int:
        mk = self.markets[sym]
        n = 0
        for c in candles:
            start = parse_ts(c["ts"]) - timedelta(minutes=1)
            t = utc_to_research(start)
            if mk.last_t is not None and t <= mk.last_t:
                continue
            entries, skips = mk.on_m1(t, float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]),
                                      self.is_flat)
            self.fed[sym].append(t)
            self.last_close[sym] = float(c["close"])
            n += 1
            if live:
                for e in entries:  # only when catching up: the entry minute is already past
                    self._log_signal(e, False, "catchup_late")
                for sk in skips:
                    self._log_skip(sk)
        return n

    # --------------------------------------------------------------- warm-up
    def warmup(self, now: datetime | None = None) -> None:
        since = (now or datetime.now(UTC)) - timedelta(days=self.s.warmup_days)
        for sym in self.cfg.symbols:
            t0 = time.monotonic()
            candles = self.hub.history(sym, since)
            n = self._feed(sym, candles, live=False)
            self.last_feed_ok[sym] = time.time()
            log.info("warmup %s: %d M1 bars to %s in %.0fs", sym, n, fmt(self.markets[sym].last_t or 0),
                     time.monotonic() - t0)
            self.j.event("info", "warmup", f"{sym}: {n} M1 bars", {"last": fmt(self.markets[sym].last_t or 0)})
        self.sync(startup=True)
        for row in self.j.open_trades():
            if row["status"] in ("open", "closing") and row["id"] not in self.timers:
                self.timers[row["id"]] = TimeExit(self._index_at(row["symbol"], int(row["entry_minute"])))

    def _index_at(self, sym: str, t_entry: int) -> int:
        """M1 index the entry minute had, reconstructed from the fed timestamps (restart safety)."""
        fed = self.fed[sym]
        lst = list(fed)
        after = len(lst) - bisect_left(lst, t_entry)
        return self.markets[sym].n_m1 - after

    # ------------------------------------------------------------------ tick
    def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        boundary = now.replace(second=0, microsecond=0)
        T = utc_to_research(boundary)
        for sym in self.cfg.symbols:
            self._pull(sym, boundary)
        # broker SL/TP closes during the last minute free their strategy for an entry now (research: exit at bar k
        # allows a new entry at k + 1), so sync before resolving signals
        self.sync()
        for sym in self.cfg.symbols:
            mk = self.markets[sym]
            try:
                q = self.hub.quote(sym)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s quote: %s", sym, exc)
                q = None
            # the research qualifies pattern risk against the open of the entry minute; the bid now is that price
            bid = float(q["bid"]) if q and q.get("bid") is not None else self.last_close.get(sym, float("nan"))
            self._time_exits(sym, T)
            entries, skips = mk.signal_now(T, bid, self.is_flat)
            for sk in skips:
                self._log_skip(sk)
            for e in entries:
                self._enter(e, q)
        self.sync()

    def _pull(self, sym: str, boundary: datetime) -> None:
        """Feed every closed M1 up to `boundary`; wait up to bar_wait_seconds for the bar ending at `boundary`."""
        deadline = time.monotonic() + self.s.bar_wait_seconds
        while True:
            try:
                candles = self.hub.candles(sym, 30)
            except Exception as exc:  # noqa: BLE001 - feed errors must not kill the loop
                self._feed_error(sym, f"candles: {exc}")
                return
            closed = [c for c in candles if parse_ts(c["ts"]) <= boundary]
            self._feed(sym, closed, live=True)
            last = parse_ts(closed[-1]["ts"]) if closed else None
            if last is not None and last >= boundary:
                self.last_feed_ok[sym] = time.time()
                self.feed_alerted.discard(sym)
                return
            if time.monotonic() >= deadline:
                return  # no trades in the last minute (or a late bar): proceed, the bucket closes on time anyway
            time.sleep(2.0)

    def _feed_error(self, sym: str, msg: str) -> None:
        log.warning("%s feed error: %s", sym, msg)
        stale = time.time() - self.last_feed_ok.get(sym, time.time())
        if stale > 300 and sym not in self.feed_alerted:
            self.feed_alerted.add(sym)
            self.j.event("error", "feed", f"{sym}: no candles for {stale / 60:.0f} min", {"error": msg})
            self.notify("WF feed error", [f"{sym}: no candles for {stale / 60:.0f} min", msg])

    # --------------------------------------------------------------- signals
    def _log_signal(self, e: Entry | Skip, taken: bool, reason: str | None) -> None:
        sig = e.signal
        st = e.strategy
        self.j.signal(st.id, st.symbol, fmt(sig.bar_start), fmt(e.t), sig.pattern, sig.tf, sig.side, taken, reason,
                      {k: v for k, v in sig.detail.items() if k in ("stop", "line", "atr", "target", "neck", "H3", "h")}
                      | {"trend": sig.trend, "k": sig.k})

    def _log_skip(self, sk: Skip) -> None:
        if sk.reason in ("trend",):  # filtered by design, not interesting; keep the journal readable
            return
        self._log_signal(sk, False, sk.reason)

    # ------------------------------------------------------------------ orders
    def _enter(self, e: Entry, q: dict | None) -> None:
        st = e.strategy
        if st.id in self.j.disabled():
            self._log_signal(e, False, "killed")
            return
        if not st.enabled:
            self._log_signal(e, False, "disabled")
            return
        sym = st.symbol
        pip = self.cfg.pip[sym]
        X = st.stop_pips * pip
        tp = st.take_profit_pips * pip
        tid = self.j.new_trade(
            strategy=st.id, symbol=sym, side=e.side, status="pending", signal_bar=fmt(e.signal.bar_start),
            entry_minute=e.t, entry_utc=research_to_utc(e.t).isoformat(), lots=st.lots, stop_dist=X, tp_dist=tp,
            ref_bid=q.get("bid") if q else None, ref_ask=q.get("ask") if q else None,
            detail=json.dumps({"pattern": e.signal.pattern, "tf": e.signal.tf, "k": e.signal.k,
                               "signal": {k: v for k, v in e.signal.detail.items() if isinstance(v, int | float)}}),
        )
        self._log_signal(e, True, None)
        self.timers[tid] = TimeExit(self.markets[sym].n_m1)
        side_txt = "BUY" if e.side == 1 else "SELL"
        if self.s.dry_run:
            self.j.update_trade(tid, status="failed", exit_reason="DRY_RUN")
            log.info("DRY %s %s %s %.2f lots SL %s TP %s", st.id, side_txt, sym, st.lots, X, tp)
            return
        out = self.hub.market_order(source=st.source, instrument=sym, side=e.side, lots=st.lots, sl_distance=X,
                                    tp_distance=tp, digits=DIGITS.get(sym, 5), note=f"{st.source} {e.signal.pattern}")
        out = self.hub.wait(out, 20.0)
        self.j.update_trade(tid, operation_id=out.operation_id)
        if out.state == "succeeded" and out.position_id:
            self.j.update_trade(tid, status="open", position_id=out.position_id, fill_price=out.price)
            self.notify(f"WF {st.id} {side_txt} {sym}", [st.name, f"fill {out.price} lots {st.lots}",
                                                       f"SL {X:g} / TP {tp:g} (price units)"])
        elif out.state == "rejected":
            self.j.update_trade(tid, status="failed", exit_reason="REJECTED", detail=json.dumps({"error": out.error}))
            self.timers.pop(tid, None)
            self.j.event("error", "order", f"{st.id} {side_txt} {sym} rejected: {out.error}")
            self.notify(f"WF {st.id} order rejected", [f"{side_txt} {sym}", str(out.error)])
        else:
            # unknown/pending: the order may have reached the broker. sync() adopts the position by label.
            self.j.event("warn", "order", f"{st.id} {side_txt} {sym} outcome {out.state}: {out.error}")

    def _time_exits(self, sym: str, T: int) -> None:
        mk = self.markets[sym]
        for row in self.j.open_trades():
            if row["symbol"] != sym or row["status"] != "open":
                continue
            timer = self.timers.get(row["id"])
            if timer is None:
                continue
            why = timer.due(mk.n_m1, T, mk.last_t)
            if why is None:
                continue
            st = self.by_id[row["strategy"]]
            self.j.update_trade(row["id"], status="closing", exit_request=why)
            if self.s.dry_run:
                continue
            out = self.hub.wait(self.hub.close_position(source=st.source, position_id=row["position_id"],
                                                        lots=row["lots"]), 20.0)
            if out.state == "rejected":
                self.j.event("error", "close", f"{st.id} {why} close rejected: {out.error}")
                self.notify(f"WF {st.id} close failed", [why, str(out.error)])
                self.j.update_trade(row["id"], status="open")

    # ------------------------------------------------------------------- sync
    def sync(self, startup: bool = False) -> None:
        try:
            positions = self.hub.positions()
        except Exception as exc:  # noqa: BLE001
            log.warning("positions: %s", exc)
            return
        ours = {int(p["position_id"]): p for p in positions if (p.get("label") or "").startswith("wf_")}
        by_source = {st.source: st for st in self.cfg.strategies}
        known = {int(r["position_id"]) for r in self.j.open_trades() if r["position_id"]}
        for r in self.j.open_trades():
            if r["status"] == "pending":
                st = self.by_id[r["strategy"]]
                cand = [p for pid, p in ours.items() if p.get("label") == st.source and pid not in known]
                if cand:
                    p = cand[0]
                    self.j.update_trade(r["id"], status="open", position_id=int(p["position_id"]),
                                        fill_price=float(p["price"]) if p.get("price") else None)
                    known.add(int(p["position_id"]))
                    self.j.event("info", "adopt", f"{st.id}: pending order resolved to position {p['position_id']}")
                elif parse_ts(r["updated_at"]) < datetime.now(UTC) - timedelta(minutes=5):
                    self.j.update_trade(r["id"], status="failed", exit_reason="NO_FILL")
                    self.timers.pop(r["id"], None)
                    self.j.event("warn", "order", f"{st.id}: no position after 5 min, marked failed")
                continue
            if r["position_id"] and int(r["position_id"]) not in ours:
                self._close_from_deals(r)
        # broker positions with our labels that the journal does not know (e.g. journal lost): adopt, never duplicate
        for pid, p in ours.items():
            if pid in known:
                continue
            st = by_source.get(p.get("label"))
            if st is None:
                continue
            opened = parse_ts(p["opened_at"]) if p.get("opened_at") else datetime.now(UTC)
            t_entry = utc_to_research(opened.replace(second=0, microsecond=0))
            side = 1 if p.get("direction") == "buy" else -1
            tid = self.j.new_trade(strategy=st.id, symbol=st.symbol, side=side, status="open", position_id=pid,
                                   entry_minute=t_entry, entry_utc=opened.isoformat(), lots=float(p["volume_lots"]),
                                   fill_price=float(p["price"]) if p.get("price") else None,
                                   stop_dist=st.stop_pips * self.cfg.pip[st.symbol],
                                   tp_dist=st.take_profit_pips * self.cfg.pip[st.symbol])
            self.timers[tid] = TimeExit(self._index_at(st.symbol, t_entry))
            self.j.event("warn", "adopt", f"{st.id}: adopted unknown broker position {pid}")
        self._ftmo_check()

    def _close_from_deals(self, r) -> None:
        since = parse_ts(r["entry_utc"]) - timedelta(minutes=5)
        try:
            deals = self.hub.deals(since)
        except Exception as exc:  # noqa: BLE001
            log.warning("deals: %s", exc)
            return
        pid = int(r["position_id"])
        mine = [d for d in deals if d.get("position_id") == pid]
        closing = [d for d in mine if d.get("closing")]
        if not closing:
            return  # deal not visible yet; retry next minute
        vol = sum(float(d.get("volume_lots") or 0) for d in closing) or 1.0
        px = sum(float(d["price"]) * float(d.get("volume_lots") or 0) for d in closing) / vol
        entry = float(r["fill_price"] or (closing[0].get("entry_price") or px))
        side = int(r["side"])
        X = float(r["stop_dist"])
        gross = sum(float(d.get("gross_profit") or 0) for d in closing)
        comm = sum(float(d.get("commission") or 0) for d in mine)
        swap = sum(float(d.get("swap") or 0) for d in closing)
        net = gross + comm + swap
        r_price = side * (px - entry) / X
        usd_per_unit = gross / (side * (px - entry)) if abs(px - entry) > 1e-12 and gross else None
        r_net = net / (usd_per_unit * X) if usd_per_unit else r_price
        reason = r["exit_request"]
        if not reason:
            sl = entry - side * X
            tp = entry + side * float(r["tp_dist"])
            d_sl, d_tp = abs(px - sl), abs(px - tp)
            reason = "SL" if d_sl <= d_tp and d_sl <= 0.25 * X else "TP" if d_tp <= 0.25 * X else "MANUAL"
        exit_t = max(parse_ts(d["executed_at"]) for d in closing if d.get("executed_at"))
        pips = side * (px - entry) / self.cfg.pip[r["symbol"]]
        self.j.update_trade(r["id"], status="closed", exit_price=px, exit_utc=exit_t.isoformat(), exit_reason=reason,
                            r_price=r_price, r_net=r_net, pips=pips, gross_usd=gross, commission_usd=comm,
                            swap_usd=swap, net_usd=net)
        self.timers.pop(r["id"], None)
        self.notify(f"WF {r['strategy']} exit {reason}", [f"{r['symbol']} R {r_net:+.2f} ({pips:+.1f} pips)",
                                                          f"net ${net:+.2f}"])
        self._kill_rule(r["strategy"])

    def _kill_rule(self, sid: str) -> None:
        rows = self.j.closed_trades(sid)
        n = len(rows)
        cum = sum(float(x["r_net"] or 0) for x in rows)
        if n >= self.kill_min and cum < self.kill_below and sid not in self.j.disabled():
            msg = f"kill rule: {n} trades, cumulative R {cum:+.2f} < {self.kill_below}"
            self.j.set_disabled(sid, True, msg)
            self.j.event("warn", "kill", f"{sid} disabled ({msg})")
            self.notify(f"WF {sid} DISABLED", [msg, "Re-enable only with the owner (wfp enable)."])

    # ----------------------------------------------------- FTMO (record only)
    def _ftmo_check(self) -> None:
        closed = self.j.closed_trades()
        realised = sum(float(r["net_usd"] or 0) for r in closed)
        floating = 0.0
        for r in self.j.open_trades():
            if r["status"] == "pending" or r["fill_price"] is None:
                continue
            q = None
            try:
                q = self.hub.quote(r["symbol"])
            except Exception:  # noqa: BLE001
                pass
            if not q:
                continue
            side = int(r["side"])
            mark = float(q["bid"] if side == 1 else q["ask"])
            pnl = side * (mark - float(r["fill_price"])) * CONTRACT[r["symbol"]] * float(r["lots"])
            if r["symbol"].startswith("USD"):
                pnl /= mark
            floating += pnl
        today = datetime.now(PRAGUE).date().isoformat()
        start_key = f"ftmo_day_start:{today}"
        equity = self.s.initial_balance + realised + floating
        day_start = self.j.get(start_key)
        if day_start is None:
            self.j.put(start_key, f"{self.s.initial_balance + realised:.2f}")
            day_start = f"{self.s.initial_balance + realised:.2f}"
        day_loss = float(day_start) - equity
        self.j.put("equity_est", json.dumps({"equity": equity, "realised": realised, "floating": floating,
                                             "day_start": float(day_start), "at": datetime.now(UTC).isoformat()}))
        for kind, lim, val in (("daily", self.s.daily_loss_pct, day_loss),
                               ("max", self.s.max_loss_pct, self.s.initial_balance - equity)):
            limit = self.s.initial_balance * lim / 100
            key = f"ftmo_breach:{kind}:{today if kind == 'daily' else 'all'}"
            if val >= limit and self.j.get(key) is None:
                self.j.put(key, f"{val:.2f}")
                msg = f"FTMO {kind} loss limit breached (record only): -${val:,.0f} vs ${limit:,.0f}"
                self.j.event("warn", "ftmo", msg, {"equity": equity})
                self.notify("WF FTMO breach (record only)", [msg])

    # ------------------------------------------------------------- summary
    def daily_summary(self) -> None:
        rows = self.j.closed_trades()
        day = trading_day(utc_to_research(datetime.now(UTC)))
        today = [r for r in rows if trading_day(utc_to_research(parse_ts(r["exit_utc"]))) == day]
        cum = sum(float(r["r_net"] or 0) for r in rows)
        lines = [f"today: {len(today)} closed, R {sum(float(r['r_net'] or 0) for r in today):+.2f}, "
                 f"${sum(float(r['net_usd'] or 0) for r in today):+.2f}",
                 f"all: {len(rows)} trades, R {cum:+.2f}, open {len(self.j.open_trades())}"]
        self.notify("WF daily summary", lines)

    # ---------------------------------------------------------------- loop
    def run_forever(self) -> None:
        self.warmup()
        last_summary_day = None
        while True:
            now = datetime.now(UTC)
            nxt = now.replace(second=0, microsecond=0) + timedelta(minutes=1, seconds=2)
            time.sleep(max(0.0, (nxt - now).total_seconds()))
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - keep running; journal + notify
                log.exception("tick failed")
                self.j.event("error", "tick", str(exc))
            ny = datetime.now(ZoneInfo("America/New_York"))
            if ny.hour == 17 and ny.minute < 2 and last_summary_day != ny.date():
                last_summary_day = ny.date()
                self.daily_summary()
            self.j.put("heartbeat", datetime.now(UTC).isoformat(timespec="seconds"))
