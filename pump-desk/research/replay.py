#!/usr/bin/env python3
"""Walk-forward screen of long-only PumpSwap entries over compact pool paths."""

from __future__ import annotations

import argparse
import dataclasses
import gzip
import json
import math
import random
import statistics
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any


@dataclasses.dataclass(frozen=True)
class Point:
    time_ms: int
    price: float
    base: int
    quote: int
    quote_change_sol: float
    max_single_quote_in_sol: float


@dataclasses.dataclass(frozen=True)
class PoolPath:
    mint: str
    caller: str
    timestamp_ms: int
    mayhem: bool
    virtual_quote: int
    points: tuple[Point, ...]
    self_pump: bool


@dataclasses.dataclass(frozen=True)
class EntryRule:
    family: str
    delay_ms: int = 0
    min_return_pct: float = -math.inf
    min_flow_sol: float = -math.inf
    max_single_buy_sol: float = math.inf
    breakout_pct: float = math.inf
    max_wait_ms: int = 60_000
    drawdown_pct: float = math.inf
    rebound_pct: float = math.inf

    @property
    def name(self) -> str:
        values = [self.family]
        for field in dataclasses.fields(self):
            if field.name == "family":
                continue
            value = getattr(self, field.name)
            default = field.default
            if value != default:
                values.append(f"{field.name}={value:g}")
        return ";".join(values)


@dataclasses.dataclass(frozen=True)
class ExitRule:
    horizon_ms: int
    take_profit_pct: float
    stop_loss_pct: float

    @property
    def name(self) -> str:
        tp = "none" if math.isinf(self.take_profit_pct) else f"{self.take_profit_pct:g}"
        sl = "none" if math.isinf(self.stop_loss_pct) else f"{self.stop_loss_pct:g}"
        return f"h={self.horizon_ms / 1000:g}s,tp={tp},sl={sl}"


@dataclasses.dataclass(frozen=True)
class Trade:
    net_pct: float
    net_sol: float
    entry_ms: int
    exit_ms: int
    exit_reason: str


def build_path(record: dict[str, Any]) -> PoolPath | None:
    raw = record.get("points") or []
    if record.get("status") != "ok" or len(raw) < 1:
        return None
    virtual = int(record.get("virtual_quote") or 0)
    migration_signature = record.get("migration_signature")
    migration = next(
        (point for point in raw if point.get("signature") == migration_signature), raw[0]
    )
    migration_slot = int(migration.get("slot") or record.get("migration_slot") or 0)
    migration_index = int(migration.get("transaction_index") or 0)
    points: list[Point] = []
    previous_quote: int | None = None
    max_single_quote_in = 0.0
    for raw_point in raw:
        base = int(raw_point["base"])
        quote = int(raw_point["quote"])
        if base <= 0 or quote < 0:
            continue
        slot = int(raw_point.get("slot") or 0)
        index = int(raw_point.get("transaction_index") or 0)
        time_ms = max(0, (slot - migration_slot) * 400)
        if slot == migration_slot:
            time_ms += max(0, index - migration_index)
        change = 0.0 if previous_quote is None else (quote - previous_quote) / 1e9
        max_single_quote_in = max(max_single_quote_in, change)
        price = (quote + virtual) / 1e9 / (base / 1e6)
        points.append(Point(time_ms, price, base, quote, change, max_single_quote_in))
        previous_quote = quote
    if not points:
        return None
    initial_quote = points[0].quote
    same_slot_jump = max(
        (
            (int(raw_point["quote"]) - initial_quote) / 1e9
            for raw_point in raw
            if int(raw_point.get("slot") or 0) == migration_slot
        ),
        default=0.0,
    )
    return PoolPath(
        mint=str(record["mint"]),
        caller=str(record.get("caller") or ""),
        timestamp_ms=int(record.get("migration_ts") or 0),
        mayhem=bool(record.get("mayhem")),
        virtual_quote=virtual,
        points=tuple(points),
        self_pump=same_slot_jump >= 100.0,
    )


