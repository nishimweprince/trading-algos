#!/usr/bin/env python3
"""Be-the-completer replay: buy only when our size covers the whole remaining curve, so a landed buy IS the
completion (pump.fun buy with partial_fill takes exactly what is left). No stall risk; we sell on the
canonical pool after the migrate into the first-slot buyers.

Per trade, at the first curve point with 0 < completion cost <= frac * size:
  - the buy lands L slots later, after every tx of that slot
  - curve already complete by then         -> "lost_race": only the buy fee is lost
  - remaining cost at landing <= size       -> we complete it; sell on the pool N slots after the real migrate
                                               (or at X % take-profit), using the recorded pool path
  - remaining cost at landing > size        -> we hold a partial position: falls back to replay_curve.simulate
  - curve never completed in reality but we would have completed it -> "self_completed": no real pool data,
    counted separately and left out of the mean (nobody else wanted to finish it)
Approximation: the real migrate (after the real completer) stands in for ours; the pool opens identically because
every completion leaves the same final curve state.

  python3 replay_completer.py --input curve-paths-v2.json.gz --report-output completer.md --sizes 0.25 0.5
"""
from __future__ import annotations

import argparse
import dataclasses
import gzip
import itertools
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay_curve import (  # noqa: E402
    LAMPORTS, Costs, Rule, Trade, build_path, completion_cost, curve_buy, fmt, last_before, pool_sell,
    simulate, summarize,
)


@dataclasses.dataclass(frozen=True)
class CRule:
    frac: float       # enter when completion cost <= frac * size
    tp_pct: float     # X (0 = off)
    timeout: int      # N slots after the migrate
    stall_s: int = 30  # used only by the partial-position fallback

    @property
    def name(self) -> str:
        return f"frac={self.frac:g} X={self.tp_pct:g} N={self.timeout}"


def simulate_completer(path, rule: CRule, costs: Costs) -> Trade | None:
    pts = path.curve_points
    size_sol = costs.size_lamports / LAMPORTS
    decision = next((i for i, p in enumerate(pts)
                     if p.rtok > 0 and 0 < completion_cost(p) <= rule.frac * size_sol), None)
    if decision is None:
        return None
    land_slot = pts[decision].slot + costs.buy_land_slots
    at = last_before(pts, land_slot)
    state = pts[at]
    if state.rtok == 0:
        return Trade("lost_race", -100 * costs.buy_fee / costs.size_lamports, path.admit_ts)
    if completion_cost(state) > size_sol:
        # Our buy only partly fills the curve: same path as the curve-in strategy from this decision.
        fallback = Rule(completion_cost(pts[decision]) + 1e-9, rule.tp_pct or math.inf, rule.timeout,
                        rule.stall_s, math.inf)
        t = simulate(path, fallback, costs)
        return None if t is None else Trade("partial_" + t.outcome, t.net_pct, t.admit_ts)
    tokens, spent = curve_buy(state, costs.size_lamports)
    basis = spent + costs.buy_fee
    if path.complete_slot is None:
        return Trade("self_completed", math.nan, path.admit_ts)
    if path.migration_slot is None or not path.pool_points:
        return None  # completed in reality but its pool was not recorded
    pool = path.pool_points
    start = next((i for i, p in enumerate(pool) if p.slot >= path.migration_slot), None)
    if start is None:
        return None
    gain = lambda p: 100 * (pool_sell(p, path.virtual_quote, tokens, costs.pool_fee_bps)
                            - costs.sell_fee - basis) / costs.size_lamports
    decide, reason = None, "pool_end"
    for p in pool[start:]:
        if p.slot >= path.migration_slot + rule.timeout:
            decide, reason = p.slot, "pool_timeout"
            break
        if rule.tp_pct > 0 and gain(p) >= rule.tp_pct:
            decide, reason = p.slot, "pool_tp"
            break
    if decide is None:
        return Trade(reason, gain(pool[-1]), path.admit_ts)
    j = last_before(pool, decide + costs.sell_land_slots)
    return Trade(reason, gain(pool[j]), path.admit_ts)


