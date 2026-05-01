"""Live trade execution for auto-fired Signals.

Wraps `CapitalComClient.create_position` and uses `InstrumentMapper` for
epic resolution and `order_helpers` for SL/TP rounding. Failures are logged
and swallowed so a bad order never crashes the polling loop.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from loguru import logger

from app.config import Settings
from app.core.types import Signal
from app.data.instrument_mapper import InstrumentMapper
from app.data.providers.capital_com import CapitalComClient
from app.engine.event_log import EventLog
from app.execution.order_helpers import (
    get_decimal_places,
    get_min_deal_size,
    round_stop_loss,
    round_take_profit,
)


_MARKET_INFO_TTL = timedelta(minutes=5)


class TradeExecutor:
    """Place a live broker order for a generated Signal."""

    def __init__(self, client: CapitalComClient, settings: Settings,
                 event_log: EventLog, mapper: Optional[InstrumentMapper] = None):
        self.client = client
        self.settings = settings
        self.event_log = event_log
        self.mapper = mapper or InstrumentMapper()
        self._market_cache: Dict[str, tuple[datetime, Dict]] = {}

    def _get_market_info(self, epic: str) -> Optional[Dict]:
        cached = self._market_cache.get(epic)
        now = datetime.now(timezone.utc)
        if cached and (now - cached[0]) < _MARKET_INFO_TTL:
            return cached[1]
        try:
            info = self.client.get_market_details(epic)
            self._market_cache[epic] = (now, info)
            return info
        except Exception as e:
            logger.warning(f"TradeExecutor: market_details failed for {epic}: {e}")
            return None

    def execute(self, signal: Signal) -> Optional[Dict]:
        epic = self.mapper.to_capitalcom_epic(signal.symbol)

        market_info = self._get_market_info(epic) or {}
        decimals = get_decimal_places(market_info)
        min_deal = get_min_deal_size(market_info) or 0.0
        size = max(self.settings.capital_execution_size, min_deal)

        sl = round_stop_loss(signal.sl, decimals, signal.direction.value)
        tp = round_take_profit(signal.tp, decimals, signal.direction.value)

        try:
            response = self.client.create_position(
                epic=epic,
                direction=signal.direction.value,
                size=size,
                stop_loss=sl,
                take_profit=tp,
                guaranteed_stop=self.settings.capital_execution_guaranteed_stop,
            )
            self.event_log.log(
                signal.symbol, signal.timeframe, 'execution',
                {
                    'status': 'PLACED',
                    'signal_id': signal.id,
                    'epic': epic,
                    'direction': signal.direction.value,
                    'size': size,
                    'stop_level': sl,
                    'profit_level': tp,
                    'deal_reference': response.get('dealReference'),
                    'environment': self.settings.capital_environment,
                },
            )
            logger.info(
                f"Order placed: {signal.symbol} {signal.direction.value} size={size} "
                f"sl={sl} tp={tp} dealRef={response.get('dealReference')}"
            )
            return response
        except Exception as e:
            self.event_log.log(
                signal.symbol, signal.timeframe, 'execution',
                {
                    'status': 'FAILED',
                    'signal_id': signal.id,
                    'epic': epic,
                    'direction': signal.direction.value,
                    'size': size,
                    'stop_level': sl,
                    'profit_level': tp,
                    'error': str(e),
                    'environment': self.settings.capital_environment,
                },
            )
            logger.exception(f"Order placement failed for {signal.symbol}: {e}")
            return None
