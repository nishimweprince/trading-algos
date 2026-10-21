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

Overhang (optional veto): at the decision state, the top-K holders' tokens H,
rebuilt from every curve trade since the create. Dumping H on the curve moves
the price by 1 - (vtok / (vtok + H))^2; a rule with a finite threshold skips
entries above it (and entries whose holder history is unknown). The live
engine reads the same top-K from getTokenLargestAccounts, so the two agree up to
wallet-to-wallet transfers, which only the live read sees.

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
    # (slot, order, user, is_buy, tokens) for every curve trade since the create.
    holder_trades: tuple[tuple[int, int, str, bool, int], ...] = ()
    holders_known: bool = False


@dataclasses.dataclass(frozen=True)
class Rule:
    entry_cost_sol: float      # T
    take_profit_pct: float     # X
    timeout_slots: int         # N
    stall_seconds: int         # H
    abort_cost_sol: float      # inf = off
    overhang_max_drop_pct: float = math.inf  # inf = off
    overhang_top_k: int = 10

    @property
    def name(self) -> str:
        abort = "off" if math.isinf(self.abort_cost_sol) else f"{self.abort_cost_sol:g}"
        name = (f"T={self.entry_cost_sol:g} X={self.take_profit_pct:g} N={self.timeout_slots} "
                f"H={self.stall_seconds}s abort={abort}")
        if not math.isinf(self.overhang_max_drop_pct):
            name += f" OH<={self.overhang_max_drop_pct:g}%/top{self.overhang_top_k}"
        return name


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
    trades = [tuple(t) for t in record.get("holder_trades") or []]
    trades += [(int(p["slot"]), int(p["transaction_index"]) * 16 + int(p.get("event_index", 0)),
                str(p["user"]), bool(p["is_buy"]), int(p["token_amount"]))
               for p in raw if p.get("user") is not None]
    known = ("holder_trades" in record and not record.get("holder_truncated")
             and all(p.get("user") is not None for p in raw))
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
        holder_trades=tuple(sorted((int(t[0]), int(t[1]), str(t[2]), bool(t[3]), int(t[4])) for t in trades)),
        holders_known=known,
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


def holdings_at(path: Path_, slot: int, index: int) -> dict[str, int]:
    """Net curve tokens per wallet after every trade up to and including (slot, index)."""
    held: dict[str, int] = {}
    for t_slot, t_index, user, is_buy, tokens in path.holder_trades:
        if (t_slot, t_index) > (slot, index):
            break
        held[user] = max(0, held.get(user, 0) + (tokens if is_buy else -tokens))
    return held


def overhang(held: dict[str, int], k: int) -> int:
    """Tokens of the k largest holders."""
    return sum(sorted(held.values(), reverse=True)[:k])


def overhang_drop_pct(vtok: int, holder_tokens: int) -> float:
    """Price drop (%) if holder_tokens are sold on a curve with vtok virtual tokens (src/sol/curve.cpp)."""
    if vtok <= 0:
        return 0.0
    return 100.0 * (1.0 - (vtok / (vtok + holder_tokens)) ** 2)


# (id(path), decision, k) -> (path, drop); the path is kept so its id cannot be reused.
_DROP_CACHE: dict[tuple[int, int, int], tuple[Path_, float]] = {}


def decision_index(path: Path_, rule: Rule) -> int | None:
    return next((i for i, p in enumerate(path.curve_points)
                 if p.rtok > 0 and 0 < completion_cost(p) <= rule.entry_cost_sol), None)


def decision_drop(path: Path_, decision: int, k: int) -> float | None:
    """Overhang drop (%) at a decision point, None when the holder history is unknown."""
    if not path.holders_known:
        return None
    key = (id(path), decision, k)
    if key not in _DROP_CACHE:
        p = path.curve_points[decision]
        _DROP_CACHE[key] = (path, overhang_drop_pct(p.vtok, overhang(holdings_at(path, p.slot, p.index), k)))
    return _DROP_CACHE[key][1]


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
    decision = decision_index(path, rule)
    if decision is None:
        return None
    if not math.isinf(rule.overhang_max_drop_pct):
        drop = decision_drop(path, decision, rule.overhang_top_k)
        if drop is None or drop > rule.overhang_max_drop_pct:
            return None  # vetoed: the live engine would not have bought
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


