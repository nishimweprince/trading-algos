from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

RecommendationVerdict = Literal["buy", "sell", "wait", "insufficient_data"]


class RecommendationOut(BaseModel):
    verdict: RecommendationVerdict
    headline: str
    rationale: str
    caveats: list[str] = []

