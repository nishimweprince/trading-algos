#!/usr/bin/env python3
"""Replay "curve-in, migrate-out" on fetched curve + pool paths.

Entry: at the first curve state whose completion cost is at most T SOL, send a
fixed-size bonding-curve buy. It lands L slots later, after every curve
transaction of that slot (conservative); if the curve completed first the buy
fails and only its fee is lost.

Hold: if the curve has not completed H seconds after the buy (or its completion
cost climbs back above an abort level), sell back on the curve.

Exit: from the migrate, sell on the canonical PumpSwap pool once the net gain
reaches X %, else N slots after the migrate slot. The sell is decided at a pool
state and executes against the last state of the slot it lands in.

Selection follows replay.py: parameters are chosen on the chronological
training prefix; only the untouched test suffix counts as evidence.
"""

from __future__ import annotations

import argparse
import dataclasses
import gzip
import itertools
import json
import math
import statistics
from pathlib import Path
from typing import Any

from replay import bootstrap_ci

LAMPORTS = 1_000_000_000
STANDARD_GAP = 279_900_000_000_000  # virtual - real tokens on a standard pump.fun curve
SLOT_MS = 400


@dataclasses.dataclass(frozen=True)
class CurvePoint:
    slot: int
    index: int
    vsol: int
    vtok: int
    rtok: int
    fee_bps: int


@dataclasses.dataclass(frozen=True)
class PoolPoint:
    slot: int
    index: int
    base: int
    quote: int


@dataclasses.dataclass(frozen=True)
class Path_:
    curve: str
    admit_ts: int
    standard: bool
    mayhem: bool
    curve_points: tuple[CurvePoint, ...]
    complete_slot: int | None
    migration_slot: int | None
    virtual_quote: int
    pool_points: tuple[PoolPoint, ...]


@dataclasses.dataclass(frozen=True)
class Rule:
    entry_cost_sol: float      # T
    take_profit_pct: float     # X
    timeout_slots: int         # N
    stall_seconds: int         # H
    abort_cost_sol: float      # inf = off

    @property
    def name(self) -> str:
        abort = "off" if math.isinf(self.abort_cost_sol) else f"{self.abort_cost_sol:g}"
        return (f"T={self.entry_cost_sol:g} X={self.take_profit_pct:g} N={self.timeout_slots} "
                f"H={self.stall_seconds}s abort={abort}")


@dataclasses.dataclass(frozen=True)
class Costs:
    size_lamports: int
    buy_fee: int
    sell_fee: int
    pool_fee_bps: float
    buy_land_slots: int
    sell_land_slots: int


@dataclasses.dataclass(frozen=True)
class Trade:
    outcome: str     # pool_tp | pool_timeout | stall | abort | failed | pool_end
    net_pct: float
    admit_ts: int


def build_path(record: dict[str, Any]) -> Path_ | None:
    raw = record.get("curve_points") or []
    if record.get("status") != "ok" or not raw or record.get("curve_truncated"):
        return None
    points = tuple(
        CurvePoint(int(p["slot"]), int(p["transaction_index"]) * 16 + int(p.get("event_index", 0)),
                   int(p["vsol"]), int(p["vtok"]), int(p["rtok"]),
                   int((p.get("fee_bps") or 95) + (p.get("creator_fee_bps") or 30)) or 125)
        for p in raw
    )
    complete = next((p.slot for p in points if p.rtok == 0), None)
    standard = all(p.vtok - p.rtok == STANDARD_GAP for p in points)
    pool = record.get("pool") or {}
    pool_points: tuple[PoolPoint, ...] = ()
    migration_slot = record.get("migration_slot")
    if pool.get("status") == "ok" and not pool.get("truncated"):
        pool_points = tuple(sorted(
            (PoolPoint(int(p["slot"]), int(p["transaction_index"]), int(p["base"]), int(p["quote"]))
             for p in pool.get("points") or [] if int(p["base"]) > 0),
            key=lambda p: (p.slot, p.index),
        ))
    return Path_(
        curve=str(record["curve"]),
        admit_ts=int(record["admit_ts"]),
        standard=standard,
        mayhem=bool(pool.get("mayhem")),
        curve_points=points,
        complete_slot=complete,
        migration_slot=int(migration_slot) if migration_slot else None,
        virtual_quote=int(pool.get("virtual_quote") or 0),
        pool_points=pool_points,
    )


def completion_cost(p: CurvePoint) -> float:
    """SOL (gross of fee) to buy every remaining real token."""
    if p.rtok <= 0 or p.vtok <= p.rtok:
        return 0.0
    net = p.vtok * p.vsol / (p.vtok - p.rtok) - p.vsol
    return net / (1 - p.fee_bps / 1e4) / LAMPORTS


