"""Replay recordings through the live code path into feature rows.

    python -m research.replay features --profile dev --date 2026-10-02 [--model v1]
    python -m research.replay compare  --profile dev --hour 2026-10-02T13 [--model v1]
    python -m research.replay shadow   --profile dev --date 2026-10-02 --model v1

Nothing here re-implements a feature or a book rule. Every recorded line goes
through ``FuturesStreams.replay_line`` (the live consumer's ``_process``: depth
sync, snapshots, checkpoints, resets) and every resulting event through
``ScalperRuntime.handle`` (grid clock, state engine, burst samples). The rows
are exactly the samples the live service produced, provided the recording is
complete; ``compare`` checks that against the live sample log
(``OFI_SAMPLE_LOG_DIR``) value by value. ``--model`` applies that model's
``engine.json`` (PCA weights, microprice table), as the live service does.

``shadow`` also runs the model, the policy, risk and the shadow fill simulator
(the live bridge in shadow mode) on recorded event times, and writes the
would-have trades and signals: deterministic, so two runs give the same trades.

Files of all symbols (and ``_CONTROL``) are merged by local receive time, the
order the live consumer saw them in. Any hour can be a starting point: diff
mode writes a book checkpoint at the top of each hour, partial mode needs none.
Recordings made before checkpoints existed (before 2026-10-02's Phase B1
change) can only be replayed from the session's start, where the REST snapshot
is.
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import heapq
import json
import logging
import math
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ta_plugin_binance_futures.limiter import OrderRateGovernor
from ta_plugin_binance_futures.streams import FuturesStreams

from ofi_scalper_service.alerts import Alerts
from ofi_scalper_service.config import ExecutionMode
from ofi_scalper_service.execution_bridge import DEFAULT_FILTERS, ExecutionBridge
from ofi_scalper_service.model import LoadedModel, load_model
from ofi_scalper_service.regime_gate import RegimeGate
from ofi_scalper_service.risk import HaltReason, RiskLimits, RiskState
from ofi_scalper_service.runtime import ScalperRuntime
from ofi_scalper_service.sample_log import sample_path
from ofi_scalper_service.trades import TradeBook, summarise

__all__ = [
    "FanOutBridge",
    "ReplayConfig",
    "ShadowConfig",
    "Variant",
    "Replayer",
    "compare_samples",
    "hour_files",
    "merged_lines",
    "read_lines",
    "replay_files",
]

# Used only when a recording has no _control@session line (older recordings).
DEFAULT_TICKS = {"BTCUSDT": 0.1, "ETHUSDT": 0.01}
SESSION_PREFIX = '{"stream":"_control@session"'
SAMPLE_KEY = ("symbol", "t_ns", "trigger")
DAY_NS = 86_400 * 1_000_000_000


# --- reading -------------------------------------------------------------------


def read_lines(path: Path) -> Iterator[tuple[int, str]]:
    """``(recv_ns, raw)`` per line; a truncated tail (crash, open hour) just ends."""
    with contextlib.suppress(EOFError), gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            stamp, _, text = line.rstrip("\n").partition(" ")
            if stamp.isdigit() and text:
                yield int(stamp), text


def merged_lines(paths: Iterable[Path]) -> Iterator[tuple[int, str]]:
    """All files in local receive order (ties keep file order, which is write order)."""
    return heapq.merge(*(read_lines(path) for path in paths), key=lambda row: row[0])


def hour_files(record_dir: Path, start: datetime, hours: int) -> list[Path]:
    """Every symbol's file (and _CONTROL's) for ``hours`` UTC hours from ``start``."""
    files: list[Path] = []
    for offset in range(hours):
        stamp = start + timedelta(hours=offset)
        day, hour = stamp.strftime("%Y%m%d"), stamp.strftime("%H")
        files += sorted(record_dir.glob(f"*/{day}/*_{day}_{hour}.gz"))
    return files


def find_session(record_dir: Path, until: datetime) -> dict[str, Any] | None:
    """The last ``_control@session`` recorded before ``until``, if any.

    The service writes it once at start, possibly days before the replayed
    hour, so every earlier ``_CONTROL`` file is searched (they are tiny).
    """
    limit = f"_CONTROL_{until.strftime('%Y%m%d_%H')}.gz"
    found: dict[str, Any] | None = None
    candidates = sorted(record_dir.glob("_CONTROL/*/_CONTROL_*.gz"), key=lambda p: p.name)
    for path in candidates:
        if path.name > limit:
            break
        for _, text in read_lines(path):
            if text.startswith(SESSION_PREFIX):
                found = json.loads(text)["data"]
    return found


# --- replay --------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayConfig:
    symbols: tuple[str, ...]
    tick_sizes: dict[str, float]
    book_mode: str = "diff"
    grid_ms: int = 100
    burst_trades: int = 20
    stale_book_ms: int = 750
    partial_levels: int = 10
    partial_speed: str = "100ms"

    @classmethod
    def from_session(cls, session: dict[str, Any] | None, **overrides: Any) -> ReplayConfig:
        if session is None:
            ticks = dict(DEFAULT_TICKS)
            values: dict[str, Any] = {"symbols": tuple(ticks), "tick_sizes": ticks}
        else:
            values = {
                "symbols": tuple(session["symbols"]),
                "tick_sizes": {s: float(t or 0.01) for s, t in session["tick_sizes"].items()},
                "book_mode": session.get("book_mode", "diff"),
            }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)


@dataclass
class Variant:
    """One policy run by the shadow bridge during a replay (a backtest arm).

    ``model`` needs ``version``, ``policy`` and ``scorer`` (a ``LoadedModel``, or
    research's model over precomputed scores). Each variant has its own risk
    state, positions and records; all share the replay's books and pauses.
    """

    name: str
    model: Any
    queue_model: str = "pessimistic"
    delay_ns: int = 0
    book: TradeBook = field(default_factory=lambda: TradeBook(None, keep=10_000_000))
    halts: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ShadowConfig:
    """What a shadow replay needs beyond features: sizing, fees, risk and the gate."""

    limits: RiskLimits
    gate: dict[str, Any]
    order_notional_usd: float = 180.0
    maker_bp: float = 2.0
    taker_bp: float = 5.0
    shadow_equity_usd: float = 500.0
    book: TradeBook = field(default_factory=lambda: TradeBook(None, keep=1_000_000))
    # Several policies in one pass (the book work dominates; each bridge is cheap).
    # None: one variant from the replayer's model, recording into ``book``.
    variants: list[Variant] | None = None
    # Stands in for the operator who acknowledges a daily-loss halt next day.
    auto_ack_daily_loss: bool = True


class FanOutBridge:
    """The runtime's single bridge slot, feeding every variant's bridge."""

    def __init__(self, bridges: list[ExecutionBridge]) -> None:
        self.bridges = bridges

    def on_sample(self, sample: dict[str, Any], gate: dict[str, Any] | None) -> None:
        for bridge in self.bridges:
            bridge.on_sample(sample, gate)

    def on_trade(self, *args: Any) -> None:
        for bridge in self.bridges:
            bridge.on_trade(*args)

    def on_book(self, symbol: str, book: Any) -> None:
        for bridge in self.bridges:
            bridge.on_book(symbol, book)

    def advance(self, t_ns: int) -> None:
        for bridge in self.bridges:
            bridge.advance(t_ns)

    def on_halt(self, reason: str, t_ns: int | None = None) -> None:
        for bridge in self.bridges:
            bridge.on_halt(reason, t_ns)

    def flush_pending(self) -> None:
        for bridge in self.bridges:
            bridge.flush_pending()

    def status(self) -> dict[str, Any]:
        return {"variants": len(self.bridges)}


@dataclass
class Replayer:
    """The live consumer, minus the network: streams' book logic + the runtime."""

    config: ReplayConfig
    samples: list[dict[str, Any]] = field(default_factory=list)
    sessions: int = 0
    model: LoadedModel | None = None
    shadow: ShadowConfig | None = None
    now_ns: int = 0  # event time of the line being fed: the backtest's clock

    def __post_init__(self) -> None:
        self._build()

    def _build(self) -> None:
        """Fresh books, engine, grid and history: what a process start has."""
        cfg = self.config
        settings = SimpleNamespace(
            binance_futures_symbols=cfg.symbols,
            binance_futures_book_mode=cfg.book_mode,
            binance_futures_partial_levels=cfg.partial_levels,
            binance_futures_partial_speed=cfg.partial_speed,
            binance_futures_depth_speed="0ms",
            binance_futures_book_ticker=None,
            binance_futures_agg_trades=True,
            binance_futures_ws_url="wss://replay.invalid",
            binance_futures_depth_snapshot_limit=1000,
            binance_futures_reconnect_max_backoff_seconds=1.0,
            grid_ms=cfg.grid_ms,
            burst_trades=cfg.burst_trades,
            stale_book_ms=cfg.stale_book_ms,
            execution_mode=ExecutionMode.OFF,
            kill_file_path=Path("/nonexistent/ofi-replay-kill"),
            heartbeat_seconds=0,
        )
        # No network: a stand-in REST object (only readiness() reads it) and no
        # on_raw, so nothing is re-recorded. start() is never called.
        rest = SimpleNamespace(limiter=SimpleNamespace(used=0))
        self.streams = FuturesStreams(settings, rest=rest)  # type: ignore[arg-type]
        if self.shadow is None:
            limits = RiskLimits(1.0, 1.0, 1.0, 50.0, 1.0, cfg.stale_book_ms, 0.5)
            gate = RegimeGate(
                funding_blackout_minutes=0, spread_pctl=None, vol_1m_bp=None, liq_burst_usd=None
            )
        else:
            limits = self.shadow.limits
            gate = RegimeGate(**self.shadow.gate)
        risk = RiskState(limits, clock=self._event_time)
        alerts = Alerts(None)
        self.runtime = ScalperRuntime(
            settings,
            streams=self.streams,
            account=None,
            risk=risk,
            gate=gate,
            alerts=alerts,
            model=self.model,
        )
        self.runtime.init_engine(dict(cfg.tick_sizes))
        self.runtime.on_sample = self.samples.append
        self._variant_risks: list[tuple[Variant, RiskState]] = []
        if self.shadow is not None and (self.model is not None or self.shadow.variants):
            shadow = self.shadow
            bridge_settings = SimpleNamespace(
                execution_mode=ExecutionMode.SHADOW,
                binance_futures_symbols=cfg.symbols,
                order_notional_usd=shadow.order_notional_usd,
                shadow_equity_usd=shadow.shadow_equity_usd,
                max_consecutive_unknown=3,
                fill_poll_ms=200,
                dead_man_ms=15_000,
                execution_account="shadow",
            )
            variants = shadow.variants or [Variant("default", self.model, book=shadow.book)]
            bridges = []
            for variant in variants:
                variant_risk = RiskState(
                    limits,
                    clock=self._event_time,
                    governor=OrderRateGovernor(
                        limits.order_rate_fraction, clock=lambda: self.now_ns / 1e9
                    ),
                )
                # Pauses (stale book, resync, stream) are the replay's, shared by all.
                variant_risk.pauses = risk.pauses
                self._variant_risks.append((variant, variant_risk))
                bridges.append(
                    ExecutionBridge(
                        bridge_settings,
                        risk=variant_risk,
                        alerts=alerts,
                        model=variant.model,
                        filters={
                            s: DEFAULT_FILTERS[s] for s in cfg.symbols if s in DEFAULT_FILTERS
                        },
                        fees=lambda _symbol: (shadow.maker_bp, shadow.taker_bp),
                        book=variant.book,
                        queue_model=variant.queue_model,
                        delay_ns=variant.delay_ns,
                    )
                )
            self.runtime.bridge = bridges[0] if len(bridges) == 1 else FanOutBridge(bridges)

    def _event_time(self) -> datetime:
        return datetime.fromtimestamp(self.now_ns / 1e9, UTC)

    def _collect_halts(self) -> None:
        for variant, risk in self._variant_risks:
            for event in risk.history:
                if event["event"] == "halt" and event not in variant.halts:
                    variant.halts.append(event)

    def _roll_days(self) -> None:
        """A new UTC day: the stand-in operator acks yesterday's daily-loss halts."""
        if self.shadow is None or not self.shadow.auto_ack_daily_loss:
            return
        today = self._event_time().date()
        for _, risk in self._variant_risks:
            halt = risk.halt
            if halt is not None and halt.reason is HaltReason.DAILY_LOSS and halt.at.date() < today:
                risk.ack("backtest")

    def feed(self, recv_ns: int, text: str) -> None:
        previous_day = self.now_ns // DAY_NS
        self.now_ns = recv_ns
        if self._variant_risks and recv_ns // DAY_NS != previous_day:
            self._roll_days()
        if text.startswith(SESSION_PREFIX):
            # The service (re)started here: everything it held was gone, so the
            # replay drops its state too, keeping the counters it reports.
            verified, mismatches = (
                self.streams.checkpoints_verified,
                self.streams.checkpoint_mismatches,
            )
            if self.runtime.bridge is not None:
                # The live process lost its cycles at this restart too.
                self.runtime.bridge.flush_pending()
                self._collect_halts()
            session = json.loads(text)["data"]
            self.config = ReplayConfig.from_session(
                session,
                grid_ms=self.config.grid_ms,
                burst_trades=self.config.burst_trades,
                stale_book_ms=self.config.stale_book_ms,
            )
            self._build()
            self.runtime.grid.due(recv_ns)  # the live process anchored its grid here
            self.streams.checkpoints_verified = verified
            self.streams.checkpoint_mismatches = mismatches
            self.sessions += 1
            return
        for event in self.streams.replay_line(recv_ns, text):
            self.runtime.handle(event)

    def run(self, lines: Iterable[tuple[int, str]]) -> Iterator[dict[str, Any]]:
        """Feed every line; yield samples as they are produced."""
        for recv_ns, text in lines:
            self.feed(recv_ns, text)
            if self.samples:
                yield from self.samples
                self.samples.clear()
        if self.runtime.bridge is not None:
            self.runtime.bridge.flush_pending()
            self._collect_halts()

    @property
    def checkpoint_report(self) -> dict[str, int]:
        return {
            "verified": self.streams.checkpoints_verified,
            "mismatches": self.streams.checkpoint_mismatches,
            "sessions": self.sessions,
        }


def replay_files(
    paths: list[Path],
    *,
    session: dict[str, Any] | None = None,
    keep: Callable[[dict[str, Any]], bool] | None = None,
    model: LoadedModel | None = None,
    shadow: ShadowConfig | None = None,
    **overrides: Any,
) -> tuple[list[dict[str, Any]], Replayer]:
    replayer = Replayer(ReplayConfig.from_session(session, **overrides), model=model, shadow=shadow)
    rows = [row for row in replayer.run(merged_lines(paths)) if keep is None or keep(row)]
    return rows, replayer


# --- parity --------------------------------------------------------------------


# Windowed sums are prefix-sum differences, re-based on prune; live and a replay
# started elsewhere prune at different moments, so the last bits can differ.
REL_TOL = 1e-9
ABS_TOL = 1e-9


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) or math.isnan(b):
            return math.isnan(a) and math.isnan(b)
        return math.isclose(a, b, rel_tol=REL_TOL, abs_tol=ABS_TOL)
    return a == b


