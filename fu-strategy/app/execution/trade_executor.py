"""Live trade execution for auto-fired Signals.

Wraps `CapitalComClient.create_position` and uses `InstrumentMapper` for
epic resolution and `order_helpers` for SL/TP rounding. Stale signal levels
are pushed onto the correct side of live market price and clear of the
broker's minimum-distance rule. Failures are logged and swallowed so a bad
order never crashes the polling loop.
"""
from __future__ import annotations

from typing import Dict, Optional

from loguru import logger

from app.config import Settings
from app.core.types import Signal
from app.data.instrument_mapper import InstrumentMapper
from app.data.providers.capital_com import CapitalComClient
from app.engine.event_log import EventLog
from app.execution.order_helpers import (
    adjust_levels_for_broker,
    get_decimal_places,
    get_market_prices,
    get_min_deal_size,
    get_min_stop_distance,
    round_stop_loss,
    round_take_profit,
)


class TradeExecutor:
    """Place a live broker order for a generated Signal."""

    def __init__(self, client: CapitalComClient, settings: Settings,
                 event_log: EventLog, mapper: Optional[InstrumentMapper] = None):
        self.client = client
        self.settings = settings
        self.event_log = event_log
        self.mapper = mapper or InstrumentMapper()

    def _get_market_info(self, epic: str) -> Optional[Dict]:
        try:
            return self.client.get_market_details(epic)
        except Exception as e:
            logger.warning(f"TradeExecutor: market_details failed for {epic}: {e}")
            return None

    def execute(self, signal: Signal) -> Optional[Dict]:
        epic = self.mapper.to_capitalcom_epic(signal.symbol)

        market_info = self._get_market_info(epic) or {}
        decimals = get_decimal_places(market_info)
        min_deal = get_min_deal_size(market_info) or 0.0
        size = max(self.settings.capital_execution_size, min_deal)

        prices = get_market_prices(market_info)
        if prices is None:
            self._log_skipped(signal, epic, 'no live market prices available')
            return None

        bid, offer = prices
        direction = signal.direction.value
        anchor = offer if direction == 'BUY' else bid

        guaranteed = self.settings.capital_execution_guaranteed_stop
        min_distance = get_min_stop_distance(
            market_info, guaranteed=guaranteed, reference_price=anchor,
        )
        if min_distance is None:
            min_distance = abs(anchor) * (self.settings.capital_execution_sl_pct / 100.0) * 0.1

        if self.settings.capital_execution_use_market_distance:
            sl, tp = self._levels_from_market(direction, bid, offer)
            anchored = True
        else:
            sl = signal.sl
            tp = signal.tp
            anchored = False

        sl, tp = adjust_levels_for_broker(
            direction, sl, tp,
            bid=bid, offer=offer,
            min_distance=min_distance,
            buffer_multiplier=self.settings.capital_execution_safety_multiplier,
        )

        sl = round_stop_loss(sl, decimals, direction)
        tp = round_take_profit(tp, decimals, direction)

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
                    'direction': direction,
                    'size': size,
                    'stop_level': sl,
                    'profit_level': tp,
                    'signal_sl': signal.sl,
                    'signal_tp': signal.tp,
                    'market_bid': bid,
                    'market_offer': offer,
                    'anchored_to_market': anchored,
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
                    'direction': direction,
                    'size': size,
                    'stop_level': sl,
                    'profit_level': tp,
                    'market_bid': bid,
                    'market_offer': offer,
                    'error': str(e),
                    'environment': self.settings.capital_environment,
                },
            )
            logger.exception(f"Order placement failed for {signal.symbol}: {e}")
            return None

    def _levels_from_market(self, direction: str, bid: float, offer: float
                            ) -> tuple[float, float]:
        """Compute SL/TP as a percentage of the live entry price.

        Entry is the offer for BUY (paying ask) and the bid for SELL
        (selling at bid). TP distance = SL distance × rr_target.
        """
        sl_pct = self.settings.capital_execution_sl_pct / 100.0
        tp_pct = sl_pct * self.settings.rr_target
        if direction.upper() == 'BUY':
            entry = offer
            return entry * (1 - sl_pct), entry * (1 + tp_pct)
        entry = bid
        return entry * (1 + sl_pct), entry * (1 - tp_pct)

    def _log_skipped(self, signal: Signal, epic: str, reason: str) -> None:
        self.event_log.log(
            signal.symbol, signal.timeframe, 'execution',
            {
                'status': 'SKIPPED',
                'signal_id': signal.id,
                'epic': epic,
                'reason': reason,
                'environment': self.settings.capital_environment,
            },
        )
        logger.warning(f"TradeExecutor: skipped {signal.symbol} — {reason}")
