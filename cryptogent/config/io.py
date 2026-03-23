from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from cryptogent.config.model import AppConfig, TwitterAccountConfig


DEFAULT_CONFIG_PATH = Path("cryptogent.toml")
DEFAULT_DB_PATH = Path("cryptogent.sqlite3")
BINANCE_SPOT_BASE_URL = "https://api.binance.com"
BINANCE_SPOT_TESTNET_BASE_URL = "https://testnet.binance.vision"


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    if not value:
        return None
    return Path(value).expanduser()

def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "y", "on"):
            return True
        if v in ("0", "false", "no", "n", "off"):
            return False
    return default


def _as_optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("auto", ""):
            return None
        if v in ("1", "true", "yes", "y", "on"):
            return True
        if v in ("0", "false", "no", "n", "off"):
            return False
    return None


@dataclass(frozen=True)
class ConfigPaths:
    config_path: Path
    db_path: Path | None

    @staticmethod
    def from_cli(*, config_path: Path | None, db_path: Path | None) -> "ConfigPaths":
        resolved_config = (config_path or _env_path("CRYPTOGENT_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()
        resolved_db = db_path.expanduser() if db_path else None
        return ConfigPaths(config_path=resolved_config, db_path=resolved_db)


def ensure_default_config(config_path: Path) -> Path:
    config_path = config_path.expanduser()
    if config_path.exists():
        return config_path
    config_path.write_text(
        "\n".join(
            [
                "[app]",
                f'db_path = "{DEFAULT_DB_PATH.as_posix()}"',
                "",
                "[binance]",
                f'# base_url = "{BINANCE_SPOT_BASE_URL}"  # optional override via env CRYPTOGENT_BINANCE_BASE_URL',
                "testnet = false",
                "recv_window_ms = 5000",
                "timeout_s = 10",
                "tls_verify = true",
                '# ca_bundle_path = "/path/to/proxy-ca.pem"',
                'spot_bnb_burn = "auto"  # true|false|"auto" (sync from Binance via `cryptogent config sync-bnb-burn`)',
                "# Prefer env vars instead of storing secrets here.",
                'api_key = ""',
                'api_secret = ""',
                "",
                "[binance_testnet]",
                "# Used only when [binance].testnet = true (or when CLI --testnet is used).",
                'spot_bnb_burn = "auto"',
                'api_key = ""',
                'api_secret = ""',
                "",
                "[trading]",
                'default_exit_asset = "USDT"',
                'default_budget_mode = "manual"',  # manual | auto
                "default_stop_loss_pct = 1.0",
                "monitoring_interval_seconds = 60",
                "auto_cancel_expired_limit_orders = true",
                "",
                "[market]",
                "volume_window_fast = 20",
                "volume_window_slow = 50",
                "volume_spike_ratio = 2.0",
                "volume_zscore_threshold = 2.0",
                "volume_buy_ratio = 0.55",
                "volume_sell_ratio = 0.45",
                "volume_depth_limit = 50",
                "volume_wall_ratio = 3.0",
                "volume_imbalance_threshold = 0.2",
                "",
                "[gnews]",
                'api_key = ""',
                "cache_ttl_seconds = 3600",
                "",
                "[fear_greed]",
                "cache_ttl_seconds = 3600",
                "",
                "[reddit]",
                'client_id = ""',
                'client_secret = ""',
                'device_id = ""  # optional for installed-client flow',
                'user_agent = "cryptogent/1.0 (reddit client)"',
                "",
                "[telegram]",
                'api_id = ""',
                'api_hash = ""',
                'phone = ""',
                'session_path = "telegram.session"',
                "backfill_limit = 200",
                "join_channels = true",
                "channels = [",
                '  "binance_announcements",',
                '  "binance_api_announcements",',
                '  "cointelegraph",',
                '  "wublockchainenglish",',
                '  "lookonchainchannel",',
                "]",
                "keywords = [",
                '  "BTC", "ETH", "SOL", "BNB",',
                '  "listing", "delisting",',
                '  "hack", "exploit",',
                '  "ETF", "SEC",',
                '  "liquidation", "unlock"',
                "]",
                "",
                "[youtube]",
                'api_key = ""',
                "backfill_limit = 50",
                "comment_limit = 50",
                'language = "en"',
                "channels = [",
                '  # "CoinDesk",',
                "]",
                "keywords = [",
                '  "BTC", "ETH", "SOL", "BNB",',
                '  "ETF", "SEC",',
                '  "hack", "exploit",',
                '  "liquidation", "unlock"',
                "]",
                "",
                "[twitter]",
                'user_agent = "cryptogent/1.0 (twscrape)"',
                '# db_path = "twscrape.sqlite3"  # optional local db for twscrape accounts',
                "# At least one account is required for scraping.",
                "[[twitter.accounts]]",
                'username = ""',
                'password = ""',
                'email = ""',
                'email_password = ""',
                'phone = ""',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def load_config(config_path: Path) -> AppConfig:
    config_path = config_path.expanduser()
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))

    app = data.get("app", {})
    binance = data.get("binance", {})
    binance_testnet = data.get("binance_testnet", {})
    trading = data.get("trading", {})
    market = data.get("market", {})
    gnews = data.get("gnews", {})
    fear_greed = data.get("fear_greed", {})
    reddit = data.get("reddit", {})
    twitter = data.get("twitter", {})
    telegram = data.get("telegram", {})
    youtube = data.get("youtube", {})
    twscrape = data.get("twscrape", {})

    db_path = Path(app.get("db_path") or DEFAULT_DB_PATH).expanduser()

    # Network settings (kept in config so the client stays deterministic and testable).
    testnet_enabled = _as_bool(os.environ.get("CRYPTOGENT_BINANCE_TESTNET") or binance.get("testnet"), False)
    base_url_override = os.environ.get("CRYPTOGENT_BINANCE_BASE_URL")
    if base_url_override:
        base_url = str(base_url_override).strip()
    else:
        # Network selection is controlled by the `testnet` flag, not by editing base_url.
        base_url = BINANCE_SPOT_TESTNET_BASE_URL if testnet_enabled else BINANCE_SPOT_BASE_URL
    recv_window_ms = int(binance.get("recv_window_ms") or 5000)
    timeout_s = float(binance.get("timeout_s") or 10)
    tls_verify = _as_bool(binance.get("tls_verify"), True)
    ca_bundle_path = _env_path("CRYPTOGENT_CA_BUNDLE") or (
        Path(str(binance.get("ca_bundle_path"))).expanduser() if binance.get("ca_bundle_path") else None
    )

    if testnet_enabled:
        api_key = os.environ.get("BINANCE_TESTNET_API_KEY") or (binance_testnet.get("api_key") or None) or None
        api_secret = os.environ.get("BINANCE_TESTNET_API_SECRET") or (binance_testnet.get("api_secret") or None) or None
    else:
        api_key = os.environ.get("BINANCE_API_KEY") or (binance.get("api_key") or None) or None
        api_secret = os.environ.get("BINANCE_API_SECRET") or (binance.get("api_secret") or None) or None
    api_key = api_key if api_key not in ("", None) else None
    api_secret = api_secret if api_secret not in ("", None) else None

    default_exit_asset = str(trading.get("default_exit_asset") or "USDT").strip().upper()
    default_budget_mode = str(trading.get("default_budget_mode") or "manual").strip().lower()
    default_stop_loss_pct = str(trading.get("default_stop_loss_pct") or "1.0").strip()
    monitoring_interval_seconds = None
    try:
        if trading.get("monitoring_interval_seconds") not in (None, ""):
            monitoring_interval_seconds = int(trading.get("monitoring_interval_seconds"))
            if monitoring_interval_seconds <= 0:
                monitoring_interval_seconds = None
    except Exception:
        monitoring_interval_seconds = None
    # Default: true (safe-by-default) so expired LIMIT orders are cancelled automatically unless explicitly disabled.
    auto_cancel_expired_limit_orders = _as_bool(trading.get("auto_cancel_expired_limit_orders"), True)

    bnb_burn_value = (binance_testnet.get("spot_bnb_burn") if testnet_enabled else binance.get("spot_bnb_burn"))
    spot_bnb_burn = _as_optional_bool(os.environ.get("CRYPTOGENT_SPOT_BNB_BURN") or bnb_burn_value)

    return AppConfig(
        db_path=db_path,
        binance_api_key=api_key,
        binance_api_secret=api_secret,
        binance_base_url=base_url,
        binance_testnet=testnet_enabled,
        binance_recv_window_ms=recv_window_ms,
        binance_timeout_s=timeout_s,
        binance_tls_verify=tls_verify,
        binance_ca_bundle_path=ca_bundle_path,
        binance_spot_bnb_burn=spot_bnb_burn,
        trading_default_exit_asset=default_exit_asset,
        trading_default_budget_mode=default_budget_mode,
        trading_default_stop_loss_pct=default_stop_loss_pct,
        trading_auto_cancel_expired_limit_orders=auto_cancel_expired_limit_orders,
        trading_monitoring_interval_seconds=monitoring_interval_seconds,
        market_volume_window_fast=int(market.get("volume_window_fast") or 20),
        market_volume_window_slow=int(market.get("volume_window_slow") or 50),
        market_volume_spike_ratio=float(market.get("volume_spike_ratio") or 2.0),
        market_volume_zscore_threshold=float(market.get("volume_zscore_threshold") or 2.0),
        market_volume_buy_ratio=float(market.get("volume_buy_ratio") or 0.55),
        market_volume_sell_ratio=float(market.get("volume_sell_ratio") or 0.45),
        market_volume_depth_limit=int(market.get("volume_depth_limit") or 50),
        market_volume_wall_ratio=float(market.get("volume_wall_ratio") or 3.0),
        market_volume_imbalance_threshold=float(market.get("volume_imbalance_threshold") or 0.2),
        gnews_api_key=(str(gnews.get("api_key")).strip() if gnews.get("api_key") not in (None, "") else None),
        gnews_cache_ttl_seconds=int(gnews.get("cache_ttl_seconds") or 3600),
        fear_greed_cache_ttl_seconds=int(fear_greed.get("cache_ttl_seconds") or 3600),
        reddit_client_id=(str(reddit.get("client_id")).strip() if reddit.get("client_id") not in (None, "") else None),
        reddit_client_secret=(
            str(reddit.get("client_secret")).strip() if reddit.get("client_secret") not in (None, "") else None
        ),
        reddit_device_id=(str(reddit.get("device_id")).strip() if reddit.get("device_id") not in (None, "") else None),
        reddit_user_agent=(
            str(reddit.get("user_agent")).strip() if reddit.get("user_agent") not in (None, "") else None
        ),
        twitter_accounts=_parse_twitter_accounts(twitter.get("accounts"), default_user_agent=twitter.get("user_agent")),
        twitter_user_agent=(
            str(twitter.get("user_agent")).strip() if twitter.get("user_agent") not in (None, "") else None
        ),
        twitter_db_path=(
            Path(str(twitter.get("db_path"))).expanduser() if twitter.get("db_path") not in (None, "") else None
        ),
        telegram_api_id=_as_optional_int(telegram.get("api_id")),
        telegram_api_hash=(str(telegram.get("api_hash")).strip() if telegram.get("api_hash") not in (None, "") else None),
        telegram_phone=(str(telegram.get("phone")).strip() if telegram.get("phone") not in (None, "") else None),
        telegram_session_path=(
            Path(str(telegram.get("session_path"))).expanduser()
            if telegram.get("session_path") not in (None, "")
            else None
        ),
        telegram_backfill_limit=int(telegram.get("backfill_limit") or 200),
        telegram_join_channels=_as_bool(telegram.get("join_channels"), True),
        telegram_channels=_as_string_list(telegram.get("channels")),
        telegram_keywords=_as_string_list(telegram.get("keywords")),
        youtube_api_key=(str(youtube.get("api_key")).strip() if youtube.get("api_key") not in (None, "") else None),
        youtube_keywords=_as_string_list(youtube.get("keywords")),
        youtube_channels=_as_string_list(youtube.get("channels")),
        youtube_backfill_limit=int(youtube.get("backfill_limit") or 50),
        youtube_comment_limit=int(youtube.get("comment_limit") or 50),
        youtube_language=(
            str(youtube.get("language")).strip() if youtube.get("language") not in (None, "") else None
        ),
        twscrape_db_path=(
            Path(str(twscrape.get("db_path"))).expanduser() if twscrape.get("db_path") not in (None, "") else None
        ),
        twscrape_accounts_json=(
            Path(str(twscrape.get("accounts_json"))).expanduser()
            if twscrape.get("accounts_json") not in (None, "")
            else None
        ),
    )


def _as_optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except Exception:
            return None
    return None


def _as_string_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value if str(v).strip()]
        return tuple(items)
    if isinstance(value, str):
        v = value.strip()
        return (v,) if v else ()
    return ()


def _parse_twitter_accounts(
    raw: object, *, default_user_agent: object | None = None
) -> tuple[TwitterAccountConfig, ...]:
    if raw is None:
        return ()
    if isinstance(raw, dict):
        raw_list = [raw]
    elif isinstance(raw, list):
        raw_list = raw
    else:
        return ()

    accounts: list[TwitterAccountConfig] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        username = str(item.get("username") or "").strip()
        password = str(item.get("password") or "").strip()
        if not username or not password:
            continue
        email = str(item.get("email") or "").strip() or None
        email_password = str(item.get("email_password") or "").strip() or None
        phone = str(item.get("phone") or "").strip() or None
        user_agent = str(item.get("user_agent") or "").strip() or None
        if not user_agent and default_user_agent not in (None, ""):
            user_agent = str(default_user_agent).strip()
        accounts.append(
            TwitterAccountConfig(
                username=username,
                password=password,
                email=email,
                email_password=email_password,
                phone=phone,
                user_agent=user_agent,
            )
        )
    return tuple(accounts)
