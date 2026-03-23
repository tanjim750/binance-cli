"""
cryptogent.exchange.binance_futures
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Binance USD-M and COIN-M futures REST helpers.

Supported markets
-----------------
  usdtm   https://fapi.binance.com   (USD-margined perpetuals / delivery)
  coinm   https://dapi.binance.com   (Coin-margined perpetuals / delivery)

All public functions follow the same contract:
  - keyword-only arguments
  - raise BinanceAPIError on non-2xx or unexpected response shape
  - raise ValueError on bad caller arguments (market, symbol)
  - never swallow exceptions silently
"""
from __future__ import annotations

import logging
import ssl
from pathlib import Path
from typing import Any

from .binance_errors import BinanceAPIError
from .binance_http import HTTPResponse, request_json, with_query

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Base URLs
# ---------------------------------------------------------------------------
_BASE_URLS: dict[str, str] = {
    "usdtm": "https://fapi.binance.com",
    "coinm": "https://dapi.binance.com",
}

# ---------------------------------------------------------------------------
# Endpoint path prefixes per market
# ---------------------------------------------------------------------------
_PATH_PREFIX: dict[str, str] = {
    "usdtm": "/fapi/v1",
    "coinm": "/dapi/v1",
}

# ---------------------------------------------------------------------------
# Expected response keys for lightweight shape validation
# ---------------------------------------------------------------------------
_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "premiumIndex":        ("symbol", "markPrice", "indexPrice"),
    "openInterest":        ("symbol", "openInterest"),
    "fundingRate":         ("symbol", "fundingRate", "fundingTime"),
    "klines":              (),    # list — validated by type check, not keys
    "globalLongShortAccountRatio": ("symbol", "longShortRatio", "timestamp"),
    "topLongShortAccountRatio":    ("symbol", "longShortRatio", "timestamp"),
    "topLongShortPositionRatio":   ("symbol", "longShortRatio", "timestamp"),
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _base_url(market: str) -> str:
    """Return the base URL for *market*, raising ``ValueError`` on unknown market."""
    try:
        return _BASE_URLS[market]
    except KeyError:
        raise ValueError(
            f"Unsupported futures market {market!r}. "
            f"Valid values: {list(_BASE_URLS)}"
        )


def _path_prefix(market: str) -> str:
    return _PATH_PREFIX[market]   # KeyError impossible — _base_url validates first


def _validate_symbol(symbol: str) -> None:
    if not symbol or not isinstance(symbol, str):
        raise ValueError("Symbol is required and must be a non-empty string.")


def _ssl_context(*, tls_verify: bool, ca_bundle_path: Path | None) -> ssl.SSLContext:
    """
    Build an SSL context.

    When ``tls_verify=False``, hostname checking and certificate verification
    are both disabled.  ORDER MATTERS: ``check_hostname`` must be set to
    ``False`` before setting ``verify_mode = CERT_NONE`` — Python raises
    ``ValueError`` if done in the wrong order.
    """
    if not tls_verify:
        logger.warning(
            "TLS verification is DISABLED. This should never be used in production."
        )
        ctx = ssl.create_default_context()
        ctx.check_hostname = False          # must come first
        ctx.verify_mode = ssl.CERT_NONE     # must come second
        return ctx
    if ca_bundle_path is not None:
        return ssl.create_default_context(cafile=str(ca_bundle_path))
    return ssl.create_default_context()


def _get(
    *,
    market: str,
    endpoint: str,
    params: dict[str, Any],
    timeout_s: float,
    tls_verify: bool,
    ca_bundle_path: Path | None,
    expect_list: bool = False,
) -> Any:
    """
    Execute a GET request against a Binance futures endpoint.

    Parameters
    ----------
    market:
        ``"usdtm"`` or ``"coinm"``.
    endpoint:
        Path suffix after the API version prefix, e.g. ``"premiumIndex"``.
    params:
        Query string parameters.
    expect_list:
        When ``True``, the response is expected to be a JSON array (e.g. klines).
        When ``False`` (default), a JSON object (dict) is expected.

    Returns
    -------
    dict | list
        Parsed JSON response.

    Raises
    ------
    BinanceAPIError
        On non-2xx HTTP status or unexpected response shape.
    ValueError
        On invalid ``market`` argument.
    """
    base   = _base_url(market)          # validates market
    prefix = _path_prefix(market)
    url    = with_query(f"{base}{prefix}/{endpoint}", params)

    logger.debug("GET %s params=%s", url, params)

    resp: HTTPResponse = request_json(
        method="GET",
        url=url,
        headers={"Accept": "application/json"},
        timeout_s=timeout_s,
        ssl_context=_ssl_context(tls_verify=tls_verify, ca_bundle_path=ca_bundle_path),
    )

    # Shape validation
    if expect_list:
        if not isinstance(resp.data, list):
            raise BinanceAPIError(
                status=resp.status,
                code=None,
                msg=f"Expected list from /{endpoint}, got {type(resp.data).__name__}",
                body=resp.data,
            )
    else:
        if not isinstance(resp.data, dict):
            raise BinanceAPIError(
                status=resp.status,
                code=None,
                msg=f"Expected dict from /{endpoint}, got {type(resp.data).__name__}",
                body=resp.data,
            )
        # Key presence validation
        required = _REQUIRED_KEYS.get(endpoint, ())
        missing  = [k for k in required if k not in resp.data]
        if missing:
            raise BinanceAPIError(
                status=resp.status,
                code=None,
                msg=f"/{endpoint} response missing required keys: {missing}",
                body=resp.data,
            )

    return resp.data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_premium_index(
    *,
    symbol: str,
    market: str,
    timeout_s: float,
    tls_verify: bool = True,
    ca_bundle_path: Path | None = None,
) -> dict:
    """
    GET /fapi(dapi)/v1/premiumIndex

    Returns mark price, index price, and funding rate for *symbol*.

    Response keys: symbol, markPrice, indexPrice, estimatedSettlePrice,
                   lastFundingRate, interestRate, nextFundingTime, time
    """
    _validate_symbol(symbol)
    return _get(
        market=market,
        endpoint="premiumIndex",
        params={"symbol": symbol},
        timeout_s=timeout_s,
        tls_verify=tls_verify,
        ca_bundle_path=ca_bundle_path,
    )


