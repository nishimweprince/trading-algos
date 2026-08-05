"""The tag vocabulary and the shapes it is stored in.

A tag says "this bar looks like X", not "X will work". The whole point of the
store is to let `/base-rate` answer the second question over a population the
tagger picked out, so nothing in here is allowed to imply an outcome.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, NamedTuple

TagState = Literal["complete", "forming", "invalidated"]
TagSource = Literal["rule", "algorithm", "llm"]

_ZERO_RANGE = 1e-9


class Bar(NamedTuple):
    """One candle, with the geometry every rule reads.

    The derived properties exist so a rule reads `bar.lower_pct` instead of
    recomputing `(min(o, c) - l) / (h - l)`, and so the zero-range guard lives in
    one place rather than in five rules that each might forget it.
    """

    open: float
    high: float
    low: float
    close: float

    @property
    def range(self) -> float:
        # Same guard as `_signal_candle_anatomy`: a bar whose high equals its low
        # is a data artefact, and dividing by it would poison every ratio.
        return (self.high - self.low) or _ZERO_RANGE

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def body_pct(self) -> float:
        return self.body / self.range

    @property
    def upper_pct(self) -> float:
        return self.upper_wick / self.range

    @property
    def lower_pct(self) -> float:
        return self.lower_wick / self.range

    @property
    def close_pos(self) -> float:
        """Where the close sits in the bar's range: 0 at the low, 1 at the high."""
        return (self.close - self.low) / self.range

    @property
    def is_bull(self) -> bool:
        return self.close > self.open

    @property
    def is_bear(self) -> bool:
        return self.close < self.open


@dataclass(frozen=True)
class BarTag:
    """One pattern present on one bar.

    `side` is carried per-instance rather than read from `setups.default_side`
    because `inside_break` has no inherent direction — the break itself decides
    it, and only the tagger knows which way it went.
    """

    setup_id: str
    state: TagState = "complete"
    confidence: float = 1.0
    source: TagSource = "rule"
    side: int | None = None
    model_version: str | None = None

    def to_json(self) -> dict:
        # Fixed key order: the stored JSON is compared byte-for-byte against a
        # live recomputation in the parity test.
        return {
            "setup_id": self.setup_id,
            "state": self.state,
            "confidence": self.confidence,
            "source": self.source,
            "side": self.side,
            "model_version": self.model_version,
        }

    @classmethod
    def from_json(cls, payload: dict) -> BarTag:
        return cls(
            setup_id=str(payload["setup_id"]),
            state=payload.get("state", "complete"),
            confidence=float(payload.get("confidence", 1.0)),
            source=payload.get("source", "rule"),
            side=None if payload.get("side") is None else int(payload["side"]),
            model_version=payload.get("model_version"),
        )


@dataclass(frozen=True)
class TagResult:
    """Every tag on one bar, plus the version of the tagger that produced them."""

    tags: tuple[BarTag, ...] = ()
    version: str = ""

    @classmethod
    def of(cls, tags: Sequence[BarTag], version: str) -> TagResult:
        """Tags in a deterministic order: strongest match first, id as tie-break.

        Ordering is not cosmetic. It makes the serialized JSON a function of the
        bar alone, which is what lets the build-time and live paths be compared
        as strings instead of as sets.
        """
        return cls(tuple(sorted(tags, key=lambda t: (-t.confidence, t.setup_id))), version)

    @classmethod
    def empty(cls, version: str = "") -> TagResult:
        return cls((), version)

    @classmethod
    def from_json(cls, payload: dict | str | None) -> TagResult:
        if not payload:
            return cls.empty()
        if isinstance(payload, str):
            payload = json.loads(payload)
        return cls(
            tuple(BarTag.from_json(t) for t in payload.get("tags", [])),
            str(payload.get("version", "")),
        )

    def to_json(self) -> dict:
        return {"version": self.version, "tags": [t.to_json() for t in self.tags]}

    def setup_ids(self) -> list[str]:
        return [t.setup_id for t in self.tags]

    def primary(self) -> BarTag | None:
        """The strongest fully-formed tag, or None.

        Defined over `complete` tags only so the meaning survives Phase 4, when
        `forming` tags start appearing and must not outrank a finished pattern.
        """
        for tag in self.tags:
            if tag.state == "complete":
                return tag
        return None

    def primary_setup_id(self) -> str:
        """The primary tag's id, or "" — never None.

        An all-None string column in a quiet month writes a null-typed Parquet
        column that the union view then has to reconcile against VARCHAR. The
        empty string keeps the column typed; `bar_tags._primary_or_none` turns it
        back into None at the API boundary.
        """
        primary = self.primary()
        return primary.setup_id if primary else ""
