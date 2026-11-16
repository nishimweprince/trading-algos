"""SPEC §8.3: the incremental detectors emit exactly the batch signals of patterns.detect_all / hs.detect."""

import numpy as np
import pandas as pd
import pytest
from hs import detect
from lib import atr, resample
from patterns import detect_all

from walkforward_patterns.patterns import HsDetector, PatternDetector


def _run(det, lt, a):
    out = []
    for h, l, c, av in zip(lt.h, lt.l, lt.c, a, strict=True):
        out.extend(det.step(h, l, c, av))
    return pd.DataFrame(out)


def _same(ours: pd.DataFrame, ref: pd.DataFrame):
    assert len(ours) == len(ref)
    if len(ref) == 0:
        return
    ours = ours[ref.columns].reset_index(drop=True)
    for col in ref.columns:
        a, b = ours[col].to_numpy(), ref[col].reset_index(drop=True).to_numpy()
        if b.dtype.kind in "fc":
            np.testing.assert_allclose(a.astype(float), b.astype(float), rtol=0, atol=1e-12, err_msg=col)
        else:
            assert (a == b).all(), col


@pytest.mark.parametrize("which", ["synthetic", "real"])
@pytest.mark.parametrize("rule", ["5min", "15min"])
@pytest.mark.parametrize("k", [3, 5])
def test_patterns_incremental_equals_batch(request, which, rule, k):
    m1 = request.getfixturevalue("m1" if which == "synthetic" else "real_m1")
    lt = resample(m1, rule)
    a = atr(lt)
    ref = detect_all(lt, a, k)
    ours = _run(PatternDetector(k), lt, a.values)
    assert len(ref) > 10
    _same(ours, ref)


@pytest.mark.parametrize("which", ["synthetic", "real"])
@pytest.mark.parametrize("rule", ["5min", "15min"])
@pytest.mark.parametrize("k", [3, 5])
def test_hs_incremental_equals_batch(request, which, rule, k):
    m1 = request.getfixturevalue("m1" if which == "synthetic" else "real_m1")
    lt = resample(m1, rule)
    a = atr(lt)
    ref = detect(lt, a, k)
    ours = _run(HsDetector(k), lt, a.values)
    _same(ours, ref)
