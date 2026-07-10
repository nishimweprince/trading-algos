from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Literal, Union

Direction = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class Signal:
    id: str
    source_chat: str
    message_id: int
    message_date: str
    raw_text: str
    symbol: str
    direction: Direction
    entry: Decimal | None = None
    sl: Decimal | None = None
    tp: Decimal | None = None
    all_tps: list[Decimal] = field(default_factory=list)

    @property
    def missing_sl(self) -> bool:
        return self.sl is None

    @property
    def missing_tp(self) -> bool:
        return self.tp is None

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("entry", "sl", "tp"):
            data[key] = str(data[key]) if data[key] is not None else None
        data["all_tps"] = [str(tp) for tp in self.all_tps]
        data["missing_sl"] = self.missing_sl
        data["missing_tp"] = self.missing_tp
        return data


@dataclass(frozen=True)
class ParseFailure:
    reason: str


ParsedSignal = Union[Signal, ParseFailure]


@dataclass(frozen=True)
class ExecutionPlan:
    signal: Signal
    mt5_symbol: str
    volume: float
    dry_run: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "signal": self.signal.to_json(),
            "mt5_symbol": self.mt5_symbol,
            "volume": self.volume,
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True)
class ExecutionResult:
    status: Literal["dry_run", "sent", "rejected", "failed"]
    plan: ExecutionPlan
    details: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "plan": self.plan.to_json(),
            "details": self.details,
        }
