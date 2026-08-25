"""UTC entry-hour exclusion, and the daylight-saving trap it hides."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from filters import entry_filter_reason, entry_hour_blocked, parse_hours
from models import EngineParams


def _reason(ts: datetime, excluded: frozenset[int]) -> str | None:
    return entry_filter_reason(
        filter_d1_ema50=False,
        filter_nr7=False,
        filter_orb_atr_min=0,
        filter_orb_atr_max=0,
        entry_hours_utc_exclude=excluded,
        ts=ts,
        bullish=True,
        range_price=10,
        session_orb_ranges=[],
        prior_d1=None,
        atr=None,
    )


class TestParsing:
    def test_empty_is_off(self) -> None:
        assert parse_hours("") == frozenset()
        assert parse_hours("  ,  ") == frozenset()

    def test_parses_and_dedupes(self) -> None:
        assert parse_hours("13, 14,13") == frozenset({13, 14})

    @pytest.mark.parametrize("text", ["24", "-1", "99"])
    def test_rejects_out_of_range(self, text: str) -> None:
        with pytest.raises(ValueError, match="0-23"):
            parse_hours(text)

    def test_engine_params_rejects_out_of_range(self) -> None:
        with pytest.raises(ValidationError, match="0-23"):
            EngineParams(entry_hours_utc_exclude=[24])

    def test_default_is_off(self) -> None:
        assert EngineParams().entry_hours_utc_exclude == []


class TestBlocking:
    def test_off_by_default_allows_everything(self) -> None:
        assert _reason(datetime(2026, 6, 1, 13, 0, tzinfo=UTC), frozenset()) is None

    def test_blocks_the_listed_hour(self) -> None:
        assert (
            _reason(datetime(2026, 6, 1, 13, 0, tzinfo=UTC), frozenset({13})) == "filter_entry_hour"
        )

    def test_allows_an_unlisted_hour(self) -> None:
        assert _reason(datetime(2026, 6, 1, 14, 0, tzinfo=UTC), frozenset({13})) is None

    def test_compares_in_utc_not_local(self) -> None:
        local = datetime(2026, 6, 1, 9, 0, tzinfo=ZoneInfo("America/New_York"))
        assert entry_hour_blocked(local, frozenset({13})) is True
        assert entry_hour_blocked(local, frozenset({9})) is False


class TestDaylightSavingTrap:
    """One local session anchor maps to two UTC hours. Excluding one excludes a season."""

    def test_new_york_0900_is_1300_utc_in_summer_and_1400_in_winter(self) -> None:
        ny = ZoneInfo("America/New_York")
        summer = datetime(2026, 6, 1, 9, 0, tzinfo=ny).astimezone(UTC)
        winter = datetime(2026, 1, 5, 9, 0, tzinfo=ny).astimezone(UTC)
        assert summer.hour == 13
        assert winter.hour == 14

        excluded = frozenset({13})
        assert entry_hour_blocked(summer, excluded) is True
        assert entry_hour_blocked(winter, excluded) is False
