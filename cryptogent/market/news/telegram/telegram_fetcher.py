"""
cryptogent.market.news.telegram_fetcher
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Fetch Telegram messages for configured public channels.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class TelegramFetchResult:
    channel: str
    messages: tuple[Any, ...]
    error: str | None


async def resolve_channel(client: Any, *, username: str) -> Any | None:
    try:
        return await client.get_entity(username)
    except Exception:
        return None


async def join_channel_if_needed(client: Any, *, entity: Any) -> bool:
    try:
        from telethon.tl.functions.channels import JoinChannelRequest
    except Exception:
        return False
    try:
        await client(JoinChannelRequest(entity))
        return True
    except Exception:
        return False


async def fetch_channel_messages(
    client: Any,
    *,
    username: str,
    limit: int,
    min_id: int | None,
    join_channels: bool,
) -> TelegramFetchResult:
    entity = await resolve_channel(client, username=username)
    if entity is None:
        return TelegramFetchResult(channel=username, messages=(), error="invalid_or_unavailable")

    if join_channels:
        await join_channel_if_needed(client, entity=entity)

    messages = []
    safe_min_id = int(min_id or 0)
    try:
        async for msg in client.iter_messages(entity, limit=limit, min_id=safe_min_id):
            messages.append(msg)
    except Exception as exc:
        return TelegramFetchResult(channel=username, messages=(), error=str(exc))

    return TelegramFetchResult(channel=username, messages=tuple(messages), error=None)


def message_timestamp_utc(msg: Any) -> str:
    value = getattr(msg, "date", None)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(microsecond=0).isoformat()
    return datetime.now(UTC).replace(microsecond=0).isoformat()
