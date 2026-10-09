"""Provenance of reverse.jsonl: the Python ReverseEngine (research/reverse_replay.py)
on tape.txt.gz, gap 0.10, cap 0.60, notional 50, latency 47 ms, limit offset 0.02,
cash 1e9 (its defaults besides the gap), at build 1286ad5."""

import gzip
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVICE = HERE.parents[3]
sys.path[:0] = [str(SERVICE / "src"), str(SERVICE)]

from poly_kalshi_service.decision import StrategyParams  # noqa: E402
from poly_kalshi_service.ledger import Ledger  # noqa: E402
from poly_kalshi_service.risk import RiskLimits, RiskState  # noqa: E402
from poly_kalshi_service.shadow import Shadow  # noqa: E402
from poly_kalshi_service.venue import PaperVenue  # noqa: E402
from research.reverse_replay import ReverseEngine  # noqa: E402


class Capture:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def write(self, t_ns: int, record: dict) -> None:
        self.records.append(record)

    def close(self) -> None:
        pass


params = StrategyParams(0.10, 0.60, 50.0, 20, 60)
cap = Capture()
engine = ReverseEngine(
    params, ("BTC", "ETH", "SOL", "XRP"),
    ledger=Ledger(start_cash=1e9, cash=1e9),
    risk=RiskState(RiskLimits(max_open_notional_usd=1e12, daily_loss_limit_usd=1e12)),
    venue=PaperVenue(47), journal=cap, shadow=Shadow((), params), limit_offset=0.02, role="full",
)
last = 0
with gzip.open(HERE / "tape.txt.gz", "rt") as f:
    for line in f:
        ns, venue, text = line.rstrip("\n").split(" ", 2)
        last = int(ns)
        engine.handle_line(last, venue, text)
engine.handle_line(last + 200 * 1_000_000_000, "c", '{"type":"noop"}')
tail = {"type": "final", "counters": dict(sorted(engine.counters.items())),
        "ledger": engine.ledger.snapshot(engine.last_ns)}
with (HERE / "reverse.jsonl").open("w") as out:
    for r in [*cap.records, tail]:
        out.write(json.dumps(r, separators=(",", ":")) + "\n")
kinds: dict[str, int] = {}
for r in cap.records:
    kinds[r["type"]] = kinds.get(r["type"], 0) + 1
print(kinds)