def get_open_interest(
    *,
    symbol: str,
    market: str,
    timeout_s: float,
    tls_verify: bool = True,
    ca_bundle_path: Path | None = None,
) -> dict:
    """
    GET /fapi(dapi)/v1/openInterest

    Returns the current open interest for *symbol*.

    Response keys: symbol, openInterest, time
    """
    _validate_symbol(symbol)
    return _get(
        market=market,
        endpoint="openInterest",
        params={"symbol": symbol},
        timeout_s=timeout_s,
        tls_verify=tls_verify,
        ca_bundle_path=ca_bundle_path,
    )


def get_funding_rate(
    *,
    symbol: str,
    market: str,
    timeout_s: float,
    tls_verify: bool = True,
    ca_bundle_path: Path | None = None,
    limit: int = 1,
) -> dict:
    """
    GET /fapi(dapi)/v1/fundingRate

    Returns the most recent funding rate record for *symbol*.
    Pass ``limit > 1`` to retrieve historical funding rates (max 1000).

    Response keys: symbol, fundingRate, fundingTime
    """
    _validate_symbol(symbol)
    result = _get(
        market=market,
        endpoint="fundingRate",
        params={"symbol": symbol, "limit": limit},
        timeout_s=timeout_s,
        tls_verify=tls_verify,
        ca_bundle_path=ca_bundle_path,
        expect_list=True,
    )
    if not result:
        raise BinanceAPIError(
            status=200, code=None,
            msg="fundingRate returned empty list", body=result,
        )
    # Return the most recent entry (last element — list is oldest-first)
    return result[-1]


def get_klines(
    *,
    symbol: str,
    market: str,
    interval: str,
    timeout_s: float,
    tls_verify: bool = True,
    ca_bundle_path: Path | None = None,
    limit: int = 500,
    start_time: int | None = None,
    end_time: int | None = None,
) -> list:
    """
    GET /fapi(dapi)/v1/klines

    Returns OHLCV candlestick data for *symbol*.

    Parameters
    ----------
    interval:
        Binance interval string: ``"1m"``, ``"5m"``, ``"15m"``, ``"1h"``,
        ``"4h"``, ``"1d"``, etc.
    limit:
        Number of candles to return (max 1500).
    start_time / end_time:
        Unix millisecond timestamps (optional).

    Returns
    -------
    list[list]
        Each inner list: [open_time, open, high, low, close, volume,
        close_time, quote_vol, trades, taker_buy_base, taker_buy_quote, ignore]
    """
    _validate_symbol(symbol)
    params: dict[str, Any] = {
        "symbol":   symbol,
        "interval": interval,
        "limit":    limit,
    }
    if start_time is not None:
        params["startTime"] = start_time
    if end_time is not None:
        params["endTime"] = end_time

    return _get(
        market=market,
        endpoint="klines",
        params=params,
        timeout_s=timeout_s,
        tls_verify=tls_verify,
        ca_bundle_path=ca_bundle_path,
        expect_list=True,
    )


def get_long_short_ratio(
    *,
    symbol: str,
    market: str,
    period: str,
    timeout_s: float,
    tls_verify: bool = True,
    ca_bundle_path: Path | None = None,
    ratio_type: str = "globalAccount",
    limit: int = 1,
) -> dict:
    """
    GET /fapi(dapi)/v1/{ratio_endpoint}

    Returns long/short account or position ratio for *symbol*.

    Parameters
    ----------
    period:
        Aggregation period: ``"5m"``, ``"15m"``, ``"30m"``, ``"1h"``,
        ``"2h"``, ``"4h"``, ``"6h"``, ``"12h"``, ``"1d"``.
    ratio_type:
        ``"globalAccount"``   — all traders account ratio
        ``"topAccount"``      — top trader account ratio
        ``"topPosition"``     — top trader position ratio
    limit:
        Number of records (max 500).

    Returns
    -------
    dict
        Most recent ratio record. Keys: symbol, longShortRatio, longAccount,
        shortAccount, timestamp.
    """
    _validate_symbol(symbol)
    if market == "coinm":
        raise ValueError("Long/short ratio endpoints are not available for coinm futures.")
    _endpoint_map = {
        "globalAccount": "globalLongShortAccountRatio",
        "topAccount":    "topLongShortAccountRatio",
        "topPosition":   "topLongShortPositionRatio",
    }
    endpoint = _endpoint_map.get(ratio_type)
    if endpoint is None:
        raise ValueError(
            f"Invalid ratio_type {ratio_type!r}. "
            f"Valid values: {list(_endpoint_map)}"
        )

    result = _get(
        market=market,
        endpoint=endpoint,
        params={"symbol": symbol, "period": period, "limit": limit},
        timeout_s=timeout_s,
        tls_verify=tls_verify,
        ca_bundle_path=ca_bundle_path,
        expect_list=True,
    )
    if not result:
        raise BinanceAPIError(
            status=200, code=None,
            msg=f"{endpoint} returned empty list", body=result,
        )
    return result[-1]
