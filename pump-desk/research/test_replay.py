from __future__ import annotations

import math
import unittest

from fetch_pool_paths import canonical_pool, decode_pool
from replay import EntryRule, ExitRule, Point, PoolPath, entry_index, simulate


class FetchTests(unittest.TestCase):
    def test_canonical_pool_matches_sdk_vector(self) -> None:
        self.assertEqual(
            canonical_pool("AiJfhb3BdHsR3Scnz4XkuMj9ZAhD1HM3rnYwCEsTpump"),
            "7Adrs3Q2vFohYftLnufFMv5bYARp7EVR81YNyNG1EURK",
        )

    def test_decode_pool_rejects_short_data(self) -> None:
        self.assertIsNone(decode_pool(b"short", "owner", "address"))


def make_path(moves: list[float], flows: list[float] | None = None) -> PoolPath:
    base = 200_000_000_000_000
    initial_quote = 67_000_000_000
    virtual = 18_000_000_000
    points = []
    previous_quote = initial_quote
    max_buy = 0.0
    for index, move in enumerate(moves):
        effective_quote = int((initial_quote + virtual) * (1 + move / 100))
        quote = effective_quote - virtual
        if flows is not None:
            quote = initial_quote + int(flows[index] * 1e9)
            effective_quote = quote + virtual
        change = (quote - previous_quote) / 1e9 if index else 0.0
        max_buy = max(max_buy, change)
        points.append(
            Point(
                time_ms=index * 1_000,
                price=effective_quote / 1e9 / (base / 1e6),
                base=base,
                quote=quote,
                quote_change_sol=change,
                max_single_quote_in_sol=max_buy,
            )
        )
        previous_quote = quote
    return PoolPath("mint", "caller", 0, False, virtual, tuple(points), False)


class ReplayTests(unittest.TestCase):
    def test_delay_momentum_requires_return_and_flow(self) -> None:
        path = make_path([0, 2, 8, 12], [0, 0.5, 3, 5])
        rule = EntryRule(
            "delay_momentum", delay_ms=2_000, min_return_pct=3, min_flow_sol=2
        )
        self.assertEqual(entry_index(path, rule), 2)

    def test_delay_momentum_keeps_watching_after_delay(self) -> None:
        path = make_path([0, 1, 2, 12], [0, 0.5, 1, 5])
        rule = EntryRule(
            "delay_momentum", delay_ms=1_000, min_return_pct=3, min_flow_sol=2
        )
        self.assertEqual(entry_index(path, rule), 3)

    def test_dip_rebound_enters_only_after_rebound(self) -> None:
        path = make_path([0, -5, -25, -22, -10])
        rule = EntryRule(
            "dip_rebound", drawdown_pct=20, rebound_pct=10, max_wait_ms=10_000
        )
        self.assertEqual(entry_index(path, rule), 4)

    def test_flat_round_trip_loses_fees_tip_and_impact(self) -> None:
        path = make_path([0, 0, 0, 0])
        trade = simulate(path, 0, ExitRule(3_000, math.inf, math.inf), 0.5, 125, 0.0011)
        self.assertLess(trade.net_pct, -2.5)
        self.assertGreater(trade.net_pct, -5.0)

    def test_take_profit_fires(self) -> None:
        path = make_path([0, 5, 15, 25])
        trade = simulate(path, 0, ExitRule(5_000, 10, math.inf), 0.5, 125, 0.0011)
        self.assertEqual(trade.exit_reason, "TAKE_PROFIT")
        self.assertGreater(trade.net_pct, 0)


if __name__ == "__main__":
    unittest.main()
