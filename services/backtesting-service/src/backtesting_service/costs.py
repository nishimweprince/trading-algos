"""Transaction-side execution costs and rollover financing, denominated in pips."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

NUMERIC_COST_FIELDS = frozenset(
    {
        "spread_pips_per_side",
        "slippage_pips_per_side",
        "commission_pips_per_side",
        "swap_long_pips_per_rollover",
        "swap_short_pips_per_rollover",
    }
)
COST_SESSION_NAMES = frozenset({"tokyo", "london", "new_york"})

# Floating-point identity for ``net == gross - cost``. Engine arithmetic is not
# bit-identical across every path; reports and tests treat residuals at or below
# this absolute tolerance as identity, not as an "exact" pytest.approx default.
COST_IDENTITY_ABS_TOL = 1e-9


@dataclass(frozen=True)
class CostSchedule:
    spread_pips_per_side: float = 0.0
    slippage_pips_per_side: float = 0.0
    commission_pips_per_side: float = 0.0
    swap_long_pips_per_rollover: float = 0.0
    swap_short_pips_per_rollover: float = 0.0

    @property
    def execution_pips_per_side(self) -> float:
        return (
            self.spread_pips_per_side + self.slippage_pips_per_side + self.commission_pips_per_side
        )

    @property
    def all_in_pips_per_side(self) -> float:
        """Per-side all-in execution cost. Swap is a holding cost, not a stop floor input."""
        return self.execution_pips_per_side


@dataclass(frozen=True)
class CostBreakdown:
    execution_pips: float = 0.0
    financing_pips: float = 0.0

    @property
    def total_pips(self) -> float:
        return self.execution_pips + self.financing_pips


def schedule_for(
    *,
    session: str,
    enabled: bool,
    base: CostSchedule,
    overrides: dict[str, dict[str, float]],
) -> CostSchedule:
    if not enabled:
        return CostSchedule()
    values = {name: float(getattr(base, name)) for name in NUMERIC_COST_FIELDS}
    values.update(overrides.get(session, {}))
    return CostSchedule(**values)


def rollover_units(
    entry_ts: datetime,
    as_of: datetime,
    *,
    timezone: str,
    rollover_time: str,
    triple_weekday: str,
) -> int:
    """Return broker rollover units crossed in ``(entry_ts, as_of]``.

    Weekday rollovers are charged once, except the configured triple weekday. Saturday and Sunday
    have no separate rollover because the triple weekday prices the weekend.
    """
    if as_of <= entry_ts:
        return 0
    zone = ZoneInfo(timezone)
    clock = time.fromisoformat(rollover_time)
    triple = WEEKDAYS[triple_weekday.lower()]
    start_local = entry_ts.astimezone(zone)
    end_local = as_of.astimezone(zone)
    current_date = start_local.date()
    units = 0
    while current_date <= end_local.date():
        boundary = datetime.combine(current_date, clock, tzinfo=zone)
        if entry_ts.astimezone(UTC) < boundary.astimezone(UTC) <= as_of.astimezone(UTC):
            weekday = boundary.weekday()
            if weekday < 5:
                units += 3 if weekday == triple else 1
        current_date += timedelta(days=1)
    return units


def leg_cost(
    *,
    schedule: CostSchedule,
    entry_ts: datetime,
    as_of: datetime,
    is_long: bool,
    exited: bool,
    timezone: str,
    rollover_time: str,
    triple_weekday: str,
) -> CostBreakdown:
    sides = 2 if exited else 1
    swap_rate = (
        schedule.swap_long_pips_per_rollover if is_long else schedule.swap_short_pips_per_rollover
    )
    units = rollover_units(
        entry_ts,
        as_of,
        timezone=timezone,
        rollover_time=rollover_time,
        triple_weekday=triple_weekday,
    )
    return CostBreakdown(
        execution_pips=sides * schedule.execution_pips_per_side,
        financing_pips=units * swap_rate,
    )


def cost_derived_min_stop_pips(schedule: CostSchedule, multiple: float) -> float:
    """Floor in pips: per-side all-in execution cost times ``multiple``.

    A multiple of 0 disables the cost-derived floor. Frozen Phase 3 coordinates use 2 and 3.
    """
    if multiple <= 0:
        return 0.0
    return schedule.all_in_pips_per_side * multiple


def effective_min_stop_pips(
    *,
    min_stop_pips: float,
    min_stop_cost_mult: float,
    schedule: CostSchedule,
) -> float:
    """Configured pip floor and the cost-derived floor, whichever is larger."""
    return max(min_stop_pips, cost_derived_min_stop_pips(schedule, min_stop_cost_mult))


def breakeven_cost_per_side(gross_expectancy_pips: float, side_equivalents: float) -> float | None:
    if side_equivalents <= 0:
        return None
    return gross_expectancy_pips / side_equivalents


def headroom_ratio(
    breakeven_pips_per_side: float | None, spread_pips_per_side: float
) -> float | None:
    if breakeven_pips_per_side is None or spread_pips_per_side <= 0:
        return None
    return breakeven_pips_per_side / spread_pips_per_side
