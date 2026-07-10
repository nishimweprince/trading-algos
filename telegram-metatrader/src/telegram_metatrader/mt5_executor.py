from __future__ import annotations

from decimal import Decimal
from typing import Any

from .config import AppConfig, resolve_symbol
from .models import ExecutionPlan, ExecutionResult, Signal


class Mt5Executor:
    def __init__(self, config: AppConfig, mt5: Any | None = None) -> None:
        self.config = config
        if mt5 is None:
            import MetaTrader5 as mt5_module

            mt5 = mt5_module
        self.mt5 = mt5
        self._initialized = False

    def build_plan(self, signal: Signal) -> ExecutionPlan:
        mt5_symbol = resolve_symbol(self.config.symbol_map, signal.symbol)
        if mt5_symbol is None:
            raise ValueError(f"No MT5 symbol mapping for parsed symbol {signal.symbol}")
        return ExecutionPlan(
            signal=signal,
            mt5_symbol=mt5_symbol,
            volume=self.config.default_volume,
            dry_run=not self.config.live_trading_enabled,
        )

    def execute(self, signal: Signal) -> ExecutionResult:
        plan = self.build_plan(signal)
        warnings = {
            "missing_sl": signal.missing_sl,
            "missing_tp": signal.missing_tp,
            "extra_tps": [str(tp) for tp in signal.all_tps[1:]],
        }
        if plan.dry_run:
            return ExecutionResult("dry_run", plan, {"warnings": warnings})

        self.initialize()
        self._ensure_symbol(plan.mt5_symbol)
        request = self._request(plan)

        check = self.mt5.order_check(request)
        check_retcode = getattr(check, "retcode", None)
        done_retcode = getattr(self.mt5, "TRADE_RETCODE_DONE", 10009)
        if check is None or check_retcode != done_retcode:
            return ExecutionResult("rejected", plan, {"order_check": _object_to_dict(check), "warnings": warnings})

        result = self.mt5.order_send(request)
        result_retcode = getattr(result, "retcode", None)
        if result is not None and result_retcode == done_retcode:
            return ExecutionResult("sent", plan, {"order_send": _object_to_dict(result), "warnings": warnings})
        return ExecutionResult("failed", plan, {"order_send": _object_to_dict(result), "warnings": warnings})

    def initialize(self) -> None:
        if self._initialized:
            return
        kwargs: dict[str, Any] = {"timeout": self.config.mt5_timeout_ms}
        if self.config.mt5_login is not None:
            kwargs["login"] = self.config.mt5_login
        if self.config.mt5_password is not None:
            kwargs["password"] = self.config.mt5_password
        if self.config.mt5_server is not None:
            kwargs["server"] = self.config.mt5_server

        if self.config.mt5_terminal_path:
            ok = self.mt5.initialize(self.config.mt5_terminal_path, **kwargs)
        else:
            ok = self.mt5.initialize(**kwargs)
        if not ok:
            raise RuntimeError(f"MT5 initialize failed: {self.mt5.last_error()}")

        terminal_info = self.mt5.terminal_info()
        if terminal_info is None:
            raise RuntimeError(f"MT5 terminal_info failed: {self.mt5.last_error()}")
        account_info = self.mt5.account_info()
        if account_info is None:
            raise RuntimeError(f"MT5 account_info failed: {self.mt5.last_error()}")
        self._initialized = True

    def shutdown(self) -> None:
        if self._initialized:
            self.mt5.shutdown()
            self._initialized = False

    def _ensure_symbol(self, symbol: str) -> None:
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"MT5 symbol not found: {symbol}")
        if getattr(info, "visible", False):
            return
        if not self.mt5.symbol_select(symbol, True):
            raise RuntimeError(f"MT5 symbol_select failed for {symbol}: {self.mt5.last_error()}")

    def _request(self, plan: ExecutionPlan) -> dict[str, Any]:
        tick = self.mt5.symbol_info_tick(plan.mt5_symbol)
        if tick is None:
            raise RuntimeError(f"MT5 symbol_info_tick failed for {plan.mt5_symbol}: {self.mt5.last_error()}")

        order_type = self.mt5.ORDER_TYPE_BUY if plan.signal.direction == "BUY" else self.mt5.ORDER_TYPE_SELL
        price = getattr(tick, "ask" if plan.signal.direction == "BUY" else "bid")
        request: dict[str, Any] = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": plan.mt5_symbol,
            "volume": plan.volume,
            "type": order_type,
            "price": price,
            "deviation": self.config.max_deviation_points,
            "magic": self.config.order_magic,
            "comment": f"tg:{plan.signal.message_id}",
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self.mt5.ORDER_FILLING_RETURN,
        }
        if plan.signal.sl is not None:
            request["sl"] = _decimal_to_float(plan.signal.sl)
        if plan.signal.tp is not None:
            request["tp"] = _decimal_to_float(plan.signal.tp)
        return request


def _decimal_to_float(value: Decimal) -> float:
    return float(value)


def _object_to_dict(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "_asdict"):
        return value._asdict()
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return str(value)

