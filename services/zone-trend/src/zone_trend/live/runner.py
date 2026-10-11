"""The hourly runner shared by shadow and live mode.

Startup (spec order):

1. Config and secrets; clock check against Binance (live aborts on more than
   ``ZT_MAX_CLOCK_DRIFT_MS`` of drift; signing uses the server offset either way).
2. exchangeInfo filters; in live mode leverage brackets too.
3. Live: one-way mode verified; ISOLATED margin and the configured leverage set
   per symbol (idempotent).
4. Warm-up: every closed 1D/4H/1H bar since the symbol's listing, replayed
   through ``SymbolCore`` exactly as the backtester feeds it (see
   ``fetch_history`` for why a shorter window is not enough).
5. Live: reconcile and repair before trading.
6. Every hour, 5 s after the close: fetch the closed bars, run ``step``.

``step`` is the hour, in the backtest engine's order: settle the hour that just
ended (paper fills, or drain the live order manager), mark equity and update
the breakers, decide on new signals through ``risk.decide``, then manage open
positions with ``core.exits``. Every decision is written to the signals table,
which is what the parity replay compares with the backtest.

Data sanity: a signal is skipped when Binance's mark price is more than
``mark_last_max_diff`` from the hour's close. Bars come from REST at each
close, so there is no stream to go stale; a symbol whose closed bar cannot be
fetched sits the hour out.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from zone_trend.config import Config
from zone_trend.core import exits
from zone_trend.core.indicators import pearson
from zone_trend.core.replay import replay
from zone_trend.core.signals import signal_id
from zone_trend.core.symbol import SymbolCore
from zone_trend.live import ops
from zone_trend.live.settings import STAGES, LiveSettings
from zone_trend.live.venues import LiveVenue, Venue
from zone_trend.persistence.repo import Repo
from zone_trend.risk.decide import DecisionContext, decide
from zone_trend.risk.liquidation import DEFAULT_BRACKET, Bracket
from zone_trend.risk.portfolio import OpenExposure, PortfolioGuard
from zone_trend.risk.sizing import DEFAULT_FILTERS, Filters, Size
from zone_trend.types import TF, Bar, Signal

__all__ = ["Runner", "acquire_lock", "kill"]

log = logging.getLogger(__name__)

H = 3_600_000
DAY = 24 * H
BTC = "BTCUSDT"
KLINE_PAGE = 1500
CLOSE_DELAY_S = 5.0
GUARD_FIELDS = (
    "peak",
    "day_anchor",
    "week_anchor",
    "day",
    "week",
    "paused_until",
    "halted",
    "halved_dd",
    "consec_losses",
    "halve_trades_left",
)


def acquire_lock(path: Path):
    """Single instance: an exclusive flock held for the life of the process."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"another zone-trend instance holds {path}") from None
    fh.write(str(time.time()))
    fh.flush()
    return fh


def _bar(row: list) -> Bar:
    return Bar(
        int(row[0]),
        int(row[6]),
        float(row[1]),
        float(row[2]),
        float(row[3]),
        float(row[4]),
        float(row[5]),
    )


def _frame(rows: list[list]) -> pd.DataFrame:
    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time"]
    df = pd.DataFrame([r[:7] for r in rows], columns=cols)
    for c in cols:
        df[c] = df[c].astype("int64" if c.endswith("time") else float)
    return df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)