OVERHANG_GRID = (math.inf, 75.0, 50.0, 35.0, 20.0)
DROP_BUCKETS = ((0, 20), (20, 35), (35, 50), (50, 75), (75, 101))
LIVE_RULE = Rule(2.0, 20.0, 25, 30, math.inf)  # the rule the live bot runs (config.example.toml)


def rules(top_k: int = 10) -> list[Rule]:
    return [Rule(*r, overhang_top_k=top_k) for r in itertools.product(
        (0.5, 1.0, 2.0, 3.0, 4.0),
        (0.0, 3.0, 5.0, 10.0, 20.0),
        (1, 2, 3, 5, 10, 25),
        (30, 120, 600),
        (math.inf, 6.0),
        OVERHANG_GRID,
    )]


def overhang_buckets(paths: list[Path_], rule: Rule, costs: Costs) -> list[tuple[str, list[Trade]]]:
    """Trades of `rule` (overhang off) grouped by the overhang drop at their decision."""
    base = dataclasses.replace(rule, overhang_max_drop_pct=math.inf)
    groups: dict[str, list[Trade]] = {f"{lo}-{min(hi, 100)}%": [] for lo, hi in DROP_BUCKETS}
    groups["unknown"] = []
    for path in paths:
        trade = simulate(path, base, costs)
        if trade is None:
            continue
        drop = decision_drop(path, decision_index(path, base), rule.overhang_top_k)
        label = "unknown" if drop is None else next(
            f"{lo}-{min(hi, 100)}%" for lo, hi in DROP_BUCKETS if lo <= drop < hi)
        groups[label].append(trade)
    return list(groups.items())


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
    parser.add_argument("--overhang-top-k", type=int, default=10,
                        help="holders summed into the overhang (curve.overhang_top_k live)")
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
    for rule in rules(args.overhang_top_k):
        tr = run(train, rule, costs)
        if len(tr) < 10:
            continue
        scored.append((statistics.fmean(t.net_pct for t in tr), rule, tr))
    scored.sort(key=lambda row: row[0], reverse=True)
    best_mean, best, best_train = scored[0]
    test_trades = run(test, best, costs)
    s_train, s_test = summarize(best_train), summarize(test_trades)
    off = next(row for row in scored if math.isinf(row[1].overhang_max_drop_pct))
    on = next((row for row in scored if not math.isinf(row[1].overhang_max_drop_pct)), None)
    vetoed = len(run(test, dataclasses.replace(best, overhang_max_drop_pct=math.inf), costs)) - len(test_trades)
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
        f"- overhang vetoes on test: {vetoed}; standard paths with unknown holder history: "
        f"{sum(not p.holders_known for p in standard)}",
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
    live = dataclasses.replace(LIVE_RULE, overhang_top_k=args.overhang_top_k)
    lines += [
        "",
        f"## Overhang diagnostic (all standard paths, `{live.name}`, top {args.overhang_top_k} holders)",
        "",
        "Drop = price fall if the top holders sold everything on the curve at the decision state. "
        "Holder balances come from curve trades only (wallet-to-wallet transfers are invisible here).",
        "",
        "| drop at decision | trades |",
        "|---|---|",
    ]
    for label, trades in overhang_buckets(standard, live, costs):
        lines.append(f"| {label} | {fmt(summarize(trades))} |")
    lines += [
        "",
        "## Best train rule with overhang off vs on",
        "",
        "| | rule | train | test |",
        "|---|---|---|---|",
    ]
    for label, row in (("off", off), ("on", on)):
        if row is None:
            lines.append(f"| {label} | – | – | – |")
            continue
        lines.append(f"| {label} | `{row[1].name}` | {fmt(summarize(row[2]))} | "
                     f"{fmt(summarize(run(test, row[1], costs)))} |")
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
