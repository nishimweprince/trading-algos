"""ofi-unit-alert: the systemd OnFailure hook (standard library only)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from ofi_scalper_service import unit_alert
from ofi_scalper_service.unit_alert import build_message, main, read_env, should_send

UNIT = "ofi-scalper-service.service"
FACTS = {
    "ActiveState": "activating",
    "SubState": "auto-restart",
    "Result": "exit-code",
    "ExecMainStatus": "1",
    "NRestarts": "7",
    "journal": (
        "Invalid configuration in .env.dev:\n  OFI_EXECUTION_MODE=testnet needs EXECUTION_API_KEY"
    ),
}


class Recorder:
    def __init__(self, statuses: dict[str, int]) -> None:
        self.statuses = statuses
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> int:
        self.calls.append((url, json.loads(body), headers))
        for prefix, status in self.statuses.items():
            if url.startswith(prefix):
                return status
        return 0


def env_file(tmp_path: Path, **values: str) -> Path:
    lines = ["# comment", "", "export NOTIFICATIONS_ENABLED=true", "BROKEN LINE"]
    lines += [f"{k}={v}" for k, v in values.items()]
    path = tmp_path / ".env.dev"
    path.write_text("\n".join(lines) + "\n")
    return path


def run(tmp_path: Path, post: Recorder, *, now: float = 1000.0, **env: str) -> int:
    return main(
        [
            UNIT,
            "--env-file",
            str(env_file(tmp_path, **env)),
            "--state-dir",
            str(tmp_path / "state"),
        ],
        post=post,
        facts=lambda _unit: dict(FACTS),
        now=lambda: now,
    )


NOTIFY = {
    "NOTIFICATION_SERVICE_URL": "http://127.0.0.1:3010",
    "NOTIFICATION_API_KEY": "'notify-key-123'",
    "NOTIFICATION_CHANNELS": "TELEGRAM",
}
BOT = {"OFI_TELEGRAM_BOT_TOKEN": "123:abc", "OFI_TELEGRAM_ADMIN_USER_IDS": "11, 22"}


def test_reads_env_leniently(tmp_path: Path) -> None:
    env = read_env(env_file(tmp_path, A="1", B='"two words"', C="x=y"))
    assert env["A"] == "1" and env["B"] == "two words" and env["C"] == "x=y"
    assert env["NOTIFICATIONS_ENABLED"] == "true" and "BROKEN LINE" not in env
    assert read_env(tmp_path / "missing") == {}


def test_sends_through_notification_service(tmp_path: Path) -> None:
    post = Recorder({"http://127.0.0.1:3010": 202})
    assert run(tmp_path, post, **NOTIFY, **BOT) == 0
    ((url, body, headers),) = post.calls
    assert url == "http://127.0.0.1:3010/notifications"
    assert body["subject"].startswith(f"{UNIT} FAILED on ")
    assert "OFI_EXECUTION_MODE=testnet needs EXECUTION_API_KEY" in body["message"]
    assert "restarts 7" in body["message"] and body["channels"] == ["TELEGRAM"]
    assert headers["X-API-Key"] == "notify-key-123" and body["source"] == "ofi-scalper.unit-alert"


def test_falls_back_to_the_scalpers_bot(tmp_path: Path) -> None:
    post = Recorder({"http://127.0.0.1:3010": 0, "https://api.telegram.org": 200})
    run(tmp_path, post, **NOTIFY, **BOT)
    urls = [c[0] for c in post.calls]
    assert urls[0].endswith("/notifications")
    assert urls[1:] == ["https://api.telegram.org/bot123:abc/sendMessage"] * 2
    assert {c[1]["chat_id"] for c in post.calls[1:]} == {"11", "22"}


def test_notification_service_failing_is_itself_reported(tmp_path: Path) -> None:
    post = Recorder({"https://api.telegram.org": 200})
    run(tmp_path, post, NOTIFICATIONS_ENABLED="false", **BOT)
    assert [c[0] for c in post.calls] == ["https://api.telegram.org/bot123:abc/sendMessage"] * 2


def test_a_crash_loop_gives_one_alert_per_cooldown(tmp_path: Path) -> None:
    post = Recorder({"http://127.0.0.1:3010": 202})
    for second in range(0, 300, 5):  # restarting every 5 s for 5 minutes
        run(tmp_path, post, now=1000.0 + second, **NOTIFY)
    assert len(post.calls) == 1
    run(tmp_path, post, now=1000.0 + 31 * 60, **NOTIFY)
    assert len(post.calls) == 2
    assert "59 more failure(s) since the last alert" in post.calls[1][1]["message"]


def test_cooldown_state(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    assert should_send(path, 0.0, 60) == (True, 0)
    assert should_send(path, 10.0, 60) == (False, 0)
    assert should_send(path, 70.0, 60) == (True, 1)
    path.write_text("not json")
    assert should_send(path, 80.0, 60) == (True, 0)


def test_never_fails(tmp_path: Path) -> None:
    def explode(_unit: str) -> dict:
        raise RuntimeError("journal unavailable")

    assert main([UNIT, "--state-dir", str(tmp_path)], post=Recorder({}), facts=explode) == 0
    (tmp_path / "x").mkdir()
    assert run(tmp_path / "x", Recorder({})) == 0  # no channel configured at all


def test_message_mentions_how_to_look(tmp_path: Path) -> None:
    subject, lines = build_message(UNIT, {}, host="ofi", suppressed=0)
    assert subject == f"{UNIT} FAILED on ofi"
    assert any(f"journalctl -u {UNIT}" in line for line in lines)


def test_standard_library_only() -> None:
    """It runs under the system python3, outside the venv: no third-party imports."""
    import sys

    tree = ast.parse(Path(unit_alert.__file__).read_text())
    modules = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0
    }
    assert modules <= set(sys.stdlib_module_names) | {"__future__"}, modules
    assert not any(isinstance(n, ast.ImportFrom) and n.level for n in ast.walk(tree))
