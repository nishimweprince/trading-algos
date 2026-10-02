"""Read-only signed account reads: fee rates, account config, server time.

These exist for the OFI scalper's Stage 0 measurements (actual fee tier,
signed-request round trip). Every call is a GET; the key should be created
with "Enable Reading" only.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .rest import FapiRest

__all__ = ["AccountReader", "CommissionRate"]

WEIGHT_COMMISSION_RATE = 20
WEIGHT_ACCOUNT_CONFIG = 5
WEIGHT_SERVER_TIME = 1


@dataclass(frozen=True, slots=True)
class CommissionRate:
    symbol: str
    maker: float  # a fraction: 0.0002 is 2 bp
    taker: float

    @property
    def maker_bp(self) -> float:
        return self.maker * 10_000

    @property
    def taker_bp(self) -> float:
        return self.taker * 10_000


class AccountReader:
    def __init__(self, rest: FapiRest, *, clock_ns: Callable[[], int] = time.perf_counter_ns):
        self._rest = rest
        self._clock_ns = clock_ns

    @property
    def available(self) -> bool:
        return self._rest.can_sign

    async def commission_rate(self, symbol: str) -> CommissionRate:
        row = await self._rest.signed_get(
            "/fapi/v1/commissionRate",
            {"symbol": symbol},
            WEIGHT_COMMISSION_RATE,
            unavailable="account_unavailable",
        )
        return CommissionRate(
            symbol=row["symbol"],
            maker=float(row["makerCommissionRate"]),
            taker=float(row["takerCommissionRate"]),
        )

    async def account_config(self) -> dict[str, Any]:
        """feeTier, canTrade, canWithdraw, dualSidePosition, multiAssetsMargin."""
        payload = await self._rest.signed_get(
            "/fapi/v1/accountConfig", {}, WEIGHT_ACCOUNT_CONFIG, unavailable="account_unavailable"
        )
        keep = (
            "feeTier",
            "canTrade",
            "canDeposit",
            "canWithdraw",
            "dualSidePosition",
            "multiAssetsMargin",
            "tradeGroupId",
        )
        return {key: payload[key] for key in keep if key in payload}

    async def server_time_ms(self) -> int:
        payload = await self._rest.get(
            "/fapi/v1/time", {}, WEIGHT_SERVER_TIME, unavailable="broker_not_ready"
        )
        return int(payload["serverTime"])

    async def timed(self, signed: bool) -> float:
        """One round trip in milliseconds: accountConfig if signed, else time."""
        start = self._clock_ns()
        if signed:
            await self.account_config()
        else:
            await self.server_time_ms()
        return (self._clock_ns() - start) / 1e6