def entry_index(path: PoolPath, rule: EntryRule) -> int | None:
    points = path.points
    initial = points[0]
    if rule.family == "delay_momentum":
        for index, point in enumerate(points):
            if point.time_ms < rule.delay_ms:
                continue
            market_return = (point.price / initial.price - 1) * 100
            flow = (point.quote - initial.quote) / 1e9
            if (
                market_return >= rule.min_return_pct
                and flow >= rule.min_flow_sol
                and point.max_single_quote_in_sol <= rule.max_single_buy_sol
            ):
                return index
        return None
    if rule.family == "breakout":
        for index, point in enumerate(points[1:], 1):
            if point.time_ms > rule.max_wait_ms:
                break
            market_return = (point.price / initial.price - 1) * 100
            if (
                market_return >= rule.breakout_pct
                and point.max_single_quote_in_sol <= rule.max_single_buy_sol
            ):
                return index
        return None
    if rule.family == "dip_rebound":
        high = initial.price
        trough = initial.price
        armed = False
        for index, point in enumerate(points[1:], 1):
            if point.time_ms > rule.max_wait_ms:
                break
            high = max(high, point.price)
            trough = min(trough, point.price)
            if (point.price / high - 1) * 100 <= -rule.drawdown_pct:
                armed = True
                trough = point.price
            if armed:
                trough = min(trough, point.price)
                if (point.price / trough - 1) * 100 >= rule.rebound_pct:
                    return index
        return None
    if rule.family == "post_self_pump":
        if not path.self_pump:
            return None
        for index, point in enumerate(points):
            if point.time_ms >= rule.delay_ms:
                return index
        return None
    raise ValueError(f"unknown entry family: {rule.family}")


def simulate(
    path: PoolPath,
    entry: int,
    exit_rule: ExitRule,
    size_sol: float,
    fee_bps: float,
    tx_cost_sol: float,
) -> Trade:
    fee = fee_bps / 10_000
    point = path.points[entry]
    effective_input = size_sol / (1 + fee)
    effective_quote_sol = (point.quote + path.virtual_quote) / 1e9
    tokens = (point.base / 1e6) * effective_input / (effective_quote_sol + effective_input)
    execution_price = size_sol / tokens
    selected = point
    reason = "HORIZON"
    for candidate in path.points[entry + 1 :]:
        elapsed = candidate.time_ms - point.time_ms
        if elapsed > exit_rule.horizon_ms:
            break
        selected = candidate
        marked = (candidate.price / execution_price - 1) * 100
        if marked >= exit_rule.take_profit_pct:
            reason = "TAKE_PROFIT"
            break
        if marked <= -exit_rule.stop_loss_pct:
            reason = "STOP_LOSS"
            break
    effective_quote = (selected.quote + path.virtual_quote) / 1e9
    base_tokens = selected.base / 1e6
    gross_out = effective_quote * tokens / (base_tokens + tokens)
    net_out = gross_out * (1 - fee)
    net_sol = net_out - size_sol - tx_cost_sol
    return Trade(
        net_pct=net_sol / size_sol * 100,
        net_sol=net_sol,
        entry_ms=point.time_ms,
        exit_ms=selected.time_ms,
        exit_reason=reason,
    )


def entry_rules() -> list[EntryRule]:
    rules: list[EntryRule] = []
    for delay in (1_000, 2_000, 3_000, 5_000, 10_000):
        for minimum_return in (0, 5, 10, 20):
            for flow in (0, 1, 2, 5, 10):
                for max_single in (10, 30, math.inf):
                    rules.append(
                        EntryRule(
                            "delay_momentum",
                            delay_ms=delay,
                            min_return_pct=minimum_return,
                            min_flow_sol=flow,
                            max_single_buy_sol=max_single,
                        )
                    )
    for breakout in (5, 10, 20, 50):
        for wait in (3_000, 5_000, 10_000, 20_000):
            for max_single in (10, 30, math.inf):
                rules.append(
                    EntryRule(
                        "breakout",
                        breakout_pct=breakout,
                        max_wait_ms=wait,
                        max_single_buy_sol=max_single,
                    )
                )
    for drawdown in (10, 20, 30, 50):
        for rebound in (3, 5, 10, 20):
            for wait in (10_000, 20_000, 40_000):
                rules.append(
                    EntryRule(
                        "dip_rebound",
                        drawdown_pct=drawdown,
                        rebound_pct=rebound,
                        max_wait_ms=wait,
                    )
                )
    for delay in (400, 1_000, 2_000, 5_000):
        rules.append(EntryRule("post_self_pump", delay_ms=delay))
    return rules


def exit_rules() -> list[ExitRule]:
    return [
        ExitRule(horizon, take_profit, stop_loss)
        for horizon in (5_000, 10_000, 20_000, 40_000, 60_000)
        for take_profit in (5, 10, 20, math.inf)
        for stop_loss in (10, 20, 35, math.inf)
    ]


