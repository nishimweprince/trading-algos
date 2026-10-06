"""The engine: every recorded line in, decisions and paper fills out.

Live and replay drive the same ``handle_line(recv_ns, venue, text)``, so a
replay of the tape makes the decisions the live process made. The venues are:

- ``k``: a raw Kalshi WS frame (``orderbook_snapshot`` / ``orderbook_delta``)
- ``p``: a raw Polymarket market-channel frame (``book``, ``price_change``,
  ``best_bid_ask``, ``market_resolved``, ...)
- ``c``: a control line written by this process: ``window`` (identity),
  ``reconnect`` / ``resync`` (drop a venue's books), ``resolution``

**Window identity and the ghost gap (kpc trap 3).** Books are keyed by the
window's own ticker and token ids, never by asset, so the old contract's
terminal 0.99/0.01 book can never be read as the new window's. On top of that,
at every roll the old window's books are dropped, and a leg counts as valid
only if its book has updated since the window opened.

**Evaluation (kpc trap 5).** The rule runs on every book update of the
asset's current window, not on a one-second sample.
"""

from __future__ import annotations

import logging
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import orjson
from ta_core.logging_config import log_event

from .book import Book, Quote, px
from .decision import WINDOW_S, Action, Decision, StrategyParams, decide
from .fees import taker_fee
from .ledger import Ledger, Position
from .metrics import Metrics
from .risk import RiskState
from .trades import JsonlDaily
from .venue import Venue
from .windows import Window, window_open

__all__ = ["AssetState", "Engine"]

NS = 1_000_000_000
SERIES_POINTS = 2 * WINDOW_S
KEEP_WINDOWS_S = 6 * 3600  # identities kept for late resolutions

Alert = Callable[[str, str, list[str]], None]


@dataclass
class AssetState:
    window: Window | None = None
    open_unix: int | None = None
    traded: bool = False
    decision: dict[str, Any] | None = None
    max_abs_gap: float = 0.0
    evals: int = 0
    reasons: Counter[str] = field(default_factory=Counter)
    last_gap: float | None = None


@dataclass
class _Pending:
    due_ns: int
    window: Window
    token: str
    side: str
    decision: Decision
    decision_ns: int


