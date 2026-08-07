"""Meta-model folds, leakage guards, and threshold selection."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.ml.meta import metrics as M
from app.ml.meta.baselines import EventFrequency, TakeAll, build_preprocessor, expand_features
from app.ml.meta.features import (
    META_CATEGORICAL_FEATURES,
    META_INPUT_FEATURES,
    META_NUMERIC_FEATURES,
    assert_causal,
    is_outcome_column,
)
from app.ml.meta.folds import assert_no_overlap, audit_split, year_folds


def _events(years: range, per_year: int = 12) -> pd.DataFrame:
    """A synthetic event population with real entry/exit intervals."""
    rows = []
    rng = np.random.default_rng(0)
    for year in years:
        for i in range(per_year):
            entry = pd.Timestamp(year=year, month=1, day=1, tz="UTC") + pd.Timedelta(days=i * 25)
            rows.append(
                {
                    "signal_ts": entry - pd.Timedelta(hours=1),
                    "entry_ts": entry,
                    "exit_ts": entry + pd.Timedelta(hours=24),
                    "side": 1 if i % 2 else -1,
                    "primary_setup_id": "pin_bar_long" if i % 3 else "bull_engulfing",
                    "session": "london",
                    "trend_state": "aligned" if i % 2 else "opposed",
                    "atr_bucket": "mid",
                    "y_meta": int(rng.random() < 0.42),
                    "net_r_3": float(rng.normal(-0.04, 1.2)),
                    "net_r_5": float(rng.normal(-0.07, 1.2)),
                    "net_r_8": float(rng.normal(-0.12, 1.2)),
                }
            )
    return pd.DataFrame(rows).sort_values("signal_ts").reset_index(drop=True)


# --------------------------------------------------------------------------
# Leakage boundary
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "column",
    ["y_meta", "y_meta_3", "net_r_3", "net_r_8", "gross_r", "cost_r_5", "outcome",
     "exit_price", "exit_ts", "bars_to_resolution", "ambiguous_bar", "fwd24_max_atr"],
)
def test_outcome_columns_are_refused_as_features(column):
    """`net_r_3` in particular: the outcome package's deny-list returns False for
    it, so it was excluded only by being absent from an allow-list."""
    assert is_outcome_column(column)
    with pytest.raises(ValueError, match="Outcome columns"):
        assert_causal([column])


def test_the_declared_feature_set_is_causal():
    assert_causal(META_INPUT_FEATURES)
    assert not any(is_outcome_column(name) for name in META_INPUT_FEATURES)


def test_absolute_price_and_atr_stay_out():
    forbidden = {"close", "ema_value", "atr_at_signal", "atr_at_bar", "volume_z"}
    assert forbidden.isdisjoint(META_INPUT_FEATURES)


def test_every_feature_is_categorical_numeric_or_the_shape_vector():
    covered = {*META_CATEGORICAL_FEATURES, *META_NUMERIC_FEATURES, "shape_48"}
    assert covered == set(META_INPUT_FEATURES)


# --------------------------------------------------------------------------
# Folds
# --------------------------------------------------------------------------


def test_folds_expand_and_test_one_year_each():
    frame = _events(range(2010, 2016))
    folds = year_folds(frame, first_test_year=2012, last_test_year=2015)

    assert [f.test_year for f in folds] == [2012, 2013, 2014, 2015]
    sizes = [len(f.train_idx) for f in folds]
    assert sizes == sorted(sizes), "training window must expand"
    for fold in folds:
        assert set(fold.train_idx).isdisjoint(fold.test_idx)


def test_a_trade_still_open_at_the_boundary_is_purged():
    """The overlap that makes naive chronological splits leak."""
    frame = _events(range(2010, 2013))
    # Stretch the last 2011 trade so it is still running well into 2012.
    straddler = frame.index[frame.entry_ts.dt.year == 2011][-1]
    frame.loc[straddler, "exit_ts"] = pd.Timestamp("2012-02-01", tz="UTC")

    folds = year_folds(frame, first_test_year=2012, last_test_year=2012)

    assert straddler not in set(folds[0].train_idx)
    assert folds[0].purged >= 1
    assert_no_overlap(frame, folds)


def test_leakage_check_catches_an_overlap_it_should_have_purged():
    frame = _events(range(2010, 2013))
    folds = year_folds(frame, first_test_year=2012, last_test_year=2012)
    poisoned = [type(folds[0])(2012, np.array([0, 1]), folds[0].test_idx, 0)]
    frame.loc[0, "exit_ts"] = pd.Timestamp("2012-06-01", tz="UTC")

    with pytest.raises(AssertionError, match="Leakage"):
        assert_no_overlap(frame, poisoned)


def test_candidate_evaluation_never_scores_an_audit_event():
    """The threshold is chosen on out-of-fold predictions and then frozen. If a
    fold could reach into the audit years, that choice would be fitted to the
    block it is supposed to be tested on."""
    frame = _events(range(2010, 2017))
    frame["event_id"] = [f"e{i}" for i in range(len(frame))]
    development_idx, audit_idx = audit_split(frame, audit_from_year=2015)
    development = frame.iloc[development_idx].reset_index(drop=True)

    folds = year_folds(development, first_test_year=2012, last_test_year=2014)
    scored = {
        development.iloc[i]["event_id"]
        for fold in folds
        for i in [*fold.train_idx, *fold.test_idx]
    }
    audit_ids = set(frame.iloc[audit_idx]["event_id"])

    assert scored, "folds must actually score something"
    assert scored.isdisjoint(audit_ids)
    assert max(f.test_year for f in folds) < 2015


def test_audit_block_is_separated_by_the_embargo():
    frame = _events(range(2010, 2016))
    development, audit = audit_split(frame, audit_from_year=2015)

    assert set(development).isdisjoint(audit)
    assert pd.to_datetime(frame.iloc[audit].entry_ts).min().year == 2015
    assert pd.to_datetime(frame.iloc[development].exit_ts).max() <= pd.Timestamp(
        "2015-01-01", tz="UTC"
    )


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def test_lift_is_measured_against_take_all_on_the_same_block():
    """Absolute net R is not comparable across blocks — take-all is +0.03R on the
    audit years and -0.05R on development, so only lift means anything."""
    frame = _events(range(2010, 2012))
    frame["net_r_3"] = [1.0] * 12 + [-1.0] * 12
    p = np.array([0.9] * 12 + [0.1] * 12)

    result = M.at_threshold(frame, p, 0.5)

    assert result["taken"] == 12
    assert result["net_r_3_per_event"] == pytest.approx(1.0)
    assert result["lift_vs_take_all"] == pytest.approx(1.0)


def test_a_model_that_selects_nothing_useful_shows_no_lift():
    frame = _events(range(2010, 2012))
    p = np.full(len(frame), 0.6)

    result = M.at_threshold(frame, p, 0.5)

    assert result["take_rate"] == 1.0
    assert result["lift_vs_take_all"] == pytest.approx(0.0)


def test_threshold_selection_honours_the_minimum_take_rate():
    """Without a floor the sweep always picks the extreme, where a handful of
    lucky events set the mean."""
    frame = _events(range(2010, 2013))
    p = np.linspace(0.0, 1.0, len(frame))

    rows = M.sweep(frame, p, min_take_rate=0.25)

    assert rows
    assert all(row["take_rate"] >= 0.25 for row in rows)


def test_reliability_bins_the_positive_probability_not_argmax_confidence():
    y = np.array([0, 0, 1, 1, 1, 1])
    p = np.array([0.1, 0.1, 0.9, 0.9, 0.9, 0.9])

    out = M.reliability(y, p, bins=10)

    # Both groups are 0.1 away from their observed rate, so ECE is 0.1 whatever
    # the weighting: 2/6 * |0.1 - 0.0| + 4/6 * |0.9 - 1.0|.
    assert out["ece"] == pytest.approx(0.1, abs=1e-9)
    counts = [b["count"] for b in out["bins"]]
    assert counts[1] == 2, "p=0.1 belongs to the [0.1, 0.2) bin"
    assert counts[9] == 4, "p=0.9 belongs to the final bin"
    assert sum(counts) == len(y)


# --------------------------------------------------------------------------
# Baselines
# --------------------------------------------------------------------------


def test_take_all_predicts_the_base_rate_and_takes_everything():
    frame = _events(range(2010, 2012))
    model = TakeAll().fit(frame, frame["y_meta"])

    p = model.predict_proba(frame)

    assert np.allclose(p, frame["y_meta"].mean())
    assert M.at_threshold(frame, p, 0.0)["take_rate"] == 1.0


def test_event_frequency_shrinks_thin_cells_toward_the_global_rate():
    frame = _events(range(2010, 2013))
    frame.loc[frame.index[:2], "primary_setup_id"] = "rectangle"
    frame.loc[frame.index[:2], "y_meta"] = 1

    model = EventFrequency(smoothing=10.0).fit(frame, frame["y_meta"])
    rare = model.predict_proba(frame.iloc[:1])[0]

    # Two wins out of two, but shrunk well below 1.0 by the smoothing prior.
    assert model.global_ < rare < 1.0


def test_an_unseen_category_does_not_break_transform():
    """A session that only appears in a later fold must not fail at predict."""
    train = _events(range(2010, 2012))
    later = _events(range(2012, 2013))
    later["session"] = "sydney"

    for name in META_INPUT_FEATURES:
        if name not in train.columns:
            train[name] = 0.0 if name != "shape_48" else [[0.0] * 48] * len(train)
            later[name] = 0.0 if name != "shape_48" else [[0.0] * 48] * len(later)

    pre = build_preprocessor().fit(expand_features(train))
    out = pre.transform(expand_features(later))

    assert out.shape[0] == len(later)
    assert np.isfinite(out).all()
