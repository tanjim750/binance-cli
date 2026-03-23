"""
cryptogent.sync.youtube_sync
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Quota-aware YouTube ingestion.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cryptogent.config.io import ConfigPaths, ensure_default_config, load_config
from cryptogent.db.connection import connect
from cryptogent.market.news.youtube.youtube_client import (
    YouTubeClientError,
    YouTubeQuotaError,
    build_youtube_client,
)
from cryptogent.market.news.youtube.youtube_config import build_youtube_config
from cryptogent.market.news.youtube.youtube_discovery import (
    discover_by_keyword,
    discover_channel_videos,
    resolve_channel_id,
)
from cryptogent.market.news.youtube.youtube_comments import fetch_comment_threads
from cryptogent.market.news.youtube.youtube_parser import build_keyword_patterns, parse_comments, parse_videos
from cryptogent.market.news.youtube.youtube_state import (
    get_channel_state,
    get_discovery_state,
    update_channel_state,
    update_discovery_state,
)
from cryptogent.market.news.youtube.youtube_storage import persist_comments, persist_videos
from cryptogent.market.news.youtube.youtube_videos import fetch_videos
from cryptogent.state.manager import StateManager
from cryptogent.util.time import parse_utc_iso, utcnow_iso


@dataclass(frozen=True)
class YouTubeSyncResult:
    status: str
    videos_saved: int
    comments_saved: int


def sync_youtube(*, config_path: str | None = None, db_path: str | None = None) -> YouTubeSyncResult:
    paths = ConfigPaths.from_cli(
        config_path=Path(config_path) if config_path else None,
        db_path=Path(db_path) if db_path else None,
    )
    config_path = ensure_default_config(paths.config_path)
    cfg = load_config(config_path)
    yt_cfg = build_youtube_config(cfg)

    if not yt_cfg.api_key:
        return YouTubeSyncResult(status="error", videos_saved=0, comments_saved=0)

    try:
        service = build_youtube_client(api_key=yt_cfg.api_key)
    except YouTubeClientError:
        return YouTubeSyncResult(status="error", videos_saved=0, comments_saved=0)

    keyword_patterns = build_keyword_patterns(yt_cfg.keywords)
    videos_saved = 0
    comments_saved = 0

    with connect(paths.db_path or cfg.db_path) as conn:
        state = StateManager(conn)
        try:
            for channel in yt_cfg.channels:
                channel_id, channel_name = resolve_channel_id(service, channel=channel)
                if not channel_id:
                    state.append_audit(
                        level="WARN",
                        event="youtube_channel_not_found",
                        details={"channel": channel},
                    )
                    continue

                state_row = get_channel_state(state, channel_id=channel_id)
                limit = yt_cfg.backfill_limit
                video_ids = discover_channel_videos(service, channel_id=channel_id, limit=limit)
                if not video_ids:
                    update_channel_state(
                        state,
                        channel_id=channel_id,
                        channel_name=channel_name,
                        last_video_published_at_utc=state_row.last_video_published_at_utc if state_row else None,
                    )
                    continue

                videos = fetch_videos(service, video_ids=video_ids)
                if state_row and state_row.last_video_published_at_utc:
                    videos = _filter_newer(videos, state_row.last_video_published_at_utc)
                parsed_videos = parse_videos(videos, keyword_patterns=keyword_patterns, language=yt_cfg.language)
                videos_saved += persist_videos(state, videos=parsed_videos)

                latest_published = _max_published_at(videos)
                update_channel_state(
                    state,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    last_video_published_at_utc=latest_published,
                )

                for item in videos:
                    video_id = item.get("id")
                    if not video_id:
                        continue
                    comment_threads = fetch_comment_threads(
                        service,
                        video_id=video_id,
                        limit=yt_cfg.comment_limit,
                        order="time",
                    )
                    parsed_comments = parse_comments(
                        comment_threads,
                        keyword_patterns=keyword_patterns,
                        language=yt_cfg.language,
                    )
                    comments_saved += persist_comments(state, comments=parsed_comments)

            for keyword in yt_cfg.keywords:
                discovery_key = f"keyword:{keyword.lower()}"
                d_state = get_discovery_state(state, discovery_key=discovery_key)
                published_after = d_state.last_published_at_utc if d_state else None
                video_ids = discover_by_keyword(
                    service,
                    keyword=keyword,
                    limit=yt_cfg.backfill_limit,
                    published_after=published_after,
                    language=yt_cfg.language,
                )
                if not video_ids:
                    update_discovery_state(state, discovery_key=discovery_key, last_published_at_utc=published_after)
                    continue

                videos = fetch_videos(service, video_ids=video_ids)
                if d_state and d_state.last_published_at_utc:
                    videos = _filter_newer(videos, d_state.last_published_at_utc)
                parsed_videos = parse_videos(videos, keyword_patterns=keyword_patterns, language=yt_cfg.language)
                videos_saved += persist_videos(state, videos=parsed_videos)

                latest_published = _max_published_at(videos)
                update_discovery_state(state, discovery_key=discovery_key, last_published_at_utc=latest_published)

                for item in videos:
                    video_id = item.get("id")
                    if not video_id:
                        continue
                    comment_threads = fetch_comment_threads(
                        service,
                        video_id=video_id,
                        limit=yt_cfg.comment_limit,
                        order="time",
                    )
                    parsed_comments = parse_comments(
                        comment_threads,
                        keyword_patterns=keyword_patterns,
                        language=yt_cfg.language,
                    )
                    comments_saved += persist_comments(state, comments=parsed_comments)

            state.append_audit(
                level="INFO",
                event="youtube_sync_complete",
                details={
                    "at": utcnow_iso(),
                    "videos_saved": videos_saved,
                    "comments_saved": comments_saved,
                },
            )
            return YouTubeSyncResult(status="ok", videos_saved=videos_saved, comments_saved=comments_saved)
        except YouTubeQuotaError as exc:
            state.append_audit(
                level="WARN",
                event="youtube_quota_exceeded",
                details={"error": str(exc)},
            )
            return YouTubeSyncResult(status="quota", videos_saved=videos_saved, comments_saved=comments_saved)
        except Exception as exc:
            state.append_audit(
                level="ERROR",
                event="youtube_sync_error",
                details={"error": str(exc)},
            )
            return YouTubeSyncResult(status="error", videos_saved=videos_saved, comments_saved=comments_saved)


def _max_published_at(videos: list[dict]) -> str | None:
    published = []
    for item in videos:
        value = item.get("snippet", {}).get("publishedAt")
        if value:
            published.append(str(value))
    return max(published) if published else None


def _filter_newer(videos: list[dict], last_published_at_utc: str) -> list[dict]:
    try:
        last_dt = parse_utc_iso(last_published_at_utc)
    except Exception:
        return videos
    filtered = []
    for item in videos:
        value = item.get("snippet", {}).get("publishedAt")
        if not value:
            continue
        try:
            if parse_utc_iso(str(value)) > last_dt:
                filtered.append(item)
        except Exception:
            filtered.append(item)
    return filtered
