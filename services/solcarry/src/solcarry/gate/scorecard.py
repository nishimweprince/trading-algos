"""Pre-registered thresholds -> PASS / FAIL / INSUFFICIENT_DATA per variant; CONTINUE / KILL."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import Thresholds

PASS, FAIL, INSUFFICIENT = "PASS", "FAIL", "INSUFFICIENT_DATA"
CONTINUE, KILL = "CONTINUE", "KILL"


@dataclass
class Criterion:
    key: str
    value: float | None
    bar: str
    ok: bool | None  # None = not evaluable


@dataclass
class VariantScore:
    variant: str
    status: str
    criteria: list[Criterion]
    failing: list[str]
    flags: list[str] = field(default_factory=list)
    note: str = ""

    def to_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {"variant": self.variant, "status": self.status}
        for c in self.criteria:
            row[c.key] = c.value
            row[f"{c.key}_ok"] = c.ok
        row["failing"] = ";".join(self.failing)
        row["flags"] = ";".join(self.flags)
        row["note"] = self.note
        return row


def _finite(x: Any) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)


def _cmp(value: Any, bar: float, op: str) -> bool | None:
    if not _finite(value):
        return None
    v = float(value)
    return {">": v > bar, "<": v < bar, ">=": v >= bar}[op]


def score_variant(
    summary: dict[str, Any],
    th: Thresholds,
    depth_impact: float | None,
    short_sample_flag: bool = False,
) -> VariantScore:
    v = summary["variant"]
    ex = summary.get("excess", {})
    crit: list[Criterion] = []

    def add(key: str, value: Any, bar: str, test: bool | None) -> None:
        crit.append(Criterion(key, float(value) if _finite(value) else None, bar, test))

    med, p5, shn = ex.get("median"), ex.get("p5"), summary.get("share_negative")
    add(
        "excess_median",
        med,
        f"> {th.excess_median_min}",
        _cmp(med, th.excess_median_min, ">"),
    )
    add("excess_p5", p5, f"> {th.excess_p5_min}", _cmp(p5, th.excess_p5_min, ">"))
    add(
        "share_negative",
        shn,
        f"< {th.share_negative_max}",
        _cmp(shn, th.share_negative_max, "<"),
    )
    if summary.get("kind") == "P":
        nr = summary.get("neg_run_days")
        add(
            "neg_run_days",
            nr,
            f"< {th.neg_run_max_days}",
            _cmp(nr, th.neg_run_max_days, "<"),
        )
    if summary.get("uses_lst"):
        add(
            "depth_impact",
            depth_impact,
            f"> {th.depth_impact_max}",
            _cmp(depth_impact, th.depth_impact_max, ">"),
        )
    days = summary.get("sample_days") or 0.0
    add("sample_days", days, f">= {th.min_sample_days}", days >= th.min_sample_days)

    flags = list(summary.get("flags", []))
    if short_sample_flag and "SHORT_SAMPLE" not in flags:
        flags.append("SHORT_SAMPLE")
    failing = [c.key for c in crit if c.ok is False and c.key != "sample_days"]
    unevaluable = [c.key for c in crit if c.ok is None]
    if days < th.min_sample_days:
        status, note = INSUFFICIENT, f"sample {days:.0f} d < {th.min_sample_days:.0f} d"
        if "SHORT_SAMPLE" not in flags:
            flags.append("SHORT_SAMPLE")
    elif unevaluable:
        status, note = INSUFFICIENT, "not evaluable: " + ", ".join(unevaluable)
    elif failing:
        status, note = FAIL, ""
    else:
        status, note = PASS, ""
    return VariantScore(v, status, crit, failing, flags, note)


def overall(scores: list[VariantScore]) -> str:
    return CONTINUE if any(s.status == PASS for s in scores) else KILL


def scores_to_json(scores: list[VariantScore]) -> list[dict[str, Any]]:
    return [asdict(s) for s in scores]
