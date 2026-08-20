"""Headline measurement metrics for the session-open hedge."""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from statistics import mean, median
from typing import Literal

Z95 = 1.959963984540054

OutcomeKind = Literal["tp", "lock", "breakeven", "whipsaw", "time_exit"]


@dataclass(frozen=True, slots=True)
class OutcomeMix:
    tp: float
    lock: float
    breakeven: float
    whipsaw: float
    time_exit: float


@dataclass(frozen=True, slots=True)
class HeadlineMetrics:
    survivor_tp_rate: float | None
    mean_loss_r: float | None
    breakeven_tp_rate_required: float | None
    tp_rate_margin_pp: float | None
    tp_rate_margin_pp_ci_low: float | None
    tp_rate_margin_pp_ci_high: float | None
    outcome_mix: OutcomeMix
    max_concurrent_structures: int
    median_concurrent: float | None
    n_closed: int


def classify_pair(
    *,
    locked: bool,
    same_bar: bool,
    long_bucket: str | None,
    short_bucket: str | None,
    pair_r: float,
    time_exit: bool = False,
) -> OutcomeKind:
    """Classify a resolved pair.

    A lock-exit survivor is a ``win`` of about ``LOCK_PIPS``, not a target. Survivor-TP is the
    ``+2R`` outcome (stopped hedge at ``-1R``, target at ``+RR``). Pair R around ``+2`` is the
    discriminator; a ``+20 pip`` lock must not count as TP.
    """
    if time_exit:
        return "time_exit"
    buckets = [bucket for bucket in (long_bucket, short_bucket) if bucket is not None]
    if buckets.count("loss") == 2:
        return "whipsaw"
    if pair_r >= 1.5:
        return "tp"
    if "be" in buckets and "loss" in buckets:
        return "lock" if locked else "breakeven"
    if buckets and all(bucket == "be" for bucket in buckets):
        return "breakeven"
    if locked:
        return "lock"
    if pair_r <= -1.5:
        return "whipsaw"
    return "lock"


def breakeven_tp_rate_required(mean_loss_r: float) -> float:
    abs_loss = abs(mean_loss_r)
    return abs_loss / (2.0 + abs_loss)


def wilson_interval(k: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    if n <= 0:
        return None
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    spread = z * sqrt((p * (1 - p) + z2 / (4 * n)) / n) / denom
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def headline(
    *,
    outcomes: list[OutcomeKind],
    r_multiples: list[float],
    concurrent_samples: list[int],
) -> HeadlineMetrics:
    n = len(outcomes)
    mix = OutcomeMix(tp=0.0, lock=0.0, breakeven=0.0, whipsaw=0.0, time_exit=0.0)
    if n == 0:
        max_c = max(concurrent_samples) if concurrent_samples else 0
        med_c = float(median(concurrent_samples)) if concurrent_samples else None
        return HeadlineMetrics(
            survivor_tp_rate=None,
            mean_loss_r=None,
            breakeven_tp_rate_required=None,
            tp_rate_margin_pp=None,
            tp_rate_margin_pp_ci_low=None,
            tp_rate_margin_pp_ci_high=None,
            outcome_mix=mix,
            max_concurrent_structures=max_c,
            median_concurrent=med_c,
            n_closed=0,
        )
    counts = {
        kind: outcomes.count(kind)
        for kind in ("tp", "lock", "breakeven", "whipsaw", "time_exit")
    }
    mix = OutcomeMix(
        tp=counts["tp"] / n,
        lock=counts["lock"] / n,
        breakeven=counts["breakeven"] / n,
        whipsaw=counts["whipsaw"] / n,
        time_exit=counts["time_exit"] / n,
    )
    tp_rate = mix.tp
    loss_rs = [r for kind, r in zip(outcomes, r_multiples, strict=True) if kind != "tp"]
    mean_loss = mean(loss_rs) if loss_rs else 0.0
    required = breakeven_tp_rate_required(mean_loss)
    margin_pp = (tp_rate - required) * 100.0
    interval = wilson_interval(counts["tp"], n)
    if interval is None:
        lo_pp = hi_pp = None
    else:
        lo_pp = (interval[0] - required) * 100.0
        hi_pp = (interval[1] - required) * 100.0
    max_c = max(concurrent_samples) if concurrent_samples else 0
    med_c = float(median(concurrent_samples)) if concurrent_samples else None
    return HeadlineMetrics(
        survivor_tp_rate=tp_rate,
        mean_loss_r=mean_loss,
        breakeven_tp_rate_required=required,
        tp_rate_margin_pp=margin_pp,
        tp_rate_margin_pp_ci_low=lo_pp,
        tp_rate_margin_pp_ci_high=hi_pp,
        outcome_mix=mix,
        max_concurrent_structures=max_c,
        median_concurrent=med_c,
        n_closed=n,
    )
