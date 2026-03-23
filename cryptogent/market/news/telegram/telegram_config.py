"""
cryptogent.market.news.telegram_config
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Configuration helpers for Telegram channel ingestion.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from cryptogent.config.model import AppConfig


OFFICIAL_CHANNELS = {
    "binance_announcements",
    "binance_api_announcements",
}

NEWS_CHANNELS = {
    "cointelegraph",
    "wublockchainenglish",
    "lookonchainchannel",
}


DEFAULT_KEYWORDS = (
    "BTC",
    "ETH",
    "SOL",
    "BNB",
    "listing",
    "delisting",
    "hack",
    "exploit",
    "ETF",
    "SEC",
    "liquidation",
    "unlock",
)


@dataclass(frozen=True)
class TelegramChannelConfig:
    username: str
    source_type: str
    enabled: bool = True


@dataclass(frozen=True)
class TelegramIngestionConfig:
    api_id: int | None
    api_hash: str | None
    phone: str | None
    session_path: str
    backfill_limit: int
    join_channels: bool
    channels: tuple[TelegramChannelConfig, ...]
    keywords: tuple[str, ...]


def build_telegram_config(cfg: AppConfig) -> TelegramIngestionConfig:
    channels = _build_channel_list(cfg.telegram_channels)
    keywords = cfg.telegram_keywords or DEFAULT_KEYWORDS
    session_path = str(cfg.telegram_session_path or "telegram.session")
    return TelegramIngestionConfig(
        api_id=cfg.telegram_api_id,
        api_hash=cfg.telegram_api_hash,
        phone=cfg.telegram_phone,
        session_path=session_path,
        backfill_limit=cfg.telegram_backfill_limit,
        join_channels=cfg.telegram_join_channels,
        channels=channels,
        keywords=tuple(keywords),
    )


def _build_channel_list(channels: Iterable[str]) -> tuple[TelegramChannelConfig, ...]:
    result: list[TelegramChannelConfig] = []
    for name in channels:
        username = str(name).strip().lstrip("@")
        if not username:
            continue
        lower = username.lower()
        if lower in OFFICIAL_CHANNELS:
            source_type = "official_announcement"
        elif lower in NEWS_CHANNELS:
            source_type = "news"
        else:
            source_type = "community_chatter"
        result.append(TelegramChannelConfig(username=username, source_type=source_type))
    return tuple(result)
