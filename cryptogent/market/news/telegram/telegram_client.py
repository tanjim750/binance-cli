"""
cryptogent.market.news.telegram_client
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Telethon client/session helpers.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


class TelegramClientError(RuntimeError):
    pass


def build_telegram_client(*, api_id: int | None, api_hash: str | None, session_path: str) -> Any:
    if not api_id or not api_hash:
        raise TelegramClientError("Telegram api_id/api_hash missing in config.")
    try:
        from telethon import TelegramClient
    except ImportError as exc:
        raise TelegramClientError("Telethon is not installed. Install with: pip install telethon") from exc

    path = Path(session_path).expanduser()
    return TelegramClient(str(path), api_id, api_hash)


async def ensure_authorized(client: Any, *, phone: str | None = None) -> None:
    if await client.is_user_authorized():
        return
    if phone:
        await client.start(phone=phone)
    else:
        await client.start()