def compare_samples(
    live: Iterable[dict[str, Any]], replayed: Iterable[dict[str, Any]], *, examples: int = 5
) -> dict[str, Any]:
    """Match samples by (symbol, t_ns, trigger) and compare every value exactly."""
    live_by = {tuple(s[k] for k in SAMPLE_KEY): s for s in live}
    replay_by = {tuple(s[k] for k in SAMPLE_KEY): s for s in replayed}
    common = sorted(live_by.keys() & replay_by.keys())
    mismatched: list[dict[str, Any]] = []
    worst: dict[str, float] = {}
    for key in common:
        a, b = live_by[key], replay_by[key]
        diffs = {
            name: (a.get(name), b.get(name))
            for name in a.keys() | b.keys()
            if not _same(a.get(name), b.get(name))
        }
        if diffs:
            mismatched.append({"key": list(key), "diffs": diffs})
            for name, (x, y) in diffs.items():
                if isinstance(x, int | float) and isinstance(y, int | float):
                    worst[name] = max(worst.get(name, 0.0), abs(float(x) - float(y)))
    return {
        "live": len(live_by),
        "replayed": len(replay_by),
        "matched": len(common),
        "only_live": len(live_by.keys() - replay_by.keys()),
        "only_replayed": len(replay_by.keys() - live_by.keys()),
        "mismatched": len(mismatched),
        "max_abs_diff": dict(sorted(worst.items(), key=lambda kv: -kv[1])[:10]),
        "examples": mismatched[:examples],
        "ok": not mismatched and len(common) > 0,
    }


