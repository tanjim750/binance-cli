"""
cryptogent.sentiment.fear_greed
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Crypto Fear & Greed Index client.

Source: alternative.me/crypto/fear-and-greed-index/
API:    https://api.alternative.me/fng/

Index ranges 0–100:
  0–24   Extreme Fear   (historically: buy signal)
  25–49  Fear
  50–74  Greed
  75–100 Extreme Greed  (historically: caution / sell signal)

The index is updated once per day.  Cache responses for at least
``time_until_update_s`` seconds (or 1 hour minimum) to avoid
hammering a free public API unnecessarily.
"""
from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from cryptogent.util.time import s_to_utc_iso, utcnow_iso

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known classification strings from the API
# ---------------------------------------------------------------------------
_KNOWN_CLASSIFICATIONS = frozenset({
    "Extreme Fear",
    "Fear",
    "Neutral",
    "Greed",
    "Extreme Greed",
})

_BASE_URL = "https://api.alternative.me/fng/"


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------

class FearGreedAPIError(RuntimeError):
    """Raised on any failure to fetch or parse the Fear & Greed index."""


# ---------------------------------------------------------------------------
# Public data contracts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FearGreedReading:
    """
    A single Fear & Greed index reading.

    Attributes
    ----------
    value:
        Index value 0–100.
    value_classification:
        Human-readable label: "Extreme Fear" | "Fear" | "Neutral"
        | "Greed" | "Extreme Greed".
    timestamp_utc:
        ISO-8601 UTC timestamp of when this reading was published.
    time_until_update_s:
        Seconds until the next reading is published.
        ``None`` when not provided by the API.
    """
    value: int
    value_classification: str
    timestamp_utc: str
    time_until_update_s: int | None

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def is_extreme_fear(self) -> bool:
        return self.value <= 24

    @property
    def is_fear(self) -> bool:
        return 25 <= self.value <= 49

    @property
    def is_neutral(self) -> bool:
        return 50 <= self.value <= 54

    @property
    def is_greed(self) -> bool:
        return 55 <= self.value <= 74

    @property
    def is_extreme_greed(self) -> bool:
        return self.value >= 75

    @property
    def signal(self) -> str:
        """
        Simplified directional signal for LLM prompt injection.

        Returns one of: "strong_buy_signal" | "buy_signal" | "neutral"
        | "caution_signal" | "strong_caution_signal"

        Note: these are contrarian signals — extreme fear historically
        precedes recoveries; extreme greed precedes corrections.
        """
        if self.value <= 24:
            return "strong_buy_signal"
        if self.value <= 49:
            return "buy_signal"
        if self.value <= 54:
            return "neutral"
        if self.value <= 74:
            return "caution_signal"
        return "strong_caution_signal"


