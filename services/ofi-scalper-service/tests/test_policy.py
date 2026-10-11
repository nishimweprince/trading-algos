"""The entry/exit state machine (plan §1.5): pure inputs in, actions out."""

from __future__ import annotations

from decimal import Decimal

import pytest

from ofi_scalper_service.policy import (
    Cancel,
    Close,
    Context,
    Cycle,
    Filters,
    Halt,
    Phase,
    Place,
    PolicyParams,
    Scores,
    SymbolPolicy,
    kelly_fraction,
    kelly_size,
)

T0 = 1_790_000_000_000_000_000
MS = 1_000_000
S = 1_000_000_000
PARAMS = PolicyParams(
    horizon_s=5, threshold=0.6, barrier_bp=8, buffer_bp=1, entry_timeout_ms=1000, time_stop_mult=2
)
BTC = Filters(Decimal("0.1"), Decimal("0.001"), Decimal("0.001"), Decimal("100"))


def ctx(t: int = T0, *, up: float = 0.8, down: float = 0.05, **overrides) -> Context:
    values = {
        "t_ns": t,
        "bid": 60000.0,
        "ask": 60000.1,
        "scores": Scores(up=up, down=down, none=1 - up - down),
        "notional_usd": 180.0,
        "maker_bp": 2.0,
        "taker_bp": 5.0,
    }
    values.update(overrides)
    return Context(**values)


def entered(policy: SymbolPolicy | None = None) -> tuple[SymbolPolicy, Place]:
    policy = policy or SymbolPolicy("BTCUSDT", PARAMS, BTC)
    step = policy.on_sample(ctx())
    (place,) = step.actions
    assert isinstance(place, Place)
    return policy, place


def opened(fill_price: float = 60000.0) -> tuple[SymbolPolicy, Place]:
    policy, place = entered()
    actions = policy.on_update(place.cycle_id, "entry", place.qty, fill_price, True, T0 + 200 * MS)
    (tp,) = actions
    assert isinstance(tp, Place) and tp.leg == "tp"
    return policy, tp


# --- entry ---------------------------------------------------------------------------


def test_entry_is_post_only_at_the_touch_with_lot_rounded_size() -> None:
    policy = SymbolPolicy("BTCUSDT", PARAMS, BTC)
    step = policy.on_sample(ctx())
    (place,) = step.actions
    assert place == Place(
        "BTCUSDT", f"BTCUSDT-{T0}", "entry", "buy", Decimal("0.003"), Decimal("60000.0"), False
    )
    assert place.post_only and policy.phase is Phase.ENTRY_RESTING
    assert step.signal is not None and step.signal.action == "enter"
    # (0.8 - 0.05) x 8 bp = 6 >= 2 x 2 bp maker + 1 bp buffer
    assert step.signal.edge_bp == pytest.approx(6.0) and step.signal.cost_bp == 5.0


def test_short_entry_rests_at_the_ask() -> None:
    policy = SymbolPolicy("BTCUSDT", PARAMS, BTC)
    (place,) = policy.on_sample(ctx(up=0.05, down=0.85)).actions
    assert place.side == "sell" and place.price == Decimal("60000.1")


def test_below_threshold_is_silent_and_gate_multiplier_raises_it() -> None:
    policy = SymbolPolicy("BTCUSDT", PARAMS, BTC)
    assert policy.on_sample(ctx(up=0.55)).signal is None
    step = policy.on_sample(ctx(up=0.8, threshold_mult=1.5))  # threshold 0.9
    assert step.signal is None and not step.actions


def test_edge_must_cover_the_maker_round_trip_plus_buffer() -> None:
    policy = SymbolPolicy("BTCUSDT", PARAMS, BTC)
    # (0.62 - 0.0) x 8 = 4.96 bp < 5 bp
    step = policy.on_sample(ctx(up=0.62, down=0.0))
    assert not step.actions and step.signal.action == "skip:edge_below_cost"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"blocked": "paused:stale_book"}, "skip:paused:stale_book"),
        ({"blocked": "gate:funding_blackout"}, "skip:gate:funding_blackout"),
        ({"blocked": "halted"}, "skip:halted"),
        ({"bid": None}, "skip:no_quote"),
        ({"notional_usd": 50.0}, "skip:below_minimum"),
    ],
)
def test_no_entry_while_blocked_or_too_small(overrides: dict, reason: str) -> None:
    policy = SymbolPolicy("BTCUSDT", PARAMS, BTC)
    step = policy.on_sample(ctx(**overrides))
    assert not step.actions and step.signal.action == reason
    assert policy.phase is Phase.FLAT


