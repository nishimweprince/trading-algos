from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from ta_plugin_binance_futures.limiter import OrderRateGovernor

from ofi_scalper_service.risk import HaltReason, OrderIntent, RiskLimits, RiskState

LIMITS = RiskLimits(
    max_position_notional_usd=1_000,
    max_total_notional_usd=1_500,
    daily_loss_limit_usd=50,
    max_drawdown_pct=10,
    manual_approval_notional_usd=800,
    stale_book_ms=750,
    order_rate_fraction=0.5,
)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 1, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def state(clock: Clock | None = None) -> RiskState:
    return RiskState(LIMITS, clock=clock or Clock())


def buy(symbol: str = "BTCUSDT", notional: float = 100, reduce_only: bool = False):
    return OrderIntent(symbol, "buy", notional, reduce_only)


def test_limits_are_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        LIMITS.max_position_notional_usd = 1e9  # type: ignore[misc]


def test_position_and_total_caps() -> None:
    risk = state()
    risk.set_position("BTCUSDT", 950)
    assert risk.check_order(buy(notional=100)).reason == "max_position_notional"
    assert risk.check_order(OrderIntent("BTCUSDT", "sell", 100)).allowed
    risk.set_position("ETHUSDT", -600)
    assert risk.check_order(buy("BTCUSDT", 50)).reason == "max_total_notional"


def test_manual_approval_above_threshold() -> None:
    decision = state().check_order(buy(notional=900))
    assert not decision.allowed and decision.needs_approval


def test_kill_halts_until_ack_and_allows_reduce_only() -> None:
    risk = state()
    assert risk.kill("http")
    assert not risk.kill("telegram")  # first reason kept
    assert risk.check_order(buy()).reason == "halted:kill_switch"
    assert risk.check_order(buy(reduce_only=True)).allowed
    assert risk.ack("operator") == (True, "cleared kill_switch")
    assert risk.check_order(buy()).allowed


def test_daily_loss_halts_until_next_utc_day_and_ack() -> None:
    clock = Clock()
    risk = state(clock)
    risk.update_equity(1_000)
    assert risk.update_equity(949) is HaltReason.DAILY_LOSS
    ok, why = risk.ack("operator")
    assert not ok and "next UTC day" in why
    clock.now += timedelta(days=1)
    assert risk.halted  # a new day alone does not clear it
    assert risk.ack("operator")[0]


def test_drawdown_from_peak_halts() -> None:
    risk = state()
    risk.update_equity(400)
    risk.update_equity(420)
    assert risk.update_equity(377) is HaltReason.DRAWDOWN  # 10.2% from 420
    assert risk.ack("operator")[0]
    assert risk.update_equity(377) is None  # peak reset at ack


def test_pause_blocks_new_exposure_only() -> None:
    risk = state()
    assert risk.set_pause("BTCUSDT", "stale_book", True)
    assert not risk.set_pause("BTCUSDT", "stale_book", True)
    assert risk.check_order(buy()).reason == "paused:stale_book"
    assert risk.check_order(buy(reduce_only=True)).allowed
    assert risk.check_order(buy("ETHUSDT")).allowed
    risk.set_pause("BTCUSDT", "stale_book", False)
    assert risk.check_order(buy()).allowed


def test_rate_governor_refuses_without_waiting() -> None:
    now = [0.0]
    risk = RiskState(LIMITS, governor=OrderRateGovernor(0.5, clock=lambda: now[0]))
    allowed = sum(risk.check_order(buy(notional=1)).allowed for _ in range(200))
    assert allowed == 150
    assert risk.check_order(buy(notional=1)).reason == "order_rate_governor"


def test_no_api_changes_limits() -> None:
    risk = state()
    public = {name for name in dir(risk) if not name.startswith("_")}
    assert not any(name.startswith("set_limit") or name == "update_limits" for name in public)
    assert risk.snapshot()["limits"]["daily_loss_limit_usd"] == 50


def test_a_fault_is_recorded_without_a_halt() -> None:
    risk = state()
    event = risk.record_fault("c1: 3 closes failed")
    assert not risk.halted and risk.check_order(buy()).allowed
    assert event["event"] == "fault" and event["reason"] == "execution_fault"
    assert risk.history[-1] == event


def test_record_only_loss_limits_never_halt_and_record_once() -> None:
    clock = Clock()
    risk = RiskState(LIMITS, clock=clock, enforce_loss_limits=False)
    risk.update_equity(1_000)
    assert risk.update_equity(949) is None and not risk.halted
    (daily,) = risk.take_breaches()
    assert daily["reason"] == "daily_loss_limit" and daily["enforced"] is False
    assert risk.update_equity(940) is None
    assert risk.take_breaches() == []  # once per UTC day
    assert risk.update_equity(899) is None  # 10.1% from the 1000 peak
    (drawdown,) = risk.take_breaches()
    assert drawdown["reason"] == "max_drawdown"
    assert [b["reason"] for b in risk.snapshot()["breaches"]] == [
        "daily_loss_limit",
        "max_drawdown",
    ]
    risk.update_equity(880)
    assert risk.take_breaches() == []  # the same drawdown episode
    clock.now += timedelta(days=1)
    risk.update_equity(880)
    assert risk.snapshot()["breaches"] == []  # a new day
    risk.update_equity(820)  # down 60 today: a new daily breach
    assert [b["reason"] for b in risk.take_breaches()] == ["daily_loss_limit"]
    risk.update_equity(1_100)  # a new peak re-arms the drawdown breach
    risk.update_equity(980)
    assert [b["reason"] for b in risk.take_breaches()] == ["max_drawdown"]
    assert not risk.halted
    assert risk.check_order(buy()).allowed


# --- persisted halt ----------------------------------------------------------------------


def test_a_kill_survives_a_new_state_until_ack(tmp_path) -> None:
    path = tmp_path / "state" / "halt.json"
    clock = Clock()
    first = RiskState(LIMITS, clock=clock, state_path=path)
    assert not first.restored and not path.exists()
    first.kill("http", "drill")
    second = RiskState(LIMITS, clock=clock, state_path=path)
    assert second.halted and second.restored
    assert second.halt is not None and second.halt.reason is HaltReason.KILL
    assert second.halt.source == "http" and second.halt.detail == "drill"
    assert second.snapshot()["halt"]["restored"] is True
    assert not second.check_order(buy()).allowed
    assert second.ack("http")[0] and not second.restored
    third = RiskState(LIMITS, clock=clock, state_path=path)
    assert not third.halted
    assert [h["event"] for h in third.history] == ["halt", "ack"]
    assert not list(path.parent.glob("*.tmp"))


def test_an_unreadable_state_file_starts_halted(tmp_path) -> None:
    path = tmp_path / "halt.json"
    path.write_text("{not json")
    risk = RiskState(LIMITS, clock=Clock(), state_path=path)
    assert risk.halt is not None and risk.halt.reason is HaltReason.KILL
    assert risk.halt.source == "state" and "unreadable" in risk.halt.detail
    assert risk.ack("http")[0]
    assert RiskState(LIMITS, clock=Clock(), state_path=path).halted is False


def test_without_a_state_path_nothing_is_written(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    risk = state()
    risk.kill("http")
    risk.ack("http")
    assert list(tmp_path.iterdir()) == []


def test_a_failed_save_keeps_the_halt_and_says_so(tmp_path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    risk = RiskState(LIMITS, clock=Clock(), state_path=blocker / "halt.json")
    risk.kill("http")
    assert risk.halted and risk.snapshot()["persist_error"]