@dataclass(frozen=True)
class FearGreedResponse:
    """
    Full response from the Fear & Greed API.

    Attributes
    ----------
    source:
        Data provider identifier.
    reading:
        The most recent (current) Fear & Greed reading.
    history:
        All readings returned when ``limit > 1``, ordered newest → oldest.
        Contains at least one entry (same as ``reading``).
    """
    source: str
    reading: FearGreedReading
    history: tuple[FearGreedReading, ...]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_fear_greed(
    *,
    limit: int = 1,
    timeout_s: float = 10.0,
    ca_bundle: Path | None = None,
    insecure: bool = False,
) -> FearGreedResponse:
    """
    Fetch the current (and optionally historical) Fear & Greed index.

    Parameters
    ----------
    limit:
        Number of readings to fetch (1 = current only, max ~365 for history).
        All readings are returned in ``FearGreedResponse.history``;
        the most recent is always in ``FearGreedResponse.reading``.
    timeout_s:
        HTTP request timeout in seconds.
    ca_bundle:
        Path to a custom CA bundle for TLS verification.
    insecure:
        When ``True``, TLS certificate verification is disabled.
        Never use in production.

    Returns
    -------
    FearGreedResponse

    Raises
    ------
    FearGreedAPIError
        On any HTTP error, network failure, or unexpected response shape.
    ValueError
        On invalid ``limit`` argument.
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")

    ssl_ctx = _build_ssl_context(ca_bundle=ca_bundle, insecure=insecure)
    url     = _build_url(limit)
    payload = _fetch_json(url, timeout_s=timeout_s, ssl_ctx=ssl_ctx)
    return _parse_response(payload)


# ---------------------------------------------------------------------------
# Private: HTTP
# ---------------------------------------------------------------------------

def _build_url(limit: int) -> str:
    query = urllib.parse.urlencode({"limit": str(limit), "format": "json"})
    return f"{_BASE_URL}?{query}"


def _build_ssl_context(
    *,
    ca_bundle: Path | None,
    insecure: bool,
) -> ssl.SSLContext:
    """
    Always return an explicit SSLContext — never pass None to urlopen.

    When insecure=True, both check_hostname and verify_mode are disabled.
    ORDER MATTERS: check_hostname must be set False before CERT_NONE
    (Python raises ValueError otherwise).
    """
    if insecure:
        logger.warning(
            "TLS verification DISABLED for Fear & Greed API. "
            "Never use insecure=True in production."
        )
        ctx = ssl.create_default_context()
        ctx.check_hostname = False          # must come first
        ctx.verify_mode    = ssl.CERT_NONE  # must come second
        return ctx

    if ca_bundle is not None:
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cafile=str(ca_bundle.expanduser()))
        return ctx

    return ssl.create_default_context()


def _fetch_json(
    url: str,
    *,
    timeout_s: float,
    ssl_ctx: ssl.SSLContext,
) -> dict:
    """
    Execute GET request and return parsed JSON payload.

    Raises FearGreedAPIError on any HTTP, network, or decode failure.
    """
    req = urllib.request.Request(
        url=url,
        method="GET",
        headers={"Accept": "application/json"},
    )
    logger.debug("GET %s", url)

    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=ssl_ctx) as resp:
            raw_bytes = resp.read()
    except urllib.error.HTTPError as exc:
        raise FearGreedAPIError(
            f"HTTP {exc.code} from Fear & Greed API"
        ) from exc
    except urllib.error.URLError as exc:
        raise FearGreedAPIError(
            f"Network error fetching Fear & Greed: {exc.reason}"
        ) from exc

    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FearGreedAPIError(
            "Non-JSON response from Fear & Greed API"
        ) from exc

    if not isinstance(payload, dict):
        raise FearGreedAPIError(
            f"Unexpected top-level type: expected dict, got {type(payload).__name__}"
        )

    return payload


# ---------------------------------------------------------------------------
# Private: parsing
# ---------------------------------------------------------------------------

def _parse_response(payload: dict) -> FearGreedResponse:
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise FearGreedAPIError("Fear & Greed API returned empty or missing 'data'")

    readings: list[FearGreedReading] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise FearGreedAPIError(
                f"data[{idx}] is not a dict: {type(item).__name__}"
            )
        readings.append(_parse_reading(item, idx=idx))

    return FearGreedResponse(
        source="alternative.me",
        reading=readings[0],           # most recent is always first
        history=tuple(readings),
    )


def _parse_reading(item: dict, *, idx: int) -> FearGreedReading:
    """Parse and validate a single Fear & Greed data item."""

    # Value — must be 0–100
    raw_value = item.get("value")
    value = _parse_int(raw_value, field=f"data[{idx}].value")
    if not (0 <= value <= 100):
        raise FearGreedAPIError(
            f"data[{idx}].value={value} is outside valid range 0–100"
        )

    # Classification
    classification = str(item.get("value_classification") or "").strip()
    if not classification:
        raise FearGreedAPIError(
            f"data[{idx}].value_classification is missing or empty"
        )
    if classification not in _KNOWN_CLASSIFICATIONS:
        logger.warning(
            "Unexpected value_classification %r at data[%d] — "
            "API may have changed. Proceeding with raw value.",
            classification, idx,
        )

    # Timestamp — must be a positive unix timestamp
    raw_ts = item.get("timestamp")
    timestamp_s = _parse_int(raw_ts, field=f"data[{idx}].timestamp")
    if timestamp_s <= 0:
        raise FearGreedAPIError(
            f"data[{idx}].timestamp={timestamp_s!r} is not a valid Unix timestamp"
        )
    timestamp_utc = s_to_utc_iso(timestamp_s)

    # Time until update — optional, soft failure
    time_until_update_s: int | None = None
    raw_tuu = item.get("time_until_update")
    if raw_tuu not in (None, ""):
        try:
            time_until_update_s = int(str(raw_tuu))
        except (ValueError, TypeError):
            logger.debug(
                "Could not parse time_until_update=%r at data[%d] — ignoring.",
                raw_tuu, idx,
            )

    return FearGreedReading(
        value=value,
        value_classification=classification,
        timestamp_utc=timestamp_utc,
        time_until_update_s=time_until_update_s,
    )


def _parse_int(value: object, *, field: str) -> int:
    """
    Convert a scalar to int.

    Only catches ValueError and TypeError — not bare Exception,
    which would mask unexpected errors.
    """
    try:
        return int(str(value))
    except (ValueError, TypeError) as exc:
        raise FearGreedAPIError(
            f"Cannot parse {field}={value!r} as integer"
        ) from exc


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main() -> int:
    resp = fetch_fear_greed()
    r = resp.reading
    print(
        f"value={r.value} classification={r.value_classification} "
        f"timestamp_utc={r.timestamp_utc} source={resp.source}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
