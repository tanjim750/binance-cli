"""
cryptogent.market.news.youtube_parser
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Normalize and filter YouTube videos/comments.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable


@dataclass(frozen=True)
class ParsedYouTubeVideo:
    video_id: str
    channel_id: str
    channel_title: str | None
    title: str
    description: str | None
    published_at_utc: str
    tags: list[str]
    view_count: int | None
    like_count: int | None
    comment_count: int | None
    raw_json: dict
    topic_labels: list[str]
    sentiment_score: float
    impact_score: float
    source_type: str


@dataclass(frozen=True)
class ParsedYouTubeComment:
    video_id: str
    comment_id: str
    published_at_utc: str
    text: str | None
    like_count: int | None
    reply_count: int | None
    author_channel_id: str | None
    source_type: str
    topic_labels: list[str]
    sentiment_score: float
    impact_score: float
    raw_json: dict


def build_keyword_patterns(keywords: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    patterns: list[re.Pattern[str]] = []
    for kw in keywords:
        term = str(kw).strip()
        if not term:
            continue
        if term.isalpha() and term.isupper():
            pattern = re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
        else:
            pattern = re.compile(re.escape(term), re.IGNORECASE)
        patterns.append(pattern)
    return tuple(patterns)


def parse_videos(
    items: Iterable[dict],
    *,
    keyword_patterns: tuple[re.Pattern[str], ...],
    language: str | None,
) -> list[ParsedYouTubeVideo]:
    parsed: list[ParsedYouTubeVideo] = []
    for item in items:
        snippet = item.get("snippet", {})
        statistics = item.get("statistics", {})
        title = str(snippet.get("title") or "").strip()
        description = str(snippet.get("description") or "").strip() or None
        if not title:
            continue
        published_at = snippet.get("publishedAt")
        published_at_utc = _to_utc_iso(published_at)
        channel_id = snippet.get("channelId") or ""
        channel_title = snippet.get("channelTitle")
        video_id = item.get("id") or ""

        if language and snippet.get("defaultLanguage") and snippet.get("defaultLanguage") != language:
            continue

        if keyword_patterns:
            text_blob = f"{title} {description or ''}"
            if not _matches_keywords(text_blob, keyword_patterns):
                continue

        parsed.append(
            ParsedYouTubeVideo(
                video_id=video_id,
                channel_id=channel_id,
                channel_title=channel_title,
                title=title,
                description=description,
                published_at_utc=published_at_utc,
                tags=list(snippet.get("tags") or []),
                view_count=_as_int(statistics.get("viewCount")),
                like_count=_as_int(statistics.get("likeCount")),
                comment_count=_as_int(statistics.get("commentCount")),
                raw_json=_safe_json(item),
                topic_labels=_topic_labels(title, description),
                sentiment_score=_score_sentiment(title + " " + (description or "")),
                impact_score=_score_impact(
                    title + " " + (description or ""),
                    views=_as_int(statistics.get("viewCount")),
                ),
                source_type="video_description" if description else "video_title",
            )
        )
    return parsed


def parse_comments(
    items: Iterable[dict],
    *,
    keyword_patterns: tuple[re.Pattern[str], ...],
    language: str | None,
) -> list[ParsedYouTubeComment]:
    parsed: list[ParsedYouTubeComment] = []
    for item in items:
        snippet = item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
        video_id = item.get("snippet", {}).get("videoId") or ""
        comment_id = item.get("id") or ""
        text = str(snippet.get("textDisplay") or "").strip()
        if not text or _looks_spam(text):
            continue

        if language and snippet.get("language") and snippet.get("language") != language:
            continue

        if keyword_patterns and not _matches_keywords(text, keyword_patterns):
            continue

        parsed.append(
            ParsedYouTubeComment(
                video_id=video_id,
                comment_id=comment_id,
                published_at_utc=_to_utc_iso(snippet.get("publishedAt")),
                text=text,
                like_count=_as_int(snippet.get("likeCount")),
                reply_count=_as_int(item.get("snippet", {}).get("totalReplyCount")),
                author_channel_id=_get_author_channel_id(snippet),
                source_type="comment",
                topic_labels=_topic_labels(text, None),
                sentiment_score=_score_sentiment(text),
                impact_score=_score_impact(text, likes=_as_int(snippet.get("likeCount"))),
                raw_json=_safe_json(item),
            )
        )
    return parsed


def _matches_keywords(text: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    for pat in patterns:
        if pat.search(text):
            return True
    return False


def _looks_spam(text: str) -> bool:
    if len(text) < 6:
        return True
    if text.count("http") >= 2:
        return True
    if _repeated_chars(text):
        return True
    return False


def _repeated_chars(text: str) -> bool:
    return bool(re.search(r"(.)\1\1\1", text))


def _to_utc_iso(value: object) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(microsecond=0).isoformat()
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC).replace(microsecond=0).isoformat()
        except Exception:
            return value
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _get_author_channel_id(snippet: dict) -> str | None:
    channel_id = snippet.get("authorChannelId", {})
    if isinstance(channel_id, dict):
        return channel_id.get("value")
    return None


def _topic_labels(title: str, description: str | None) -> list[str]:
    text = f"{title} {description or ''}".lower()
    labels = []
    for key in ("btc", "eth", "sol", "bnb", "etf", "sec", "hack", "exploit", "liquidation", "unlock"):
        if key in text:
            labels.append(key.upper())
    return labels


def _score_sentiment(text: str) -> float:
    text_l = text.lower()
    positives = ("listing", "approved", "approval", "partnership", "launch")
    negatives = ("delist", "hack", "exploit", "lawsuit", "sec", "ban", "liquidation")
    pos = sum(1 for w in positives if w in text_l)
    neg = sum(1 for w in negatives if w in text_l)
    if pos == 0 and neg == 0:
        return 0.0
    score = (pos - neg) / max(1, pos + neg)
    return max(-1.0, min(1.0, score))


def _score_impact(text: str, *, views: int | None = None, likes: int | None = None) -> float:
    text_l = text.lower()
    score = 0.0
    if "hack" in text_l or "exploit" in text_l:
        score += 60.0
    if "delist" in text_l:
        score += 50.0
    if "listing" in text_l:
        score += 40.0
    if "etf" in text_l or "sec" in text_l:
        score += 35.0
    if views:
        score += min(20.0, views / 100000.0)
    if likes:
        score += min(10.0, likes / 1000.0)
    return min(100.0, score)


def _safe_json(item: dict) -> dict:
    try:
        return json.loads(json.dumps(item))
    except Exception:
        return {"repr": repr(item)}


def compute_video_hash(title: str, description: str | None) -> str:
    norm = _normalize_text(f"{title} {description or ''}")
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"https?://\\S+", "", text)
    text = re.sub(r"[^a-z0-9\\s]+", " ", text)
    text = re.sub(r"\\s+", " ", text).strip()
    return text