class Engine:
    def __init__(
        self,
        params: StrategyParams,
        assets: tuple[str, ...],
        *,
        ledger: Ledger,
        risk: RiskState,
        venue: Venue | None,
        journal: JsonlDaily | None = None,
        alert: Alert | None = None,
        stamp: dict[str, str] | None = None,
    ) -> None:
        self.params = params
        self.assets = assets
        # epoch / build / config, added to every journal record.
        self.stamp = stamp or {}
        self.ledger = ledger
        self.risk = risk
        self.venue = venue
        self.journal = journal or JsonlDaily(None)
        self._alert = alert
        self.windows: dict[str, dict[int, Window]] = {a: {} for a in assets}
        self.by_condition: dict[str, Window] = {}
        self.books: dict[str, Book] = {}
        self.ids: dict[str, tuple[str, int]] = {}  # book id -> (asset, open_unix)
        self.state = {a: AssetState() for a in assets}
        self.pending: list[_Pending] = []
        self.resolutions: dict[tuple[str, int], dict[str, str]] = {}
        self.recent: deque[dict[str, Any]] = deque(maxlen=200)
        self.counters: Counter[str] = Counter()
        self.last_ns = 0
        self.last_by_venue: dict[str, int] = {}
        self.metrics = Metrics()
        # One price point per asset per second, two windows deep, for the charts.
        self.series: dict[str, deque[tuple[Any, ...]]] = {
            a: deque(maxlen=SERIES_POINTS) for a in assets
        }
        self._sample_sec: int | None = None
        self._grid_open: int | None = None

    # --- input ------------------------------------------------------------------

    def handle_line(self, recv_ns: int, venue: str, text: str) -> None:
        self.last_ns = recv_ns
        self.last_by_venue[venue] = recv_ns
        sec = recv_ns // NS
        if sec != self._sample_sec:
            if self._sample_sec is not None:
                self._sample(self._sample_sec)  # the books as they stood at its end
            self._sample_sec = sec
        grid = window_open(recv_ns / NS)
        if grid != self._grid_open:
            self._grid_open = grid
            self._roll(recv_ns)
        if self.pending:
            self._resolve_pending(recv_ns)
        try:
            body = orjson.loads(text)
        except ValueError:
            self.counters["bad_json"] += 1
            return
        if venue == "p":
            touched = self._poly(body, recv_ns)
        elif venue == "k":
            touched = self._kalshi(body, recv_ns)
        elif venue == "c":
            touched = self._control(body, recv_ns)
        else:
            self.counters["unknown_venue"] += 1
            return
        for asset in touched:
            self._evaluate(asset, recv_ns)

    def _sample(self, sec: int) -> None:
        for asset, st in self.state.items():
            window = st.window
            if window is None or not window.open_unix <= sec < window.close_unix:
                continue
            quotes = self.quotes(asset)
            if quotes is None:
                continue
            k, y, n = quotes
            self.series[asset].append(
                (sec, window.open_unix, _r(k.mid), _r(y.mid), y.bid, y.ask, n.bid, n.ask)
            )

    def series_since(self, since: int) -> dict[str, list[tuple[Any, ...]]]:
        return {a: [p for p in pts if p[0] > since] for a, pts in self.series.items()}

    # --- windows ----------------------------------------------------------------

    def _register(self, window: Window, recv_ns: int) -> None:
        if window.asset not in self.windows:
            return
        known = self.windows[window.asset].get(window.open_unix)
        if known == window:
            return
        if known is not None:
            log_event("window_changed", level=logging.WARNING, asset=window.asset)
            for old in known.ids:
                self.books.pop(old, None)
                self.ids.pop(old, None)
        self.windows[window.asset][window.open_unix] = window
        self.by_condition[window.condition_id] = window
        if window.close_unix > recv_ns / NS:
            for book_id in window.ids:
                self.books.setdefault(book_id, Book())
                self.ids[book_id] = (window.asset, window.open_unix)
        self._roll(recv_ns)

    def _roll(self, recv_ns: int) -> None:
        now_open = window_open(recv_ns / NS)
        for asset in self.assets:
            st = self.state[asset]
            window = self.windows[asset].get(now_open)
            if st.open_unix == now_open and st.window is window:
                continue
            if st.open_unix != now_open:
                self._close_window(asset, st, recv_ns)
                self.state[asset] = st = AssetState(open_unix=now_open)
            st.window = window
            # Drop every book of an earlier window: nothing may read them again.
            for book_id, (owner, open_unix) in list(self.ids.items()):
                if owner == asset and open_unix < now_open:
                    self.ids.pop(book_id)
                    self.books.pop(book_id, None)
            cutoff = now_open - KEEP_WINDOWS_S
            for open_unix in [o for o in self.windows[asset] if o < cutoff]:
                old = self.windows[asset].pop(open_unix)
                self.by_condition.pop(old.condition_id, None)
                self.resolutions.pop((asset, open_unix), None)

    def _close_window(self, asset: str, st: AssetState, recv_ns: int) -> None:
        if st.open_unix is None:
            return
        if st.window is None:
            self.counters["windows_unidentified"] += 1
        self._write(
            recv_ns,
            {
                "type": "window",
                "asset": asset,
                "open_unix": st.open_unix,
                "identified": st.window is not None,
                "action": (st.decision or {}).get("action", "none"),
                "max_abs_gap": round(st.max_abs_gap, 4),
                "evals": st.evals,
                "reasons": dict(st.reasons),
            },
            keep=False,
        )

    # --- venues -----------------------------------------------------------------

    def _book_for(self, book_id: str) -> Book | None:
        return self.books.get(book_id) if book_id in self.ids else None

    def _poly(self, body: Any, recv_ns: int) -> set[str]:
        touched: set[str] = set()
        for msg in body if isinstance(body, list) else [body]:
            if not isinstance(msg, dict):
                continue
            self._feed_latency("feed_ms_p", msg.get("timestamp"), recv_ns)
            kind = msg.get("event_type") or ("book" if "bids" in msg else None)
            if kind == "book":
                book = self._book_for(str(msg.get("asset_id")))
                if book is None:
                    continue
                book.snapshot(
                    [(px(lv["price"]), float(lv["size"])) for lv in msg.get("bids", [])],
                    [(px(lv["price"]), float(lv["size"])) for lv in msg.get("asks", [])],
                    recv_ns,
                )
                touched.add(self.ids[str(msg["asset_id"])][0])
            elif kind == "price_change":
                for change in msg.get("price_changes", []):
                    book_id = str(change.get("asset_id"))
                    book = self._book_for(book_id)
                    if book is None or book.snapshot_ns is None:
                        continue
                    side = "bid" if change.get("side") == "BUY" else "ask"
                    book.set_level(side, px(change["price"]), float(change["size"]), recv_ns)
                    self._check_bbo(book, change, "pc")
                    touched.add(self.ids[book_id][0])
            elif kind == "best_bid_ask":
                book = self._book_for(str(msg.get("asset_id")))
                if book is not None and book.snapshot_ns is not None:
                    self._check_bbo(book, msg, "event")
            elif kind == "market_resolved":
                self._poly_resolved(msg, recv_ns)
        return touched

    def _check_bbo(self, book: Book, msg: dict[str, Any], source: str) -> None:
        """The venue's own top of book against ours: a desync shows up here first.

        ``pc``: the best bid/ask carried on each ``price_change``. On the first
        tape (2026-10-06, 1.07M checks) ours matched 99.86%; the misses were the
        venue's own field lagging its deltas by one message. This is the desync
        alarm.

        ``event``: the separate ``best_bid_ask`` event. On the same tape it
        disagreed 97% of the time, because it carries the top of book from
        *before* the ``price_change`` that arrived just ahead of it. Never price
        off it (kpc trap 6, again); it is counted only to notice if that changes.
        """
        bid, ask = msg.get("best_bid"), msg.get("best_ask")
        if bid is None or ask is None:
            return
        self.counters[f"bbo_checks_{source}"] += 1
        ours_bid, ours_ask = book.best_bid(), book.best_ask()
        if (ours_bid is not None and px(bid) != ours_bid) or (
            ours_ask is not None and px(ask) != ours_ask
        ):
            self.counters[f"bbo_mismatch_{source}"] += 1

    def _poly_resolved(self, msg: dict[str, Any], recv_ns: int) -> None:
        window = self.by_condition.get(str(msg.get("market")))
        if window is None:
            return
        winner = str(msg.get("winning_asset_id") or "")
        outcome = (
            "yes" if winner == window.yes_token else "no" if winner == window.no_token else None
        )
        if outcome is not None:
            self._resolution(window.asset, window.open_unix, "poly", outcome, recv_ns)

    def _feed_latency(self, name: str, venue_ms: Any, recv_ns: int) -> None:
        """Receive time minus the venue's own timestamp (ms). Includes clock skew."""
        if isinstance(venue_ms, str):
            if not venue_ms.isdigit():
                return
            venue_ms = int(venue_ms)
        if isinstance(venue_ms, int | float) and venue_ms > 0:
            self.metrics.add(name, recv_ns / 1e6 - venue_ms)

    def _kalshi(self, body: dict[str, Any], recv_ns: int) -> set[str]:
        kind = body.get("type")
        msg = body.get("msg") or {}
        if kind not in ("orderbook_snapshot", "orderbook_delta"):
            return set()
        self._feed_latency("feed_ms_k", msg.get("ts_ms") or body.get("sending_ts_ms"), recv_ns)
        ticker = str(msg.get("market_ticker"))
        book = self._book_for(ticker)
        if book is None:
            return set()
        if kind == "orderbook_snapshot":
            yes = [(px(p), float(s)) for p, s in _kalshi_levels(msg, "yes")]
            no = [(px(1 - float(p)), float(s)) for p, s in _kalshi_levels(msg, "no")]
            book.snapshot(yes, no, recv_ns)
        else:
            if book.snapshot_ns is None:
                return set()
            price, delta = _kalshi_delta(msg)
            if msg.get("side") == "yes":
                book.add_level("bid", px(price), delta, recv_ns)
            else:
                book.add_level("ask", px(1 - price), delta, recv_ns)
        return {self.ids[ticker][0]}

    def _control(self, body: dict[str, Any], recv_ns: int) -> set[str]:
        kind = body.get("type")
        if kind == "window":
            window = Window.from_control(body)
            self._register(window, recv_ns)
            return {window.asset} if window.asset in self.state else set()
        if kind in ("reconnect", "resync"):
            prefix = body.get("venue")
            for book_id, book in self.books.items():
                is_kalshi = book_id.startswith("KX")
                if (prefix == "k") == is_kalshi:
                    book.invalidate()
            self.counters[f"{kind}_{prefix}"] += 1
            return set()
        if kind == "resolution":
            self._resolution(
                body["asset"], int(body["open_unix"]), body["venue"], body["outcome"], recv_ns
            )
        return set()

    # --- decisions --------------------------------------------------------------

    def quotes(self, asset: str) -> tuple[Quote, Quote, Quote] | None:
        st = self.state[asset]
        if st.window is None:
            return None
        since = st.window.open_unix * NS
        k, y, n = (self.books.get(i) for i in st.window.ids)
        if k is None or y is None or n is None:
            return None
        return k.quote(since), y.quote(since), n.quote(since)

    def _evaluate(self, asset: str, recv_ns: int) -> None:
        st = self.state[asset]
        if st.window is None or st.traded:
            return
        quotes = self.quotes(asset)
        if quotes is None:
            return
        k, y, n = quotes
        sec_into = recv_ns / NS - st.window.open_unix
        decision = decide(k, y, n, self.params, sec_into, st.traded)
        st.evals += 1
        st.reasons[decision.reason or decision.action.value] += 1
        if decision.reason == "invalid_leg":
            self._count_invalid_legs(st.window)
        if decision.gap is not None:
            st.last_gap = decision.gap
            st.max_abs_gap = max(st.max_abs_gap, abs(decision.gap))
        if not decision.consumes_window:
            return
        st.traded = True
        ages = {
            leg: None if b is None or b.last_update_ns is None
            else round((recv_ns - b.last_update_ns) / 1e6, 1)
            for leg, b in zip(
                ("kalshi", "poly_yes", "poly_no"),
                (self.books.get(i) for i in st.window.ids),
                strict=True,
            )
        }  # fmt: skip
        if ages["kalshi"] is not None:
            self.metrics.add("age_ms_kalshi", ages["kalshi"])
        poly_age = ages["poly_yes"] if decision.buy_yes else ages["poly_no"]
        if poly_age is not None:
            self.metrics.add("age_ms_poly", poly_age)
        record = {
            "type": "decision",
            "asset": asset,
            "open_unix": st.window.open_unix,
            "t_ns": recv_ns,
            "sec_into": round(sec_into, 3),
            "action": decision.action.value,
            "reason": decision.reason,
            "gap": round(decision.gap or 0.0, 4),
            "side": "yes" if decision.buy_yes else "no",
            "entry_px": decision.entry_px,
            "contracts": decision.contracts,
            "kalshi": k.as_dict(),
            "poly_yes": y.as_dict(),
            "poly_no": n.as_dict(),
            # How old each leg's book was at the decision: a stale signal shows here.
            "age_ms": ages,
            "params": {
                "gap_threshold": self.params.gap_threshold,
                "max_entry_px": self.params.max_entry_px,
                "notional_usd": self.params.notional_usd,
            },
        }
        if decision.action is Action.ENTER:
            record["blocked"] = self._submit(st.window, decision, recv_ns)
        st.decision = record
        self._write(recv_ns, record)

    def _count_invalid_legs(self, window: Window) -> None:
        """Why legs were invalid at evaluation: ``invalid_kalshi_crossed`` and the like.
        Only runs on the invalid path, so it costs nothing while the books are good."""
        since = window.open_unix * NS
        for leg, book_id in zip(("kalshi", "poly_yes", "poly_no"), window.ids, strict=True):
            book = self.books.get(book_id)
            state = "no_book" if book is None else book.state(since)
            if state != "ok":
                self.counters[f"invalid_{leg}_{state}"] += 1

    def _submit(self, window: Window, decision: Decision, recv_ns: int) -> str | None:
        if self.venue is None:
            return "mode_off"
        assert decision.entry_px is not None
        est = decision.contracts * decision.entry_px
        est += taker_fee(
            decision.entry_px, decision.contracts, window.fee_rate, window.fee_exponent
        )
        blocked = self.risk.check(self.ledger, est, recv_ns)
        if blocked == "insufficient_cash":
            self.ledger.reject(blocked, asset=window.asset, cost=round(est, 2))
            self._send_alert(
                "insufficient_cash",
                "poly-kalshi: paper entry rejected, insufficient cash",
                [f"cash={self.ledger.cash:.2f} needed={est:.2f}", "P&L will flat-line from here"],
            )
        if blocked is not None:
            self.counters[f"blocked_{blocked}"] += 1
            return blocked
        side = "yes" if decision.buy_yes else "no"
        token = window.yes_token if decision.buy_yes else window.no_token
        self.pending.append(
            _Pending(recv_ns + self.venue.latency_ns, window, token, side, decision, recv_ns)
        )
        return None

    def _resolve_pending(self, recv_ns: int) -> None:
        due = [p for p in self.pending if p.due_ns <= recv_ns]
        if not due:
            return
        self.pending = [p for p in self.pending if p.due_ns > recv_ns]
        for order in due:
            self._fill(order, recv_ns)

    def _fill(self, order: _Pending, recv_ns: int) -> None:
        assert self.venue is not None and order.decision.entry_px is not None
        decision, window = order.decision, order.window
        book = self.books.get(order.token) or Book()
        result = self.venue.fill(
            book, decision.entry_px, decision.contracts, window.fee_rate, window.fee_exponent
        )
        touch_fee = taker_fee(
            decision.entry_px, decision.contracts, window.fee_rate, window.fee_exponent
        )
        record = {
            "type": "fill",
            "asset": window.asset,
            "open_unix": window.open_unix,
            "t_ns": recv_ns,
            "latency_ms": round((recv_ns - order.decision_ns) / 1e6, 1),
            "side": order.side,
            "limit_px": decision.entry_px,
            "ordered": decision.contracts,
            "shares": result.shares,
            "vwap": None if result.vwap is None else round(result.vwap, 4),
            "cost": round(result.cost, 4),
            "fee": round(result.fee, 4),
            "levels": result.levels,
            "best_ask_at_fill": result.best_ask_at_fill,
            "touch_px": decision.entry_px,
            "touch_shares": decision.contracts,
            "touch_fee": round(touch_fee, 4),
        }
        self.counters["fills" if result.shares > 0 else "no_fills"] += 1
        if result.shares > 0:
            self.ledger.buy(
                Position(
                    asset=window.asset,
                    open_unix=window.open_unix,
                    side=order.side,
                    shares=result.shares,
                    cost=result.cost,
                    fee=result.fee,
                    entry_ns=recv_ns,
                )
            )
            known = self.resolutions.get((window.asset, window.open_unix), {}).get("poly")
            if known is not None:
                self._settle(window.asset, window.open_unix, known, recv_ns)
        self._write(recv_ns, record)

    # --- settlement -------------------------------------------------------------

    def _resolution(
        self, asset: str, open_unix: int, venue: str, outcome: str, recv_ns: int
    ) -> None:
        if outcome not in ("yes", "no"):
            return
        seen = self.resolutions.setdefault((asset, open_unix), {})
        if venue in seen:
            return
        seen[venue] = outcome
        delay_s = round(recv_ns / NS - (open_unix + WINDOW_S), 1)
        self.metrics.add(f"resolution_s_{venue}", delay_s)
        self._write(
            recv_ns,
            {
                "type": "resolution",
                "asset": asset,
                "open_unix": open_unix,
                "venue": venue,
                "outcome": outcome,
                "delay_s": delay_s,
            },
            keep=False,
        )
        if venue == "poly":
            self._settle(asset, open_unix, outcome, recv_ns)

    def _settle(self, asset: str, open_unix: int, outcome: str, recv_ns: int) -> None:
        position = self.ledger.settle(asset, open_unix, outcome, recv_ns)
        if position is None:
            return
        self.counters["settled"] += 1
        self._write(
            recv_ns,
            {
                "type": "settle",
                "asset": asset,
                "open_unix": open_unix,
                "side": position.side,
                "outcome": outcome,
                "shares": position.shares,
                "vwap": round(position.vwap, 4),
                "payout": position.payout,
                "pnl": round(position.pnl or 0.0, 4),
                "cash": round(self.ledger.cash, 2),
            },
        )

    # --- output -----------------------------------------------------------------

    def _write(self, recv_ns: int, record: dict[str, Any], *, keep: bool = True) -> None:
        if self.stamp:
            record = record | self.stamp
        self.journal.write(recv_ns, record)
        if keep:
            self.recent.append(record)

    def _send_alert(self, key: str, subject: str, lines: list[str]) -> None:
        if self._alert is not None:
            self._alert(key, subject, lines)

    def status(self) -> dict[str, Any]:
        now = self.last_ns
        assets = {}
        for asset, st in self.state.items():
            quotes = self.quotes(asset)
            sec_into = None if st.open_unix is None else now / NS - st.open_unix
            gap = preview = None
            ages: dict[str, float | None] = {}
            legs: dict[str, dict[str, Any]] = {}
            if quotes is not None and st.window is not None and sec_into is not None:
                k, y, n = quotes
                if k.mid is not None and y.mid is not None:
                    gap = round(k.mid - y.mid, 4)
                # What the rule would do with the books as they stand, ignoring
                # whether this window is already consumed.
                d = decide(k, y, n, self.params, sec_into, False)
                preview = {
                    "action": d.action.value,
                    "reason": d.reason,
                    "side": None if d.buy_yes is None else ("yes" if d.buy_yes else "no"),
                    "entry_px": d.entry_px,
                }
                since = st.window.open_unix * NS
                for leg, book_id in zip(
                    ("kalshi", "poly_yes", "poly_no"), st.window.ids, strict=True
                ):
                    book = self.books.get(book_id)
                    ages[leg] = (
                        None
                        if book is None or book.last_update_ns is None
                        else round((now - book.last_update_ns) / 1e6, 0)
                    )
                    # Why a leg is invalid, with whatever prices it does have.
                    legs[leg] = (
                        {"state": "no_book", "bid": None, "ask": None}
                        if book is None
                        else {
                            "state": book.state(since),
                            "bid": book.best_bid(),
                            "ask": book.best_ask(),
                        }
                    )
            decided = st.decision or {}
            assets[asset] = {
                "open_unix": st.open_unix,
                "identified": st.window is not None,
                "sec_into": None if sec_into is None else round(sec_into, 1),
                "traded": st.traded,
                "action": decided.get("action"),
                "decision": {
                    k: decided.get(k)
                    for k in ("action", "side", "entry_px", "sec_into", "blocked", "gap")
                }
                if decided
                else None,
                "gap": gap,
                "last_gap": None if st.last_gap is None else round(st.last_gap, 4),
                "max_abs_gap": round(st.max_abs_gap, 4),
                "evals": st.evals,
                "preview": preview,
                "age_ms": ages,
                "legs": legs,
                "kalshi": quotes[0].as_dict() if quotes else None,
                "poly_yes": quotes[1].as_dict() if quotes else None,
                "poly_no": quotes[2].as_dict() if quotes else None,
            }
        return {
            "now_ns": now,
            "feed_age_s": {v: round((now - t) / NS, 1) for v, t in self.last_by_venue.items()},
            "assets": assets,
            "books": len(self.books),
            "pending": len(self.pending),
            "counters": dict(self.counters),
            "latency": self.metrics.snapshot(),
            "ledger": self.ledger.snapshot(now) if now else None,
            "risk": self.risk.snapshot(),
        }


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _kalshi_levels(msg: dict[str, Any], side: str) -> list[tuple[float, float]]:
    """Snapshot levels in dollars; older frames carried integer cents."""
    for key in (f"{side}_dollars_fp", f"{side}_dollars"):
        if msg.get(key) is not None:
            return [(float(p), float(s)) for p, s in msg[key]]
    return [(int(p) / 100, float(s)) for p, s in msg.get(side) or []]


def _kalshi_delta(msg: dict[str, Any]) -> tuple[float, float]:
    price = (
        float(msg["price_dollars"]) if msg.get("price_dollars") is not None else msg["price"] / 100
    )
    delta = float(msg["delta_fp"]) if msg.get("delta_fp") is not None else float(msg["delta"])
    return price, delta
