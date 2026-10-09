"""Phase one of a backtest: replay each symbol's core once and record a tape.

The tape holds what the portfolio engine needs per entry-bar close: the
signals and rejections, plus the inputs to the exit rules (ATR14(entry),
momentum decay per side, the 4H CHoCH events) and the 1D trend for the BTC
gate. Exits are evaluated by the same ``core.exits.evaluate`` the live runner
calls, fed from the tape instead of a live core, so splitting the replay from
the portfolio loop changes nothing about the decisions.

Each tape covers the symbol's whole history up to ``end_ms`` (warm-up
included), so a symbol entering the universe arrives with warm state. Tapes
are cached on disk by config, data snapshot and range, and built in parallel.

The benchmark tape (``ema_benchmark_tape``) is the spec's 1D EMA 20/100 trend
rule: long on a cross up, short on a cross down, stop 2 x ATR14(1D), exit on
the opposite cross; it then goes through the same risk, sizing and costs.
"""

from __future__ import annotations

import hashlib
import json
import math
import pickle
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from zone_trend.config import Config
from zone_trend.core.indicators import atr_series, ema_series
from zone_trend.core.replay import replay
from zone_trend.core.signals import signal_id
from zone_trend.data.store import DataPaths, load
from zone_trend.types import TF, Rejected, Side, Signal, Trend

__all__ = ["Tape", "build_tapes", "ema_benchmark_tape", "zone_tape"]

TAPE_VERSION = 2
_TREND_CODE = {Trend.RANGE: 0, Trend.UP: 1, Trend.DOWN: -1}


@dataclass
class Tape:
    symbol: str
    close_time: np.ndarray  # int64, entry-bar close times
    close: np.ndarray  # entry-bar last-price close (marks between fills)
    atr_entry: np.ndarray
    decay_long: np.ndarray  # bool
    decay_short: np.ndarray
    d1_trend: np.ndarray  # int8: 1 up, -1 down, 0 range
    choch_down: list[int] = field(default_factory=list)  # 4H CHoCH against longs
    choch_up: list[int] = field(default_factory=list)  # against shorts
    signals: dict[int, list[Signal]] = field(default_factory=dict)
    rejected: list[Rejected] = field(default_factory=list)
    force_exit: dict[int, Side] = field(default_factory=dict)  # benchmark: exit this side
    managed: bool = True  # zone trades run the exit rules; the benchmark does not

    def index(self, t: int) -> int | None:
        i = int(np.searchsorted(self.close_time, t))
        return i if i < len(self.close_time) and self.close_time[i] == t else None

    def trend_at(self, t: int) -> Trend | None:
        i = self.index(t)
        if i is None:
            return None
        return {1: Trend.UP, -1: Trend.DOWN, 0: Trend.RANGE}[int(self.d1_trend[i])]

    def choch_against(self, side: Side, since: int, until: int) -> bool:
        times = self.choch_down if side is Side.LONG else self.choch_up
        return any(since <= t <= until for t in times)


def _frames(paths: DataPaths, symbol: str, entry_tf: str, end_ms: int | None):
    frames = tuple(
        load(paths, "klines", symbol, tf, end_ms=end_ms) for tf in (entry_tf, "4h", "1d")
    )
    if any(f.empty for f in frames):
        raise ValueError(
            f"{symbol}: {entry_tf}, 4h and 1d klines are all required; sync them first"
        )
    return frames


def zone_tape(symbol: str, paths: DataPaths, cfg: Config, end_ms: int | None = None) -> Tape:
    entry_tf = cfg.timeframes.entry
    entry, h4, d1 = _frames(paths, symbol, entry_tf, end_ms)
    n = len(entry)
    atr = np.full(n, np.nan)
    dec_l = np.zeros(n, bool)
    dec_s = np.zeros(n, bool)
    d1t = np.zeros(n, np.int8)
    signals: dict[int, list[Signal]] = defaultdict(list)
    rejected: list[Rejected] = []
    core = None
    for i, (core, step) in enumerate(replay(symbol, entry, h4, d1, cfg)):
        atr[i] = core.atr(TF(entry_tf))
        dec_l[i] = core.decay(Side.LONG)
        dec_s[i] = core.decay(Side.SHORT)
        d1t[i] = _TREND_CODE[core.structure[TF.D1].trend]
        for s in step.result.signals:
            signals[s.bar_time].append(s)
        rejected += step.result.rejected
    events = core.structure[TF.H4].events if core else []
    return Tape(
        symbol,
        entry["close_time"].to_numpy(np.int64),
        entry["close"].to_numpy(float),
        atr,
        dec_l,
        dec_s,
        d1t,
        [t for t, k in events if k == "choch_down"],
        [t for t, k in events if k == "choch_up"],
        dict(signals),
        rejected,
    )


