from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import OPEN, make_settings, window
from pydantic import ValidationError

from poly_kalshi_service.status_app import pick
from poly_kalshi_service.windows import Discovery, poly_slug, window_open

YES, NO = "111", "222"


def _poly_event(open_unix: int, **market: Any) -> list[dict[str, Any]]:
    start = datetime.fromtimestamp(open_unix, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "conditionId": "0xc",
        "clobTokenIds": json.dumps([YES, NO]),
        "outcomes": json.dumps(["Up", "Down"]),
        "eventStartTime": start,
        "feesEnabled": True,
        "feeSchedule": {"exponent": 1, "rate": 0.07, "takerOnly": True},
        "closed": False,
    }
    body.update(market)
    return [{"slug": poly_slug("BTC", open_unix), "markets": [body]}]


def _kalshi(open_unix: int, ticker: str = "KXBTC15M-X", result: str = "") -> dict[str, Any]:
    return {
        "ticker": ticker,
        "open_time": datetime.fromtimestamp(open_unix, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "result": result,
    }


def _discovery(handler: Any) -> tuple[Discovery, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Discovery(http, gamma_url="https://g.test", kalshi_rest_url="https://k.test/v2"), http


async def test_discover_maps_up_down_to_yes_no_with_the_fee_schedule() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "g.test":
            return httpx.Response(200, json=_poly_event(OPEN))
        assert request.url.params["series_ticker"] == "KXBTC15M"
        assert int(request.url.params["min_close_ts"]) == OPEN + 899
        return httpx.Response(200, json={"markets": [_kalshi(OPEN)]})

    discovery, http = _discovery(handler)
    w = await discovery.discover("BTC", OPEN)
    await http.aclose()
    assert w is not None
    assert (w.yes_token, w.no_token, w.kalshi_ticker) == (YES, NO, "KXBTC15M-X")
    assert (w.fee_rate, w.fee_exponent) == (0.07, 1.0)
    assert seen[0].url.params["slug"] == f"btc-updown-15m-{OPEN}"
    assert seen[0].headers["user-agent"].startswith("poly-kalshi")


async def test_discover_refuses_a_misaligned_window() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "g.test":
            return httpx.Response(200, json=_poly_event(OPEN + 900))
        return httpx.Response(200, json={"markets": [_kalshi(OPEN)]})

    discovery, http = _discovery(handler)
    with pytest.raises(RuntimeError, match="eventStartTime"):
        await discovery.discover("BTC", OPEN)
    await http.aclose()


async def test_discover_is_none_until_both_venues_list_it() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "g.test":
            return httpx.Response(200, json=_poly_event(OPEN))
        return httpx.Response(200, json={"markets": []})

    discovery, http = _discovery(handler)
    assert await discovery.discover("BTC", OPEN) is None
    await http.aclose()


@pytest.mark.parametrize(
    ("market", "expected"),
    [
        ({"closed": True, "outcomePrices": '["1", "0"]'}, "yes"),
        ({"closed": True, "outcomePrices": '["0", "1"]'}, "no"),
        ({"closed": False, "outcomePrices": '["1", "0"]'}, None),
        ({"closed": True, "outcomePrices": '["0.5", "0.5"]'}, None),
    ],
)
async def test_poly_outcome(market: dict[str, Any], expected: str | None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_poly_event(OPEN, **market))

    discovery, http = _discovery(handler)
    assert await discovery.poly_outcome(window()) == expected
    await http.aclose()


async def test_kalshi_outcome() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"market": _kalshi(OPEN, result="no")})

    discovery, http = _discovery(handler)
    assert await discovery.kalshi_outcome(window()) == "no"
    await http.aclose()


def test_window_open() -> None:
    assert window_open(OPEN) == OPEN
    assert window_open(OPEN + 899.9) == OPEN
    assert window_open(OPEN + 900) == OPEN + 900


def test_settings_defaults_are_the_deployed_set(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    assert s.assets == ("BTC", "ETH", "SOL", "XRP", "BNB")
    assert (s.gap_threshold, s.max_entry_px, s.notional_usd) == (0.20, 0.60, 50.0)
    assert (s.entry_early_s, s.entry_late_s) == (20, 60)
    assert s.execution_mode.value == "paper"
    assert s.port == 8050 and s.status_port == 8051


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"PK_EXECUTION_MODE": "live"}, "live is refused"),
        ({"PK_ASSETS": "BTC,LUNA"}, "unsupported"),
        ({"PK_ENTRY_EARLY_S": 500, "PK_ENTRY_LATE_S": 400}, "no entry window"),
        ({"PK_NOTIONAL_USD": 600}, "cannot exceed"),
        ({"PK_TELEGRAM_BOT_TOKEN": "123:abc"}, "ADMIN_USER_IDS"),
        ({"KALSHI_API_KEY_ID": "replace-with-the-kalshi-key-id"}, "placeholder"),
    ],
)
def test_settings_refuse(tmp_path: Path, overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make_settings(tmp_path, **overrides)


def test_missing_kalshi_key_fails_with_a_fix(tmp_path: Path) -> None:
    s = make_settings(tmp_path, KALSHI_PRIVATE_KEY_PATH=str(tmp_path / "nope.pem"))
    with pytest.raises(ValueError, match="KALSHI_PRIVATE_KEY_PATH"):
        s.check_paths()


def test_status_pick_is_an_allowlist() -> None:
    status = {
        "execution_mode": "paper",
        "params": {"gap_threshold": 0.2},
        "secret": "leak",
        "feeds": {"kalshi": {"connected": True, "last_error": "KALSHI-ACCESS-KEY=..."}},
        "recorder": {"root": "/home/x", "lines_written": 5},
        "engine": {
            "assets": {"BTC": {"traded": True, "internal": 1}},
            "counters": {"fills": 1, "debug_internal": 2},
            "risk": {"halted": False, "halt_reason": "telegram: user 123"},
        },
        "recent": [{"type": "fill", "vwap": 0.5, "t_ns": 1, "debug": "x"}],
    }
    picked = pick(status)
    text = json.dumps(picked)
    for leak in ("leak", "last_error", "/home/x", "internal", "user 123", "debug"):
        assert leak not in text
    assert picked["assets"]["BTC"]["traded"] is True
    assert picked["counters"] == {"fills": 1}
