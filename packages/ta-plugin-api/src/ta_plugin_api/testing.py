"""The conformance kit every plugin's tests run against its own fakes.

``assert_closed_utc_candles`` pins the parts of the candle contract a provider
can get subtly wrong and a consumer cannot detect: bar timestamps, ordering,
and the forming bar. ``MarketDataConformance`` and ``ExecutionConformance`` are
pytest mixins: subclass one in a plugin's tests, provide the fixtures its
docstring lists, and every rule of the protocol runs against that plugin.

Test-only: this module imports pytest, which is a dev dependency.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from ta_contracts import TIMEFRAME_MINUTES, Candle, Timeframe
from ta_core import ServiceError

from .execution import ExecutionProvider
from .market_data import MarketDataProvider

__all__ = ["ExecutionConformance", "MarketDataConformance", "assert_closed_utc_candles"]

UNKNOWN_SYMBOL = "NOT-A-LISTED-SYMBOL"
UNKNOWN_ACCOUNT = "not-a-configured-account"


def assert_closed_utc_candles(
    candles: Sequence[Candle], timeframe: Timeframe, *, now: datetime
) -> None:
    """Oldest first, no duplicates, aligned interval ends, none still forming."""
    step = timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
    stamps = [candle.ts for candle in candles]
    assert stamps == sorted(set(stamps)), "candles must be strictly ascending"
    for ts in stamps:
        assert ts.utcoffset() == timedelta(0), f"{ts} is not UTC"
        assert ts <= now, f"{ts} ends after {now}: the forming bar leaked"
        if step <= timedelta(days=1):
            midnight = ts.replace(hour=0, minute=0, second=0, microsecond=0)
            assert (ts - midnight) % step == timedelta(0), f"{ts} is not an interval end"


class MarketDataConformance:
    """The ``MarketDataProvider`` contract, as tests.

    Fixtures the subclass provides:

    - ``provider``: started, with its fake able to serve a quote and at least
      two closed candles of ``symbol`` at ``timeframe`` (the fake must honour
      ``to``);
    - ``feed``: a feed the provider serves (``None`` for single-feed plugins);
    - ``symbol``: a canonical symbol on that feed;
    - ``timeframe``: a timeframe the provider supports;
    - ``now``: the instant the provider's clock reads.
    """

    def test_is_a_market_data_provider_serving_the_feed(self, provider: Any, feed: Any) -> None:
        assert isinstance(provider, MarketDataProvider)
        assert isinstance(provider.name, str) and provider.name
        assert feed in provider.feeds()

    def test_readiness_is_a_flag_and_details(self, provider: Any) -> None:
        ready, details = provider.readiness()
        assert isinstance(ready, bool)
        assert isinstance(details, dict)

    def test_capabilities_advertise_the_timeframe(
        self, provider: Any, feed: Any, timeframe: Timeframe
    ) -> None:
        assert timeframe in provider.capabilities(feed).timeframes

    def test_instruments_list_the_symbol(self, provider: Any, feed: Any, symbol: str) -> None:
        instruments = provider.instruments(feed)
        assert symbol in {instrument.symbol for instrument in instruments}
        for instrument in instruments:
            assert instrument.provider == provider.name
            assert instrument.source_instrument

    async def test_candles_are_closed_and_stamped_at_utc_interval_ends(
        self, provider: Any, feed: Any, symbol: str, timeframe: Timeframe, now: datetime
    ) -> None:
        candles = await provider.candles(feed, symbol, timeframe, 3)

        assert 2 <= len(candles) <= 3
        assert_closed_utc_candles(candles, timeframe, now=now)
        assert {candle.provider for candle in candles} == {provider.name}

    async def test_candles_end_at_to(
        self, provider: Any, feed: Any, symbol: str, timeframe: Timeframe, now: datetime
    ) -> None:
        latest = await provider.candles(feed, symbol, timeframe, 3)
        to = latest[-2].ts

        candles = await provider.candles(feed, symbol, timeframe, 2, to=to)

        assert candles, "a bar ends exactly at `to`"
        assert candles[-1].ts == to
        assert_closed_utc_candles(candles, timeframe, now=now)

    async def test_an_unknown_symbol_is_a_422(self, provider: Any, feed: Any) -> None:
        for request in (
            lambda: provider.candles(feed, UNKNOWN_SYMBOL, Timeframe.H1, 1),
            lambda: provider.quote(feed, UNKNOWN_SYMBOL),
        ):
            with pytest.raises(ServiceError) as raised:
                await request()
            assert (raised.value.status_code, raised.value.code) == (422, "symbol_not_allowed")
        with pytest.raises(ServiceError) as raised:
            provider.resolve_symbols(feed, [UNKNOWN_SYMBOL])
        assert raised.value.status_code == 422

    def test_stream_filters_resolve_to_canonical_names(
        self, provider: Any, feed: Any, symbol: str
    ) -> None:
        assert provider.resolve_symbols(feed, [symbol.lower()]) == frozenset({symbol})

    async def test_an_unsupported_timeframe_is_a_422(
        self, provider: Any, feed: Any, symbol: str
    ) -> None:
        unsupported = set(Timeframe) - set(provider.capabilities(feed).timeframes)
        if not unsupported:
            pytest.skip("every timeframe is supported")
        timeframe = sorted(unsupported, key=lambda item: TIMEFRAME_MINUTES[item])[0]

        with pytest.raises(ServiceError) as raised:
            await provider.candles(feed, symbol, timeframe, 1)

        assert (raised.value.status_code, raised.value.code) == (422, "timeframe_not_supported")

    async def test_quotes_are_utc_and_never_crossed(
        self, provider: Any, feed: Any, symbol: str
    ) -> None:
        quote = await provider.quote(feed, symbol)

        assert quote.symbol == symbol
        assert quote.provider == provider.name
        assert quote.ts.utcoffset() == timedelta(0)
        assert quote.price > 0
        if quote.bid is not None and quote.ask is not None:
            assert quote.bid <= quote.ask

    def test_the_feed_has_a_hub(self, provider: Any, feed: Any) -> None:
        assert provider.hub(feed) is provider.hub(feed)


class ExecutionConformance:
    """The ``ExecutionProvider`` contract, as tests.

    Fixtures the subclass provides:

    - ``provider``: constructed (it need not be started);
    - ``account``: an account alias it serves;
    - ``client_order_id_limit``: the longest client order ID the broker keeps
      (the field or comment it travels in).
    """

    OPERATION = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    OTHER_OPERATION = UUID("aaaaaaaa-bbbb-cccc-dddd-ffffffffffff")

    def test_is_an_execution_provider_serving_the_account(
        self, provider: Any, account: str
    ) -> None:
        assert isinstance(provider, ExecutionProvider)
        assert isinstance(provider.name, str) and provider.name
        assert account in provider.accounts()

    def test_client_order_ids_are_deterministic_unique_and_fit_the_broker(
        self, provider: Any, account: str, client_order_id_limit: int
    ) -> None:
        first = provider.client_order_id(self.OPERATION, account)

        assert provider.client_order_id(self.OPERATION, account) == first
        assert provider.client_order_id(self.OTHER_OPERATION, account) != first
        assert 0 < len(first) <= client_order_id_limit

    def test_an_unknown_account_has_no_inventory(self, provider: Any) -> None:
        # ExecutionService maps KeyError to 404 account_not_found.
        with pytest.raises(KeyError):
            provider.orders(UNKNOWN_ACCOUNT)
        with pytest.raises(KeyError):
            provider.positions(UNKNOWN_ACCOUNT)

    def test_account_statuses_describe_each_account(self, provider: Any, account: str) -> None:
        statuses = {status["alias"]: status for status in provider.account_statuses()}

        assert account in statuses
        for status in statuses.values():
            assert status["provider"] == provider.name
            assert isinstance(status["environment"], str)
            assert isinstance(status["is_live"], bool)
            assert isinstance(status["order_entry_enabled"], bool)
            assert isinstance(status["position_close_enabled"], bool)

    def test_readiness_is_a_flag_and_details(self, provider: Any) -> None:
        ready, details = provider.readiness()
        assert isinstance(ready, bool)
        assert isinstance(details, dict)

    async def test_reconcile_without_a_ledger_is_harmless(self, provider: Any) -> None:
        await provider.reconcile_unknown()