def load_samples(path: Path) -> list[dict[str, Any]]:
    """Every complete row; a file still being written (or cut by a crash) keeps
    the rows before the cut instead of losing them all."""
    rows: list[dict[str, Any]] = []
    with contextlib.suppress(EOFError), gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.endswith("\n"):
                rows.append(json.loads(line))
    return rows


# --- CLI -----------------------------------------------------------------------


def _write_rows(rows: list[dict[str, Any]], out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        path = out.with_suffix(".jsonl.gz")
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, separators=(",", ":")) + "\n")
        return path
    pq.write_table(pa.Table.from_pylist(rows), out)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay recordings into feature rows")
    parser.add_argument("command", choices=("features", "compare", "shadow"))
    parser.add_argument("--profile", default=None)
    parser.add_argument("--date", type=date.fromisoformat, help="features: one UTC day")
    parser.add_argument("--hour", help="compare: UTC hour, e.g. 2026-10-02T13")
    parser.add_argument("--warmup-hours", type=int, default=1)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--model", default=None, help="a version under OFI_MODEL_DIR")
    args = parser.parse_args(argv)

    from ofi_scalper_service.config import load_settings

    settings = load_settings(args.profile)
    record_dir = settings.record_dir
    overrides = {
        "grid_ms": settings.grid_ms,
        "burst_trades": settings.burst_trades,
        "stale_book_ms": settings.stale_book_ms,
    }

    def session_for(until: datetime) -> dict[str, Any] | None:
        session = find_session(record_dir, until)
        if session is None:
            # Recorded before session lines existed: trust the profile's mode.
            overrides["book_mode"] = settings.binance_futures_book_mode
            overrides["partial_levels"] = settings.binance_futures_partial_levels
            overrides["partial_speed"] = settings.binance_futures_partial_speed
        return session

    logging.disable(logging.WARNING)  # replay re-walks stale/gap events; keep stdout clean
    # Research may replay a model that has not passed (yet); nothing here trades.
    model = load_model(settings.model_dir, args.model, require_pass=False) if args.model else None

    if args.command == "shadow":
        if model is None:
            parser.error("shadow needs --model")
        day = args.date or (datetime.now(UTC).date() - timedelta(days=1))
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        end_ns = int((start + timedelta(days=1)).timestamp() * 1e9)
        out = args.out or Path(
            f"research/data/shadow/{settings.profile or 'default'}/{model.version}"
        )
        book = TradeBook(out, keep=1_000_000)
        shadow = ShadowConfig(
            limits=RiskLimits.from_settings(settings),
            gate={
                "funding_blackout_minutes": settings.funding_blackout_minutes,
                "spread_pctl": settings.gate_spread_pctl,
                "vol_1m_bp": settings.gate_vol_1m_bp,
                "liq_burst_usd": settings.gate_liq_burst_usd,
            },
            order_notional_usd=settings.order_notional_usd,
            maker_bp=settings.maker_fee_bp if settings.maker_fee_bp is not None else 2.0,
            taker_bp=settings.taker_fee_bp if settings.taker_fee_bp is not None else 5.0,
            shadow_equity_usd=settings.shadow_equity_usd,
            book=book,
        )
        paths = hour_files(record_dir, start, 24)
        for _ in replay_files(
            paths,
            session=session_for(start + timedelta(hours=23)),
            keep=lambda r: r["t_ns"] < end_ns,
            model=model,
            shadow=shadow,
            **overrides,
        )[0]:
            pass
        trades = list(book.recent_trades)
        report = summarise(trades, list(book.recent_signals))
        report.update(date=day.isoformat(), model=model.version, files=len(paths), out=str(out))
        print(json.dumps(report, indent=2, default=str))
        return 0

    if args.command == "features":
        day = args.date or (datetime.now(UTC).date() - timedelta(days=1))
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        paths = hour_files(record_dir, start, 24)
        end_ns = int((start + timedelta(days=1)).timestamp() * 1e9)
        rows, replayer = replay_files(
            paths,
            session=session_for(start + timedelta(hours=23)),
            keep=lambda r: r["t_ns"] < end_ns,
            model=model,
            **overrides,
        )
        out = args.out or Path(f"research/data/features/{settings.profile or 'default'}")
        written = _write_rows(rows, out / f"{day.isoformat()}.parquet")
        print(
            json.dumps(
                {
                    "date": day.isoformat(),
                    "files": len(paths),
                    "rows": len(rows),
                    "written": str(written),
                    "checkpoints": replayer.checkpoint_report,
                }
            )
        )
        return 0 if rows else 1

    if not args.hour:
        parser.error("compare needs --hour")
    if settings.sample_log_dir is None:
        parser.error("compare needs OFI_SAMPLE_LOG_DIR: the live sample log to compare against")
    hour = datetime.fromisoformat(args.hour).replace(tzinfo=UTC, minute=0, second=0)
    start_ns, end_ns = (int(t.timestamp() * 1e9) for t in (hour, hour + timedelta(hours=1)))
    paths = hour_files(record_dir, hour - timedelta(hours=args.warmup_hours), args.warmup_hours + 1)
    replayed, replayer = replay_files(
        paths,
        session=session_for(hour),
        keep=lambda r: start_ns <= r["t_ns"] < end_ns,
        model=model,
        **overrides,
    )
    live = [
        s
        for s in load_samples(sample_path(settings.sample_log_dir, start_ns))
        if start_ns <= s["t_ns"] < end_ns
    ]
    # Replay can only sample between its first and last recorded line: before the
    # first nothing has started the grid, after the last nothing drives it.
    first_ns = min((r["t_ns"] for r in replayed), default=0)
    last_ns = max((r["t_ns"] for r in replayed), default=0)
    report = compare_samples([s for s in live if first_ns <= s["t_ns"] <= last_ns], replayed)
    report["hour"] = hour.isoformat()
    report["checkpoints"] = replayer.checkpoint_report
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