def test_gate_inventory_multiplier_shrinks_the_size() -> None:
    policy = SymbolPolicy("BTCUSDT", PARAMS, BTC)
    (place,) = policy.on_sample(ctx(notional_usd=250.0, size_mult=0.5)).actions
    assert place.qty == Decimal("0.002")


def test_a_shrunk_size_rounds_up_to_the_venue_minimum() -> None:
    # 180 x 0.5 = $90 is under BTC's $100 minimum; the full $180 is not.
    policy = SymbolPolicy("BTCUSDT", PARAMS, BTC)
    (place,) = policy.on_sample(ctx(size_mult=0.5)).actions
    assert place.qty == Decimal("0.002")


def test_a_shrunk_size_that_is_already_valid_is_kept() -> None:
    eth = Filters(Decimal("0.01"), Decimal("0.001"), Decimal("0.001"), Decimal("20"))
    policy = SymbolPolicy("ETHUSDT", PARAMS, eth)
    (place,) = policy.on_sample(ctx(bid=2500.0, ask=2500.01, size_mult=0.5)).actions
    assert place.qty == Decimal("0.036")


def test_one_cycle_at_a_time() -> None:
    policy, _ = entered()
    assert not policy.on_sample(ctx(T0 + 100 * MS)).actions


# --- resting entry ------------------------------------------------------------------


def test_entry_cancelled_on_timeout_once() -> None:
    policy, place = entered()
    assert not policy.on_sample(ctx(T0 + 900 * MS)).actions
    (cancel,) = policy.on_sample(ctx(T0 + 1000 * MS)).actions
    assert cancel == Cancel("BTCUSDT", place.cycle_id, "entry", "timeout")
    assert not policy.on_sample(ctx(T0 + 1100 * MS)).actions  # not twice


def test_entry_cancelled_when_the_signal_decays_or_is_blocked() -> None:
    policy, _ = entered()
    (cancel,) = policy.on_sample(ctx(T0 + 100 * MS, up=0.5)).actions
    assert cancel.reason == "decay"
    policy, _ = entered()
    (cancel,) = policy.on_sample(ctx(T0 + 100 * MS, blocked="paused:depth_resync")).actions
    assert cancel.reason == "blocked"


def test_unfilled_entry_ends_the_cycle() -> None:
    policy, place = entered()
    assert policy.on_update(place.cycle_id, "entry", Decimal(0), None, True, T0 + S) == []
    assert policy.phase is Phase.FLAT
    (cycle,) = policy.take_finished()
    assert cycle.exit_reason == "entry_unfilled" and cycle.entry_filled == 0


def test_a_rejected_entry_waits_out_the_reentry_cooldown() -> None:
    from dataclasses import replace

    policy = SymbolPolicy("BTCUSDT", replace(PARAMS, reentry_cooldown_ms=5000), BTC)
    (place,) = policy.on_sample(ctx(T0)).actions
    # Rejected at once (post-only would cross): the signal is still on.
    policy.on_update(place.cycle_id, "entry", Decimal(0), None, True, T0 + 50 * MS)
    assert policy.on_sample(ctx(T0 + 100 * MS)).actions == []
    assert policy.on_sample(ctx(T0 + 4900 * MS)).actions == []
    (again,) = policy.on_sample(ctx(T0 + 5100 * MS)).actions
    assert isinstance(again, Place) and again.leg == "entry"
    # Without a cooldown (the default) the next sample re-enters: research parity.
    policy, place = entered()
    policy.on_update(place.cycle_id, "entry", Decimal(0), None, True, T0 + 50 * MS)
    assert len(policy.on_sample(ctx(T0 + 100 * MS)).actions) == 1


def test_partial_fill_keeps_the_filled_part() -> None:
    policy, place = entered()
    policy.on_update(place.cycle_id, "entry", Decimal("0.001"), 60000.0, False, T0 + 100 * MS)
    assert policy.phase is Phase.ENTRY_RESTING
    (cancel,) = policy.on_sample(ctx(T0 + 1000 * MS)).actions
    (tp,) = policy.on_update(place.cycle_id, "entry", Decimal("0.001"), 60000.0, True, T0 + S)
    assert tp.qty == Decimal("0.001") and policy.phase is Phase.OPEN
    assert policy.cycle.entry_fill_ns == T0 + 100 * MS


