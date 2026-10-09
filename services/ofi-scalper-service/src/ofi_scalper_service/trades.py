"""Trade and signal records, and the daily trading summary.

Two append-only JSONL streams under ``<state_dir>``, one file per UTC day
(by event time, so a replay writes the days it replays):

- ``trades/YYYY-MM-DD.jsonl``: one line per cycle, filled or not. Entry and
  exit fills, fees, funding, gross and net P&L, the exit reason, adverse
  selection (the mid 1/5/30 s after the entry fill) and the order round trip.
- ``signals/YYYY-MM-DD.jsonl``: every sample whose probability crossed the
  threshold, with what was done about it.
- ``markouts/YYYY-MM-DD.jsonl``: per signal episode (entered or not), the
  signed mid move from the signal's own sample at 0.1 s to 900 s. Fill-time
  marks only see filled, already adverse-selected entries; these show whether
  the signal predicts price at all.

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
    "BNB_DISCOUNT",
    "SIGNAL_HORIZONS_S",
    "BackgroundAppender",
    "JsonlDaily",
    "SignalMarks",
    "TradeBook",
    "summarise",
    "summarise_markouts",
    "trade_record",
]

ADVERSE_HORIZONS_S = (1, 5, 30)
# Fees paid in BNB cost 10% less (OFI_BNB_FEE_DISCOUNT).
BNB_DISCOUNT = 0.9
SIGNAL_HORIZONS_S = (0.1, 0.5, 1, 5, 30, 60, 300, 600, 900)
NS = 1_000_000_000
# A held signal repeats every grid step; one episode per symbol and side this long.
SIGNAL_EPISODE_S = 5.0
# A pause in a symbol's samples longer than this breaks its open markouts.
MARK_GAP_NS = 1_000_000_000


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
        # Signal to first entry fill; the policy's entry timeout runs on the same clock.
        "fill_ms": None
        if cycle.entry_fill_ns is None
        else round((cycle.entry_fill_ns - cycle.signal_ns) / 1e6, 1),
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


def _horizon_key(horizon_s: float) -> str:
    return f"{horizon_s:g}s"


class SignalMarks:
    """Signed mid moves after each signal episode, from the bridge's own samples.

    A move at ``h`` is taken at the first sample at or after ``signal + h``;
    a gap in the symbol's samples (feed pause, restart) leaves the remaining
    horizons None, as research's ``markouts`` does across a recording gap.
    """

    def __init__(
        self,
        journal: JsonlDaily,
        *,
        horizons_s: tuple[float, ...] = SIGNAL_HORIZONS_S,
        episode_s: float = SIGNAL_EPISODE_S,
        keep: int = 2000,
    ) -> None:
        self.journal = journal
        self.horizons_s = horizons_s
        self.episode_ns = int(episode_s * NS)
        self.open: dict[str, list[dict[str, Any]]] = {}
        self.last_signal_ns: dict[tuple[str, str], int] = {}
        self.last_sample_ns: dict[str, int] = {}
        self.recent: deque[dict[str, Any]] = deque(maxlen=keep)

    def on_signal(self, t_ns: int, record: dict[str, Any], mid: float | None) -> None:
        symbol, side = record.get("symbol"), record.get("side")
        if mid is None or not mid > 0 or side not in ("buy", "sell") or symbol is None:
            return
        key = (str(symbol), str(side))
        last = self.last_signal_ns.get(key)
        self.last_signal_ns[key] = t_ns
        if last is not None and t_ns - last <= self.episode_ns:
            return
        self.open.setdefault(str(symbol), []).append(
            {
                "symbol": symbol,
                "side": side,
                "t_ns": t_ns,
                "at": _iso(t_ns),
                "action": str(record.get("action", "")).split(":")[0],
                "model_version": record.get("model_version"),
                "mid": mid,
                "moves": {_horizon_key(h): None for h in self.horizons_s},
            }
        )

    def on_sample(self, symbol: str, t_ns: int, mid: float | None) -> None:
        last = self.last_sample_ns.get(symbol)
        self.last_sample_ns[symbol] = t_ns
        pending = self.open.get(symbol)
        if not pending:
            return
        if last is not None and t_ns - last > MARK_GAP_NS:
            for mark in pending:
                self._write(mark)
            pending.clear()
            return
        keep: list[dict[str, Any]] = []
        for mark in pending:
            sign = 1.0 if mark["side"] == "buy" else -1.0
            for h in self.horizons_s:
                key = _horizon_key(h)
                if mark["moves"][key] is None and mid and t_ns >= mark["t_ns"] + h * NS:
                    mark["moves"][key] = round(sign * (mid / mark["mid"] - 1) * 10_000, 3)
            if t_ns >= mark["t_ns"] + self.horizons_s[-1] * NS:
                self._write(mark)
            else:
                keep.append(mark)
        self.open[symbol] = keep

    def flush(self) -> None:
        for pending in self.open.values():
            for mark in pending:
                self._write(mark)
        self.open.clear()

    def _write(self, mark: dict[str, Any]) -> None:
        self.recent.append(mark)
        self.journal.write(mark["t_ns"], mark)


def summarise_markouts(marks: list[dict[str, Any]]) -> dict[str, Any]:
    """Per horizon: the mean signed move, its t-stat and n; overall and by action."""

    def block(subset: list[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        keys: list[str] = []
        for mark in subset:
            keys.extend(k for k in mark.get("moves", {}) if k not in keys)
        for key in keys:
            values = [m["moves"][key] for m in subset if m.get("moves", {}).get(key) is not None]
            stats: dict[str, Any] = {"n": len(values)}
            if values:
                mean = sum(values) / len(values)
                stats["mean_bp"] = round(mean, 3)
                if len(values) >= 2:
                    sd = math.sqrt(sum((x - mean) ** 2 for x in values) / (len(values) - 1))
                    stats["t_stat"] = round(mean / (sd / math.sqrt(len(values))), 3) if sd else None
            out[key] = stats
        return out

    actions = sorted({str(m.get("action")) for m in marks})
    return {
        "episodes": len(marks),
        "overall": block(marks),
        "by_action": {a: block([m for m in marks if str(m.get("action")) == a]) for a in actions},
    }


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
        self.halts = JsonlDaily(None if state_dir is None else state_dir / "halts", writer)
        self.marks = SignalMarks(
            JsonlDaily(None if state_dir is None else state_dir / "markouts", writer), keep=keep
        )
        self.recent_trades: deque[dict[str, Any]] = deque(maxlen=keep)
        self.recent_signals: deque[dict[str, Any]] = deque(maxlen=keep)

    def signal(self, t_ns: int, record: dict[str, Any]) -> None:
        self.recent_signals.append(record)
        self.signals.write(t_ns, record)

    def halt(self, t_ns: int, record: dict[str, Any]) -> None:
        self.halts.write(t_ns, record)

    def trade(self, t_ns: int, record: dict[str, Any]) -> None:
        self.recent_trades.append(record)
        self.trades.write(t_ns, record)

    def close(self) -> None:
        self.marks.flush()
        if self.writer is not None:
            self.writer.close()


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 3)


# Entry timeouts the fill-time report answers for: would a shorter wait keep the fills?
FILL_CHECKPOINTS_MS = (1000, 2000, 3000, 5000)


def time_to_fill(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Signal-to-fill times of filled entries, and the fill rate each shorter entry
    timeout would have left (fills at or under it / all entries; an upper bound on
    what that timeout keeps, since the decay and block cancels are unchanged)."""
    times = [t["fill_ms"] for t in entries if t.get("fill_ms") is not None]
    return {
        "n": len(times),
        "p50": _pct(times, 0.5),
        "p90": _pct(times, 0.9),
        "max": round(max(times), 1) if times else None,
        "fill_rate_within_ms": {
            str(limit): round(sum(1 for x in times if x <= limit) / len(entries), 4)
            if entries
            else None
            for limit in FILL_CHECKPOINTS_MS
        },
    }


