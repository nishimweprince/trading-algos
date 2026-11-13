"""Rule-based event gates applied ahead of the meta-model."""

from __future__ import annotations

import pandas as pd
import pytest

from app.ml.meta.gates import GATES, apply_gate, gate_mask


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "event": ["a", "b", "c", "d", "e"],
            "trend_state": ["aligned", "aligned", "opposed", "opposed", None],
            "htf_trend_state": ["aligned", "opposed", "aligned", "opposed", "aligned"],
        },
        index=[10, 11, 12, 13, 14],
    )


def test_none_keeps_everything() -> None:
    assert gate_mask(_frame(), "none").tolist() == [True] * 5


def test_htf_aligned_uses_only_the_higher_timeframe() -> None:
    assert apply_gate(_frame(), "htf_aligned")["event"].tolist() == ["a", "c", "e"]


def test_trend_aligned_requires_both_timeframes() -> None:
    assert apply_gate(_frame(), "trend_aligned")["event"].tolist() == ["a"]


def test_missing_state_is_not_alignment() -> None:
    # Event "e" has HTF alignment but no LTF state: it must not pass.
    assert not gate_mask(_frame(), "trend_aligned").loc[14]


def test_apply_gate_preserves_order_and_resets_index() -> None:
    gated = apply_gate(_frame(), "htf_aligned")
    assert gated.index.tolist() == [0, 1, 2]


def test_mask_aligns_with_input_index() -> None:
    frame = _frame()
    assert gate_mask(frame, "trend_aligned").index.equals(frame.index)


def test_unknown_gate_raises() -> None:
    with pytest.raises(ValueError, match="Unknown gate"):
        gate_mask(_frame(), "nope")


def test_registry_names() -> None:
    assert set(GATES) == {"none", "htf_aligned", "trend_aligned"}
