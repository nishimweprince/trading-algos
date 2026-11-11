from __future__ import annotations

import pandas as pd
import pytest

from solcarry.analytics.sizing import required_capital, sizing_table
from solcarry.analytics.stats import describe, longest_true_run, quantiles, share_negative
from solcarry.config import Thresholds
from solcarry.gate.scorecard import CONTINUE, FAIL, INSUFFICIENT, KILL, PASS, overall, score_variant


def _summary(kind="P", med=0.05, p5=0.0, shn=0.1, run=10.0, days=400, lst=True):
    return {
        "variant": "X",
        "kind": kind,
        "excess": {"median": med, "p5": p5},
        "share_negative": shn,
        "neg_run_days": run,
        "sample_days": days,
        "uses_lst": lst,
        "flags": [],
    }


def test_pass():
    s = score_variant(_summary(), Thresholds(), depth_impact=-0.001)
    assert s.status == PASS and not s.failing


def test_fail_lists_criteria():
    s = score_variant(_summary(med=0.01, shn=0.5), Thresholds(), depth_impact=-0.01)
    assert s.status == FAIL
    assert set(s.failing) == {"excess_median", "share_negative", "depth_impact"}


def test_insufficient_sample():
    s = score_variant(_summary(days=100), Thresholds(), depth_impact=-0.001)
    assert s.status == INSUFFICIENT and "SHORT_SAMPLE" in s.flags


def test_missing_depth_not_evaluable():
    s = score_variant(_summary(), Thresholds(), depth_impact=None)
    assert s.status == INSUFFICIENT


def test_dated_ignores_neg_run_and_native_ignores_depth():
    s = score_variant(_summary(kind="D", run=None, lst=False), Thresholds(), depth_impact=None)
    assert s.status == PASS
    assert {c.key for c in s.criteria} == {
        "excess_median",
        "excess_p5",
        "share_negative",
        "sample_days",
    }


def test_overall():
    p = score_variant(_summary(), Thresholds(), -0.001)
    f = score_variant(_summary(med=-1), Thresholds(), -0.001)
    assert overall([p, f]) == CONTINUE
    assert overall([f]) == KILL
    assert overall([]) == KILL


def test_stats():
    s = pd.Series([-1.0, 0.0, 1.0, 2.0])
    assert share_negative(s) == 0.25
    assert describe(s)["median"] == 0.5
    assert quantiles(s, [0.5])["q0.5"] == 0.5
    assert longest_true_run([True, True, False, True]) == 2
    assert longest_true_run([]) == 0


def test_sizing():
    assert required_capital(1000, 0.08) == pytest.approx(50_000)
    assert required_capital(1000, -0.01) == float("inf")
    t = sizing_table([{"variant": "A", "R_median": 0.1, "R_p25": 0.05}], 1000)
    assert t["capital_at_p25"].iloc[0] == pytest.approx(80_000)