def summarize(trades: list[Trade]) -> dict[str, Any]:
    values = [trade.net_pct for trade in trades]
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    mean = statistics.fmean(values)
    std = statistics.stdev(values) if len(values) > 1 else 0.0
    return {
        "n": len(values),
        "win_pct": 100 * sum(value > 0 for value in values) / len(values),
        "mean_pct": mean,
        "median_pct": statistics.median(values),
        "se_pct": std / math.sqrt(len(values)),
        "p05_pct": ordered[max(0, math.ceil(0.05 * len(ordered)) - 1)],
        "total_sol": sum(trade.net_sol for trade in trades),
        "take_profit_pct": 100
        * sum(trade.exit_reason == "TAKE_PROFIT" for trade in trades)
        / len(trades),
    }


def bootstrap_ci(values: list[float], seed: int = 7, samples: int = 2_000) -> tuple[float, float]:
    if not values:
        return (math.nan, math.nan)
    randomizer = random.Random(seed)
    means = sorted(
        statistics.fmean(randomizer.choice(values) for _ in values) for _ in range(samples)
    )
    return means[int(0.025 * samples)], means[int(0.975 * samples)]


def evaluate(
    paths: Iterable[PoolPath],
    entry_rule: EntryRule,
    exit_rule: ExitRule,
    size_sol: float,
    fee_bps: float,
    tx_cost_sol: float,
) -> list[Trade]:
    trades: list[Trade] = []
    for path in paths:
        index = entry_index(path, entry_rule)
        if index is not None:
            trades.append(simulate(path, index, exit_rule, size_sol, fee_bps, tx_cost_sol))
    return trades