def ema_benchmark_tape(
    symbol: str, paths: DataPaths, cfg: Config, end_ms: int | None = None
) -> Tape:
    entry, _, d1 = _frames(paths, symbol, cfg.timeframes.entry, end_ms)
    n = len(entry)
    close = d1["close"].to_numpy(float)
    fast, slow = ema_series(close, 20), ema_series(close, 100)
    atr = atr_series(d1["high"].to_numpy(float), d1["low"].to_numpy(float), close, 14)
    signals: dict[int, list[Signal]] = {}
    force_exit: dict[int, Side] = {}
    above_prev = None
    for i, t in enumerate(d1["close_time"].to_numpy(np.int64)):
        if math.isnan(slow[i]) or math.isnan(atr[i]):
            continue
        above = fast[i] > slow[i]
        if above_prev is not None and above != above_prev:
            side = Side.LONG if above else Side.SHORT
            force_exit[int(t)] = Side.SHORT if above else Side.LONG
            stop = close[i] - side.sign * 2 * atr[i]
            sid = signal_id(symbol, "ema20_100", "EMA", int(t))
            signals[int(t)] = [
                Signal(sid, symbol, side, "EMA", int(t), close[i], stop, math.nan, None, 0, "")
            ]
        above_prev = above
    return Tape(
        symbol,
        entry["close_time"].to_numpy(np.int64),
        entry["close"].to_numpy(float),
        np.full(n, np.nan),
        np.zeros(n, bool),
        np.zeros(n, bool),
        np.zeros(n, np.int8),
        signals=signals,
        force_exit=force_exit,
        managed=False,
    )


def _cache_key(kind: str, symbol: str, cfg: Config, snapshot: str, end_ms: int | None) -> str:
    core_cfg = cfg.model_dump(
        mode="json",
        include={"timeframes", "swings", "structure", "zones", "entries", "exits", "filters"},
    )
    blob = json.dumps(
        [TAPE_VERSION, kind, symbol, core_cfg, snapshot, end_ms], sort_keys=True
    ).encode()
    return hashlib.sha256(blob).hexdigest()[:20]


def _build_one(args: tuple) -> tuple[str, Tape]:
    kind, symbol, root, cfg, end_ms = args
    paths = DataPaths(Path(root))
    fn = zone_tape if kind == "zone" else ema_benchmark_tape
    return symbol, fn(symbol, paths, cfg, end_ms)


def build_tapes(
    paths: DataPaths,
    symbols: list[str],
    cfg: Config,
    snapshot: str,
    end_ms: int | None = None,
    kind: str = "zone",
    workers: int = 4,
    use_cache: bool = True,
) -> dict[str, Tape]:
    """Tapes for ``symbols``; cached ones are loaded, the rest built in parallel."""
    cache = paths.root / "cache" / "tapes"
    cache.mkdir(parents=True, exist_ok=True)
    tapes: dict[str, Tape] = {}
    todo = []
    for sym in symbols:
        f = cache / f"{_cache_key(kind, sym, cfg, snapshot, end_ms)}.pkl"
        if use_cache and f.exists():
            # Our own cache under the data directory; never fed untrusted files.
            tapes[sym] = pickle.loads(f.read_bytes())  # noqa: S301
        else:
            todo.append((kind, sym, str(paths.root), cfg, end_ms))
    if todo:
        with ProcessPoolExecutor(max_workers=max(1, min(workers, len(todo)))) as pool:
            for sym, tape in pool.map(_build_one, todo):
                tapes[sym] = tape
                f = cache / f"{_cache_key(kind, sym, cfg, snapshot, end_ms)}.pkl"
                tmp = f.with_suffix(".part")
                tmp.write_bytes(pickle.dumps(tape))
                tmp.replace(f)
    return {s: tapes[s] for s in symbols}