def test_updates_for_another_cycle_are_ignored() -> None:
    policy, _ = entered()
    assert policy.on_update("BTCUSDT-1", "entry", Decimal("1"), 1.0, True, T0) == []
    assert policy.phase is Phase.ENTRY_RESTING


# --- exits ---------------------------------------------------------------------------


def test_take_profit_is_reduce_only_at_the_barrier() -> None:
    _, tp = opened()
    # 60000 x (1 + 8 bp) = 60048.0
    assert tp.price == Decimal("60048.0") and tp.side == "sell" and tp.reduce_only


def test_take_profit_fill_ends_the_cycle() -> None:
    policy, tp = opened()
    assert policy.on_update(tp.cycle_id, "tp", tp.qty, 60048.0, True, T0 + 3 * S) == []
    (cycle,) = policy.take_finished()
    assert cycle.exit_reason == "take_profit" and cycle.exit_avg == 60048.0
    assert policy.phase is Phase.FLAT


def test_stop_at_the_opposite_barrier_cancels_tp_and_closes_at_market() -> None:
    policy, tp = opened()
    assert not policy.on_sample(ctx(T0 + S, bid=59960.0, ask=59960.1)).actions
    actions = policy.on_sample(ctx(T0 + S, bid=59951.9, ask=59952.0))
    assert actions.actions == [
        Cancel("BTCUSDT", tp.cycle_id, "tp", "stop"),
        Close("BTCUSDT", tp.cycle_id, 1, "sell", Decimal("0.003"), "stop"),
    ]
    assert policy.phase is Phase.CLOSING
    assert not policy.on_sample(ctx(T0 + 2 * S, bid=59000.0, ask=59000.1)).actions


def test_time_stop_at_horizon_times_k() -> None:
    policy, tp = opened()
    assert not policy.on_sample(ctx(T0 + 200 * MS + 9 * S)).actions
    actions = policy.on_sample(ctx(T0 + 200 * MS + 10 * S)).actions
    assert [type(a) for a in actions] == [Cancel, Close] and actions[1].reason == "time_stop"


def test_close_fill_finishes_after_the_tp_is_gone() -> None:
    policy, tp = opened()
    policy.on_sample(ctx(T0 + 20 * S))  # time stop
    assert policy.on_update(tp.cycle_id, "tp", Decimal(0), None, True, T0 + 20 * S) == []
    policy.on_update(tp.cycle_id, "close1", Decimal("0.003"), 59990.0, True, T0 + 20 * S)
    (cycle,) = policy.take_finished()
    assert cycle.exit_reason == "time_stop" and cycle.exit_avg == 59990.0


def test_failed_closes_retry_then_halt() -> None:
    policy, tp = opened()
    policy.on_sample(ctx(T0 + 20 * S))
    policy.on_update(tp.cycle_id, "tp", Decimal(0), None, True, T0 + 20 * S)
    (retry,) = policy.on_update(tp.cycle_id, "close1", Decimal(0), None, True, T0 + 20 * S)
    assert isinstance(retry, Close) and retry.attempt == 2
    (retry,) = policy.on_update(tp.cycle_id, "close2", Decimal(0), None, True, T0 + 20 * S)
    (halt,) = policy.on_update(tp.cycle_id, "close3", Decimal(0), None, True, T0 + 20 * S)
    assert isinstance(halt, Halt) and "3 closes failed" in halt.reason


def test_a_lost_take_profit_closes_the_position() -> None:
    policy, tp = opened()
    (close,) = policy.on_update(tp.cycle_id, "tp", Decimal(0), None, True, T0 + S)
    assert isinstance(close, Close) and close.reason == "tp_lost"


# --- halts ---------------------------------------------------------------------------


def test_liquidate_cancels_a_resting_entry_and_closes_what_filled() -> None:
    policy, place = entered()
    (cancel,) = policy.liquidate("kill_switch", T0 + 10 * MS)
    assert cancel.leg == "entry"
    (close,) = policy.on_update(place.cycle_id, "entry", Decimal("0.001"), 60000.0, True, T0 + S)
    assert isinstance(close, Close) and close.reason == "kill_switch"


def test_liquidate_an_open_position() -> None:
    policy, tp = opened()
    actions = policy.liquidate("daily_loss_limit", T0 + S)
    assert [type(a) for a in actions] == [Cancel, Close]
    assert policy.liquidate("again", T0 + S) == []  # already closing


