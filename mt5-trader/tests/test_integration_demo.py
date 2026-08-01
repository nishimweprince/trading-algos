from __future__ import annotations

import os

import pytest


@pytest.mark.integration
def test_demo_terminal_connectivity_only() -> None:
    if os.getenv("RUN_MT5_DEMO_INTEGRATION") != "1":
        pytest.skip("set RUN_MT5_DEMO_INTEGRATION=1 on the Windows demo host")

    from mt5_signal_service.config import load_settings
    from mt5_signal_service.mt5_adapter import RealMT5Adapter

    settings = load_settings()
    if "demo" not in settings.server.lower():
        pytest.fail("integration checks are restricted to broker servers containing 'demo'")

    adapter = RealMT5Adapter()
    try:
        assert adapter.initialize(settings), adapter.last_error()
        connection = adapter.connection_snapshot()
        assert connection.connected
        assert connection.login == settings.login
        assert connection.trade_allowed
        assert connection.expert_allowed
    finally:
        adapter.shutdown()
