from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TwitterAccountConfig:
    username: str
    password: str
    email: str | None
    email_password: str | None
    phone: str | None
    user_agent: str | None


@dataclass(frozen=True)
class AppConfig:
    db_path: Path
    binance_api_key: str | None
    binance_api_secret: str | None
    binance_base_url: str
    binance_testnet: bool
    binance_recv_window_ms: int
    binance_timeout_s: float
    binance_tls_verify: bool
    binance_ca_bundle_path: Path | None
    binance_spot_bnb_burn: bool | None

    trading_default_exit_asset: str
    trading_default_budget_mode: str
    trading_default_stop_loss_pct: str
    trading_auto_cancel_expired_limit_orders: bool
    trading_monitoring_interval_seconds: int | None

    market_volume_window_fast: int
    market_volume_window_slow: int
    market_volume_spike_ratio: float
    market_volume_zscore_threshold: float
    market_volume_buy_ratio: float
    market_volume_sell_ratio: float
    market_volume_depth_limit: int
    market_volume_wall_ratio: float
    market_volume_imbalance_threshold: float

    gnews_api_key: str | None
    fear_greed_cache_ttl_seconds: int
    gnews_cache_ttl_seconds: int

    reddit_client_id: str | None
    reddit_client_secret: str | None
    reddit_device_id: str | None
    reddit_user_agent: str | None

    twitter_accounts: tuple[TwitterAccountConfig, ...]
    twitter_user_agent: str | None
    twitter_db_path: Path | None

    telegram_api_id: int | None
    telegram_api_hash: str | None
    telegram_phone: str | None
    telegram_session_path: Path | None
    telegram_backfill_limit: int
    telegram_join_channels: bool
    telegram_channels: tuple[str, ...]
    telegram_keywords: tuple[str, ...]

    youtube_api_key: str | None
    youtube_keywords: tuple[str, ...]
    youtube_channels: tuple[str, ...]
    youtube_backfill_limit: int
    youtube_comment_limit: int
    youtube_language: str | None

    twscrape_db_path: Path | None
    twscrape_accounts_json: Path | None
