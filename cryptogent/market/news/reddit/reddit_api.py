"""
cryptogent.market.news.reddit_api
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Reddit API client (OAuth, app-only) for fetching subreddit posts.

Uses OAuth2 application-only grants:
  - client_credentials (confidential clients with secret)
  - installed_client (no secret; requires device_id)
"""
from __future__ import annotations

import base64
import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from cryptogent.util.time import s_to_utc_iso

logger = logging.getLogger(__name__)

_OAUTH_URL = "https://www.reddit.com/api/v1/access_token"
_API_BASE = "https://oauth.reddit.com"

_DEFAULT_USER_AGENT = "cryptogent/1.0 (reddit client)"


class RedditAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class RedditPost:
    id: str
    title: str
    url: str
    permalink: str
    author: str | None
    subreddit: str | None
    created_at_utc: str | None
    score: int | None
    num_comments: int | None
    selftext: str | None
    is_self: bool
    source: str
    raw: dict


@dataclass(frozen=True)
class RedditResponse:
    request_kind: str
    request_params: dict
    posts: tuple[RedditPost, ...]


def fetch_reddit_posts(
    *,
    subreddit: str,
    sort: str = "new",
    limit: int = 20,
    client_id: str | None = None,
    client_secret: str | None = None,
    device_id: str | None = None,
    user_agent: str | None = None,
    timeout_s: float = 10.0,
    ca_bundle: Path | None = None,
    insecure: bool = False,
) -> RedditResponse:
    subreddit = str(subreddit).strip().lstrip("r/").strip("/")
    if not subreddit:
        raise RedditAPIError("subreddit is empty")
    sort = str(sort or "new").strip().lower()
    if sort not in ("new", "hot", "top", "rising"):
        raise RedditAPIError(f"Invalid sort={sort!r}")

    token = _get_access_token(
        client_id=client_id,
        client_secret=client_secret,
        device_id=device_id,
        user_agent=user_agent,
        timeout_s=timeout_s,
        ca_bundle=ca_bundle,
        insecure=insecure,
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": user_agent or _DEFAULT_USER_AGENT,
        "Accept": "application/json",
    }
    params = {"limit": str(int(limit))}
    url = f"{_API_BASE}/r/{urllib.parse.quote(subreddit)}/{sort}?{urllib.parse.urlencode(params)}"
    payload = _request_json(
        url=url,
        headers=headers,
        timeout_s=timeout_s,
        ca_bundle=ca_bundle,
        insecure=insecure,
    )
    posts = _parse_listing(payload)
    return RedditResponse(
        request_kind="reddit_api",
        request_params={"subreddit": subreddit, "sort": sort, "limit": int(limit)},
        posts=tuple(posts),
    )


def _get_access_token(
    *,
    client_id: str | None,
    client_secret: str | None,
    device_id: str | None,
    user_agent: str | None,
    timeout_s: float,
    ca_bundle: Path | None,
    insecure: bool,
) -> str:
    cid = (client_id or "").strip()
    csec = (client_secret or "").strip()
    if not cid:
        raise RedditAPIError("Missing client_id for Reddit API")

    if csec:
        grant = "client_credentials"
        body = urllib.parse.urlencode({"grant_type": grant}).encode("utf-8")
    else:
        grant = "https://oauth.reddit.com/grants/installed_client"
        dev = (device_id or "DO_NOT_TRACK_THIS_DEVICE").strip()
        body = urllib.parse.urlencode({"grant_type": grant, "device_id": dev}).encode("utf-8")

    auth = base64.b64encode(f"{cid}:{csec}".encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": f"Basic {auth}",
        "User-Agent": user_agent or _DEFAULT_USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    payload = _request_json(
        url=_OAUTH_URL,
        headers=headers,
        data=body,
        timeout_s=timeout_s,
        ca_bundle=ca_bundle,
        insecure=insecure,
    )
    token = payload.get("access_token")
    if not token:
        raise RedditAPIError(f"Token response missing access_token: {payload}")
    return str(token)


def _request_json(
    *,
    url: str,
    headers: dict[str, str],
    data: bytes | None = None,
    timeout_s: float,
    ca_bundle: Path | None,
    insecure: bool,
) -> dict:
    ssl_ctx = _build_ssl_context(ca_bundle=ca_bundle, insecure=insecure)
    req = urllib.request.Request(url=url, method="POST" if data is not None else "GET", headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=ssl_ctx) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        raise RedditAPIError(f"HTTP {exc.code} from Reddit API: {body}") from exc
    except urllib.error.URLError as exc:
        raise RedditAPIError(f"Network error from Reddit API: {exc.reason}") from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RedditAPIError("Non-JSON response from Reddit API") from exc
    if not isinstance(payload, dict):
        raise RedditAPIError("Unexpected response type from Reddit API")
    return payload


def _parse_listing(payload: dict) -> list[RedditPost]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RedditAPIError("Listing missing data")
    children = data.get("children")
    if not isinstance(children, list):
        raise RedditAPIError("Listing missing children")

    posts: list[RedditPost] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        cdata = child.get("data")
        if not isinstance(cdata, dict):
            continue
        post_id = str(cdata.get("id") or "").strip()
        title = str(cdata.get("title") or "").strip()
        url = str(cdata.get("url") or "").strip()
        permalink = str(cdata.get("permalink") or "").strip()
        if not (post_id and title and url and permalink):
            continue
        created_utc = cdata.get("created_utc")
        created_at_utc = None
        try:
            if created_utc is not None:
                created_at_utc = s_to_utc_iso(int(float(created_utc)))
        except Exception:
            created_at_utc = None
        posts.append(
            RedditPost(
                id=post_id,
                title=title,
                url=url,
                permalink=f"https://www.reddit.com{permalink}",
                author=str(cdata.get("author") or "") or None,
                subreddit=str(cdata.get("subreddit") or "") or None,
                created_at_utc=created_at_utc,
                score=int(cdata.get("score")) if cdata.get("score") is not None else None,
                num_comments=int(cdata.get("num_comments")) if cdata.get("num_comments") is not None else None,
                selftext=str(cdata.get("selftext") or "") or None,
                is_self=bool(cdata.get("is_self")),
                source="api",
                raw=cdata,
            )
        )
    return posts


def _build_ssl_context(*, ca_bundle: Path | None, insecure: bool) -> ssl.SSLContext | None:
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if ca_bundle is not None:
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cafile=str(ca_bundle.expanduser()))
        return ctx
    return None


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main() -> int:
    import os
    import sys

    subreddit = sys.argv[1].strip() if len(sys.argv) > 1 and sys.argv[1].strip() else "bitcoin"
    client_id = os.environ.get("REDDIT_CLIENT_ID")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET")
    device_id = os.environ.get("REDDIT_DEVICE_ID")

    resp = fetch_reddit_posts(
        subreddit=subreddit,
        client_id=client_id,
        client_secret=client_secret,
        device_id=device_id,
    )
    print(f"returned={len(resp.posts)} subreddit={subreddit}")
    for p in resp.posts[:10]:
        print(f"  {p.created_at_utc or '?'} [{p.subreddit}] {p.title}")
        print(f"    {p.url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
