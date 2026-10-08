"""Trade and signal records, and the daily trading summary.

Two append-only JSONL streams under ``<state_dir>``, one file per UTC day
(by event time, so a replay writes the days it replays):

- ``trades/YYYY-MM-DD.jsonl``: one line per cycle, filled or not. Entry and
  exit fills, fees, funding, gross and net P&L, the exit reason, adverse
  selection (the mid 1/5/30 s after the entry fill) and the order round trip.
- ``signals/YYYY-MM-DD.jsonl``: every sample whose probability crossed the
  threshold, with what was done about it.

``summarise`` turns a day of trades into the plan's daily report (§5 <output>).
"""

from __future__ import annotations

import atexit
import json
import math
import queue
import threading
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from .policy import Cycle

__all__ = [
    "ADVERSE_HORIZONS_S",
    "BackgroundAppender",
    "JsonlDaily",
    "TradeBook",
    "summarise",
    "trade_record",
]

ADVERSE_HORIZONS_S = (1, 5, 30)
NS = 1_000_000_000


def _iso(t_ns: int | None) -> str | None:
    if t_ns is None:
        return None
    return datetime.fromtimestamp(t_ns / 1e9, UTC).isoformat().replace("+00:00", "Z")


class BackgroundAppender:
    """Serialise and append records on a writer thread, so the decision loop
    never waits on ``json.dumps`` or the disk (a signal record is written in the
    same callback that queues its order). The loop only enqueues; a full queue
    drops the record and counts it rather than blocking. Files stay open."""

    def __init__(self, queue_limit: int = 100_000) -> None:
        self._queue: queue.Queue[tuple[Path, dict[str, Any]] | None] = queue.Queue(queue_limit)
        self._handles: dict[Path, IO[str]] = {}
        self.dropped = 0
        self.errors = 0
        self._thread = threading.Thread(target=self._run, name="ofi-records", daemon=True)
        self._thread.start()
        atexit.register(self.close)

    def append(self, path: Path, record: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait((path, record))
        except queue.Full:
            self.dropped += 1

    def flush(self, timeout: float = 5.0) -> None:
        """Wait until everything enqueued so far is on disk (tests, shutdown)."""
        done = threading.Event()
        self._queue.put((Path(), {"__flush__": done}))
        done.wait(timeout)

    def close(self) -> None:
        if self._thread.is_alive():
            self._queue.put(None)
            self._thread.join(5.0)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            path, record = item
            marker = record.get("__flush__")
            if marker is not None:
                for handle in self._handles.values():
                    handle.flush()
                marker.set()
                continue
            try:
                handle = self._handles.get(path)
                if handle is None:
                    for old in list(self._handles):  # a new day: close the others
                        self._handles.pop(old).close()
                    path.parent.mkdir(parents=True, exist_ok=True)
                    handle = self._handles[path] = path.open("a", buffering=1)
                handle.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
            except OSError:
                self.errors += 1
        for handle in self._handles.values():
            handle.close()


class JsonlDaily:
    """Append JSON lines to ``<dir>/<YYYY-MM-DD>.jsonl`` by the record's event time.

    With a ``writer`` the append happens on its thread; without one, inline.
    """

    def __init__(self, directory: Path | None, writer: BackgroundAppender | None = None) -> None:
        self.directory = directory
        self.writer = writer
        self.errors = 0

    def write(self, t_ns: int, record: dict[str, Any]) -> None:
        if self.directory is None:
            return
        day = datetime.fromtimestamp(t_ns / 1e9, UTC).strftime("%Y-%m-%d")
        path = self.directory / f"{day}.jsonl"
        if self.writer is not None:
            self.writer.append(path, record)
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with path.open("a") as handle:
                handle.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
        except OSError:
            self.errors += 1  # a record must never stop trading or the kill path

    def read(self, day: str) -> list[dict[str, Any]]:
        if self.directory is None:
            return []
        path = self.directory / f"{day}.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def trade_record(
    cycle: Cycle,
    *,
    mode: str,
    model_version: str | None,
    maker_bp: float,
    taker_bp: float,
    liquidity: dict[str, str],
    funding: dict[str, Any] | None,
    adverse_bp: dict[str, float | None],
    rtt_ms: dict[str, float],
) -> dict[str, Any]:
    """One cycle -> one record. ``liquidity``: leg -> "maker" | "taker"."""
    sign = 1 if cycle.side == "buy" else -1
    filled = float(cycle.entry_filled)
    entry_avg = cycle.entry_avg
    exit_avg = cycle.exit_avg
    legs = {
        leg: {"qty": str(qty), "price": price, "liquidity": liquidity.get(leg, "maker")}
        for leg, (qty, price) in cycle.fills.items()
    }
    fees = 0.0
    for leg, (qty, price) in cycle.fills.items():
        rate = taker_bp if liquidity.get(leg) == "taker" else maker_bp
        fees += float(qty) * price * rate / 10_000
    record: dict[str, Any] = {
        "cycle_id": cycle.cycle_id,
        "mode": mode,
        "model_version": model_version,
        "symbol": cycle.symbol,
        "side": cycle.side,
        "p": round(cycle.p, 6),
        "threshold": round(cycle.threshold, 6),
        "signal_at": _iso(cycle.signal_ns),
        "filled": filled > 0,
        "entry_limit": str(cycle.entry_price),
        "entry_qty_ordered": str(cycle.entry_qty),
        "entry_qty": str(cycle.entry_filled),
        "entry_price": entry_avg,
        "entry_at": _iso(cycle.entry_fill_ns),
        "exit_price": exit_avg,
        "exit_at": _iso(cycle.exit_ns),
        "exit_reason": cycle.exit_reason,
        "tp_price": None if cycle.tp_price is None else str(cycle.tp_price),
        "legs": legs,
        "fees_usd": round(fees, 6),
        "rtt_ms": rtt_ms,
        "adverse_bp": adverse_bp,
    }
    if filled > 0 and entry_avg is not None and exit_avg is not None:
        notional = filled * entry_avg
        gross = sign * (exit_avg - entry_avg) * filled
        funding_usd = 0.0
        if funding and cycle.entry_fill_ns is not None and cycle.exit_ns is not None:
            at = funding.get("at_ns")
            rate = funding.get("rate")
            if at is not None and rate is not None and cycle.entry_fill_ns <= at <= cycle.exit_ns:
                funding_usd = -sign * notional * rate  # longs pay a positive rate
        net = gross - fees + funding_usd
        record.update(
            gross_usd=round(gross, 6),
            funding_usd=round(funding_usd, 6),
            net_usd=round(net, 6),
            gross_bp=round(gross / notional * 10_000, 3),
            net_bp=round(net / notional * 10_000, 3),
        )
    return record