class Runner:
    def __init__(
        self,
        cfg: Config,
        settings: LiveSettings,
        trading: Any,
        venue: Venue,
        repo: Repo,
        alerts: Callable,
        symbols: list[str],
        clock_ms: Callable[[], int],
    ) -> None:
        # The runner warms up and steps on hourly bars. Entry == 1h leaves
        # [1d, 4h] as the only valid slow/fast pair, so this one check pins
        # the whole ladder to the default triple.
        if cfg.timeframes.entry != "1h":
            raise SystemExit(
                f"live/shadow step on hourly bars but timeframes.entry is "
                f"{cfg.timeframes.entry}; sub-hourly live stepping is not built yet"
            )
        self.cfg = cfg
        self.settings = settings
        self.t = trading
        self.venue = venue
        self.repo = repo
        self.alerts = alerts
        self.symbols = symbols
        self.rank = {s: i for i, s in enumerate(symbols)}
        self.now = clock_ms
        self.mode = venue.mode
        self.auto_stage = settings.zt_stage == "auto"
        if self.mode != "live":
            self.stage_name = "full"
        elif self.auto_stage:
            self.stage_name = repo.get_state("stage", {"name": "micro"})["name"]
            if repo.get_state("stage") is None:
                repo.set_state("stage", {"name": "micro", "since": clock_ms()})
        else:
            self.stage_name = settings.zt_stage
        self.stage = STAGES[self.stage_name]
        limits = cfg.portfolio
        if "max_positions" in self.stage:
            limits = limits.model_copy(update={"max_positions": self.stage["max_positions"]})
        self.guard = PortfolioGuard(limits, cfg.filters)
        saved = repo.get_state("guard")
        if saved:
            for k in GUARD_FIELDS:
                setattr(self.guard, k, saved.get(k, getattr(self.guard, k)))
        if repo.get_state("halted", False):
            self.guard.halted = True
        self.cores: dict[str, SymbolCore] = {}
        self.filters: dict[str, Filters] = {}
        self.brackets: dict[str, Bracket] = {}
        self.last_t: int = repo.get_state("last_hour", 0) or 0
        self._closed_seen = len(venue.closed_trades())
        self._last_bars: dict[str, Bar] = {}

    # -- startup -------------------------------------------------------------------
    async def startup(self) -> None:
        reply = await self.t.exchange_info()
        if reply.ok:
            for row in reply.body.get("symbols", []):
                f = {x["filterType"]: x for x in row.get("filters", [])}
                self.filters[row["symbol"]] = Filters(
                    float(f.get("PRICE_FILTER", {}).get("tickSize", 0)),
                    float(f.get("LOT_SIZE", {}).get("stepSize", 0)),
                    float(f.get("LOT_SIZE", {}).get("minQty", 0)),
                    float(f.get("MIN_NOTIONAL", {}).get("notional", 5)),
                )
        if self.mode == "live":
            await self._setup_account()
        if isinstance(self.venue, LiveVenue):
            self.venue.oms.filters = self.filters
        await self.warm_up()

    async def _setup_account(self) -> None:
        mode = await self.t.position_mode()
        if not mode.ok or mode.body.get("dualSidePosition"):
            raise SystemExit("position mode must be One-way (dualSidePosition false)")
        for sym in self.symbols:
            for reply in (
                await self.t.set_margin_type(sym, "ISOLATED"),
                await self.t.set_leverage(sym, self.cfg.exchange.leverage),
            ):
                if not reply.ok:
                    raise SystemExit(f"{sym}: account setup failed: {reply.code} {reply.msg}")
        brackets = await self.t.leverage_brackets()
        if brackets.ok:
            for row in brackets.body or []:
                first = (row.get("brackets") or [{}])[0]
                self.brackets[row["symbol"]] = Bracket(
                    float(first.get("maintMarginRatio", DEFAULT_BRACKET.mmr)),
                    float(first.get("cum", 0.0)),
                )

    async def fetch_history(self, sym: str, interval: str, end: int) -> list[list]:
        """Every closed kline from the symbol's first bar on Binance through ``end``.

        The core's state is path-dependent with a long memory (zone ancestry,
        the swing collapse chain): a core warmed from a fixed number of days
        can disagree with one replayed from listing, and longer windows do not
        converge (measured on ETH/BTC 2026-10: 420d 61%, 800d 100%, 1100d 66%
        zone agreement). Replaying from the first bar, exactly as the
        backtest does, makes the live state identical by construction.
        """
        rows: list[list] = []
        cursor = 0
        while True:
            reply = await self.t.klines(sym, interval, KLINE_PAGE, start_time=cursor, end_time=end)
            page = [r for r in (reply.body or []) if int(r[6]) <= end] if reply.ok else []
            if not reply.ok:
                raise RuntimeError(f"{sym} {interval} history: {reply.outcome} {reply.code}")
            if not page:
                return rows
            rows += page
            cursor = int(page[-1][0]) + 1
            if len(page) < KLINE_PAGE:
                return rows

    async def warm_up(self) -> None:
        last_close = (self.now() // H) * H - 1
        for sym in sorted(set(self.symbols) | {BTC}):
            frames = [
                _frame(await self.fetch_history(sym, tf, last_close)) for tf in ("1h", "4h", "1d")
            ]
            if any(f.empty for f in frames):
                log.warning("%s: no history to warm up", sym)
                continue
            core = SymbolCore(sym, self.cfg)
            for _core, _step in replay(sym, *frames, self.cfg, core=core):
                pass
            self.cores[sym] = core
            self._last_bars[sym] = core.series[TF.H1].last
        self.last_t = max(self.last_t, last_close)
        log.info("warmed up %d symbols through %s", len(self.cores), last_close)

    # -- the hour --------------------------------------------------------------------
    async def fetch_closed(self, sym: str, t: int) -> tuple[Bar | None, Bar | None, Bar | None]:
        out: list[Bar | None] = []
        for interval, period in (("1h", H), ("4h", 4 * H), ("1d", DAY)):
            if interval != "1h" and (t + 1) % period:
                out.append(None)
                continue
            reply = await self.t.klines(sym, interval, 3, end_time=t)
            rows = [r for r in (reply.body or []) if int(r[6]) == t] if reply.ok else []
            out.append(_bar(rows[0]) if rows else None)
        return out[0], out[1], out[2]

    async def step(self, t: int, bars: dict[str, tuple[Bar, Bar | None, Bar | None]]) -> dict:
        """One hour. ``bars``: symbol -> (1H, 4H or None, 1D or None), all closing at ``t``."""
        summary: dict[str, Any] = {"t": t, "signals": 0, "accepted": 0}
        await self.venue.before_hour(t + 1 - H)
        closed = self.venue.closed_trades()
        for tr in closed[self._closed_seen :]:
            self.guard.trade_closed(int(tr["exit_time"] or t), float(tr["net"] or 0))
        self._closed_seen = len(closed)

        results = {}
        for sym, (h1, h4, d1) in bars.items():
            core = self.cores.get(sym)
            if core is None or h1 is None:
                continue
            results[sym] = core.on_close(h1, h4, d1)
            self._last_bars[sym] = h1
            for r in results[sym].rejected:
                self.repo.record_signal(
                    _rejected_as_signal(r), "rejected", "core", r.reason, self.mode, self.now()
                )

        marks = {s: b.close for s, b in self._last_bars.items()}
        equity = self.venue.equity(marks)
        was_halted = self.guard.halted
        self.guard.mark(t, equity)
        self.repo.record_equity(t, equity, equity, max(self.guard.peak, equity))
        if self.guard.halted and not was_halted:
            self.repo.set_state("halted", True)
            await self.alerts(
                "critical",
                "drawdown halt: manual review required",
                [
                    f"equity {equity:.2f}, peak {self.guard.peak:.2f}",
                    "no new entries; exchange stops stay; restart with --resume",
                ],
            )

        signals = sorted(
            (s for r in results.values() for s in r.signals),
            key=lambda s: (-s.quality, s.symbol, s.id),
        )
        for sig in signals:
            summary["signals"] += 1
            accepted = await self._decide(sig, t, equity)
            summary["accepted"] += int(accepted)

        await self._manage(t)
        if isinstance(self.venue, LiveVenue):
            self.venue.persist_excursions(self.venue.positions())
        self.last_t = t
        self.repo.set_state("last_hour", t)
        self.repo.set_state("guard", {k: getattr(self.guard, k) for k in GUARD_FIELDS})
        self._equity = equity
        if (t + 1) % DAY == 0:
            await self._daily(t, equity)
        return summary

    async def _daily(self, t: int, equity: float) -> None:
        positions = self.venue.positions()
        open_risk = sum(p.qty_open * abs(p.entry - p.stop) for p in positions.values())
        lines = ops.daily_summary(self.repo, t, equity, max(self.guard.peak, equity), open_risk)
        await self.alerts(
            "warning" if self.guard.halted else "info", f"daily summary ({self.mode})", lines
        )
        day = (t + 1) // DAY
        if (day + 3) % 7 == 0:  # Monday 00:00 UTC
            report = ops.weekly_report(
                self.repo, self.settings.zt_wf_dir, self.cfg, self.rank, self.now()
            )
            await self.alerts("info", "weekly report", report)
        if self.mode == "live":
            gate = ops.stage_gates(self.repo, self.settings.zt_wf_dir, self.rank, self.now())
            if gate.passed and gate.next_stage and self.auto_stage:
                self.repo.set_state("stage", {"name": gate.next_stage, "since": self.now()})
                self.stage_name, self.stage = gate.next_stage, STAGES[gate.next_stage]
                limits = self.cfg.portfolio
                if "max_positions" in self.stage:
                    limits = limits.model_copy(
                        update={"max_positions": self.stage["max_positions"]}
                    )
                self.guard.limits = limits
                await self.alerts("warning", f"stage advanced to {gate.next_stage}", gate.detail)
            elif gate.passed and gate.next_stage:
                await self.alerts(
                    "info", f"gate to {gate.next_stage} passed (ZT_STAGE is fixed)", gate.detail
                )
            elif (day + 3) % 7 == 0 and gate.next_stage:
                await self.alerts("info", f"still at {gate.stage}: gate not met", gate.detail)

    def _k_sym(self, sym: str, entry_type: str) -> float:
        if not self.cfg.sizing.displacement.enabled:
            return 1.0
        rows = self.repo.get_state("k_sym_cache")
        return float((rows or {}).get(f"{sym}|{entry_type}", 1.0))

    def _allowed(self, sym: str) -> bool:
        if "symbols" in self.stage and sym not in self.stage["symbols"]:
            return False
        return not ("top_n" in self.stage and self.rank.get(sym, 99) >= self.stage["top_n"])

    def _corr(self, a: str, b: str, t: int) -> float:
        def returns(sym: str) -> pd.Series:
            core = self.cores.get(sym)
            if core is None:
                return pd.Series(dtype=float)
            bars = [x for x in core.series[TF.D1].bars if x.close_time < t][-31:]
            closes = np.log([x.close for x in bars])
            return pd.Series(np.diff(closes), index=[x.close_time for x in bars[1:]])

        joined = pd.concat([returns(a), returns(b)], axis=1, join="inner").dropna()
        if len(joined) < 10:
            return math.nan
        return pearson(joined.iloc[:, 0], joined.iloc[:, 1])

    async def _funding_8h(self, sym: str, t: int) -> float | None:
        reply = await self.t.funding_history(sym, t - 9 * H, t)
        rows = reply.body if reply.ok else None
        if not rows:
            return None
        rate = float(rows[-1]["fundingRate"])
        if len(rows) >= 2:
            hours = (int(rows[-1]["fundingTime"]) - int(rows[-2]["fundingTime"])) / H
            if 0 < hours < 8:
                return rate * 8 / round(hours)
        return rate

    async def _mark_ok(self, sym: str) -> bool:
        reply = await self.t.premium_index(sym)
        bar = self._last_bars.get(sym)
        if not reply.ok or bar is None:
            return False
        mark = float(reply.body.get("markPrice", 0))
        return mark > 0 and abs(mark - bar.close) / mark <= self.cfg.filters.mark_last_max_diff

    async def _decide(self, sig: Signal, t: int, equity: float) -> bool:
        sym = sig.symbol
        positions = self.venue.positions()
        exposure = [OpenExposure(s, p.side, p.qty_open * p.entry) for s, p in positions.items()]
        exposure += [OpenExposure(s, side, n) for s, (side, n) in self.venue.pending().items()]
        btc = self.cores.get(BTC)
        ctx = DecisionContext(
            t=t,
            equity=equity,
            available=equity - self.venue.margin(),
            rank=self.rank.get(sym) if self._allowed(sym) else None,
            quarantined=False,
            data_flagged=not await self._mark_ok(sym),
            exposure=exposure,
            corr=lambda other: self._corr(sym, other, t),
            funding_8h=await self._funding_8h(sym, t),
            btc_trend=btc.structure[TF.D1].trend if btc is not None else None,
            filters=self.filters.get(sym, DEFAULT_FILTERS),
            tp1_fraction=self.cfg.exits.tp1_fraction,
            bracket=self.brackets.get(sym, DEFAULT_BRACKET),
            stage_mult=self.stage.get("size_mult", 1.0),
            k_sym=self._k_sym(sym, sig.entry_type),
        )
        d = decide(sig, ctx, self.cfg, self.guard)
        if d.accepted and self.stage.get("min_size"):
            d = _with_min_size(d, sig, ctx.filters)
        self.repo.record_signal(
            sig, "accepted" if d.accepted else "rejected", d.stage, d.reason, self.mode, self.now()
        )
        if not d.accepted:
            return False
        self.guard.trade_opened()
        await self.venue.open(sig, d, ctx.k_sym)
        return True

    async def _manage(self, t: int) -> None:
        for sym, pos in self.venue.positions().items():
            core = self.cores.get(sym)
            if core is None or pos.opened_at > t:
                continue
            if isinstance(self.venue, LiveVenue):
                exits.observe(pos, core.series[TF.H1].last)
            else:
                pos.bars_open += 1  # the paper broker tracks excursions per minute
            for act in exits.evaluate(
                pos,
                core.atr(TF.H1),
                decay=core.decay(pos.side),
                choch_against=core.structure[TF.H4].choch_against(pos.side, pos.opened_at),
                fee_taker=self.cfg.exchange.fee_taker,
                cfg=self.cfg.exits,
                bar_scale=core.entry_scale,
            ):
                if isinstance(act, exits.MoveStop):
                    await self.venue.move_stop(sym, act.price)
                else:
                    await self.venue.exit(sym, act.reason)

    # -- forever ----------------------------------------------------------------------
    async def run(self, reconciler=None) -> None:
        tasks = []
        if reconciler is not None:
            tasks.append(asyncio.create_task(self._reconcile_loop(reconciler)))
        tasks.append(asyncio.create_task(self._heartbeat_loop()))
        try:
            while True:
                nxt = self.last_t + H
                wait = (nxt + 1) / 1000 + CLOSE_DELAY_S - self.now() / 1000
                if wait > 0:
                    await asyncio.sleep(wait)
                bars = {}
                for sym in sorted(self.cores):
                    h1, h4, d1 = await self.fetch_closed(sym, nxt)
                    if h1 is None:
                        log.warning("%s: no closed 1H bar for %s; sitting the hour out", sym, nxt)
                        continue
                    bars[sym] = (h1, h4, d1)
                summary = await self.step(nxt, bars)
                log.info("hour %s: %s", nxt, summary)
        finally:
            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _heartbeat_loop(self) -> None:
        limiter = getattr(getattr(self.t, "rest", None), "limiter", None)
        while True:
            try:
                positions = self.venue.positions()
                ops.write_metrics(
                    self.repo,
                    self.now(),
                    equity=getattr(self, "_equity", self.settings.zt_capital),
                    peak=self.guard.peak,
                    open_positions=len(positions),
                    last_hour=self.last_t,
                    halted=int(self.guard.halted),
                    api_weight_used=limiter.used if limiter else 0,
                    stage=self.stage_name,
                    mode=self.mode,
                )
            except Exception:  # noqa: BLE001 - the heartbeat must keep beating
                log.exception("heartbeat failed")
            await asyncio.sleep(60)

    async def _reconcile_loop(self, reconciler) -> None:
        while True:
            try:
                await reconciler.run_once()
            except Exception:  # noqa: BLE001 - the loop must survive one bad pass
                log.exception("reconcile pass failed")
            await asyncio.sleep(self.cfg.live.reconcile_interval_s)


def _with_min_size(d, sig: Signal, f: Filters):
    """Micro stage: the smallest order the exchange accepts, a single take-profit."""
    step = f.step or 1e-8
    floor_notional = max(f.min_notional, f.min_qty * sig.entry_ref) * 1.02
    qty = math.ceil(floor_notional / sig.entry_ref / step) * step
    notional = qty * sig.entry_ref
    risk = notional * abs(sig.entry_ref - sig.stop) / sig.entry_ref
    return type(d)(
        True, d.stage, d.reason, Size(qty, notional, qty, True, risk), d.slip, d.size_mult
    )


def _rejected_as_signal(r) -> Signal:
    return Signal(
        signal_id(r.symbol, r.zone_id, r.entry_type, r.bar_time),
        r.symbol,
        r.side,
        r.entry_type,
        r.bar_time,
        math.nan,
        math.nan,
        math.nan,
        None,
        0,
        r.zone_id,
    )


async def kill(oms, repo: Repo, trading, alerts) -> list[str]:
    """Close every zone-trend position at market, then cancel zt- orders on flat symbols; halt.

    A symbol Binance still shows a position on keeps its stop (the close may
    have failed); the alert says so.
    """
    done = []
    repo.set_state("halted", True)
    for pos in repo.open_positions():
        await oms.close(pos["symbol"], "kill")
        done.append(f"close {pos['symbol']}")
    await oms.drain()
    pos_reply = await trading.positions()
    still_open = (
        {r["symbol"] for r in (pos_reply.body or []) if abs(float(r.get("positionAmt", 0))) > 0}
        if pos_reply.ok
        else None
    )
    reply = await trading.open_algo_orders()
    rows = reply.body if reply.ok and isinstance(reply.body, list) else []
    for a in rows:
        cid = str(a.get("clientAlgoId", ""))
        if not cid.startswith("zt-"):
            continue
        if still_open is None or a.get("symbol") in still_open:
            done.append(f"kept {cid} ({a.get('symbol')} still open or unknown)")
            continue
        await trading.cancel_algo(cid)
        done.append(f"cancel {cid}")
    await alerts("critical", "kill switch", done or ["nothing open"])
    return done