def _fee_bp(trade: dict[str, Any]) -> float:
    """A trade's fees in bp of its entry notional."""
    notional = float(trade.get("entry_price") or 0) * float(trade.get("entry_qty") or 0)
    return trade.get("fees_usd", 0.0) / notional * 10_000 if notional > 0 else 0.0


def summarise(trades: list[dict[str, Any]], signals: list[dict[str, Any]] | None = None) -> dict:
    """The daily report: P&L, fees, win rate, fill rate, adverse selection, Brier."""
    entries = [t for t in trades if t.get("entry_qty_ordered")]
    done = [t for t in entries if t.get("filled") and t.get("net_usd") is not None]
    net = [t["net_usd"] for t in done]
    wins = [t for t in done if t["net_usd"] > 0]
    priced = [t for t in done if t.get("gross_bp") is not None and t.get("net_bp") is not None]
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
        # The edge before costs, and the costs it must clear (fees and funding, bp).
        "gross_bp_mean": round(sum(t["gross_bp"] for t in priced) / len(priced), 3)
        if priced
        else None,
        "cost_bp_mean": round(sum(t["gross_bp"] - t["net_bp"] for t in priced) / len(priced), 3)
        if priced
        else None,
        # What paying fees in BNB would have added (gates stay on undiscounted fees).
        "net_bp_mean_with_bnb": round(
            sum(t["net_bp"] + (1 - BNB_DISCOUNT) * _fee_bp(t) for t in priced) / len(priced), 3
        )
        if priced
        else None,
        "exit_liquidity": {},
        "time_to_fill_ms": time_to_fill(entries),
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
        for leg, fill in (t.get("legs") or {}).items():
            if leg != "entry" and isinstance(fill, dict) and fill.get("liquidity"):
                kind = fill["liquidity"]
                out["exit_liquidity"][kind] = out["exit_liquidity"].get(kind, 0) + 1
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
    # 95% interval of the mean net bp: whether a symbol (or a day) differs from the
    # rest needs this, not its point estimate on a few dozen trades.
    bps = [t["net_bp"] for t in done]
    if len(bps) >= 2:
        mean_bp = sum(bps) / len(bps)
        sd_bp = math.sqrt(sum((x - mean_bp) ** 2 for x in bps) / (len(bps) - 1))
        half = 1.96 * sd_bp / math.sqrt(len(bps))
        out["net_bp_ci95"] = [round(mean_bp - half, 3), round(mean_bp + half, 3)]
    else:
        out["net_bp_ci95"] = None
    return out
