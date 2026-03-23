"""
cryptogent.market.news.youtube_comments
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Comment thread retrieval.
"""
from __future__ import annotations

from typing import Any

from cryptogent.market.news.youtube.youtube_client import execute_request


def fetch_comment_threads(
    service: Any,
    *,
    video_id: str,
    limit: int,
    order: str = "time",
) -> list[dict]:
    request = service.commentThreads().list(
        part="snippet",
        videoId=video_id,
        maxResults=min(100, int(limit)),
        order=order,
        textFormat="plainText",
    )
    resp = execute_request(request)
    return resp.get("items", [])
