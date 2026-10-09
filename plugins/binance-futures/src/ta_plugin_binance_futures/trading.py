"""Direct USDⓈ-M trading calls for services that run their own order manager.

``BinanceFuturesExecution`` is execution-service's adapter (one order per
request, no exchange-held protection). Some strategies need what it does not
offer: exchange-held stops and take-profits, which since 2025-12 live in the
Algo Service (``/fapi/v1/algoOrder``, ``algoType=CONDITIONAL``), plus the account
setup the strategy depends on (one-way mode, isolated margin, leverage) and the
leverage brackets for liquidation maths. ``FuturesTrading`` is that surface:
thin, typed, and honest about outcomes.

Every call returns a ``Reply`` whose ``outcome`` is

- ``ok``: Binance answered 2xx;
- ``rejected``: Binance answered with an error code (nothing happened);
- ``unknown``: no response, or a 5xx; the request may have taken effect, so
  the caller must query before it retries (never resend blindly);
- ``not_sent``: refused locally (rate-limit back-off, no key); safe to retry later.

Reached through ``EXECUTION_FACTORY.trading(settings)``, so services keep
discovering the plugin by entry point.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .rest import FapiRest, VenueNotSent, VenueTransportError

__all__ = ["ALGO_EVENTS", "FuturesTrading", "Reply"]

# The user-stream subscription a service with algo orders needs.
ALGO_EVENTS = "ORDER_TRADE_UPDATE/ACCOUNT_UPDATE/ALGO_UPDATE"

# Request weights (USDⓈ-M docs; conservative where they vary).
W_ORDER = 1
W_QUERY = 1
W_POSITIONS = 5
W_ACCOUNT = 5
W_BRACKET = 1
W_CONFIG = 1
W_OPEN_ALL = 40
W_KLINES = 5
W_TIME = 1

NO_NEED_TO_CHANGE = {-4046, -4059}  # margin type / position mode already as asked

Outcome = Literal["ok", "rejected", "unknown", "not_sent"]


@dataclass(frozen=True)
class Reply:
    outcome: Outcome
    body: Any = None
    code: int | None = None
    msg: str | None = None
    status: int | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


def _bool(v: bool) -> str:
    return "true" if v else "false"


def _num(v: float | str) -> str:
    """Plain decimal text: Binance rejects exponent notation."""
    if isinstance(v, str):
        return v
    text = f"{v:.12f}".rstrip("0").rstrip(".")
    return text or "0"


class FuturesTrading:
    def __init__(self, rest: FapiRest) -> None:
        self.rest = rest

    async def _call(
        self, method: str, path: str, params: dict[str, Any], weight: int, auth: str = "signed"
    ) -> Reply:
        try:
            response = await self.rest.raw(method, path, params, weight, auth=auth)
        except VenueNotSent as exc:
            return Reply("not_sent", msg=str(exc))
        except VenueTransportError as exc:
            return Reply("unknown", msg=str(exc))
        body = response.json() if response.content else None
        if response.status_code >= 500:
            return Reply("unknown", body, status=response.status_code)
        if response.status_code >= 400:
            code = body.get("code") if isinstance(body, dict) else None
            msg = body.get("msg") if isinstance(body, dict) else None
            return Reply("rejected", body, code, msg, response.status_code)
        return Reply("ok", body, status=response.status_code)

    # -- time and market data ------------------------------------------------------
    async def server_time(self) -> Reply:
        return await self._call("GET", "/fapi/v1/time", {}, W_TIME, auth="none")

    async def klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
        end_time: int | None = None,
        start_time: int | None = None,
    ) -> Reply:
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if end_time is not None:
            params["endTime"] = end_time
        if start_time is not None:
            params["startTime"] = start_time
        return await self._call("GET", "/fapi/v1/klines", params, W_KLINES, auth="none")

    async def exchange_info(self) -> Reply:
        return await self._call("GET", "/fapi/v1/exchangeInfo", {}, 1, auth="none")

    async def premium_index(self, symbol: str) -> Reply:
        """Mark price, index price and the current funding estimate."""
        return await self._call("GET", "/fapi/v1/premiumIndex", {"symbol": symbol}, 1, auth="none")

    # -- account setup ---------------------------------------------------------------
    async def position_mode(self) -> Reply:
        """``body["dualSidePosition"]`` is False in one-way mode."""
        return await self._call("GET", "/fapi/v1/positionSide/dual", {}, 30)

    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> Reply:
        reply = await self._call(
            "POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type}, W_CONFIG
        )
        if reply.outcome == "rejected" and reply.code in NO_NEED_TO_CHANGE:
            return Reply("ok", reply.body, reply.code, reply.msg, reply.status)
        return reply

    async def set_leverage(self, symbol: str, leverage: int) -> Reply:
        return await self._call(
            "POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, W_CONFIG
        )

    async def leverage_brackets(self, symbol: str | None = None) -> Reply:
        params = {"symbol": symbol} if symbol else {}
        return await self._call("GET", "/fapi/v1/leverageBracket", params, W_BRACKET)

    async def symbol_config(self, symbol: str) -> Reply:
        return await self._call("GET", "/fapi/v1/symbolConfig", {"symbol": symbol}, W_CONFIG)

    # -- reads -------------------------------------------------------------------------
    async def account(self) -> Reply:
        return await self._call("GET", "/fapi/v3/account", {}, W_ACCOUNT)

    async def positions(self, symbol: str | None = None) -> Reply:
        params = {"symbol": symbol} if symbol else {}
        return await self._call("GET", "/fapi/v3/positionRisk", params, W_POSITIONS)

    async def open_orders(self, symbol: str | None = None) -> Reply:
        params = {"symbol": symbol} if symbol else {}
        return await self._call(
            "GET", "/fapi/v1/openOrders", params, W_QUERY if symbol else W_OPEN_ALL
        )

    async def user_trades(self, symbol: str, start_time: int | None = None) -> Reply:
        params: dict[str, Any] = {"symbol": symbol}
        if start_time is not None:
            params["startTime"] = start_time
        return await self._call("GET", "/fapi/v1/userTrades", params, 5)

    async def income(
        self, symbol: str, income_type: str = "FUNDING_FEE", start_time: int | None = None
    ) -> Reply:
        """Account ledger rows (funding fees, commissions, realized PnL)."""
        params: dict[str, Any] = {"symbol": symbol, "incomeType": income_type, "limit": 1000}
        if start_time is not None:
            params["startTime"] = start_time
        return await self._call("GET", "/fapi/v1/income", params, 30)

    async def mark_klines(
        self, symbol: str, interval: str, limit: int, end_time: int | None = None
    ) -> Reply:
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if end_time is not None:
            params["endTime"] = end_time
        return await self._call("GET", "/fapi/v1/markPriceKlines", params, W_KLINES, auth="none")

    async def funding_history(self, symbol: str, start_time: int, end_time: int) -> Reply:
        params = {"symbol": symbol, "startTime": start_time, "endTime": end_time, "limit": 100}
        return await self._call("GET", "/fapi/v1/fundingRate", params, 1, auth="none")

    # -- regular orders ----------------------------------------------------------------
    async def new_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        *,
        quantity: float | str,
        client_id: str,
        reduce_only: bool = False,
        price: float | str | None = None,
        time_in_force: str | None = None,
        good_till: int | None = None,
    ) -> Reply:
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": _num(quantity),
            "newClientOrderId": client_id,
            "newOrderRespType": "RESULT",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        if price is not None:
            params["price"] = _num(price)
        if time_in_force:
            params["timeInForce"] = time_in_force
        if good_till is not None:
            params["goodTillDate"] = good_till
        return await self._call("POST", "/fapi/v1/order", params, W_ORDER)

    async def query_order(self, symbol: str, client_id: str) -> Reply:
        """``rejected`` with code -2013 means Binance has no such order."""
        return await self._call(
            "GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id}, W_QUERY
        )

    async def cancel_order(self, symbol: str, client_id: str) -> Reply:
        return await self._call(
            "DELETE", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id}, W_ORDER
        )

    # -- algo (conditional) orders ---------------------------------------------------------
    async def new_algo(
        self,
        symbol: str,
        side: str,
        order_type: str,
        *,
        trigger_price: float | str,
        client_id: str,
        close_position: bool = False,
        quantity: float | str | None = None,
        reduce_only: bool = False,
        working_type: str = "MARK_PRICE",
        price_protect: bool = True,
    ) -> Reply:
        """STOP_MARKET / TAKE_PROFIT_MARKET through the Algo Service.

        ``close_position`` closes whatever is open when it triggers and cannot
        be combined with ``quantity`` or ``reduce_only``.
        """
        if close_position and (quantity is not None or reduce_only):
            raise ValueError("closePosition cannot be combined with quantity or reduceOnly")
        params: dict[str, Any] = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "triggerPrice": _num(trigger_price),
            "workingType": working_type,
            "priceProtect": _bool(price_protect),
            "clientAlgoId": client_id,
        }
        if close_position:
            params["closePosition"] = "true"
        else:
            if quantity is None:
                raise ValueError("quantity is required without closePosition")
            params["quantity"] = _num(quantity)
            if reduce_only:
                params["reduceOnly"] = "true"
        return await self._call("POST", "/fapi/v1/algoOrder", params, W_ORDER)

    async def query_algo(self, client_id: str) -> Reply:
        return await self._call("GET", "/fapi/v1/algoOrder", {"clientAlgoId": client_id}, W_QUERY)

    async def cancel_algo(self, client_id: str) -> Reply:
        return await self._call(
            "DELETE", "/fapi/v1/algoOrder", {"clientAlgoId": client_id}, W_ORDER
        )

    async def open_algo_orders(self, symbol: str | None = None) -> Reply:
        params = {"symbol": symbol} if symbol else {}
        return await self._call(
            "GET", "/fapi/v1/openAlgoOrders", params, W_QUERY if symbol else W_OPEN_ALL
        )

    async def cancel_all_algo(self, symbol: str) -> Reply:
        return await self._call("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol}, W_ORDER)

    async def cancel_all(self, symbol: str) -> Reply:
        return await self._call("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, W_ORDER)