class TradeBook:
    """Keeps recent records in memory and writes them to the daily files."""

    def __init__(
        self,
        state_dir: Path | None,
        *,
        keep: int = 500,
        writer: BackgroundAppender | None = None,
    ) -> None:
        self.writer = writer
        self.trades = JsonlDaily(None if state_dir is None else state_dir / "trades", writer)
        self.signals = JsonlDaily(None if state_dir is None else state_dir / "signals", writer)
        self.recent_trades: deque[dict[str, Any]] = deque(maxlen=keep)
        self.recent_signals: deque[dict[str, Any]] = deque(maxlen=keep)

    def signal(self, t_ns: int, record: dict[str, Any]) -> None:
        self.recent_signals.append(record)
        self.signals.write(t_ns, record)

    def trade(self, t_ns: int, record: dict[str, Any]) -> None:
        self.recent_trades.append(record)
        self.trades.write(t_ns, record)

    def close(self) -> None:
        if self.writer is not None:
            self.writer.close()


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 3)


def summarise(trades: list[dict[str, Any]], signals: list[dict[str, Any]] | None = None) -> dict:
    """The daily report: P&L, fees, win rate, fill rate, adverse selection, Brier."""
    entries = [t for t in trades if t.get("entry_qty_ordered")]
    done = [t for t in entries if t.get("filled") and t.get("net_usd") is not None]
    net = [t["net_usd"] for t in done]
    wins = [t for t in done if t["net_usd"] > 0]
    out: dict[str, Any] = {
        "signals": len(signals or []),
        "entries": len(entries),
        "trades": len(done),
        "maker_fill_rate": round(len([t for t in entries if t.get("filled")]) / len(entries), 4)
        if entries
        else None,
        "net_usd": round(sum(net), 4),
        "fees_usd": round(sum(t.get("fees_usd", 0.0) for t in done), 4),
        "funding_usd": round(sum(t.get("funding_usd", 0.0) for t in done), 4),
        "win_rate": round(len(wins) / len(done), 4) if done else None,
        "largest_loss_usd": round(min(net), 4) if net and min(net) < 0 else 0.0,
        "mean_net_bp": round(sum(t["net_bp"] for t in done) / len(done), 3) if done else None,
        "exit_reasons": {},
        "adverse_bp_mean": {},
        "entry_rtt_ms": {
            "p50": _pct(
                [t["rtt_ms"]["entry"] for t in entries if "entry" in t.get("rtt_ms", {})], 0.5
            ),
            "p99": _pct(
                [t["rtt_ms"]["entry"] for t in entries if "entry" in t.get("rtt_ms", {})], 0.99
            ),
        },
    }
    for t in done:
        reason = t.get("exit_reason") or "unknown"
        out["exit_reasons"][reason] = out["exit_reasons"].get(reason, 0) + 1
    for horizon in ADVERSE_HORIZONS_S:
        key = f"{horizon}s"
        values = [
            t["adverse_bp"][key] for t in done if t.get("adverse_bp", {}).get(key) is not None
        ]
        out["adverse_bp_mean"][key] = round(sum(values) / len(values), 3) if values else None
    # Calibration on our own fills: predicted p of the side taken vs reaching the barrier.
    if done:
        brier = sum(
            (t["p"] - (1.0 if t.get("exit_reason") == "take_profit" else 0.0)) ** 2 for t in done
        )
        out["brier"] = round(brier / len(done), 5)
    else:
        out["brier"] = None
    if len(net) >= 2:
        mean = sum(net) / len(net)
        sd = math.sqrt(sum((x - mean) ** 2 for x in net) / (len(net) - 1))
        out["t_stat"] = round(mean / (sd / math.sqrt(len(net))), 3) if sd > 0 else None
    else:
        out["t_stat"] = None
    return out
