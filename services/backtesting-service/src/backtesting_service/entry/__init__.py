"""Entry-mode plans used by the shared closed-bar engine."""

from .base import EntryPlan
from .hedge_pair import (
    contingent_hedge_fill,
    contingent_hedge_touched,
    failure_threshold,
    hedge_pair_plan,
)
from .synthetic import SyntheticOrderPlan, synthetic_order_plan

__all__ = [
    "EntryPlan",
    "SyntheticOrderPlan",
    "contingent_hedge_fill",
    "contingent_hedge_touched",
    "failure_threshold",
    "hedge_pair_plan",
    "synthetic_order_plan",
]
