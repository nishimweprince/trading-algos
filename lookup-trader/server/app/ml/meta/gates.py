"""Rule-based event gates applied before the meta-model sees an event.

A gate is a fixed, causal rule over signal-time columns. It is not fitted: it
removes a slice of the event population the model would otherwise have to learn
to avoid, and it is chosen once, ahead of training, rather than tuned per fold.

`trend_state` and `htf_trend_state` are side-canonicalised in the export —
`aligned` means the bar's trend agrees with the trade's side — so the same rule
covers longs and shorts.

Evidence for `trend_aligned` on XAUUSD H1/H4, 2009-2024 (meta_events_v1,
2 ATR stop / 3 ATR target / 24 bars, 3-pip cost):

    htf opposed            net -0.081R  gross -0.032R  0/16 years positive
    ltf + htf aligned      net -0.011R  gross +0.038R  8/16 years positive

The opposed half loses before costs in every development year; the aligned half
carries the only positive gross expectancy in the population.
"""

from __future__ import annotations

from collections.abc import Callable

import pandas as pd

ALIGNED = "aligned"


def _none(frame: pd.DataFrame) -> pd.Series:
    return pd.Series(True, index=frame.index)


def _htf_aligned(frame: pd.DataFrame) -> pd.Series:
    return frame["htf_trend_state"].eq(ALIGNED)


def _trend_aligned(frame: pd.DataFrame) -> pd.Series:
    # A missing state is not alignment: `eq` is False for null, so an event
    # whose context could not be computed is dropped rather than admitted.
    return frame["trend_state"].eq(ALIGNED) & frame["htf_trend_state"].eq(ALIGNED)


GATES: dict[str, Callable[[pd.DataFrame], pd.Series]] = {
    "none": _none,
    "htf_aligned": _htf_aligned,
    "trend_aligned": _trend_aligned,
}


def gate_mask(frame: pd.DataFrame, name: str) -> pd.Series:
    """Boolean mask of events that pass gate `name`."""
    try:
        rule = GATES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown gate {name!r}; expected one of {sorted(GATES)}") from exc
    return rule(frame).fillna(False).astype(bool)


def apply_gate(frame: pd.DataFrame, name: str) -> pd.DataFrame:
    """Events that pass gate `name`, in their original order with a fresh index."""
    return frame.loc[gate_mask(frame, name)].reset_index(drop=True)
