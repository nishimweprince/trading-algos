from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from telegram_metatrader.config import load_config
from telegram_metatrader.models import Signal
from telegram_metatrader.mt5_executor import Mt5Executor


def env(live: bool = False) -> dict[str, str]:
    return {
        "TELEGRAM_API_ID": "123",
        "TELEGRAM_API_HASH": "hash",
        "CHAT_ID": "@blacktrade",
        "LIVE_TRADING_ENABLED": "true" if live else "false",
        "SYMBOL_MAP_JSON": '{"GOLD":"XAUUSDm"}',
    }


def signal() -> Signal:
    return Signal(
        id="sig-1",
        source_chat="-1001",
        message_id=42,
        message_date="2026-07-10T00:00:00Z",
        raw_text="BUY GOLD SL 2310 TP 2330",
        symbol="GOLD",
        direction="BUY",
        sl=Decimal("2310"),
        tp=Decimal("2330"),
        all_tps=[Decimal("2330")],
    )


class FakeMt5:
    TRADE_RETCODE_DONE = 10009
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_RETURN = 2

    def __init__(self, *, initialize_ok: bool = True, check_retcode: int = 10009, send_retcode: int = 10009):
        self.initialize_ok = initialize_ok
        self.check_retcode = check_retcode
        self.send_retcode = send_retcode
        self.sent_requests: list[dict] = []

    def initialize(self, *args, **kwargs):
        return self.initialize_ok

    def last_error(self):
        return (1, "boom")

    def terminal_info(self):
        return SimpleNamespace(connected=True)

    def account_info(self):
        return SimpleNamespace(login=123)

    def symbol_info(self, symbol):
        return SimpleNamespace(visible=True)

    def symbol_select(self, symbol, visible):
        return True

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(ask=2321.5, bid=2321.0)

    def order_check(self, request):
        return SimpleNamespace(retcode=self.check_retcode)

    def order_send(self, request):
        self.sent_requests.append(request)
        return SimpleNamespace(retcode=self.send_retcode, order=777)

    def shutdown(self):
        return None


def test_dry_run_does_not_initialize_or_send(tmp_path):
    config = load_config(tmp_path, env(live=False))
    fake = FakeMt5(initialize_ok=False)

    result = Mt5Executor(config, fake).execute(signal())

    assert result.status == "dry_run"
    assert fake.sent_requests == []


def test_live_sends_order_after_check(tmp_path):
    config = load_config(tmp_path, env(live=True))
    fake = FakeMt5()

    result = Mt5Executor(config, fake).execute(signal())

    assert result.status == "sent"
    assert fake.sent_requests[0]["symbol"] == "XAUUSDm"
    assert fake.sent_requests[0]["sl"] == 2310.0
    assert fake.sent_requests[0]["tp"] == 2330.0


def test_live_rejects_failed_order_check(tmp_path):
    config = load_config(tmp_path, env(live=True))
    fake = FakeMt5(check_retcode=10030)

    result = Mt5Executor(config, fake).execute(signal())

    assert result.status == "rejected"
    assert fake.sent_requests == []


def test_initialize_failure_raises(tmp_path):
    config = load_config(tmp_path, env(live=True))

    with pytest.raises(RuntimeError, match="initialize failed"):
        Mt5Executor(config, FakeMt5(initialize_ok=False)).execute(signal())