def run(paths, rule, costs):
    trades = [t for t in (simulate_completer(p, rule, costs) for p in paths) if t is not None]
    scored = [t for t in trades if not math.isnan(t.net_pct)]
    return scored, sum(1 for t in trades if math.isnan(t.net_pct))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--report-output", type=Path, required=True)
    ap.add_argument("--sizes", type=float, nargs="+", default=[0.05, 0.25, 0.5])
    ap.add_argument("--buy-fee-lamports", type=int, default=1_000_000)
    ap.add_argument("--sell-fee-lamports", type=int, default=1_450_000)
    ap.add_argument("--pool-fee-bps", type=float, default=125.0)
    ap.add_argument("--buy-land-slots", type=int, default=2)
    ap.add_argument("--sell-land-slots", type=int, default=2)
    ap.add_argument("--train-fraction", type=float, default=0.67)
    ap.add_argument("--min-test", type=int, default=20)
    args = ap.parse_args()

    data = json.load(gzip.open(args.input, "rt"))
    paths = sorted((p for p in (build_path(r) for r in data["records"]) if p), key=lambda p: p.admit_ts)
    standard = [p for p in paths if p.standard and not p.mayhem]
    mayhem = [p for p in paths if p.standard and p.mayhem]
    cut = int(len(standard) * args.train_fraction)
    train, test = standard[:cut], standard[cut:]
    grid = [CRule(f, x, n) for f, x, n in itertools.product((1.0, 0.8, 0.5), (0.0, 3.0, 5.0, 10.0), (1, 2, 3, 5, 10, 25))]
    lines = ["# Be-the-completer replay", "",
             f"Input: {len(data['records'])} admitted curves → {len(paths)} usable paths; standard non-mayhem "
             f"{len(standard)} ({len(train)} train / {len(test)} chronological test), mayhem {len(mayhem)}.",
             f"Buy fee {args.buy_fee_lamports} / sell fee {args.sell_fee_lamports} lamports; pool fee "
             f"{args.pool_fee_bps:g} bps; buy lands +{args.buy_land_slots}, sell +{args.sell_land_slots} slots.", ""]
    verdicts = []
    for size in args.sizes:
        costs = Costs(int(size * LAMPORTS), args.buy_fee_lamports, args.sell_fee_lamports, args.pool_fee_bps,
                      args.buy_land_slots, args.sell_land_slots)
        fast = dataclasses.replace(costs, buy_land_slots=1, sell_land_slots=1)
        scored = []
        for rule in grid:
            tr, _ = run(train, rule, costs)
            if len(tr) >= 5:
                scored.append((statistics.fmean(t.net_pct for t in tr), rule, tr))
        lines += [f"## Size {size:g} SOL", ""]
        allr = CRule(1.0, 0.0, 3)
        a, a_self = run(standard, allr, costs)
        af, _ = run(standard, allr, fast)
        m, m_self = run(mayhem, allr, costs)
        lines += [f"- all standard, `{allr.name}`: {fmt(summarize(a))}; self-completed (unscored): {a_self}",
                  f"- same, landing +1/+1: {fmt(summarize(af))}",
                  f"- mayhem, same rule: {fmt(summarize(m))}; self-completed: {m_self}"]
        if not scored:
            lines += ["- too few train trades to select a rule", ""]
            verdicts.append((size, "n/a"))
            continue
        scored.sort(key=lambda r: r[0], reverse=True)
        best_mean, best, btr = scored[0]
        te, te_self = run(test, best, costs)
        s_tr, s_te = summarize(btr), summarize(te)
        passed = s_tr["mean"] > 0 and s_te.get("n", 0) >= args.min_test and s_te["mean"] > 0
        verdicts.append((size, "PASS" if passed else "FAIL"))
        lines += [f"- best train rule `{best.name}`: train {fmt(s_tr)}",
                  f"- **test: {fmt(s_te)}**; self-completed (unscored): {te_self}",
                  f"- gate (train>0, test>0, test n≥{args.min_test}): **{'PASS' if passed else 'FAIL'}**", "",
                  "| rule | train | test |", "|---|---|---|"]
        for _, rule, tr in scored[:5]:
            lines.append(f"| `{rule.name}` | {fmt(summarize(tr))} | {fmt(summarize(run(test, rule, costs)[0]))} |")
        lines.append("")
    lines += ["## Verdict", ""] + [f"- {s:g} SOL: {v}" for s, v in verdicts]
    report = "\n".join(lines) + "\n"
    args.report_output.parent.mkdir(parents=True, exist_ok=True)
    args.report_output.write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
