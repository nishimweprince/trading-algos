from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, model_validator


class MetaEventReviewIn(BaseModel):
    verdict: Literal["valid", "invalid", "uncertain"] | None = None
    notes: str | None = None
    phase: Literal["pre", "post"] = "pre"

    @model_validator(mode="after")
    def validate_phase(self):
        if self.phase == "pre" and self.verdict is None:
            raise ValueError("verdict is required before reveal")
        if self.phase == "post" and not (self.notes or "").strip():
            raise ValueError("post-reveal notes cannot be empty")
        return self
