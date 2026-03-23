"""
cryptogent.market.crypto
~~~~~~~~~~~~~~~~~~~~~~~~
Crypto-specific analytics for market status:

  - Funding rate (futures)
  - Open interest (futures)

Whale activity and exchange inflow/outflow are placeholders for later.
"""
from __future__ import annotations

from dataclasses import dataclass

from cryptogent.exchange.binance_futures import get_open_interest, get_premium_index


@dataclass(frozen=True)
class CryptoMetrics:
    futures_market: str | None
    funding_rate: str | None
    next_funding_time: str | None
    open_interest: str | None


def compute_crypto_metrics(
    *,
    symbol: str,
    futures_market: str,
    timeout_s: float,
    tls_verify: bool,
    ca_bundle_path,
) -> CryptoMetrics:
    funding_rate = None
    next_funding_time = None
    open_interest = None

    try:
        prem = get_premium_index(
            symbol=symbol,
            market=futures_market,
            timeout_s=timeout_s,
            tls_verify=tls_verify,
            ca_bundle_path=ca_bundle_path,
        )
        if isinstance(prem, dict):
            funding_rate = prem.get("lastFundingRate") or prem.get("fundingRate")
            next_funding_time = prem.get("nextFundingTime")
    except Exception:
        funding_rate = None
        next_funding_time = None

    try:
        oi = get_open_interest(
            symbol=symbol,
            market=futures_market,
            timeout_s=timeout_s,
            tls_verify=tls_verify,
            ca_bundle_path=ca_bundle_path,
        )
        if isinstance(oi, dict):
            open_interest = oi.get("openInterest")
    except Exception:
        open_interest = None

    return CryptoMetrics(
        futures_market=futures_market,
        funding_rate=str(funding_rate) if funding_rate is not None else None,
        next_funding_time=str(next_funding_time) if next_funding_time is not None else None,
        open_interest=str(open_interest) if open_interest is not None else None,
    )
