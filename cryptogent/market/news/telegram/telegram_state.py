"""
cryptogent.market.news.telegram_state
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Helpers for per-channel Telegram sync state.
"""
from __future__ import annotations

from dataclasses import dataclass

from cryptogent.state.manager import StateManager
from cryptogent.util.time import utcnow_iso


@dataclass(frozen=True)
class TelegramChannelState:
    channel: str
    last_message_id: int | None
    last_synced_at_utc: str | None


def get_channel_state(state: StateManager, *, channel: str) -> TelegramChannelState | None:
    row = state.get_telegram_channel_state(channel=channel)
    if not row:
        return None
    return TelegramChannelState(
        channel=row.get("channel"),
        last_message_id=row.get("last_message_id"),
        last_synced_at_utc=row.get("last_synced_at_utc"),
    )


def update_channel_state(
    state: StateManager,
    *,
    channel: str,
    last_message_id: int | None,
) -> None:
    state.upsert_telegram_channel_state(
        channel=channel,
        last_message_id=last_message_id,
        last_synced_at_utc=utcnow_iso(),
    )
