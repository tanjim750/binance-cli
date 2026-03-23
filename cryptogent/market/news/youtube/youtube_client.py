"""
cryptogent.market.news.youtube_client
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
YouTube Data API v3 client helpers.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any


class YouTubeClientError(RuntimeError):
    pass


class YouTubeQuotaError(YouTubeClientError):
    pass


class YouTubeRequestError(YouTubeClientError):
    pass


@dataclass(frozen=True)
class YouTubeErrorDetails:
    status: int | None
    reason: str | None
    message: str | None


def build_youtube_client(*, api_key: str | None) -> Any:
    if not api_key or not str(api_key).strip():
        raise YouTubeClientError("Missing YouTube API key in config.")
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise YouTubeClientError(
            "google-api-python-client is not installed. Install with: pip install google-api-python-client"
        ) from exc
    return build("youtube", "v3", developerKey=str(api_key).strip(), cache_discovery=False)


def execute_request(call: Any, *, retries: int = 2, backoff_s: float = 1.0) -> dict:
    try:
        return call.execute()
    except Exception as exc:  # googleapiclient.errors.HttpError
        details = _extract_error_details(exc)
        if _is_quota_error(details):
            raise YouTubeQuotaError(details.message or "YouTube quota exceeded") from exc
        if _is_retryable(details) and retries > 0:
            time.sleep(backoff_s)
            return execute_request(call, retries=retries - 1, backoff_s=backoff_s * 2)
        raise YouTubeRequestError(details.message or "YouTube request failed") from exc


def _extract_error_details(exc: Exception) -> YouTubeErrorDetails:
    status = getattr(exc, "status_code", None)
    reason = None
    message = str(exc)
    content = getattr(exc, "content", None)
    if content:
        try:
            payload = json.loads(content.decode("utf-8"))
            error = payload.get("error", {})
            status = error.get("code", status)
            message = error.get("message", message)
            if error.get("errors"):
                reason = error["errors"][0].get("reason")
        except Exception:
            pass
    return YouTubeErrorDetails(status=status, reason=reason, message=message)


def _is_quota_error(details: YouTubeErrorDetails) -> bool:
    if details.status in (403, 429) and details.reason in {
        "quotaExceeded",
        "userRateLimitExceeded",
        "rateLimitExceeded",
    }:
        return True
    return False


def _is_retryable(details: YouTubeErrorDetails) -> bool:
    return details.status in (500, 503)
