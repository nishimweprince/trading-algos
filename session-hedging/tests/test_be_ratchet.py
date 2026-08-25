"""MFE-armed breakeven ratchet for single-sided (OCO bracket) legs.

The incumbent lock only fires when one leg of a hedge pair is stopped and the other
survives. An OCO bracket fills one side and cancels the sibling, so that path is
unreachable and ``LOCK_PIPS`` never applied. These tests pin the replacement.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from fills import resolve_bar_levels_ratchet
from models import Candle, EngineParams, IntrabarMode


def _bar(ts: datetime, *, o: float, h: float, low: float, c: float) -> Candle:
    return Candle(
        ts=ts,
        open=o,
        high=h,
        low=low,
        close=c,
        volume=1.0,
        provider="test",
        source_instrument="XAUUSD",
    )


def _m1(start: datetime, specs: list[tuple[float, float, float, float]]) -> list[Candle]:
    return [
        _bar(start + timedelta(minutes=i + 1), o=o, h=h, low=low, c=c)
        for i, (o, h, low, c) in enumerate(specs)
    ]


def _resolve(**kwargs: object):
    base = dict(
        mode=IntrabarMode.M1_CONSERVATIVE,
        is_long=True,
        stop=99.0,
        tp=106.0,
        arm_level=101.5,
        ratchet_stop=100.2,
        armed=False,
        m1_bars=None,
        parent_minutes=5,
    )
    base.update(kwargs)
    return resolve_bar_levels_ratchet(**base)  # type: ignore[arg-type]


class TestArming:
    def test_unarmed_bar_keeps_original_stop(self) -> None:
        bar = _bar(datetime(2026, 1, 5, 12, 5, tzinfo=UTC), o=100, h=101.0, low=99.5, c=100.5)
        hit = _resolve(bar=bar)
        assert hit.kind == "none"
        assert hit.armed is False
        assert hit.stop == pytest.approx(99.0)

    def test_touching_arm_level_moves_the_stop(self) -> None:
        bar = _bar(datetime(2026, 1, 5, 12, 5, tzinfo=UTC), o=100, h=101.5, low=99.5, c=101.0)
        hit = _resolve(bar=bar)
        assert hit.armed is True
        assert hit.stop == pytest.approx(100.2)

    def test_armed_state_carries_in(self) -> None:
        bar = _bar(datetime(2026, 1, 5, 12, 5, tzinfo=UTC), o=100.3, h=100.5, low=100.1, c=100.3)
        hit = _resolve(bar=bar, stop=100.2, armed=True)
        assert hit.kind == "stop"
        assert hit.fill == pytest.approx(100.2)

    def test_short_side_arms_on_the_low(self) -> None:
        bar = _bar(datetime(2026, 1, 5, 12, 5, tzinfo=UTC), o=100, h=100.5, low=98.5, c=99.0)
        hit = _resolve(
            bar=bar, is_long=False, stop=101.0, tp=94.0, arm_level=98.5, ratchet_stop=99.8
        )
        assert hit.armed is True
        assert hit.stop == pytest.approx(99.8)


class TestNoLookAhead:
    """The bar that arms the ratchet must not also be filled at the armed stop."""

    def test_without_m1_the_arming_bar_keeps_the_original_stop(self) -> None:
        # Runs to the arm level and collapses back through the ratchet stop in one bar.
        bar = _bar(datetime(2026, 1, 5, 12, 5, tzinfo=UTC), o=100, h=101.5, low=99.6, c=99.7)
        hit = _resolve(bar=bar)
        assert hit.kind == "none", "arming bar must not fill at a stop it just created"
        assert hit.armed is True
        assert hit.stop == pytest.approx(100.2)

    def test_with_m1_the_ratchet_fills_only_after_the_arming_child(self) -> None:
        start = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        parent = _bar(start + timedelta(minutes=5), o=100, h=101.5, low=99.6, c=99.7)
        children = _m1(
            start,
            [
                (100.0, 100.4, 99.9, 100.3),
                (100.3, 101.5, 100.2, 101.4),  # arms here
                (101.4, 101.4, 99.6, 99.7),  # ratchet stop now in force
                (99.7, 99.8, 99.6, 99.7),
            ],
        )
        hit = resolve_bar_levels_ratchet(
            mode=IntrabarMode.M1_CONSERVATIVE,
            is_long=True,
            bar=parent,
            stop=99.0,
            tp=106.0,
            arm_level=101.5,
            ratchet_stop=100.2,
            armed=False,
            m1_bars=children,
            parent_minutes=5,
        )
        assert hit.kind == "stop"
        assert hit.fill == pytest.approx(100.2)
        assert hit.armed is True

    def test_stop_before_arming_child_uses_the_original_stop(self) -> None:
        start = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        parent = _bar(start + timedelta(minutes=5), o=100, h=101.5, low=98.9, c=101.0)
        children = _m1(
            start,
            [
                (100.0, 100.2, 98.9, 99.0),  # original stop hit first
                (99.0, 101.5, 99.0, 101.4),
                (101.4, 101.5, 101.0, 101.2),
                (101.2, 101.3, 101.0, 101.1),
            ],
        )
        hit = resolve_bar_levels_ratchet(
            mode=IntrabarMode.M1_CONSERVATIVE,
            is_long=True,
            bar=parent,
            stop=99.0,
            tp=106.0,
            arm_level=101.5,
            ratchet_stop=100.2,
            armed=False,
            m1_bars=children,
            parent_minutes=5,
        )
        assert hit.kind == "stop"
        assert hit.fill == pytest.approx(99.0)
        assert hit.armed is False


class TestTargetStillWins:
    def test_target_reached_before_reversal_still_pays_full(self) -> None:
        start = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        parent = _bar(start + timedelta(minutes=5), o=100, h=106.0, low=99.8, c=105.9)
        children = _m1(
            start,
            [
                (100.0, 101.5, 99.9, 101.4),  # arms
                (101.4, 106.0, 101.3, 105.9),  # target
                (105.9, 106.0, 105.5, 105.8),
                (105.8, 105.9, 105.5, 105.7),
            ],
        )
        hit = resolve_bar_levels_ratchet(
            mode=IntrabarMode.M1_CONSERVATIVE,
            is_long=True,
            bar=parent,
            stop=99.0,
            tp=106.0,
            arm_level=101.5,
            ratchet_stop=100.2,
            armed=False,
            m1_bars=children,
            parent_minutes=5,
        )
        assert hit.kind == "tp"
        assert hit.fill == pytest.approx(106.0)


class TestConfig:
    def test_ratchet_is_off_by_default(self) -> None:
        assert EngineParams().be_trigger_r == 0.0

    def test_ratchet_requires_a_lock_distance(self) -> None:
        with pytest.raises(ValidationError, match="LOCK_PIPS"):
            EngineParams(be_trigger_r=0.75, lock_mode="absolute", lock_pips=0)
        with pytest.raises(ValidationError, match="LOCK_MODE other than"):
            EngineParams(be_trigger_r=0.75, lock_mode="none")

    def test_breakeven_lock_mode_is_allowed(self) -> None:
        assert EngineParams(be_trigger_r=0.75, lock_mode="breakeven").be_trigger_r == 0.75


class TestOcoEndToEnd:
    """The behaviour the incumbent lock could not reach: an OCO leg protecting itself."""

    @staticmethod
    def _engine(**overrides: object):
        from engine import ClosedBarEngine
        from sessions import build_windows

        params = EngineParams.model_validate(
            EngineParams(
                entry_mode="oco_bracket",
                stop_mode="fixed_pips",
                fixed_stop_pips=10,
                pip_size=1,
                rr=3,
                lock_pips=2,
                lock_mode="absolute",
                cost_model="none",
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

    def _run(self, engine, bars: list[tuple[float, float, float, float]]):
        entry_ts = datetime(2026, 1, 14, 12, 45, tzinfo=UTC)
        assert engine._stage_oco_bracket(
            session="new_york",
            entry=100,
            range_price=10,
            range_high=105,
            range_low=95,
            ts=entry_ts,
            bullish=True,
        )
        # Fills long at 106; stop 96, target 136, so 1R = 10 and +0.75R = 113.5.
        engine._fill_entry_orders(
            _bar(entry_ts + timedelta(minutes=15), o=100, h=106, low=99, c=105)
        )
        for i, (o, h, low, c) in enumerate(bars, start=2):
            bar = _bar(entry_ts + timedelta(minutes=15 * i), o=o, h=h, low=low, c=c)
            engine._record_excursions(bar)
            engine._manage_pairs(bar)
        return engine.pairs[0]

    def test_incumbent_lock_never_fires_on_an_oco_leg(self) -> None:
        """Documents the defect: pair.locked is True at fill, so _apply_lock is dead."""
        engine = self._engine()
        pair = self._run(engine, [(106, 114, 105, 113), (113, 113, 95, 96)])
        assert pair.locked is True
        assert not any(event.kind == "lock" for event in engine.events)
        assert pair.long_sl == pytest.approx(96), "stop never moved"

    def test_ratchet_converts_a_giveback_loss_into_a_locked_win(self) -> None:
        engine = self._engine(be_trigger_r=0.75)
        # Bar 1 runs to 114 (past +0.75R = 113.5) and arms. Bar 2 collapses to 95.
        pair = self._run(engine, [(106, 114, 105, 113), (113, 113, 95, 96)])
        assert pair.long_be_armed is True
        assert pair.long_sl == pytest.approx(108), "entry 106 + 2 pip lock"
        assert any(event.kind == "be_ratchet_armed" for event in engine.events)
        trade = engine.trades[-1]
        assert trade.pnl_pips == pytest.approx(2), "locked +2 instead of -10"

    def test_ratchet_does_not_arm_below_the_trigger(self) -> None:
        engine = self._engine(be_trigger_r=0.75)
        pair = self._run(engine, [(106, 112, 105, 111), (111, 111, 95, 96)])
        assert pair.long_be_armed is False
        trade = engine.trades[-1]
        assert trade.pnl_pips == pytest.approx(-10), "full stop, ratchet never armed"

    def test_ratchet_never_widens_the_stop(self) -> None:
        engine = self._engine(be_trigger_r=0.75, lock_pips=500)
        pair = self._run(engine, [(106, 114, 105, 113)])
        assert pair.long_sl <= 116.0, "clamped to entry + sl_dist"
        assert pair.long_sl >= 96.0

    def test_target_still_reachable_with_the_ratchet_on(self) -> None:
        engine = self._engine(be_trigger_r=0.75)
        self._run(engine, [(106, 114, 105, 113), (113, 136, 112, 135)])
        trade = engine.trades[-1]
        assert trade.pnl_pips == pytest.approx(30), "full 3R target still paid"


class TestShippedConfiguration:
    """Pins the configuration this project ships, and why.

    The ratchet was built because ``LOCK_PIPS`` was inert in ``oco_bracket`` mode: the
    incumbent lock only fires when one leg of a hedge pair is stopped and the other
    survives, and an OCO bracket cancels its sibling on fill.

    Recorded measurement, XAUUSD H1, 503 structures, 2024-12 to 2026-08. On 501 matched
    structures a 0.75R trigger moved the win rate 41.9% -> 57.5% while expectancy fell
    59.1 -> 41.7 pips: 92 trades helped for +19,191 pips, 85 hurt for -27,914, because
    the edge is carried by ~66 outsized winners and a breakeven stop clips them. It is
    enabled anyway by explicit instruction, trading expectancy for hit rate and
    slippage tolerance. These assertions exist so the choice stays deliberate.
    """

    def test_ratchet_is_enabled_at_the_configured_trigger(self) -> None:
        from config import Settings

        # Pin the shipped option without depending on a developer's ignored local .env.
        params = Settings(be_trigger_r=0.75, lock_pips=20.0).engine_params()
        assert params.be_trigger_r == 0.75
        assert params.lock_pips == 20.0
        assert params.lock_mode is not None

    def test_stop_and_target_defaults_are_unchanged(self) -> None:
        from config import load_settings

        params = load_settings().engine_params()
        assert params.sl_mult == 2.0, "44% of winners take >0.75R of heat; do not tighten"
        assert params.rr == 3.0, "every target below 3R measured worse"

    def test_thirteen_hundred_utc_is_excluded(self) -> None:
        """13:00 UTC is New York 09:00 EDT; 14:00 UTC is the same anchor in EST.

        Excluding 13 therefore removes New York for the daylight-saving half of the
        year rather than a time of day. Set deliberately as a cost-efficiency measure:
        that hour ran 108 structures at PF 0.99 for -244 pips.
        """
        from config import load_settings

        assert load_settings().engine_params().entry_hours_utc_exclude == [13]

    def test_costs_are_configured(self) -> None:
        from config import load_settings

        params = load_settings().engine_params()
        per_side = (
            params.spread_pips_per_side
            + params.slippage_pips_per_side
            + params.commission_pips_per_side
        )
        assert per_side > 0, "a costless backtest biases every sweep toward over-trading"
