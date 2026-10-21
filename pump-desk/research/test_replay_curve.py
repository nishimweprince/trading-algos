from __future__ import annotations

import math
import unittest

from fetch_curve_paths import TRADE_EVENT, decode_trade_event
from replay_curve import (
    STANDARD_GAP,
    Costs,
    CurvePoint,
    Path_,
    PoolPoint,
    Rule,
    completion_cost,
    curve_buy,
    simulate,
)

COSTS = Costs(size_lamports=50_000_000, buy_fee=1_000_000, sell_fee=1_000_000,
              pool_fee_bps=125, buy_land_slots=2, sell_land_slots=2)


def standard_point(slot: int, rtok: int, index: int = 0) -> CurvePoint:
    """A standard curve state with `rtok` real tokens left (k = 30 SOL x 1.073B tokens)."""
    vtok = rtok + STANDARD_GAP
    vsol = int(30 * 1e9 * 1_073_000_000_000_000 / vtok)
    return CurvePoint(slot, index, vsol, vtok, rtok, 125)


def make_path(curve: list[CurvePoint], pool: list[PoolPoint], migration_slot: int | None) -> Path_:
    complete = next((p.slot for p in curve if p.rtok == 0), None)
    return Path_("curve", 0, True, False, tuple(curve), complete, migration_slot,
                 17_584_492_770, tuple(pool))


class CurveMathTests(unittest.TestCase):
    def test_completion_cost_of_a_standard_curve_near_the_end(self) -> None:
        # About 4 SOL left when ~10.2M tokens remain (85 SOL raised at completion).
        cost = completion_cost(standard_point(0, 10_169_321_182_874))
        self.assertTrue(3.5 < cost < 4.5, cost)
        self.assertEqual(completion_cost(standard_point(0, 0)), 0.0)

    def test_buy_is_capped_at_the_remaining_tokens(self) -> None:
        point = standard_point(0, 1_000_000_000)  # ~1000 tokens left: far less than 0.05 SOL buys
        tokens, spent = curve_buy(point, 50_000_000)
        self.assertEqual(tokens, point.rtok)
        self.assertLess(spent, 50_000_000)

    def test_trade_event_round_trip(self) -> None:
        body = bytearray(225)
        body[:8] = TRADE_EVENT
        body[56] = 1
        for offset, value in ((97, 11), (105, 22), (113, 33), (121, 44), (161, 95), (209, 30)):
            body[offset:offset + 8] = value.to_bytes(8, "little")
        event = decode_trade_event(bytes(body))
        assert event is not None
        self.assertEqual((event["vsol"], event["vtok"], event["rsol"], event["rtok"]), (11, 22, 33, 44))
        self.assertEqual((event["fee_bps"], event["creator_fee_bps"]), (95, 30))
        self.assertTrue(event["is_buy"])


class SimulateTests(unittest.TestCase):
    rule = Rule(entry_cost_sol=4.0, take_profit_pct=5.0, timeout_slots=3, stall_seconds=30,
                abort_cost_sol=math.inf)

    def test_buy_fails_when_the_curve_completes_before_landing(self) -> None:
        curve = [standard_point(100, 9_500_000_000_000), standard_point(101, 0)]
        trade = simulate(make_path(curve, [], 104), self.rule, COSTS)
        assert trade is not None
        self.assertEqual(trade.outcome, "failed")
        self.assertAlmostEqual(trade.net_pct, -2.0)

    def test_pool_take_profit_after_migration(self) -> None:
        curve = [standard_point(100, 9_500_000_000_000), standard_point(102, 9_000_000_000_000),
                 standard_point(110, 0)]
        base, quote = 206_900_000_000_000, 68_400_000_000
        pool = [PoolPoint(112, 0, base, quote), PoolPoint(112, 5, base - 20_000_000_000_000, quote + 8_000_000_000),
                PoolPoint(114, 0, base - 20_000_000_000_000, quote + 8_000_000_000)]
        trade = simulate(make_path(curve, pool, 112), self.rule, COSTS)
        assert trade is not None
        self.assertEqual(trade.outcome, "pool_tp")
        self.assertGreater(trade.net_pct, 5.0)

    def test_stall_sells_back_on_the_curve(self) -> None:
        curve = [standard_point(100, 9_500_000_000_000), standard_point(102, 9_000_000_000_000),
                 standard_point(300, 9_400_000_000_000)]
        trade = simulate(make_path(curve, [], None), self.rule, COSTS)
        assert trade is not None
        self.assertEqual(trade.outcome, "stall")
        self.assertLess(trade.net_pct, 0)


if __name__ == "__main__":
    unittest.main()
