"""
cryptogent.market.news.youtube_videos
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Video metadata retrieval.
"""
from __future__ import annotations

from typing import Any, Iterable

from cryptogent.market.news.youtube.youtube_client import execute_request


def fetch_videos(service: Any, *, video_ids: Iterable[str]) -> list[dict]:
    ids = [vid for vid in video_ids if vid]
    if not ids:
        return []
    request = service.videos().list(
        part="snippet,statistics,contentDetails",
        id=",".join(ids),
        maxResults=min(50, len(ids)),
    )
    resp = execute_request(request)
    return resp.get("items", [])
