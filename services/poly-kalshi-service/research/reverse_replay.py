"""Reverse-direction replay: Polymarket is the signal, Kalshi is the execution venue.

    python -m research.reverse_replay --record-dir data/raw/dev --out research/data/rev1
    python -m research.reverse_replay --record-dir data/raw/dev --out research/data/rev_t10 \
        --gap 0.10

The forward trade buys the cheap Polymarket leg when |Kalshi mid - Poly mid|
crosses the threshold. This buys the cheap Kalshi leg instead, with the same
first-crossing / cap / timing / hold-to-settlement rules:

- ``gap = poly_yes_mid - kalshi_yes_mid``; ``|gap| >= threshold`` enters.
- ``gap > 0`` buys Kalshi YES at the YES ask; ``gap < 0`` buys Kalshi NO at
  ``1 - YES bid`` (Kalshi publishes bids only; NO asks are the mirrored YES
  bids, cheapest first).
- Size is ``floor(notional / entry_px)`` whole contracts; the limit sits
  ``--limit-offset`` above the touch, never above the cap.
- The order meets the book ``--latency-ms`` after the decision (default 47,
  the US-East host assumption in ~/poly-kalshi-reverse-direction.md) and walks
  the Kalshi ladder; partial fills are kept.
- Fee is Kalshi's taker ``0.07 * p * (1-p)`` per share, rounded UP to the cent
  per order. Verify the 15-minute series schedule before trusting magnitudes.
- Settlement is Kalshi's outcome; Polymarket's is recorded alongside so venue
  disagreement is measured on traded windows.

Implementation reuses the live engine's book/window/roll parsing exactly by
subclassing ``Engine`` and overriding only decision, fill and settlement. The
live trader is untouched. Markouts/exits are disabled in v1 (they would read
the wrong leg); decisions, fills, settles, resolutions and window summaries
are written to the journal like a forward replay.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from poly_kalshi_service.book import Book
from poly_kalshi_service.decision import WINDOW_S, Action, Decision, StrategyParams
from poly_kalshi_service.engine import NS, Engine, _Pending
from poly_kalshi_service.ledger import Ledger, Position
from poly_kalshi_service.metrics import Metrics
from poly_kalshi_service.recordings import iter_files, read_lines
from poly_kalshi_service.risk import RiskLimits, RiskState
from poly_kalshi_service.shadow import Shadow
from poly_kalshi_service.trades import JsonlDaily, read_journal
from poly_kalshi_service.venue import PaperVenue
from research.replay import ALL_ASSETS, shift_lines

__all__ = ["ReverseEngine", "kalshi_fee", "kalshi_no_asks", "reverse_decide"]

KALSHI_FEE_RATE = 0.07  # same shape as Polymarket; confirm the 15M schedule


def kalshi_fee(fills: list[tuple[float, float]], rate: float = KALSHI_FEE_RATE) -> float:
    """Kalshi taker fee for one order: sum per level, rounded UP to the cent."""
    raw = sum(shares * rate * price * (1.0 - price) for price, shares in fills)
    if raw <= 0:
        return 0.0
    return math.ceil(raw * 100 - 1e-9) / 100


def kalshi_no_asks(book: Book, limit: float) -> list[tuple[float, float]]:
    """NO ask levels at or below ``limit``, cheapest first.

    Kalshi publishes YES bids; a YES bid at ``p`` is a NO ask at ``1 - p``.
    The cheapest NO ask is the highest YES bid.
    """
    return sorted(((1.0 - p), s) for p, s in book.bids.items() if (1.0 - p) <= limit + 1e-9)


def reverse_decide(
    kalshi_yes: Any,
    poly_yes: Any,
    params: StrategyParams,
    sec_into: float,
    already_traded: bool,
) -> Decision:
    """Mirror of ``decision.decide``: Polymarket is the reference, Kalshi executes."""
    if already_traded:
        return Decision(Action.NONE, "traded")
    if sec_into < params.entry_early_s:
        return Decision(Action.NONE, "too_early")
    if sec_into > WINDOW_S - params.entry_late_s:
        return Decision(Action.NONE, "too_late")
    if not kalshi_yes.valid or not poly_yes.valid:
        return Decision(Action.NONE, "invalid_leg")

    k_mid, p_mid = kalshi_yes.mid, poly_yes.mid
    if k_mid is None or p_mid is None:
        return Decision(Action.NONE, "invalid_leg")
    gap = p_mid - k_mid
    if abs(gap) < params.gap_threshold:
        return Decision(Action.NONE, "below_threshold", gap)
    if params.max_kalshi_spread is not None:
        spread = (kalshi_yes.ask or 0.0) - (kalshi_yes.bid or 0.0)
        if spread > params.max_kalshi_spread + 1e-9:
            return Decision(Action.NONE, "kalshi_spread", gap)
    if params.max_poly_spread is not None:
        spread = (poly_yes.ask or 0.0) - (poly_yes.bid or 0.0)
        if spread > params.max_poly_spread + 1e-9:
            return Decision(Action.NONE, "poly_spread", gap)

    buy_yes = gap > 0
    if buy_yes:
        entry_px = kalshi_yes.ask
    else:
        entry_px = None if kalshi_yes.bid is None else 1.0 - kalshi_yes.bid
    if entry_px is None or entry_px <= 0 or entry_px >= 1:
        return Decision(Action.NONE, "bad_price", gap)
    if entry_px > params.max_entry_px:
        return Decision(Action.SKIP, "above_cap", gap, buy_yes, entry_px)
    if params.min_entry_px is not None and entry_px < params.min_entry_px - 1e-9:
        return Decision(Action.SKIP, "below_floor", gap, buy_yes, entry_px)

    contracts = math.floor(params.notional_usd / entry_px)
    if contracts < 1:
        return Decision(Action.NONE, "too_small", gap)
    return Decision(Action.ENTER, "", gap, buy_yes, entry_px, contracts)


class ReverseEngine(Engine):
    """The live engine with the execution venue flipped to Kalshi."""

    def _k_depth(self, kalshi_ticker: str, buy_yes: bool, limit: float) -> float:
        book = self.books.get(kalshi_ticker)
        if book is None or book.snapshot_ns is None:
            return 0.0
        if buy_yes:
            return sum(size for _p, size in book.asks_upto(limit))
        return sum(size for _p, size in kalshi_no_asks(book, limit))

    def _evaluate(self, asset: str, recv_ns: int) -> None:
        st = self.state[asset]
        if st.window is None:
            return
        quotes = self.quotes(asset)
        if quotes is None:
            return
        k, y, _n = quotes
        window = st.window
        sec_into = recv_ns / NS - window.open_unix
        if self.observe:
            self._track_gap(st, k, y, sec_into)
        if st.traded:
            return
        decision = reverse_decide(k, y, self.params, sec_into, st.traded)
        st.evals += 1
        if self.observe:
            st.reasons[decision.reason or decision.action.value] += 1
            if decision.reason == "invalid_leg":
                self._count_invalid_legs(st.window)
        if decision.gap is not None:
            st.last_gap = decision.gap
            st.max_abs_gap = max(st.max_abs_gap, abs(decision.gap))
        if not decision.consumes_window:
            return
        st.traded = True
        if decision.action is Action.ENTER and self.size_to_depth:
            assert decision.entry_px is not None
            offered = self._k_depth(
                window.kalshi_ticker,
                bool(decision.buy_yes),
                self.limit_px(decision.entry_px),
            )
            capped = max(1, min(decision.contracts, math.floor(offered)))
            if capped < decision.contracts:
                decision = replace(decision, contracts=capped)
        ages = {
            leg: None
            if b is None or b.last_update_ns is None
            else round((recv_ns - b.last_update_ns) / 1e6, 1)
            for leg, b in zip(
                ("kalshi", "poly_yes", "poly_no"),
                (self.books.get(i) for i in st.window.ids),
                strict=True,
            )
        }
        if ages["kalshi"] is not None:
            self.metrics.add("age_ms_kalshi", ages["kalshi"])
        record = {
            "type": "decision",
            "direction": "reverse",
            "venue": "kalshi",
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
            "age_ms": ages,
            "depth_at_limit": None
            if decision.entry_px is None
            else round(
                self._k_depth(window.kalshi_ticker, bool(decision.buy_yes), decision.entry_px),
                2,
            ),
            "params": {
                "gap_threshold": self.params.gap_threshold,
                "max_entry_px": self.params.max_entry_px,
                "notional_usd": self.params.notional_usd,
            },
        }
        if decision.action is Action.ENTER:
            record["blocked"] = self._submit_reverse(window, decision, recv_ns)
        st.decision = record
        self._write(recv_ns, record)

    def _submit_reverse(self, window: Any, decision: Decision, recv_ns: int) -> str | None:
        if self.venue is None:
            return "mode_off"
        assert decision.entry_px is not None
        est = decision.contracts * decision.entry_px + kalshi_fee(
            [(decision.entry_px, float(decision.contracts))]
        )
        blocked = self.risk.check(self.ledger, est, recv_ns)
        if blocked is not None:
            self.counters[f"blocked_{blocked}"] += 1
            return blocked
        side = "yes" if decision.buy_yes else "no"
        self.pending.append(
            _Pending(
                recv_ns + self.venue.latency_ns,
                window,
                window.kalshi_ticker,
                side,
                decision,
                recv_ns,
            )
        )
        return None

    def _fill(self, order: _Pending, recv_ns: int) -> None:
        assert self.venue is not None and order.decision.entry_px is not None
        decision, window = order.decision, order.window
        book = self.books.get(order.token) or Book()
        limit = self.limit_px(decision.entry_px)
        buy_yes = order.side == "yes"
        if book.snapshot_ns is not None:
            levels = book.asks_upto(limit) if buy_yes else kalshi_no_asks(book, limit)
            best = (
                book.best_ask()
                if buy_yes
                else (None if book.best_bid() is None else round(1.0 - book.best_bid(), 4))
            )
        else:
            levels, best = [], None
        remaining = float(decision.contracts)
        cost = 0.0
        taken: list[tuple[float, float]] = []
        n_levels = 0
        for price, size in levels:
            if remaining <= 0:
                break
            take = min(size, remaining)
            cost += take * price
            taken.append((price, take))
            remaining -= take
            n_levels += 1
        filled = decision.contracts - remaining
        fee = kalshi_fee(taken)
        touch_fee = kalshi_fee([(decision.entry_px, float(decision.contracts))])
        record = {
            "type": "fill",
            "direction": "reverse",
            "venue": "kalshi",
            "asset": window.asset,
            "open_unix": window.open_unix,
            "t_ns": recv_ns,
            "latency_ms": round((recv_ns - order.decision_ns) / 1e6, 1),
            "side": order.side,
            "limit_px": limit,
            "ordered": decision.contracts,
            "shares": filled,
            "vwap": None if not filled else round(cost / filled, 4),
            "cost": round(cost, 4),
            "fee": round(fee, 4),
            "levels": n_levels,
            "best_ask_at_fill": best,
            "touch_px": decision.entry_px,
            "touch_shares": decision.contracts,
            "touch_fee": round(touch_fee, 4),
            "depth_at_limit": round(sum(s for _, s in levels), 2),
        }
        self.counters["fills" if filled > 0 else "no_fills"] += 1
        if filled > 0:
            self.ledger.buy(
                Position(
                    asset=window.asset,
                    open_unix=window.open_unix,
                    side=order.side,
                    shares=filled,
                    cost=cost,
                    fee=fee,
                    entry_ns=recv_ns,
                )
            )
            known = self.resolutions.get((window.asset, window.open_unix), {}).get("kalshi")
            if known is not None:
                self._settle(window.asset, window.open_unix, known, recv_ns)
        self._write(recv_ns, record)

    def _add_markout(self, *args: Any, **kwargs: Any) -> None:
        return  # v1: markouts would read the Poly leg; disabled for the reverse run

    def _resolution(
        self, asset: str, open_unix: int, venue: str, outcome: str, recv_ns: int
    ) -> None:
        if outcome not in ("yes", "no"):
            return
        seen = self.resolutions.setdefault((asset, open_unix), {})
        if venue in seen:
            return
        seen[venue] = outcome
        if self.observe:
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
        if venue == "kalshi" and self.trade:
            self._settle(asset, open_unix, outcome, recv_ns)


def summarize(journal_dir: Path) -> dict[str, Any]:
    records = list(read_journal(journal_dir))
    decisions = [r for r in records if r.get("type") == "decision"]
    fills = [r for r in records if r.get("type") == "fill"]
    settles = [r for r in records if r.get("type") == "settle"]
    resolutions: dict[tuple[str, int], dict[str, str]] = {}
    for r in records:
        if r.get("type") == "resolution":
            resolutions.setdefault((r["asset"], r["open_unix"]), {})[r["venue"]] = r["outcome"]
    filled = [f for f in fills if f["shares"] > 0]
    wins = [s for s in settles if s["side"] == s["outcome"]]
    pnl = round(sum(s["pnl"] for s in settles), 2)
    fee_paid = round(sum(f["fee"] for f in filled), 2)
    by_asset: dict[str, Any] = {}
    for a in sorted({s["asset"] for s in settles} | {f["asset"] for f in filled}):
        sa = [s for s in settles if s["asset"] == a]
        fa = [f for f in filled if f["asset"] == a]
        nets = [
            100
            * (
                s["payout"]
                - s["vwap"] * s["shares"]
                - next((f["fee"] for f in fa if f["open_unix"] == s["open_unix"]), 0.0)
            )
            / s["shares"]
            for s in sa
            if s["shares"]
        ]
        by_asset[a] = {
            "fills": len(fa),
            "settled": len(sa),
            "wins": sum(1 for s in sa if s["side"] == s["outcome"]),
            "pnl_usd": round(sum(s["pnl"] for s in sa), 2),
            "net_cents_per_share": round(sum(nets) / len(nets), 2) if nets else None,
        }
    traded_keys = {(s["asset"], s["open_unix"]) for s in settles}
    disagree = sum(
        1
        for k in traded_keys
        if k in resolutions
        and resolutions[k].get("poly") is not None
        and resolutions[k].get("kalshi") is not None
        and resolutions[k]["poly"] != resolutions[k]["kalshi"]
    )
    return {
        "decisions": len(decisions),
        "enters": sum(1 for d in decisions if d["action"] == "enter"),
        "skips": sum(1 for d in decisions if d["action"] == "skip"),
        "fills": len(filled),
        "no_fills": len(fills) - len(filled),
        "fill_rate": round(len(filled) / len(fills), 4) if fills else None,
        "settled": len(settles),
        "open_unsettled": len(filled) - len(settles),
        "wins": len(wins),
        "win_rate": round(len(wins) / len(settles), 4) if settles else None,
        "pnl_usd": pnl,
        "fees_usd": fee_paid,
        "venue_disagreement_on_settled": disagree,
        "by_asset": by_asset,
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="research.reverse_replay")
    parser.add_argument("--record-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--start", help="YYYYMMDD[_HH]")
    parser.add_argument("--end", help="YYYYMMDD[_HH]")
    parser.add_argument("--assets", default=ALL_ASSETS)
    parser.add_argument("--gap", type=float, default=0.15)
    parser.add_argument("--cap", type=float, default=0.60)
    parser.add_argument("--notional", type=float, default=50.0)
    parser.add_argument("--early", type=int, default=20)
    parser.add_argument("--late", type=int, default=60)
    parser.add_argument("--latency-ms", type=int, default=47)
    parser.add_argument("--limit-offset", type=float, default=0.02)
    parser.add_argument("--shift-ms", default="", help="per-venue delay, e.g. p=37,k=0")
    args = parser.parse_args()

    files = iter_files(args.record_dir, args.start, args.end)
    if not files:
        raise SystemExit(f"no recordings under {args.record_dir}")
    stats: dict[str, int] = {}
    params = StrategyParams(args.gap, args.cap, args.notional, args.early, args.late)
    assets = tuple(a.strip().upper() for a in args.assets.split(",") if a.strip())
    args.out.mkdir(parents=True, exist_ok=True)
    shift = {
        venue.strip(): float(ms)
        for venue, _, ms in (p.partition("=") for p in args.shift_ms.split(",") if p)
    }
    lines = read_lines(files, stats)
    if any(shift.values()):
        lines = shift_lines(lines, shift, stats)

    engine = ReverseEngine(
        params,
        assets,
        ledger=Ledger(start_cash=1e9, cash=1e9),
        risk=RiskState(RiskLimits(max_open_notional_usd=1e12, daily_loss_limit_usd=1e12)),
        venue=PaperVenue(args.latency_ms),
        journal=JsonlDaily(args.out / "journal"),
        shadow=Shadow((), params),
        limit_offset=args.limit_offset,
        role="full",
    )
    engine.metrics = Metrics(window=2_000_000)

    print(
        f"reverse replay: {len(files)} files gap={args.gap} cap={args.cap} "
        f"latency={args.latency_ms}ms offset={args.limit_offset} shift={shift or 'none'}",
        flush=True,
    )
    t0 = time.time()
    n = 0
    for recv_ns, venue, text in lines:
        engine.handle_line(recv_ns, venue, text)
        n += 1
        if n % 2_000_000 == 0:
            dt = time.time() - t0
            c = engine.counters
            print(
                f"  {n / 1e6:.0f}M lines {dt / 60:.1f}m "
                f"decisions={c.get('fills', 0) + c.get('no_fills', 0)} "
                f"fills={c.get('fills', 0)} settled={c.get('settled', 0)}",
                flush=True,
            )
    engine.journal.close()
    dt = time.time() - t0
    print(f"done: {n} lines in {dt / 60:.1f}m", flush=True)

    summary = {
        "files": len(files),
        "lines": n,
        "elapsed_s": round(dt, 1),
        "reader": stats,
        "counters": dict(engine.counters),
        "shift_ms": shift,
        "feed_ms_k": engine.metrics.snapshot().get("feed_ms_k") or {},
        "feed_ms_p": engine.metrics.snapshot().get("feed_ms_p") or {},
        "params": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
        **summarize(args.out / "journal"),
    }
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str), flush=True)


if __name__ == "__main__":
    sys.exit(main())
