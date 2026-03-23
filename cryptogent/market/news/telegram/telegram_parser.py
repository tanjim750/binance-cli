"""
cryptogent.market.news.telegram_parser
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Normalize, filter, and score Telegram messages for storage.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class TelegramParsedMessage:
    channel: str
    message_id: int
    published_at_utc: str
    text: str | None
    views: int | None
    forwards: int | None
    has_media: bool
    source_type: str
    sentiment_score: float
    impact_score: float
    event_hash: str | None
    raw_json: dict
    matched_keywords: tuple[str, ...]


def parse_messages(
    messages: Iterable[Any],
    *,
    channel: str,
    source_type: str,
    keyword_patterns: tuple[re.Pattern[str], ...],
) -> list[TelegramParsedMessage]:
    parsed: list[TelegramParsedMessage] = []
    for msg in messages:
        text = _extract_text(msg)
        matched = _match_keywords(text, keyword_patterns)
        if keyword_patterns and not matched:
            continue

        message_id = int(getattr(msg, "id", 0) or 0)
        if message_id <= 0:
            continue

        published_at_utc = _extract_timestamp(msg)
        views = _as_int(getattr(msg, "views", None))
        forwards = _as_int(getattr(msg, "forwards", None))
        has_media = getattr(msg, "media", None) is not None
        sentiment_score = _score_sentiment(text or "")
        impact_score = _score_impact(text or "", source_type=source_type, views=views, forwards=forwards)
        event_hash = _hash_event(text or "")

        parsed.append(
            TelegramParsedMessage(
                channel=channel,
                message_id=message_id,
                published_at_utc=published_at_utc,
                text=text,
                views=views,
                forwards=forwards,
                has_media=has_media,
                source_type=source_type,
                sentiment_score=sentiment_score,
                impact_score=impact_score,
                event_hash=event_hash,
                raw_json=_safe_to_dict(msg),
                matched_keywords=matched,
            )
        )
    return parsed


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


def _extract_text(msg: Any) -> str | None:
    text = getattr(msg, "message", None)
    if text:
        return str(text).strip() or None
    raw_text = getattr(msg, "raw_text", None)
    if raw_text:
        return str(raw_text).strip() or None
    return None


def _match_keywords(text: str | None, patterns: tuple[re.Pattern[str], ...]) -> tuple[str, ...]:
    if not text or not patterns:
        return ()
    matches = []
    for pat in patterns:
        if pat.search(text):
            matches.append(pat.pattern)
    return tuple(matches)


def _extract_timestamp(msg: Any) -> str:
    value = getattr(msg, "date", None)
    if value is None:
        return datetime.now(UTC).replace(microsecond=0).isoformat()
    try:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC).replace(microsecond=0).isoformat()
        return value.astimezone(UTC).replace(microsecond=0).isoformat()
    except Exception:
        return str(value)


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _score_sentiment(text: str) -> float:
    text_l = text.lower()
    positives = ("list", "listing", "launch", "partnership", "approved", "approval", "bull")
    negatives = ("delist", "delisting", "hack", "exploit", "lawsuit", "sec", "ban", "liquidation", "rug")
    pos = sum(1 for w in positives if w in text_l)
    neg = sum(1 for w in negatives if w in text_l)
    if pos == 0 and neg == 0:
        return 0.0
    score = (pos - neg) / max(1, pos + neg)
    return max(-1.0, min(1.0, score))


def _score_impact(text: str, *, source_type: str, views: int | None, forwards: int | None) -> float:
    text_l = text.lower()
    score = 0.0
    if source_type == "official_announcement":
        score += 20.0
    if "hack" in text_l or "exploit" in text_l:
        score += 60.0
    if "delist" in text_l or "delisting" in text_l:
        score += 50.0
    if "listing" in text_l:
        score += 40.0
    if "etf" in text_l or "sec" in text_l:
        score += 35.0
    if "liquidation" in text_l:
        score += 25.0
    if "unlock" in text_l:
        score += 20.0
    if views:
        score += min(15.0, views / 10000.0)
    if forwards:
        score += min(15.0, forwards / 100.0)
    return min(100.0, score)


def _hash_event(text: str) -> str | None:
    norm = _normalize_text(text)
    if len(norm) < 20:
        return None
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"https?://\\S+", "", text)
    text = re.sub(r"[^a-z0-9\\s]+", " ", text)
    text = re.sub(r"\\s+", " ", text).strip()
    return text


def _safe_to_dict(msg: Any) -> dict:
    if hasattr(msg, "to_dict"):
        try:
            return msg.to_dict()
        except Exception:
            return {"repr": repr(msg)}
    try:
        return json.loads(json.dumps(msg, default=str))
    except Exception:
        return {"repr": repr(msg)}
