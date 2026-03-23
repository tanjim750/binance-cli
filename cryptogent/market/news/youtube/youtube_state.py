"""
cryptogent.market.news.youtube_state
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
State helpers for YouTube ingestion.
"""
from __future__ import annotations

from dataclasses import dataclass

from cryptogent.state.manager import StateManager
from cryptogent.util.time import utcnow_iso


@dataclass(frozen=True)
class YouTubeChannelState:
    channel_id: str
    last_video_published_at_utc: str | None
    last_synced_at_utc: str | None


@dataclass(frozen=True)
class YouTubeDiscoveryState:
    discovery_key: str
    last_published_at_utc: str | None
    last_synced_at_utc: str | None


def get_channel_state(state: StateManager, *, channel_id: str) -> YouTubeChannelState | None:
    row = state.get_youtube_channel_state(channel_id=channel_id)
    if not row:
        return None
    return YouTubeChannelState(
        channel_id=row.get("channel_id"),
        last_video_published_at_utc=row.get("last_video_published_at_utc"),
        last_synced_at_utc=row.get("last_synced_at_utc"),
    )


def update_channel_state(
    state: StateManager,
    *,
    channel_id: str,
    channel_name: str | None,
    last_video_published_at_utc: str | None,
) -> None:
    state.upsert_youtube_channel_state(
        channel_id=channel_id,
        channel_name=channel_name,
        last_video_published_at_utc=last_video_published_at_utc,
        last_synced_at_utc=utcnow_iso(),
    )


def get_discovery_state(state: StateManager, *, discovery_key: str) -> YouTubeDiscoveryState | None:
    row = state.get_youtube_discovery_state(discovery_key=discovery_key)
    if not row:
        return None
    return YouTubeDiscoveryState(
        discovery_key=row.get("discovery_key"),
        last_published_at_utc=row.get("last_published_at_utc"),
        last_synced_at_utc=row.get("last_synced_at_utc"),
    )


def update_discovery_state(
    state: StateManager,
    *,
    discovery_key: str,
    last_published_at_utc: str | None,
) -> None:
    state.upsert_youtube_discovery_state(
        discovery_key=discovery_key,
        last_published_at_utc=last_published_at_utc,
        last_synced_at_utc=utcnow_iso(),
    )
