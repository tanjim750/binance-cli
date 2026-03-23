"""
cryptogent.market.news.reddit_fetcher
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Reddit fetcher that prefers the official API and falls back to RSS.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:  # Allows running as a script without package context
    from .reddit_api import RedditAPIError, RedditPost, fetch_reddit_posts
    from .reddit_rss import RedditRSSError, RedditRSSPost, fetch_reddit_rss
except ImportError:  # pragma: no cover - fallback for direct execution
    from cryptogent.market.news.reddit.reddit_api import RedditAPIError, RedditPost, fetch_reddit_posts
    from cryptogent.market.news.reddit.reddit_rss import RedditRSSError, RedditRSSPost, fetch_reddit_rss


@dataclass(frozen=True)
class RedditUnifiedPost:
    id: str
    title: str
    url: str
    permalink: str | None
    author: str | None
    subreddit: str | None
    published_at_utc: str | None
    content_text: str | None
    source: str
    raw: dict


@dataclass(frozen=True)
class RedditUnifiedResponse:
    request_kind: str
    request_params: dict
    posts: tuple[RedditUnifiedPost, ...]
    source: str


def fetch_reddit_with_fallback(
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
) -> RedditUnifiedResponse:
    # Try official API first if credentials are present
    if client_id:
        try:
            api_resp = fetch_reddit_posts(
                subreddit=subreddit,
                sort=sort,
                limit=limit,
                client_id=client_id,
                client_secret=client_secret,
                device_id=device_id,
                user_agent=user_agent,
                timeout_s=timeout_s,
                ca_bundle=ca_bundle,
                insecure=insecure,
            )
            posts = [_from_api(p) for p in api_resp.posts]
            return RedditUnifiedResponse(
                request_kind=api_resp.request_kind,
                request_params=api_resp.request_params,
                posts=tuple(posts),
                source="api",
            )
        except RedditAPIError:
            pass

    # Fall back to RSS
    rss_resp = fetch_reddit_rss(
        subreddit=subreddit,
        sort=sort,
        limit=limit,
        user_agent=user_agent,
        timeout_s=timeout_s,
        ca_bundle=ca_bundle,
        insecure=insecure,
    )
    posts = [_from_rss(p) for p in rss_resp.posts]
    return RedditUnifiedResponse(
        request_kind=rss_resp.request_kind,
        request_params=rss_resp.request_params,
        posts=tuple(posts),
        source="rss",
    )


def _from_api(post: RedditPost) -> RedditUnifiedPost:
    return RedditUnifiedPost(
        id=post.id,
        title=post.title,
        url=post.url,
        permalink=post.permalink,
        author=post.author,
        subreddit=post.subreddit,
        published_at_utc=post.created_at_utc,
        content_text=post.selftext,
        source="api",
        raw=post.raw,
    )


def _from_rss(post: RedditRSSPost) -> RedditUnifiedPost:
    return RedditUnifiedPost(
        id=post.id,
        title=post.title,
        url=post.url,
        permalink=None,
        author=post.author,
        subreddit=post.subreddit,
        published_at_utc=post.published_at_utc,
        content_text=post.content_text,
        source="rss",
        raw=post.raw,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main() -> int:
    import sys

    from cryptogent.config.io import ConfigPaths, ensure_default_config, load_config

    paths = ConfigPaths.from_cli(config_path=None, db_path=None)
    config_path = ensure_default_config(paths.config_path)
    cfg = load_config(config_path)

    subreddit = sys.argv[1].strip() if len(sys.argv) > 1 and sys.argv[1].strip() else "bitcoin"
    resp = fetch_reddit_with_fallback(
        subreddit=subreddit,
        client_id=cfg.reddit_client_id,
        client_secret=cfg.reddit_client_secret,
        device_id=cfg.reddit_device_id,
        user_agent=cfg.reddit_user_agent,
    )
    print(f"source={resp.source} returned={len(resp.posts)} subreddit={subreddit}")
    for p in resp.posts[:10]:
        print(f"  {p.published_at_utc or '?'} [{p.subreddit}] {p.title}")
        print(f"    {p.url}")
        if p.content_text:
            preview = p.content_text[:300].rstrip()
            suffix = "…" if len(p.content_text) > 300 else ""
            print(f"    body: {preview}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
