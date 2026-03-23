"""
cryptogent.market.news.youtube_config
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Configuration helpers for YouTube ingestion.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from cryptogent.config.model import AppConfig


DEFAULT_KEYWORDS = (
    "BTC",
    "ETH",
    "SOL",
    "BNB",
    "ETF",
    "SEC",
    "hack",
    "exploit",
    "liquidation",
    "unlock",
)


@dataclass(frozen=True)
class YouTubeIngestionConfig:
    api_key: str | None
    keywords: tuple[str, ...]
    channels: tuple[str, ...]
    backfill_limit: int
    comment_limit: int
    language: str | None


def build_youtube_config(cfg: AppConfig) -> YouTubeIngestionConfig:
    return YouTubeIngestionConfig(
        api_key=cfg.youtube_api_key,
        keywords=_normalize_list(cfg.youtube_keywords or DEFAULT_KEYWORDS),
        channels=_normalize_list(cfg.youtube_channels),
        backfill_limit=int(cfg.youtube_backfill_limit or 50),
        comment_limit=int(cfg.youtube_comment_limit or 50),
        language=cfg.youtube_language,
    )


def _normalize_list(items: Iterable[str]) -> tuple[str, ...]:
    normalized = []
    for item in items:
        value = str(item).strip()
        if not value:
            continue
        normalized.append(value)
    return tuple(normalized)
