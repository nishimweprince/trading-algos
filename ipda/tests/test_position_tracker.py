from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ipda.data_client import Tick
from ipda.position_tracker import PositionTracker, TrackedTrade, tracked_trade_from_fill

OPENED = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _trade(direction: str = "buy", entry: float = 1.1000) -> TrackedTrade:
    return TrackedTrade(
        signal_id=f"sig-{direction}",
        quote="EURUSD",
        symbol="EURUSD",
        direction=direction,
        entry=entry,
        pip_size=0.0001,
        stop_loss_pips=40.0,
        take_profit_pips=50.0,
        opened_at=OPENED.isoformat(),
    )


def _tracker(tmp_path: Path, ttl_hours: float = 24.0) -> PositionTracker:
    return PositionTracker(
        state_path=tmp_path / "open_trades.json",
        break_even_pips=30.0,
        ttl_hours=ttl_hours,
    )


def test_buy_excursion_uses_the_bid() -> None:
    trade = _trade("buy")

    # A buy closes at the bid, so the bid is what break-even has to beat.
    assert trade.excursion_pips(Tick("EURUSD", bid=1.1030, ask=1.1032)) == 30.0
    assert trade.excursion_pips(Tick("EURUSD", bid=1.0990, ask=1.0992)) == -10.0


def test_sell_excursion_uses_the_ask() -> None:
    trade = _trade("sell")

    assert trade.excursion_pips(Tick("EURUSD", bid=1.0968, ask=1.0970)) == 30.0
    assert trade.excursion_pips(Tick("EURUSD", bid=1.1008, ask=1.1010)) == -10.0


def test_break_even_fires_once_at_the_trigger(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.track(_trade("buy"))

    below = tracker.observe("EURUSD", Tick("EURUSD", bid=1.1029, ask=1.1031))
    assert below == []

    at_trigger = tracker.observe("EURUSD", Tick("EURUSD", bid=1.1030, ask=1.1032))
    assert [u.break_even_reached for u in at_trigger] == [True]

    # Still open, still above the trigger — must not notify twice.
    again = tracker.observe("EURUSD", Tick("EURUSD", bid=1.1035, ask=1.1037))
    assert [u.break_even_reached for u in again] == []


def test_peak_excursion_survives_a_retrace(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.track(_trade("buy"))

    tracker.observe("EURUSD", Tick("EURUSD", bid=1.1025, ask=1.1027))
    tracker.observe("EURUSD", Tick("EURUSD", bid=1.1005, ask=1.1007))
    updates = tracker.observe("EURUSD", Tick("EURUSD", bid=1.1030, ask=1.1032))

    assert [u.break_even_reached for u in updates] == [True]
    assert updates[0].trade.mfe_pips == 30.0


def test_take_profit_distance_infers_a_close(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.track(_trade("buy"))

    updates = tracker.observe("EURUSD", Tick("EURUSD", bid=1.1050, ask=1.1052))

    assert updates[0].closed_reason == "take_profit_reached"
    assert tracker.trades == []


def test_stop_loss_distance_infers_a_close(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.track(_trade("sell"))

    # A sell 40 pips offside: ask 40 pips above entry.
    updates = tracker.observe("EURUSD", Tick("EURUSD", bid=1.1038, ask=1.1040))

    assert updates[0].closed_reason == "stop_loss_reached"
    assert tracker.trades == []


def test_ttl_expiry_drops_stale_trades(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path, ttl_hours=1.0)
    tracker.track(_trade("buy"))

    assert tracker.expire(now=OPENED + timedelta(minutes=30)) == []

    updates = tracker.expire(now=OPENED + timedelta(hours=2))
    assert [u.closed_reason for u in updates] == ["ttl_expired"]
    assert tracker.trades == []


def test_track_is_idempotent_per_signal(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)

    assert tracker.track(_trade("buy")) is True
    assert tracker.track(_trade("buy")) is False
    assert len(tracker.trades) == 1


def test_state_round_trips_across_a_restart(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.track(_trade("buy"))
    tracker.observe("EURUSD", Tick("EURUSD", bid=1.1030, ask=1.1032))

    restarted = _tracker(tmp_path)
    restarted.load()

    assert len(restarted.trades) == 1
    restored = restarted.trades[0]
    assert restored.signal_id == "sig-buy"
    assert restored.mfe_pips == 30.0
    # The alert already went out before the restart; it must not repeat.
    assert restored.break_even_notified is True
    assert restarted.observe("EURUSD", Tick("EURUSD", bid=1.1035, ask=1.1037)) == []


def test_load_tolerates_a_corrupt_state_file(tmp_path: Path) -> None:
    (tmp_path / "open_trades.json").write_text("{not json", encoding="utf-8")

    tracker = _tracker(tmp_path)
    tracker.load()

    assert tracker.trades == []


def test_quotes_are_deduplicated(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.track(_trade("buy"))
    tracker.track(_trade("sell"))

    assert tracker.quotes() == ["EURUSD"]


def test_fill_prefers_the_broker_execution_price() -> None:
    trade = tracked_trade_from_fill(
        signal_id="sig-1",
        quote="EURUSD",
        symbol="EURUSD",
        direction="buy",
        fallback_entry=1.1000,
        pip_size=0.0001,
        stop_loss_pips=40.0,
        take_profit_pips=50.0,
        detail={"execution_price": "1.10025"},
    )

    assert trade.entry == 1.10025


def test_fill_falls_back_to_the_bar_close_when_price_is_absent() -> None:
    for detail in (None, {}, {"execution_price": None}, {"execution_price": "n/a"}):
        trade = tracked_trade_from_fill(
            signal_id="sig-1",
            quote="EURUSD",
            symbol="EURUSD",
            direction="buy",
            fallback_entry=1.1000,
            pip_size=0.0001,
            stop_loss_pips=40.0,
            take_profit_pips=50.0,
            detail=detail,
        )
        assert trade.entry == 1.1000
