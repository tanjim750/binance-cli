"""
cryptogent.market.news.youtube_discovery
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Discovery helpers for YouTube videos.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Iterable

from cryptogent.market.news.youtube.youtube_client import execute_request


def discover_by_keyword(
    service: Any,
    *,
    keyword: str,
    limit: int,
    published_after: str | None = None,
    language: str | None = None,
) -> list[str]:
    params = {
        "part": "snippet",
        "q": keyword,
        "type": "video",
        "order": "date",
        "maxResults": min(50, int(limit)),
    }
    if published_after:
        params["publishedAfter"] = published_after
    if language:
        params["relevanceLanguage"] = language
    request = service.search().list(**params)
    resp = execute_request(request)
    return [item["id"]["videoId"] for item in resp.get("items", []) if item.get("id", {}).get("videoId")]


def resolve_channel_id(service: Any, *, channel: str) -> tuple[str | None, str | None]:
    channel = channel.strip().lstrip("@")
    if channel.startswith("UC"):
        return channel, None

    request = service.channels().list(
        part="id,snippet,contentDetails",
        forUsername=channel,
        maxResults=1,
    )
    resp = execute_request(request)
    items = resp.get("items", [])
    if not items:
        return None, None
    return items[0]["id"], items[0].get("snippet", {}).get("title")


def discover_channel_videos(
    service: Any,
    *,
    channel_id: str,
    limit: int,
) -> list[str]:
    uploads_playlist = _get_uploads_playlist(service, channel_id=channel_id)
    if not uploads_playlist:
        return []
    request = service.playlistItems().list(
        part="contentDetails",
        playlistId=uploads_playlist,
        maxResults=min(50, int(limit)),
    )
    resp = execute_request(request)
    return [item["contentDetails"]["videoId"] for item in resp.get("items", []) if item.get("contentDetails")]


def _get_uploads_playlist(service: Any, *, channel_id: str) -> str | None:
    request = service.channels().list(
        part="contentDetails",
        id=channel_id,
        maxResults=1,
    )
    resp = execute_request(request)
    items = resp.get("items", [])
    if not items:
        return None
    return items[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
