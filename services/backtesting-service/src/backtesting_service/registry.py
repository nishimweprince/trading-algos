"""Strategy registration.

The point of this module is that adding strategy number two is an addition, not
a fork of the service. It is the *seam*: it was introduced ahead of the engine
split so the API contract and the naming could settle while there was still
only one strategy to be wrong about, and the engine now binds to what it
resolves rather than importing the built-in by name.

What is here now:

- ``StrategyPlugin``, the shape a strategy must present.
- ``session_hedge``, the built-in, whose ``build`` owns the request-to-engine
  parameter assembly (including every hedge-pair field) behind the seam, and
  whose ``execution`` is the staging half -- the module the engine calls
  through to open and stage entries, in ``strategies.session_hedge``.
- Discovery of third-party strategies through the ``ta.strategies`` entry-point
  group, so a strategy can live in its own distribution.

A plugin therefore owns both halves of a backtest — the request-to-parameters
assembly and the entry staging — and the engine binds to whichever the request
resolved. A plugin that declares no ``execution`` runs the built-in staging,
which is what a parameters-only strategy wants.

What is deliberately NOT behind the seam: the per-bar *management* half
(``_manage_pairs``, the survivor ratchets, lock and partial resolution). It
fans out into ~20 further engine privates -- fills, PnL, costs, excursions --
so relocating it would move the god class rather than split it. The engine owns
generic execution machinery; the strategy owns parameters and staging. See
migration-spec.md section 5.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Protocol

from pydantic import BaseModel

from .models import (
    DEFAULT_DOLLARS_PER_PIP_PER_QTY,
    BacktestRequest,
    EngineParams,
    FirmProfileMode,
    PerformanceUnit,
    RiskMode,
)
from .strategies import session_hedge
from .strategies.facade import StrategyExecution

ENTRY_POINT_GROUP = "ta.strategies"
DEFAULT_STRATEGY = "session_hedge"


class StrategyPlugin(Protocol):
    """One backtestable strategy."""

    name: str

    def params_model(self) -> type[BaseModel]:
        """The request model this strategy accepts, for validation and OpenAPI."""
        ...

    def build(self, params: BaseModel) -> EngineParams:
        """Turn validated request parameters into engine parameters."""
        ...

    def execution(self) -> StrategyExecution | None:
        """The staging module the engine should call through, or None for the built-in.

        Optional: ``execution_for`` treats a plugin without it as
        parameters-only, so a third-party strategy that just reshapes engine
        parameters does not have to reimplement staging.
        """
        ...


@dataclass(frozen=True)
class SimpleStrategy:
    """A plugin defined by a name, a params -> EngineParams function and a staging module."""

    name: str
    _params_model: type[BaseModel]
    _build: Callable[[BaseModel], EngineParams]
    _execution: StrategyExecution | None = None

    def params_model(self) -> type[BaseModel]:
        return self._params_model

    def build(self, params: BaseModel) -> EngineParams:
        return self._build(params)

    def execution(self) -> StrategyExecution | None:
        return self._execution


class StrategyBuildInputs(BaseModel):
    """Everything ``session_hedge`` needs to assemble engine parameters.

    The service base comes from ``Settings.engine_params`` (service
    configuration, not strategy logic); the request carries per-call overrides
    and ``timeframe_minutes`` is resolved from the request timeframe before the
    seam, so the strategy never imports service settings or timeframes.
    """

    base: EngineParams
    body: BacktestRequest
    timeframe_minutes: int


def _session_hedge_build(params: BaseModel) -> EngineParams:
    if isinstance(params, EngineParams):
        return params
    if isinstance(params, StrategyBuildInputs):
        return _session_hedge_params(params.base, params.body, params.timeframe_minutes)
    return EngineParams.model_validate(params.model_dump(exclude_none=True))


def _session_hedge_params(
    base: EngineParams, body: BacktestRequest, timeframe_minutes: int
) -> EngineParams:
    """Assemble engine parameters from the service base plus request overrides.

    Moved verbatim behind the seam from the API layer so strategy number two
    overrides hedge-pair fields here instead of forking the service. The
    overlay set is the whole request surface — entry, hedge-pair, OCO, risk,
    cost and sizing fields — and ``model_validate`` still enforces the
    cross-field rules (fixed stop with no distance, ORB multiple of the bar).
    """
    updates: dict[str, object] = {"timeframe_minutes": timeframe_minutes}
    if body.entry_mode is not None:
        updates["entry_mode"] = body.entry_mode
    if body.lock_pips is not None:
        updates["lock_pips"] = body.lock_pips
    if body.lock_mode is not None:
        updates["lock_mode"] = body.lock_mode
    if body.lock_r is not None:
        updates["lock_r"] = body.lock_r
    if body.be_trigger_r is not None:
        updates["be_trigger_r"] = body.be_trigger_r
    if body.survivor_exit_mode is not None:
        updates["survivor_exit_mode"] = body.survivor_exit_mode
    if body.survivor_trail_activation_r is not None:
        updates["survivor_trail_activation_r"] = body.survivor_trail_activation_r
    if body.survivor_trail_gap_r is not None:
        updates["survivor_trail_gap_r"] = body.survivor_trail_gap_r
    if body.hedge_path_mode is not None:
        updates["hedge_path_mode"] = body.hedge_path_mode
    if body.entry_hours_utc_exclude is not None:
        updates["entry_hours_utc_exclude"] = body.entry_hours_utc_exclude
    if body.stop_mode is not None:
        updates["stop_mode"] = body.stop_mode
    if body.sl_mult is not None:
        updates["sl_mult"] = body.sl_mult
    if body.fixed_stop_pips is not None:
        updates["fixed_stop_pips"] = body.fixed_stop_pips
    if body.rr is not None:
        updates["rr"] = body.rr
    if body.tp_mode is not None:
        updates["tp_mode"] = body.tp_mode
    if body.partial_tp_r is not None:
        updates["partial_tp_r"] = body.partial_tp_r
    if body.partial_fraction is not None:
        updates["partial_fraction"] = body.partial_fraction
    if body.min_stop_pips is not None:
        updates["min_stop_pips"] = body.min_stop_pips
    if body.min_stop_cost_mult is not None:
        updates["min_stop_cost_mult"] = body.min_stop_cost_mult
    if body.filter_d1_ema50 is not None:
        updates["filter_d1_ema50"] = body.filter_d1_ema50
    if body.filter_nr7 is not None:
        updates["filter_nr7"] = body.filter_nr7
    if body.filter_orb_atr_min is not None:
        updates["filter_orb_atr_min"] = body.filter_orb_atr_min
    if body.filter_orb_atr_max is not None:
        updates["filter_orb_atr_max"] = body.filter_orb_atr_max
    if body.qty is not None:
        updates["qty"] = body.qty
    if body.orb_minutes is not None:
        updates["orb_minutes"] = body.orb_minutes
    if body.entry_delay_minutes is not None:
        updates["entry_delay_minutes"] = body.entry_delay_minutes
    if body.anchor_tolerance_minutes is not None:
        updates["anchor_tolerance_minutes"] = body.anchor_tolerance_minutes
    if body.intrabar_mode is not None:
        updates["intrabar_mode"] = body.intrabar_mode
    for field in (
        "cost_model",
        "spread_pips_per_side",
        "slippage_pips_per_side",
        "commission_pips_per_side",
        "swap_long_pips_per_rollover",
        "swap_short_pips_per_rollover",
        "swap_rollover_time",
        "swap_timezone",
        "swap_triple_weekday",
        "session_cost_overrides",
        "breakeven_cost_report",
        "risk_mode",
        "risk_pct_per_r",
        "max_pair_risk_pct",
        "max_open_risk_pct",
        "max_concurrent_structures",
        "one_open_per_session",
        "hedge_ratio_initial",
        "hedge_trigger_mode",
        "hedge_failure_k",
        "hedge_ratio_staged",
        "oco_buffer_mode",
        "oco_buffer_value",
        "oco_expiry_bars",
        "allow_reentry",
        "firm_profile",
        "firm_initial_balance",
        "firm_daily_loss_limit_pct",
        "firm_total_loss_limit_pct",
        "firm_timezone",
        "firm_daily_reset_time",
        "time_exit_mode",
        "max_age_hours",
    ):
        value = getattr(body, field)
        if value is not None:
            updates[field] = value
    updates["performance_unit"] = body.performance_unit or PerformanceUnit.PIPS
    updates["dollars_per_pip_per_qty"] = _dollar_rate(body, updates)
    # model_copy skips validators, which would let an override break a cross-field rule
    # (fixed stop with no distance, ORB not a multiple of the bar) and fail silently later.
    return EngineParams.model_validate(base.model_dump() | updates)


def _dollar_rate(body: BacktestRequest, updates: dict[str, object]) -> float | None:
    """The cash value of one pip at ``QTY_REF``, or None when nothing needs cash.

    The client owns this number. It is required whenever results are reported in dollars,
    and also whenever the configuration itself needs cash — fixed-fractional sizing and a
    custom firm profile both size in account currency regardless of how results are shown.
    """
    needs_cash = (
        updates.get("performance_unit") == PerformanceUnit.DOLLARS
        or updates.get("risk_mode") == RiskMode.FIXED_FRACTIONAL
        or updates.get("firm_profile") == FirmProfileMode.CUSTOM
    )
    if not needs_cash:
        return None
    return body.dollars_per_pip_per_qty or DEFAULT_DOLLARS_PER_PIP_PER_QTY


SESSION_HEDGE = SimpleStrategy(
    name=DEFAULT_STRATEGY,
    _params_model=EngineParams,
    _build=_session_hedge_build,
    _execution=session_hedge,
)

_BUILTINS: dict[str, StrategyPlugin] = {SESSION_HEDGE.name: SESSION_HEDGE}


def _discovered() -> dict[str, StrategyPlugin]:
    """Strategies published by other distributions.

    A plugin that fails to load is skipped rather than taking the service down:
    a broken third-party strategy must not stop the built-in one backtesting.
    """
    found: dict[str, StrategyPlugin] = {}
    try:
        points = entry_points(group=ENTRY_POINT_GROUP)
    except Exception:  # noqa: BLE001 - importlib.metadata surface varies by env
        return found
    for point in points:
        try:
            plugin = point.load()
        except Exception:  # noqa: BLE001 - see docstring
            continue
        name = getattr(plugin, "name", point.name)
        found[name] = plugin
    return found


def available() -> dict[str, StrategyPlugin]:
    """Built-ins first, so a third party cannot shadow session_hedge."""
    return {**_discovered(), **_BUILTINS}


def get(name: str | None) -> StrategyPlugin:
    strategies = available()
    key = name or DEFAULT_STRATEGY
    if key not in strategies:
        raise KeyError(f"unknown strategy {key!r}; available: {', '.join(sorted(strategies))}")
    return strategies[key]


def execution_for(plugin: StrategyPlugin) -> StrategyExecution:
    """The staging module to run ``plugin`` with.

    A plugin that declares no ``execution``, or whose ``execution`` returns
    None, is parameters-only and runs the built-in staging. Falling back rather
    than raising keeps the seam additive: publishing a plugin that only
    reshapes engine parameters stays a two-method job.
    """
    getter = getattr(plugin, "execution", None)
    resolved = getter() if callable(getter) else None
    return resolved or session_hedge
