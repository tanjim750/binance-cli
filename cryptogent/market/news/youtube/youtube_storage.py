"""
cryptogent.market.news.youtube_storage
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Storage helpers for YouTube ingestion.
"""
from __future__ import annotations

from typing import Iterable

from cryptogent.market.news.youtube.youtube_parser import ParsedYouTubeComment, ParsedYouTubeVideo
from cryptogent.state.manager import StateManager


def persist_videos(state: StateManager, *, videos: Iterable[ParsedYouTubeVideo]) -> int:
    videos_list = list(videos)
    if not videos_list:
        return 0
    rows = [
        {
            "video_id": v.video_id,
            "channel_id": v.channel_id,
            "channel_title": v.channel_title,
            "title": v.title,
            "description": v.description,
            "published_at_utc": v.published_at_utc,
            "tags": v.tags,
            "view_count": v.view_count,
            "like_count": v.like_count,
            "comment_count": v.comment_count,
            "topic_labels": v.topic_labels,
            "sentiment_score": v.sentiment_score,
            "impact_score": v.impact_score,
            "source_type": v.source_type,
            "raw_json": v.raw_json,
        }
        for v in videos_list
    ]
    return state.upsert_youtube_videos(videos=rows)


def persist_comments(state: StateManager, *, comments: Iterable[ParsedYouTubeComment]) -> int:
    comments_list = list(comments)
    if not comments_list:
        return 0
    rows = [
        {
            "video_id": c.video_id,
            "comment_id": c.comment_id,
            "published_at_utc": c.published_at_utc,
            "text": c.text,
            "like_count": c.like_count,
            "reply_count": c.reply_count,
            "author_channel_id": c.author_channel_id,
            "source_type": c.source_type,
            "topic_labels": c.topic_labels,
            "sentiment_score": c.sentiment_score,
            "impact_score": c.impact_score,
            "raw_json": c.raw_json,
        }
        for c in comments_list
    ]
    return state.upsert_youtube_comments(comments=rows)
