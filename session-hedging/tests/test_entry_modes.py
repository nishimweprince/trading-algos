from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engine import ClosedBarEngine
from models import Candle, EngineParams, Timeframe
from sessions import build_windows

FIXTURES = Path(__file__).parent / "fixtures"
PHASE1_BARS = FIXTURES / "xauusd_m15.jsonl"
HEDGE_PAIR_GOLDEN = FIXTURES / "phase1_hedge_pair_golden.json"


def _phase1_bars() -> list[Candle]:
    return [
        Candle.model_validate_json(line)
        for line in PHASE1_BARS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _parity_engine() -> ClosedBarEngine:
    params = EngineParams(
        entry_mode="hedge_pair",
        stop_mode="bar_range",
        cost_model="none",
        risk_mode="fixed_qty",
        intrabar_mode="optimistic",
        time_exit_mode="none",
        qty=1,
        qty_ref=1,
        timeframe_minutes=15,
        orb_minutes=60,
        entry_delay_minutes=15,
        anchor_tolerance_minutes=15,
        one_open_per_session=False,
        max_concurrent_structures=0,
        max_open_risk_pct=0,
    )
    return ClosedBarEngine(build_windows(["tokyo", "london", "new_york"], {}), params)


def _phase1_payload(engine: ClosedBarEngine) -> dict[str, object]:
    report = engine.report("XAUUSD", Timeframe.M15, "local").model_dump(mode="json")
    # ENTRY_MODE did not exist in the captured Phase 1 report. Everything else below is the
    # complete pre-refactor payload, including ordered trades/events and grouped pair ordering.
    report.pop("entry_mode")
    report.pop("pending_entry_orders")
    report.pop("unresolved_structures")
    trades = report.pop("trades")
    events = report.pop("events")
    pairs = report.pop("trade_pairs")
    for trade in trades:
        for field in (
            "qty",
            "episode",
            "entry_fills",
            "execution_cost_pips",
            "financing_cost_pips",
        ):
            trade.pop(field)
    for pair in pairs:
        for leg in [pair.get("primary"), pair.get("hedge"), *pair["unknown_legs"]]:
            if leg is not None:
                leg.pop("qty")
    return {
        "stats": engine.stats.model_dump(mode="json"),
        "trades": trades,
        "events": events,
        "trade_pairs": pairs,
        "report": report,
    }


def test_hedge_pair_matches_phase1_golden_bit_for_bit() -> None:
    golden = json.loads(HEDGE_PAIR_GOLDEN.read_text(encoding="utf-8"))
    engine = _parity_engine()
    engine.run(_phase1_bars())
    payload = _phase1_payload(engine)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    assert hashlib.sha256(canonical).hexdigest() == golden["canonical_sha256"]
    assert engine.stats.model_dump(mode="json") == golden["stats"]
    assert [pair["id"] for pair in payload["trade_pairs"]] == golden["pair_ids"]  # type: ignore[index]
    assert [
        [trade["pair_id"], trade["side"], trade["ts"]]
        for trade in payload["trades"]  # type: ignore[union-attr]
    ] == golden["trade_order"]
    assert [event["kind"] for event in payload["events"]] == golden["event_kinds"]  # type: ignore[union-attr]


def _mode_engine(
    mode: str, *, spread: float = 0.0, **overrides: object
) -> ClosedBarEngine:
    params = EngineParams.model_validate(
        EngineParams(
        entry_mode=mode,
        stop_mode="fixed_pips",
        fixed_stop_pips=10,
        pip_size=1,
        rr=3,
        lock_pips=2,
        cost_model="per_session",
        spread_pips_per_side=spread,
        intrabar_mode="optimistic",
        time_exit_mode="none",
        timeframe_minutes=15,
        orb_minutes=15,
        one_open_per_session=False,
        max_concurrent_structures=0,
        max_open_risk_pct=0,
        ).model_dump()
        | overrides
    )
    return ClosedBarEngine(build_windows(["new_york"], {}), params)


def _bar(ts: datetime, *, o: float, h: float, low: float, c: float) -> Candle:
    return Candle(
        ts=ts,
        open=o,
        high=h,
        low=low,
        close=c,
        volume=1,
        provider="test",
        source_instrument="XAUUSD",
    )


def test_synthetic_payoff_matches_hedge_after_first_stop() -> None:
    entry_ts = datetime(2026, 1, 14, 13, 0, tzinfo=UTC)
    hedge = _mode_engine("hedge_pair")
    assert hedge._open_pair("new_york", 100, 1, entry_ts, True)
    hedge._manage_pairs(
        _bar(entry_ts + timedelta(minutes=15), o=100, h=110, low=100, c=109)
    )
    hedge._manage_pairs(
        _bar(entry_ts + timedelta(minutes=30), o=110, h=130, low=109, c=130)
    )

    synthetic = _mode_engine("synthetic_breakout")
    assert synthetic._stage_synthetic_order("new_york", 100, 1, entry_ts, True)
    synthetic._fill_entry_orders(
        _bar(entry_ts + timedelta(minutes=15), o=100, h=110, low=103, c=109)
    )
    synthetic._manage_pairs(
        _bar(entry_ts + timedelta(minutes=30), o=110, h=130, low=109, c=130)
    )

    hedge_report = hedge.report("XAUUSD", Timeframe.M15, "local")
    synthetic_report = synthetic.report("XAUUSD", Timeframe.M15, "local")
    assert hedge_report.gross_realized_pips == pytest.approx(20)
    assert synthetic_report.gross_realized_pips == pytest.approx(20)
    assert hedge_report.gross_realized_r == pytest.approx(2)
    assert synthetic_report.gross_realized_r == pytest.approx(2)


def test_synthetic_charges_half_the_transaction_sides() -> None:
    entry_ts = datetime(2026, 1, 14, 13, 0, tzinfo=UTC)
    hedge = _mode_engine("hedge_pair", spread=1)
    hedge._open_pair("new_york", 100, 1, entry_ts, True)
    hedge._manage_pairs(
        _bar(entry_ts + timedelta(minutes=15), o=100, h=110, low=100, c=109)
    )
    hedge._manage_pairs(
        _bar(entry_ts + timedelta(minutes=30), o=110, h=130, low=109, c=130)
    )

    synthetic = _mode_engine("synthetic_breakout", spread=1)
    synthetic._stage_synthetic_order("new_york", 100, 1, entry_ts, True)
    synthetic._fill_entry_orders(
        _bar(entry_ts + timedelta(minutes=15), o=100, h=110, low=103, c=109)
    )
    synthetic._manage_pairs(
        _bar(entry_ts + timedelta(minutes=30), o=110, h=130, low=109, c=130)
    )
    hedge_report = hedge.report("XAUUSD", Timeframe.M15, "local")
    synthetic_report = synthetic.report("XAUUSD", Timeframe.M15, "local")
    assert hedge_report.transaction_sides == 4
    assert synthetic_report.transaction_sides == 2
    assert hedge_report.execution_cost_pips == pytest.approx(4)
    assert synthetic_report.execution_cost_pips == pytest.approx(2)


def test_synthetic_gap_through_trigger_fills_at_open() -> None:
    entry_ts = datetime(2026, 1, 14, 13, 0, tzinfo=UTC)
    engine = _mode_engine("synthetic_breakout")
    engine._stage_synthetic_order("new_york", 100, 1, entry_ts, True)
    engine._fill_entry_orders(
        _bar(entry_ts + timedelta(minutes=15), o=115, h=116, low=114, c=115)
    )
    assert len(engine.pairs) == 1
    assert engine.pairs[0].long_entry == pytest.approx(115)
    assert engine.pairs[0].long_sl == pytest.approx(102)
    assert engine.pairs[0].long_tp == pytest.approx(130)
    assert engine.pairs[0].entry_gap is True


def test_synthetic_entry_bar_uses_resolver_after_trigger() -> None:
    entry_ts = datetime(2026, 1, 14, 12, 45, tzinfo=UTC)
    optimistic = _mode_engine("synthetic_breakout")
    optimistic._stage_synthetic_order("new_york", 100, 1, entry_ts, True)
    trigger_bar = _bar(
        entry_ts + timedelta(minutes=15), o=100, h=110, low=100, c=109
    )
    optimistic.step(trigger_bar)
    assert optimistic.pairs[0].long_open is True

    pessimistic_params = optimistic.params.model_copy(update={"intrabar_mode": "pessimistic"})
    pessimistic = ClosedBarEngine(build_windows(["new_york"], {}), pessimistic_params)
    pessimistic._stage_synthetic_order("new_york", 100, 1, entry_ts, True)
    pessimistic.step(trigger_bar)
    assert pessimistic.pairs[0].long_open is False
    assert pessimistic.trades[0].exit == pytest.approx(102)


def test_synthetic_pending_order_survives_snapshot_restore() -> None:
    entry_ts = datetime(2026, 1, 14, 13, 0, tzinfo=UTC)
    engine = _mode_engine("synthetic_breakout")
    engine._stage_synthetic_order("new_york", 100, 1, entry_ts, True)
    restored = _mode_engine("synthetic_breakout")
    restored.restore(engine.snapshot())
    assert restored.open_entry_order_views() == engine.open_entry_order_views()


def _run_breakout_winner(engine: ClosedBarEngine) -> None:
    entry_ts = datetime(2026, 1, 14, 12, 45, tzinfo=UTC)
    engine._stage_synthetic_order("new_york", 100, 1, entry_ts, True)
    engine._fill_entry_orders(
        _bar(entry_ts + timedelta(minutes=15), o=100, h=110, low=103, c=109)
    )
    engine._manage_pairs(
        _bar(entry_ts + timedelta(minutes=30), o=110, h=130, low=109, c=130)
    )


def _financial_signature(engine: ClosedBarEngine) -> tuple[object, ...]:
    report = engine.report("XAUUSD", Timeframe.M15, "local")
    return (
        report.gross_realized_pips,
        report.net_realized_pips,
        report.gross_realized_r,
        report.net_realized_r,
        report.execution_cost_pips,
        report.financing_cost_pips,
        report.transaction_sides,
        [(leg.side, leg.entry, leg.exit, leg.pnl_pips, leg.qty) for leg in report.trades],
    )


def test_contingent_ratio_zero_equals_synthetic() -> None:
    synthetic = _mode_engine("synthetic_breakout", spread=1)
    contingent = _mode_engine(
        "contingent_hedge",
        spread=1,
        hedge_ratio_initial=0,
        hedge_ratio_staged=0,
    )
    _run_breakout_winner(synthetic)
    _run_breakout_winner(contingent)
    assert _financial_signature(contingent) == _financial_signature(synthetic)


def test_contingent_ratio_one_at_entry_equals_hedge_pair() -> None:
    entry_ts = datetime(2026, 1, 14, 12, 45, tzinfo=UTC)
    hedge = _mode_engine("hedge_pair", spread=1)
    contingent = _mode_engine(
        "contingent_hedge",
        spread=1,
        hedge_ratio_initial=1,
        hedge_ratio_staged=1,
    )
    for engine in (hedge, contingent):
        engine._open_pair("new_york", 100, 1, entry_ts, True)
        engine._manage_pairs(
            _bar(entry_ts + timedelta(minutes=15), o=100, h=110, low=100, c=109)
        )
        engine._manage_pairs(
            _bar(entry_ts + timedelta(minutes=30), o=110, h=130, low=109, c=130)
        )
    assert _financial_signature(contingent) == _financial_signature(hedge)


@pytest.mark.parametrize("staged_ratio", [0.5, 1.0])
def test_hedge_stages_on_failure_zone_entry(staged_ratio: float) -> None:
    entry_ts = datetime(2026, 1, 14, 12, 45, tzinfo=UTC)
    engine = _mode_engine(
        "contingent_hedge",
        hedge_ratio_initial=0,
        hedge_ratio_staged=staged_ratio,
        hedge_failure_k=0.5,
    )
    engine._stage_synthetic_order("new_york", 100, 1, entry_ts, True)
    engine._fill_entry_orders(
        _bar(entry_ts + timedelta(minutes=15), o=100, h=110, low=106, c=109)
    )
    engine._stage_contingent_hedges(
        _bar(entry_ts + timedelta(minutes=30), o=109, h=109, low=104, c=105)
    )
    pair = engine.pairs[0]
    assert pair.hedge_failure_threshold == pytest.approx(105)
    assert pair.short_open is True
    assert pair.short_entry == pytest.approx(105)
    assert pair.short_qty == pytest.approx(staged_ratio)
    assert pair.short_sl == pytest.approx(110)
    assert pair.short_tp == pytest.approx(70)
    assert engine.events[-1].kind == "hedge_staged"


def test_contingent_half_initial_ratio_scales_primary_and_counts_fills() -> None:
    entry_ts = datetime(2026, 1, 14, 12, 45, tzinfo=UTC)
    engine = _mode_engine(
        "contingent_hedge",
        spread=1,
        hedge_ratio_initial=0.5,
        hedge_ratio_staged=1,
    )
    engine._stage_fractional_contingent("new_york", 100, 1, entry_ts, True)
    pair = engine.pairs[0]
    assert pair.long_qty == pair.short_qty == pytest.approx(0.5)
    engine._fill_entry_orders(
        _bar(entry_ts + timedelta(minutes=15), o=100, h=110, low=106, c=109)
    )
    assert pair.primary_side == "long"
    assert pair.short_open is False
    assert pair.long_qty == pytest.approx(1)
    assert pair.long_entry == pytest.approx(105)
    assert pair.long_entry_fills == 2
    assert engine.trades[0].qty == pytest.approx(0.5)
    report = engine.report("XAUUSD", Timeframe.M15, "local")
    assert report.transaction_sides == 4
    assert report.cost_side_equivalents == pytest.approx(2)


def test_contingent_reopened_hedge_preserves_fill_episodes_and_costs() -> None:
    entry_ts = datetime(2026, 1, 14, 12, 45, tzinfo=UTC)
    engine = _mode_engine(
        "contingent_hedge",
        spread=1,
        hedge_ratio_initial=0.5,
        hedge_ratio_staged=1,
    )
    engine._stage_fractional_contingent("new_york", 100, 1, entry_ts, True)
    engine._fill_entry_orders(
        _bar(entry_ts + timedelta(minutes=15), o=100, h=110, low=106, c=109)
    )
    engine._stage_contingent_hedges(
        _bar(entry_ts + timedelta(minutes=30), o=109, h=109, low=104, c=105)
    )
    engine._manage_pairs(
        _bar(entry_ts + timedelta(minutes=45), o=105, h=110, low=105, c=109)
    )
    engine._manage_pairs(
        _bar(entry_ts + timedelta(minutes=60), o=110, h=130, low=109, c=130)
    )
    report = engine.report("XAUUSD", Timeframe.M15, "local")
    assert [(leg.side, leg.episode, leg.qty) for leg in report.trades] == [
        ("short", 0, 0.5),
        ("short", 1, 1.0),
        ("long", 0, 1.0),
    ]
    assert report.transaction_sides == 7
    assert report.cost_side_equivalents == pytest.approx(5)
    assert report.execution_cost_pips == pytest.approx(5)
    assert len(report.trade_pairs[0].unknown_legs) == 1


def test_staged_contingent_hedge_survives_snapshot_restore() -> None:
    entry_ts = datetime(2026, 1, 14, 12, 45, tzinfo=UTC)
    engine = _mode_engine(
        "contingent_hedge", hedge_ratio_initial=0, hedge_ratio_staged=0.5
    )
    engine._stage_synthetic_order("new_york", 100, 1, entry_ts, True)
    engine._fill_entry_orders(
        _bar(entry_ts + timedelta(minutes=15), o=100, h=110, low=106, c=109)
    )
    engine._stage_contingent_hedges(
        _bar(entry_ts + timedelta(minutes=30), o=109, h=109, low=104, c=105)
    )
    restored = _mode_engine(
        "contingent_hedge", hedge_ratio_initial=0, hedge_ratio_staged=0.5
    )
    restored.restore(engine.snapshot())
    assert restored.snapshot()["pairs"] == engine.snapshot()["pairs"]