def test_state_round_trips_for_restarts() -> None:
    policy, tp = opened()
    restored = SymbolPolicy("BTCUSDT", PARAMS, BTC)
    restored.restore(policy.snapshot())
    assert restored.phase is Phase.OPEN
    assert restored.cycle == Cycle.from_dict(policy.cycle.to_dict())
    assert restored.cycle.tp_price == Decimal("60048.0")


# --- params and sizing ---------------------------------------------------------------


def test_params_are_validated() -> None:
    with pytest.raises(ValueError, match="threshold"):
        PolicyParams(horizon_s=5, threshold=1.2, barrier_bp=6)
    with pytest.raises(ValueError, match="unknown policy fields"):
        PolicyParams.from_dict({"horizon_s": 5, "threshold": 0.6, "barrier_bp": 6, "x": 1})


def test_kelly_is_quarter_and_capped_and_zero_below_cutoff() -> None:
    assert kelly_fraction(0.6, 1.0) == pytest.approx(0.2)
    assert kelly_size(0.6, 1.0, cap=1.0) == pytest.approx(0.05)
    assert kelly_size(0.9, 1.0, cap=0.1) == 0.1
    assert kelly_size(0.4, 1.0, cap=1.0) == 0.0


# --- flow guard and maker exit chase --------------------------------------------------

CHASE = PolicyParams(
    horizon_s=5,
    threshold=0.6,
    barrier_bp=8,
    buffer_bp=1,
    entry_timeout_ms=1000,
    time_stop_mult=2,
    exit_mode="maker_chase",
    exit_chase_ms=3000,
    exit_max_rejects=2,
)
TIME_STOP = T0 + 200 * MS + 10 * S  # horizon 5 s x 2 after the fill


def chasing(params: PolicyParams = CHASE) -> tuple[SymbolPolicy, list]:
    """Long 0.003 BTC from 60000.0; the time stop has fired."""
    policy = SymbolPolicy("BTCUSDT", params, BTC)
    (place,) = policy.on_sample(ctx()).actions
    policy.on_update(place.cycle_id, "entry", place.qty, 60000.0, True, T0 + 200 * MS)
    actions = policy.on_sample(ctx(TIME_STOP, bid=60001.0, ask=60001.1)).actions
    return policy, actions


def test_guard_blocks_an_entry_and_cancels_a_resting_one() -> None:
    policy = SymbolPolicy("BTCUSDT", PARAMS, BTC)
    step = policy.on_sample(ctx(flow_against=frozenset({"buy"})))
    assert step.actions == [] and step.signal is not None
    assert step.signal.action == "skip:adverse_flow" and policy.phase is Phase.FLAT
    # Flow against the other side does not matter.
    policy, place = entered()
    assert policy.on_sample(ctx(T0 + 100 * MS, flow_against=frozenset({"sell"}))).actions == []
    (cancel,) = policy.on_sample(ctx(T0 + 200 * MS, flow_against=frozenset({"buy"}))).actions
    assert cancel == Cancel("BTCUSDT", place.cycle_id, "entry", "adverse_flow")


def test_time_stop_rests_a_maker_exit_at_the_near_touch() -> None:
    policy, actions = chasing()
    cancel_tp, exit1 = actions
    assert cancel_tp == Cancel("BTCUSDT", f"BTCUSDT-{T0}", "tp", "time_stop")
    assert isinstance(exit1, Place) and exit1.leg == "exit1"
    assert exit1.side == "sell" and exit1.price == Decimal("60001.1")
    assert exit1.reduce_only and exit1.post_only and exit1.qty == Decimal("0.003")
    assert policy.phase is Phase.CLOSING and policy.cycle.exit_reason == "time_stop"
    # Filled: the cycle ends once the TP's cancel is confirmed, as a maker exit.
    cid = exit1.cycle_id
    assert policy.on_update(cid, "exit1", Decimal("0.003"), 60001.1, True, TIME_STOP + S) == []
    assert policy.on_update(cid, "tp", Decimal(0), None, True, TIME_STOP + S) == []
    (done,) = policy.take_finished()
    assert done.exit_reason == "time_stop" and done.exit_avg == 60001.1
    assert policy.phase is Phase.FLAT


