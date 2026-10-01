from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from ta_plugin_api import OcoVenue

from ta_plugin_mt5 import FACTORY
from ta_plugin_mt5.oco import MT5OcoObservation
from ta_plugin_mt5.testing import FakeMT5Adapter

MAGIC = 234000
OFFSET = 3 * 3600
SERVER = OFFSET * 1000  # a deal stamped at UTC 0 ms, in server time


def _document(**overrides: Any) -> dict[str, Any]:
    return {
        "symbol": "EURUSD",
        "created_at": datetime.now(UTC).isoformat(),
        "server_utc_offset_seconds": OFFSET,
        **overrides,
    }


def _observation(**rows: list[dict[str, Any]]) -> MT5OcoObservation:
    return MT5OcoObservation(
        _document(),
        MAGIC,
        rows.get("orders", []),
        rows.get("positions", []),
        rows.get("history", []),
        rows.get("deals", []),
    )


def _deal(**values: Any) -> dict[str, Any]:
    return {"magic": MAGIC, "symbol": "EURUSD", **values}


def _exit(symbol: str, **values: Any) -> dict[str, Any]:
    return {"symbol": symbol, "magic": 0, "entry": 1, "volume": 0.1, "position_id": 70, **values}


def test_tags_resolve_only_owned_orders() -> None:
    observation = _observation(
        orders=[
            _deal(ticket=1, comment="oco-x-b"),
            {**_deal(ticket=2, comment="oco-x-b"), "magic": 1},
        ],
        history=[_deal(ticket=3, comment="oco-x-b"), _deal(ticket=4, comment="oco-x-s")],
    )

    assert observation.tagged_orders("oco-x-b") == {1, 3}
    assert observation.is_live(1) and not observation.is_live(3)


@pytest.mark.parametrize(
    ("state", "expected"), [(2, "cancelled"), (5, "rejected"), (6, "expired"), (4, None)]
)
def test_history_states(state: int, expected: str | None) -> None:
    observation = _observation(history=[_deal(ticket=7, state=state)])
    assert observation.terminal_state(7) == expected


def test_fills_are_volume_weighted_and_shifted_to_utc() -> None:
    observation = _observation(
        deals=[
            _deal(
                order=7,
                entry=0,
                volume=0.1,
                price=1.1,
                position_id=70,
                time_msc=OFFSET * 1000 + 500,
            ),
            _deal(
                order=7,
                entry=0,
                volume=0.3,
                price=1.2,
                position_id=71,
                time_msc=OFFSET * 1000 + 900,
            ),
            _deal(order=8, entry=0, volume=5, price=9, position_id=80, time_msc=1),
        ],
        positions=[_deal(ticket=70, identifier=70, volume=0.1)],
    )

    fills = observation.fills(7)

    assert fills is not None
    assert fills.executed_volume == Decimal("0.4")
    assert fills.fill_price == Decimal("1.175")
    assert fills.position_ids == [70, 71]
    assert fills.filled_at_msc == 500
    assert fills.open_positions is True
    assert observation.fills(9) is None


def test_exits_count_manual_closes_and_feed_accounting() -> None:
    observation = _observation(
        deals=[
            _deal(order=7, entry=0, volume=0.1, price=1.1, position_id=70, commission=-0.1),
            # A manual close in the terminal: magic 0, still this leg's position.
            {
                "symbol": "EURUSD",
                "magic": 0,
                "entry": 1,
                "volume": 0.1,
                "position_id": 70,
                "profit": 2,
            },
            {
                "symbol": "GBPUSD",
                "magic": 0,
                "entry": 1,
                "volume": 0.1,
                "position_id": 70,
                "profit": 9,
            },
        ],
    )

    fills = observation.fills(7)

    assert fills is not None
    assert fills.exit_volume == Decimal("0.1")
    assert fills.open_positions is False
    assert fills.accounting["profit"] == "2"
    assert fills.accounting["realized_net_pnl"] == "1.9"


class _Terminal(FakeMT5Adapter):
    def __init__(self) -> None:
        super().__init__()
        self.windows: list[tuple[datetime, datetime]] = []

    def history_orders(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        self.windows.append((start, end))
        return []

    def account_metadata(self) -> dict[str, Any]:
        return {"login": 123456, "server": "Broker-Demo", "margin_mode": 2, "currency": "USD"}


def _venue(terminal: _Terminal, offset: int = 0):
    settings = SimpleNamespace(
        profile="hfm",
        login=123456,
        server="Broker-Demo",
        magic_number=MAGIC,
        default_deviation_points=10,
        maximum_deviation_points=20,
        trading_enabled=True,
        allowed_symbols=frozenset({"EURUSD"}),
        maximum_volume=Decimal("1"),
        mt5_oco_server_utc_offset_seconds=offset,
    )
    execution = FACTORY.execution(settings, terminal=terminal)
    return FACTORY.oco(settings, execution)


def test_venue_satisfies_the_protocol_and_tags_legs() -> None:
    venue = _venue(_Terminal())
    group_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    assert isinstance(venue, OcoVenue)
    assert venue.account == "hfm"
    assert venue.leg_tag(group_id, "long") == "oco-aaaaaaaabbbbccccdddd-b"
    assert venue.account_identity()["account_currency"] == "USD"


def test_history_window_spans_both_clocks() -> None:
    terminal = _Terminal()
    created = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)

    _venue(terminal, offset=OFFSET).observe(_document(created_at=created.isoformat()))

    start, end = terminal.windows[0]
    assert start == created.replace(minute=0) - (
        created.replace(minute=5) - created.replace(minute=0)
    )
    assert end > datetime.now(UTC) + (datetime.min.replace(hour=3) - datetime.min)


def test_monitor_preflight_rejects_untracked_owned_orders() -> None:
    terminal = _Terminal()
    terminal.live_orders = [{"ticket": 1, "magic": MAGIC, "comment": "oco-stray-b"}]
    venue = _venue(terminal)

    with pytest.raises(RuntimeError, match="missing from durable ledger"):
        venue.monitor_preflight(True, {"oco-other-b"})
    venue.monitor_preflight(True, {"oco-stray-b"})
