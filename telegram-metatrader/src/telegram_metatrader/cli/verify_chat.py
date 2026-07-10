from __future__ import annotations

import asyncio

from ..config import load_config
from ..telegram_client import create_client, latest_message_id, resolve_chat


async def amain() -> None:
    config = load_config()
    client = await create_client(config)
    try:
        chat = await resolve_chat(client, config.chat_id)
        latest = await latest_message_id(client, chat.entity)
        print("Chat resolved successfully")
        print(f"  input: {chat.input_value}")
        print(f"  stable_id: {chat.stable_id}")
        print(f"  title: {chat.title}")
        print(f"  latest_message_id: {latest}")
    finally:
        await client.disconnect()


def run() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    run()