def test_chase_reprices_when_the_touch_moves_away() -> None:
    policy, (_, exit1) = chasing()
    cid = exit1.cycle_id
    # The ask did not move: nothing. It dropped a tick: cancel, then a new leg there.
    assert policy.on_sample(ctx(TIME_STOP + 100 * MS, bid=60001.0, ask=60001.1)).actions == []
    (cancel,) = policy.on_sample(ctx(TIME_STOP + 200 * MS, bid=60000.9, ask=60001.0)).actions
    assert cancel == Cancel("BTCUSDT", cid, "exit1", "reprice")
    assert policy.on_sample(ctx(TIME_STOP + 300 * MS, bid=60000.9, ask=60001.0)).actions == []
    (exit2,) = policy.on_update(cid, "exit1", Decimal("0.001"), 60001.1, True, TIME_STOP + 300 * MS)
    assert exit2.leg == "exit2" and exit2.price == Decimal("60001.0")
    assert exit2.qty == Decimal("0.002")  # what the first leg left


def test_chase_gives_up_for_the_market_on_timeout_stop_flow_or_rejections() -> None:
    policy, (_, exit1) = chasing()
    cid = exit1.cycle_id
    cancel, close = policy.on_sample(ctx(TIME_STOP + 3 * S, bid=60001.0, ask=60001.1)).actions
    assert cancel == Cancel("BTCUSDT", cid, "exit1", "chase_timeout")
    assert isinstance(close, Close) and close.reason == "time_stop:chase_timeout"
    assert close.qty == Decimal("0.003")
    # The cancelled leg ending changes nothing: the market close has taken over.
    assert policy.on_update(cid, "exit1", Decimal(0), None, True, TIME_STOP + 3 * S) == []

    policy, _ = chasing()
    stop_mid = 60000.0 * (1 - 8 / 10_000)
    _, close = policy.on_sample(ctx(TIME_STOP + S, bid=stop_mid - 1, ask=stop_mid)).actions
    assert isinstance(close, Close) and close.reason == "time_stop:stop"

    policy, _ = chasing()
    _, close = policy.on_sample(ctx(TIME_STOP + S, flow_against=frozenset({"buy"}))).actions
    assert isinstance(close, Close) and close.reason == "time_stop:adverse_flow"

    policy, (_, exit1) = chasing()
    # Refused (post-only would take): a fresh leg, then the second refusal gives up.
    (exit2,) = policy.on_update(cid, "exit1", Decimal(0), None, True, TIME_STOP + 100 * MS)
    assert exit2.leg == "exit2"
    (close,) = policy.on_update(cid, "exit2", Decimal(0), None, True, TIME_STOP + 200 * MS)
    assert isinstance(close, Close) and close.reason == "time_stop:rejected"


def test_no_chase_when_flow_already_runs_against_the_position_or_on_a_stop() -> None:
    policy = SymbolPolicy("BTCUSDT", CHASE, BTC)
    (place,) = policy.on_sample(ctx()).actions
    policy.on_update(place.cycle_id, "entry", place.qty, 60000.0, True, T0 + 200 * MS)
    _, close = policy.on_sample(ctx(TIME_STOP, flow_against=frozenset({"buy"}))).actions
    assert isinstance(close, Close) and close.reason == "time_stop"

    policy = SymbolPolicy("BTCUSDT", CHASE, BTC)
    (place,) = policy.on_sample(ctx()).actions
    policy.on_update(place.cycle_id, "entry", place.qty, 60000.0, True, T0 + 200 * MS)
    _, close = policy.on_sample(ctx(T0 + S, bid=59900.0, ask=59900.1)).actions
    assert isinstance(close, Close) and close.reason == "stop"


def test_liquidate_during_a_chase_closes_at_market_and_the_chase_survives_a_restore() -> None:
    policy, (_, exit1) = chasing()
    restored = SymbolPolicy("BTCUSDT", CHASE, BTC)
    restored.restore(policy.snapshot())
    assert restored.cycle.chase_leg == "exit1"
    assert restored.cycle.chase_price == Decimal("60001.1")
    cancel, close = restored.liquidate("kill_switch", TIME_STOP + S)
    assert cancel == Cancel("BTCUSDT", exit1.cycle_id, "exit1", "kill_switch")
    assert isinstance(close, Close)


def test_new_policy_fields_are_validated() -> None:
    base = {"horizon_s": 5, "threshold": 0.6, "barrier_bp": 8}
    for bad in (
        {"exit_mode": "limit"},
        {"exit_chase_ms": 0},
        {"guard_z": 0},
        {"guard_tfi": 1.5},
        {"guard_imb": 0.4},
    ):
        with pytest.raises(ValueError):
            PolicyParams(**base, **bad)
    assert PolicyParams.from_dict({**base, "guard_z": 3.0}).guard_z == 3.0
