from __future__ import annotations

import asyncio
import getpass
import os

from telethon import TelegramClient

from ..config import load_config


async def amain() -> None:
    config = load_config()
    if config.telegram_session_string:
        print("TELEGRAM_SESSION_STRING is already set; no session file login is needed.")
        return
    if not config.telegram_phone_number:
        raise SystemExit("TELEGRAM_PHONE_NUMBER is required for first login.")

    config.telegram_session_path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(
        str(config.telegram_session_path),
        config.telegram_api_id,
        config.telegram_api_hash,
    )

    await client.start(
        phone=lambda: config.telegram_phone_number or "",
        password=lambda: getpass.getpass("Telegram 2FA password, if enabled: "),
        code_callback=lambda: input("Telegram login code: "),
    )
    await client.disconnect()
    try:
        os.chmod(config.telegram_session_path, 0o600)
    except OSError:
        pass
    print(f"Telegram session saved to {config.telegram_session_path}")


def run() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    run()