def candidate_rows(
    paths: list[PoolPath],
    size_sol: float,
    fee_bps: float,
    tx_cost_sol: float,
    minimum_trades: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    exits = exit_rules()
    trade_cache: dict[tuple[str, int, ExitRule], Trade] = {}

    def cached_trade(path: PoolPath, index: int, exit_rule: ExitRule) -> Trade:
        key = (path.mint, index, exit_rule)
        trade = trade_cache.get(key)
        if trade is None:
            trade = simulate(path, index, exit_rule, size_sol, fee_bps, tx_cost_sol)
            trade_cache[key] = trade
        return trade

    for entry_rule in entry_rules():
        indices = [entry_index(path, entry_rule) for path in paths]
        selected = [
            (path, indices[position])
            for position, path in enumerate(paths)
            if indices[position] is not None
        ]
        if len(selected) < minimum_trades:
            continue
        for exit_rule in exits:
            trades = [
                cached_trade(path, index, exit_rule)
                for path, index in selected
            ]
            stats = summarize(trades)
            rows.append(
                {
                    "entry": entry_rule,
                    "exit": exit_rule,
                    "score": stats["mean_pct"] - stats["se_pct"],
                    "stats": stats,
                }
            )
    return sorted(rows, key=lambda row: row["score"], reverse=True)


def representative_by_family(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
    for row in rows:
        family = row["entry"].family
        if family not in seen:
            seen.add(family)
            selected.append(row)
    return selected


def markdown_report(result: dict[str, Any]) -> str:
    universe = result["universe"]
    lines = [
        "# PumpSwap adjacent-idea replay",
        "",
        f"Generated from {universe['all_paths']} WSOL paths; normal pools only: "
        f"{universe['normal_paths']} ({universe['train']} train / "
        f"{universe['test']} chronological test).",
        f"Excluded {universe['truncated_paths']} paths that hit the RPC pagination cap.",
        f"Position {result['config']['size_sol']:.3f} SOL, "
        f"{result['config']['fee_bps']:.0f} bps per side, "
        f"{result['config']['tx_cost_sol']:.4f} SOL round-trip transaction/tip cost.",
        "",
        "The entry and exit parameters were selected on the training prefix. The figures below are "
        "from the untouched chronological test suffix.",
        "",
        "| family | test n | win % | mean % | median % | SE % | 95% bootstrap CI | p05 % |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    selected_rules: list[str] = []
    for row in result["family_results"]:
        stats = row["test"]
        low, high = row["bootstrap_ci"]
        lines.append(
            f"| {row['family']} | {stats.get('n', 0)} | {stats.get('win_pct', math.nan):.1f} | "
            f"{stats.get('mean_pct', math.nan):+.2f} | {stats.get('median_pct', math.nan):+.2f} | "
            f"{stats.get('se_pct', math.nan):.2f} | {low:+.2f}..{high:+.2f} | "
            f"{stats.get('p05_pct', math.nan):+.2f} |"
        )
        selected_rules.append(
            f"- `{row['family']}`: entry `{row['entry']}`; exit `{row['exit']}`."
        )
    lines.extend(["", "## Selected configurations", "", *selected_rules, ""])
    winner = result["overall_winner"]
    lines.extend(
        [
            "## Verdict",
            "",
            result["verdict"],
            "",
            f"The train-selected overall winner was `{winner['entry']}` with `{winner['exit']}`. "
            f"Its test mean was {winner['test'].get('mean_pct', math.nan):+.2f}% over "
            f"{winner['test'].get('n', 0)} trades.",
            "",
            "## Dataset checks",
            "",
            f"Self-pump paths: {universe['self_pump']}; mayhem paths excluded from "
            "model selection: "
            f"{universe['mayhem_paths']}; callers: {universe['callers']}.",
            "",
            "Caveats: one short regime; only observed post-transaction reserve states; no model "
            "of how other traders react to our order; slot time is approximated as 400 ms; "
            "non-WSOL and "
            "mayhem pools are excluded from the primary screen.",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    parser.add_argument("--size-sol", type=float, default=0.5)
    parser.add_argument("--fee-bps", type=float, default=125.0)
    parser.add_argument("--tx-cost-sol", type=float, default=0.0011)
    parser.add_argument("--train-fraction", type=float, default=0.67)
    parser.add_argument("--minimum-trades", type=int, default=30)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with gzip.open(args.input, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    truncated = [
        row
        for row in payload["records"]
        if row.get("status") == "ok"
        and (
            row.get("truncated") is True
            or ("truncated" not in row and int(row.get("rpc_pages") or 0) >= 50)
        )
    ]
    truncated_mints = {str(row.get("mint")) for row in truncated}
    usable = [
        row for row in payload["records"] if str(row.get("mint")) not in truncated_mints
    ]
    paths = [path for row in usable if (path := build_path(row)) is not None]
    paths.sort(key=lambda path: (path.timestamp_ms, path.mint))
    normal = [path for path in paths if not path.mayhem]
    split = max(1, min(len(normal) - 1, round(len(normal) * args.train_fraction)))
    train, test = normal[:split], normal[split:]
    rows = candidate_rows(
        train,
        args.size_sol,
        args.fee_bps,
        args.tx_cost_sol,
        args.minimum_trades,
    )
    if not rows:
        raise SystemExit("no candidate reached --minimum-trades")
    family_results: list[dict[str, Any]] = []
    for row in representative_by_family(rows):
        test_trades = evaluate(
            test,
            row["entry"],
            row["exit"],
            args.size_sol,
            args.fee_bps,
            args.tx_cost_sol,
        )
        test_stats = summarize(test_trades)
        family_results.append(
            {
                "family": row["entry"].family,
                "entry": row["entry"].name,
                "exit": row["exit"].name,
                "train": row["stats"],
                "test": test_stats,
                "bootstrap_ci": bootstrap_ci([trade.net_pct for trade in test_trades]),
            }
        )
    overall = rows[0]
    overall_test_trades = evaluate(
        test,
        overall["entry"],
        overall["exit"],
        args.size_sol,
        args.fee_bps,
        args.tx_cost_sol,
    )
    overall_stats = summarize(overall_test_trades)
    overall_ci = bootstrap_ci([trade.net_pct for trade in overall_test_trades])
    positive = any(
        row["test"].get("n", 0) >= 10
        and row["test"].get("mean_pct", -math.inf) > 0
        and row["bootstrap_ci"][0] > 0
        for row in family_results
    )
    verdict = (
        "A delayed long entry survived out of sample with a positive 95% bootstrap interval. It is "
        "a research lead, not a live-ready edge; collect another independent day before "
        "implementation."
        if positive
        else "No long-only post-migration rule survived out of sample with a positive 95% "
        "bootstrap "
        "interval. Keep migration trading shelved; retain the replay pipeline for new hypotheses."
    )
    result = {
        "config": {
            "size_sol": args.size_sol,
            "fee_bps": args.fee_bps,
            "tx_cost_sol": args.tx_cost_sol,
            "train_fraction": args.train_fraction,
            "minimum_trades": args.minimum_trades,
        },
        "universe": {
            "all_paths": len(paths),
            "truncated_paths": len(truncated),
            "normal_paths": len(normal),
            "mayhem_paths": sum(path.mayhem for path in paths),
            "self_pump": sum(path.self_pump for path in paths),
            "train": len(train),
            "test": len(test),
            "callers": len({path.caller for path in paths}),
            "top_callers": Counter(path.caller for path in paths).most_common(10),
        },
        "searched_candidates": len(rows),
        "family_results": family_results,
        "overall_winner": {
            "entry": overall["entry"].name,
            "exit": overall["exit"].name,
            "train": overall["stats"],
            "test": overall_stats,
            "bootstrap_ci": overall_ci,
        },
        "verdict": verdict,
    }
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.report_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    report = markdown_report(result)
    args.report_output.write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