def curve_buy(p: CurvePoint, gross: int) -> tuple[int, int]:
    """(tokens out, lamports actually spent) for a gross SOL buy, capped at the remaining tokens."""
    net = gross * (1 - p.fee_bps / 1e4)
    k = p.vtok * p.vsol
    tokens = int(p.vtok - k / (p.vsol + net))
    if tokens <= p.rtok:
        return tokens, gross
    need_net = k / (p.vtok - p.rtok) - p.vsol
    return p.rtok, int(math.ceil(need_net / (1 - p.fee_bps / 1e4)))


def curve_sell(p: CurvePoint, tokens: int) -> int:
    k = p.vtok * p.vsol
    out = p.vsol - k / (p.vtok + tokens)
    return int(out * (1 - p.fee_bps / 1e4))


def pool_sell(p: PoolPoint, virtual: int, tokens: int, fee_bps: float) -> int:
    out = (p.quote + virtual) * tokens / (p.base + tokens)
    return int(min(out * (1 - fee_bps / 1e4), p.quote))


def last_before(points, slot: int):
    """Index of the last point with point.slot <= slot (state after that slot), or None."""
    best = None
    for i, p in enumerate(points):
        if p.slot <= slot:
            best = i
        else:
            break
    return best


def simulate(path: Path_, rule: Rule, costs: Costs) -> Trade | None:
    pts = path.curve_points
    decision = next((i for i, p in enumerate(pts)
                     if p.rtok > 0 and 0 < completion_cost(p) <= rule.entry_cost_sol), None)
    if decision is None:
        return None
    land_slot = pts[decision].slot + costs.buy_land_slots
    at = last_before(pts, land_slot)
    state = pts[at]
    if state.rtok == 0:
        return Trade("failed", -100 * costs.buy_fee / costs.size_lamports, path.admit_ts)
    tokens, spent = curve_buy(state, costs.size_lamports)
    basis = spent + costs.buy_fee
    stall_slot = land_slot + rule.stall_seconds * 1000 // SLOT_MS

    # Curve phase: completes, stalls, or aborts on a falling curve.
    if path.complete_slot is None or path.complete_slot > stall_slot or (
        not math.isinf(rule.abort_cost_sol)
        and any(completion_cost(p) > rule.abort_cost_sol for p in pts[at + 1:]
                if p.slot <= (path.complete_slot or stall_slot))
    ):
        exit_slot = stall_slot
        reason = "stall"
        if not math.isinf(rule.abort_cost_sol):
            trip = next((p.slot for p in pts[at + 1:]
                         if p.slot <= (path.complete_slot or stall_slot)
                         and completion_cost(p) > rule.abort_cost_sol), None)
            if trip is not None and trip < exit_slot:
                exit_slot, reason = trip, "abort"
        if path.complete_slot is not None and path.complete_slot <= exit_slot + costs.sell_land_slots:
            return None  # completed while the curve sell was in flight: the sell fails; ambiguous
        j = last_before(pts, exit_slot + costs.sell_land_slots)
        proceeds = curve_sell(pts[j], tokens) - costs.sell_fee
        return Trade(reason, 100 * (proceeds - basis) / costs.size_lamports, path.admit_ts)

    # Pool phase.
    if path.migration_slot is None or not path.pool_points:
        return None
    pool = path.pool_points
    start = next((i for i, p in enumerate(pool) if p.slot >= path.migration_slot), None)
    if start is None:
        return None
    gain = lambda p: 100 * (pool_sell(p, path.virtual_quote, tokens, costs.pool_fee_bps)
                            - costs.sell_fee - basis) / costs.size_lamports
    decide_slot, reason = None, "pool_end"
    for p in pool[start:]:
        if p.slot >= path.migration_slot + rule.timeout_slots:
            decide_slot, reason = p.slot, "pool_timeout"
            break
        if gain(p) >= rule.take_profit_pct:
            decide_slot, reason = p.slot, "pool_tp"
            break
    if decide_slot is None:
        return Trade(reason, gain(pool[-1]), path.admit_ts)
    j = last_before(pool, decide_slot + costs.sell_land_slots)
    return Trade(reason, gain(pool[j]), path.admit_ts)


def summarize(trades: list[Trade]) -> dict[str, Any]:
    values = [t.net_pct for t in trades]
    if not values:
        return {"n": 0}
    low, high = bootstrap_ci(values)
    outcomes: dict[str, int] = {}
    for t in trades:
        outcomes[t.outcome] = outcomes.get(t.outcome, 0) + 1
    return {
        "n": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "win": 100 * sum(v > 0 for v in values) / len(values),
        "se": statistics.pstdev(values) / math.sqrt(len(values)),
        "ci": (low, high),
        "p05": sorted(values)[int(0.05 * len(values))],
        "outcomes": outcomes,
    }


def rules() -> list[Rule]:
    return [Rule(*r) for r in itertools.product(
        (0.5, 1.0, 2.0, 3.0, 4.0),
        (0.0, 3.0, 5.0, 10.0, 20.0),
        (1, 2, 3, 5, 10, 25),
        (30, 120, 600),
        (math.inf, 6.0),
    )]


