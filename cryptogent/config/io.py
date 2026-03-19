from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from cryptogent.config.model import AppConfig


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
    )
