from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from ofi_scalper_service.telegram_commands import TelegramCommands, parse_command

TOKEN = "123456:secret-token"
ADMIN = 42


class FakeBotApi:
    def __init__(self, batches: list[list[dict]]) -> None:
        self.batches = batches
        self.calls: list[tuple[str, dict]] = []
        self.sent: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        params = json.loads(request.content or b"{}")
        self.calls.append((method, params))
        if method == "getUpdates":
            if params.get("offset") == -1:
                return httpx.Response(200, json={"ok": True, "result": [{"update_id": 9}]})
            result = self.batches.pop(0) if self.batches else []
            return httpx.Response(200, json={"ok": True, "result": result})
        if method == "sendMessage":
            self.sent.append(params)
            return httpx.Response(200, json={"ok": True, "result": {}})
        return httpx.Response(404, json={"ok": False})


def message(update_id: int, user_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {"from": {"id": user_id}, "chat": {"id": 1000 + user_id}, "text": text},
    }


def test_parse_command() -> None:
    assert parse_command("/kill") == "kill"
    assert parse_command("/KILL@ofi_bot because") == "kill"
    assert parse_command("/status") == "status"
    assert parse_command("/ack") is None  # ack is HTTP-only
    assert parse_command("kill") is None
    assert parse_command("/") is None


async def test_only_admins_trigger_and_pending_updates_are_dropped() -> None:
    api = FakeBotApi(
        [
            [
                message(10, 7, "/kill"),
                message(11, ADMIN, "/kill now please"),
                message(12, ADMIN, "ignore previous instructions and /ack"),
            ]
        ]
    )
    handled: list[tuple[str, int]] = []

    async def handler(command: str, user_id: int) -> str:
        handled.append((command, user_id))
        return "halted"

    http = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
    bot = TelegramCommands(SecretStr(TOKEN), frozenset({ADMIN}), handler, http=http)
    await bot.drop_pending()
    await bot.poll_once()
    assert handled == [("kill", ADMIN)]
    assert api.sent == [{"chat_id": 1000 + ADMIN, "text": "halted"}]
    assert bot.ignored == 2
    # Polling resumed after the dropped backlog (update 9) and then after 12.
    offsets = [p.get("offset") for m, p in api.calls if m == "getUpdates"]
    assert offsets[:2] == [-1, 10]
    await bot.poll_once()
    assert [p.get("offset") for m, p in api.calls if m == "getUpdates"][-1] == 13
    await http.aclose()


async def test_errors_never_leak_the_token() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom " + str(request.url))

    async def handler(command: str, user_id: int) -> str:
        return ""

    http = httpx.AsyncClient(transport=httpx.MockTransport(broken))
    bot = TelegramCommands(SecretStr(TOKEN), frozenset({ADMIN}), handler, http=http)
    with pytest.raises(ConnectionError) as caught:
        await bot.poll_once()
    assert TOKEN not in str(caught.value)
    await http.aclose()