def run(paths: list[Path_], rule: Rule, costs: Costs) -> list[Trade]:
    return [t for t in (simulate(p, rule, costs) for p in paths) if t is not None]


def fmt(s: dict[str, Any]) -> str:
    if not s.get("n"):
        return "n=0"
    return (f"n={s['n']} mean={s['mean']:+.2f}% med={s['median']:+.2f}% win={s['win']:.0f}% "
            f"se={s['se']:.2f} ci95=[{s['ci'][0]:+.2f},{s['ci'][1]:+.2f}] p05={s['p05']:+.1f}% "
            f"{s['outcomes']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--size-sol", type=float, default=0.05)
    parser.add_argument("--buy-fee-lamports", type=int, default=1_000_000)
    parser.add_argument("--sell-fee-lamports", type=int, default=1_450_000)
    parser.add_argument("--pool-fee-bps", type=float, default=125.0)
    parser.add_argument("--buy-land-slots", type=int, default=2)
    parser.add_argument("--sell-land-slots", type=int, default=2)
    parser.add_argument("--train-fraction", type=float, default=0.67)
    parser.add_argument("--min-test", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data = json.load(gzip.open(args.input, "rt"))
    built = [build_path(r) for r in data["records"]]
    paths = sorted((p for p in built if p), key=lambda p: p.admit_ts)
    standard = [p for p in paths if p.standard and not p.mayhem]
    mayhem = [p for p in paths if p.standard and p.mayhem]
    cut = int(len(standard) * args.train_fraction)
    train, test = standard[:cut], standard[cut:]
    costs = Costs(int(args.size_sol * LAMPORTS), args.buy_fee_lamports, args.sell_fee_lamports,
                  args.pool_fee_bps, args.buy_land_slots, args.sell_land_slots)
    cheap = dataclasses.replace(costs, buy_fee=500_000, sell_fee=500_000)
    fast = dataclasses.replace(costs, buy_land_slots=1, sell_land_slots=1)

    scored = []
    for rule in rules():
        tr = run(train, rule, costs)
        if len(tr) < 10:
            continue
        scored.append((statistics.fmean(t.net_pct for t in tr), rule, tr))
    scored.sort(key=lambda row: row[0], reverse=True)
    best_mean, best, best_train = scored[0]
    test_trades = run(test, best, costs)
    s_train, s_test = summarize(best_train), summarize(test_trades)
    passed = (s_train["n"] and s_train["mean"] > 0 and s_test.get("n", 0) >= args.min_test
              and s_test["mean"] > 0)

    lines = [
        "# Curve-in, migrate-out replay",
        "",
        f"Input: {len(data['records'])} admitted curves → {len(paths)} usable paths; "
        f"standard non-mayhem {len(standard)} ({len(train)} train / {len(test)} chronological test), "
        f"mayhem {len(mayhem)}, non-standard {len(paths) - len(standard) - len(mayhem)}.",
        f"Size {args.size_sol} SOL; buy fee {args.buy_fee_lamports} / sell fee {args.sell_fee_lamports} lamports; "
        f"curve fee from TradeEvents; pool fee {args.pool_fee_bps:g} bps; buy lands +{args.buy_land_slots}, "
        f"sell +{args.sell_land_slots} slots, after every tx of the landing slot.",
        "",
        "## Selected on train",
        "",
        f"- rule: `{best.name}`",
        f"- train: {fmt(s_train)}",
        f"- **test: {fmt(s_test)}**",
        f"- test, fees 0.5M/0.5M: {fmt(summarize(run(test, best, cheap)))}",
        f"- test, landing +1/+1: {fmt(summarize(run(test, best, fast)))}",
        f"- mayhem (all, same rule): {fmt(summarize(run(mayhem, best, costs)))}",
        "",
        f"## Gate: {'PASS' if passed else 'FAIL'}",
        "",
        f"Requires train mean > 0, test mean > 0 and test n ≥ {args.min_test}.",
        "",
        "## Top 10 train rules and their test result",
        "",
        "| rule | train | test |",
        "|---|---|---|",
    ]
    for mean, rule, tr in scored[:10]:
        lines.append(f"| `{rule.name}` | {fmt(summarize(tr))} | {fmt(summarize(run(test, rule, costs)))} |")
    lines += ["", "## Entry threshold sensitivity (all standard paths, X=5 N=3 H=120s abort off)", ""]
    for t_cost in (0.5, 1.0, 2.0, 3.0, 4.0):
        lines.append(f"- T={t_cost:g}: {fmt(summarize(run(standard, Rule(t_cost, 5.0, 3, 120, math.inf), costs)))}")
    report = "\n".join(lines) + "\n"
    args.report_output.parent.mkdir(parents=True, exist_ok=True)
    args.report_output.write_text(report, encoding="utf-8")
    if args.json_output:
        args.json_output.write_text(json.dumps({
            "passed": bool(passed), "rule": dataclasses.asdict(best),
            "train": s_train, "test": s_test}, default=str, indent=2), encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
