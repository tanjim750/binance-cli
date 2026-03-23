"""
cryptogent.market.news.telegram_storage
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Storage helpers for Telegram messages.
"""
from __future__ import annotations

from typing import Iterable

from cryptogent.market.news.telegram.telegram_parser import TelegramParsedMessage
from cryptogent.state.manager import StateManager


def persist_messages(
    state: StateManager,
    *,
    messages: Iterable[TelegramParsedMessage],
) -> int:
    messages_list = list(messages)
    if not messages_list:
        return 0

    hashes = [m.event_hash for m in messages_list if m.event_hash]
    existing = state.list_existing_telegram_event_hashes(hashes)
    filtered = [m for m in messages_list if not m.event_hash or m.event_hash not in existing]

    rows = [
        {
            "channel": m.channel,
            "message_id": m.message_id,
            "published_at_utc": m.published_at_utc,
            "text": m.text,
            "views": m.views,
            "forwards": m.forwards,
            "has_media": m.has_media,
            "source_type": m.source_type,
            "sentiment_score": m.sentiment_score,
            "impact_score": m.impact_score,
            "event_hash": m.event_hash,
            "raw_json": m.raw_json,
        }
        for m in filtered
    ]
    return state.upsert_telegram_messages(messages=rows)
