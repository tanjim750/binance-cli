"""
cryptogent.market.news.twitter_twscrape
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
X/Twitter scraper built on twscrape.

Requires at least one Twitter account configured in cryptogent.toml:

  [[twitter.accounts]]
  username       = "your_handle"
  password       = "your_password"
  email          = "your@email.com"
  email_password = "your_email_password"

Installation:
  pip install twscrape

Usage:
  resp = fetch_twitter_search(
      query="bitcoin OR BTC",
      accounts=cfg.twitter_accounts,
      limit=20,
  )
  for post in resp.posts:
      print(post.author_username, post.content_text)

For use inside async contexts (FastAPI, Jupyter):
  resp = await fetch_twitter_search_async(query=..., accounts=...)
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from cryptogent.config.model import TwitterAccountConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------

class TwitterScrapeError(RuntimeError):
    """Raised on configuration errors or twscrape failures."""


# ---------------------------------------------------------------------------
# Public data contracts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TwitterPost:
    """
    A single X/Twitter post returned by twscrape.

    All fields except ``post_id`` may be ``None`` when the API does not
    provide them for a particular tweet.

    Attributes
    ----------
    post_id:
        Tweet ID as string.
    url:
        Direct link to the tweet.
    content_text:
        Full tweet text (rawContent preferred over content/fullText).
    author_username:
        Twitter @handle (without @).
    author_display_name:
        Display name shown on the profile.
    author_id:
        Numeric Twitter user ID as string.
    created_at_utc:
        ISO-8601 UTC timestamp.
    lang:
        BCP-47 language tag (e.g. ``"en"``).
    like_count / retweet_count / reply_count / quote_count / view_count:
        Engagement metrics.
    """
    post_id: str
    url: str | None
    content_text: str | None
    author_username: str | None
    author_display_name: str | None
    author_id: str | None
    created_at_utc: str | None
    lang: str | None
    like_count: int | None
    retweet_count: int | None
    reply_count: int | None
    quote_count: int | None
    view_count: int | None

    @property
    def engagement_total(self) -> int | None:
        """Sum of all engagement signals. ``None`` when all are unavailable."""
        counts    = [self.like_count, self.retweet_count,
                     self.reply_count, self.quote_count]
        available = [c for c in counts if c is not None]
        return sum(available) if available else None

    @property
    def is_high_engagement(self) -> bool:
        """``True`` when total engagement >= 100."""
        total = self.engagement_total
        return total is not None and total >= 100


@dataclass(frozen=True)
class TwitterSearchResponse:
    """
    Response from a Twitter search.

    Attributes
    ----------
    queries:
        The search queries used.
    posts:
        Retrieved posts, deduplicated by post_id across all queries.
    limit:
        The per-query limit used.
    product:
        The search product used (``"Top"``, ``"Latest"``, etc.).
    total_returned:
        Number of posts after deduplication.
    """
    queries: tuple[str, ...]
    posts: tuple[TwitterPost, ...]
    limit: int
    product: str | None
    total_returned: int

    @property
    def has_results(self) -> bool:
        return len(self.posts) > 0

    @property
    def top_posts(self) -> list[TwitterPost]:
        """Posts sorted by total engagement descending."""
        return sorted(
            self.posts,
            key=lambda p: p.engagement_total or 0,
            reverse=True,
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def fetch_twitter_search_async(
    *,
    query: str | Iterable[str],
    accounts: Iterable[TwitterAccountConfig],
    limit: int = 20,
    product: str | None = None,
    db_path: Path | None = None,
    login: bool = True,
) -> TwitterSearchResponse:
    """
    Async search for tweets matching *query* via twscrape.

    Use this inside FastAPI, async scripts, or Jupyter notebooks to avoid
    the ``asyncio.run() called from a running event loop`` error.

    Parameters
    ----------
    query:
        Search query string or list of query strings.
        Multiple queries are executed sequentially and deduplicated.
    accounts:
        Twitter account configs from ``cfg.twitter_accounts``.
    limit:
        Maximum posts to return per query.
    product:
        twscrape search product: ``"Top"`` (default), ``"Latest"``,
        ``"People"``.
    db_path:
        Path to the twscrape SQLite account database.
    login:
        Whether to call ``login_all()`` before searching.
        Set ``False`` when accounts are already logged in.

    Returns
    -------
    TwitterSearchResponse

    Raises
    ------
    TwitterScrapeError
        On missing query, missing accounts, import failure, login failure,
        or search failure.
    """
    queries = _normalise_queries(query)
    if not queries:
        raise TwitterScrapeError("query must be a non-empty string or list of strings")

    accounts_tuple = tuple(accounts)
    if not accounts_tuple:
        return _fetch_public_rss(queries=queries, limit=limit)

    API = _import_twscrape()
    api = API(str(db_path) if db_path else None)

    added = await _add_accounts(api, accounts_tuple)
    if added == 0:
        return _fetch_public_rss(queries=queries, limit=limit)

    if login:
        try:
            await api.pool.login_all()
        except Exception as exc:
            return _fetch_public_rss(queries=queries, limit=limit)
    if not await _pool_has_active_accounts(api.pool):
        return _fetch_public_rss(queries=queries, limit=limit)

    posts: list[TwitterPost] = []
    seen:  set[str]          = set()

    for q in queries:
        logger.debug("twitter_twscrape: search q=%r limit=%d", q, limit)
        try:
            kv = {"product": product} if product else {}
            # Async iteration — avoids loading all results into memory at once
            async for tweet in api.search(q, limit=limit, kv=kv):
                post = _tweet_to_post(tweet)
                if post.post_id not in seen:
                    seen.add(post.post_id)
                    posts.append(post)
        except Exception as exc:
            raise TwitterScrapeError(
                f"twscrape search failed for q={q!r}: {exc}"
            ) from exc

    logger.debug("twitter_twscrape: returned %d posts", len(posts))
    return TwitterSearchResponse(
        queries=queries,
        posts=tuple(posts),
        limit=limit,
        product=product,
        total_returned=len(posts),
    )


def fetch_twitter_search(
    *,
    query: str | Iterable[str],
    accounts: Iterable[TwitterAccountConfig],
    limit: int = 20,
    product: str | None = None,
    db_path: Path | None = None,
    login: bool = True,
) -> TwitterSearchResponse:
    """
    Synchronous wrapper around ``fetch_twitter_search_async``.

    Raises ``RuntimeError`` when called from inside a running event loop
    (e.g. FastAPI, Jupyter).  Use ``fetch_twitter_search_async`` instead
    in those contexts.
    """
    try:
        return asyncio.run(
            fetch_twitter_search_async(
                query=query,
                accounts=accounts,
                limit=limit,
                product=product,
                db_path=db_path,
                login=login,
            )
        )
    except RuntimeError as exc:
        if "cannot be called from a running event loop" in str(exc):
            raise RuntimeError(
                "fetch_twitter_search() cannot be called from a running event loop. "
                "Use 'await fetch_twitter_search_async(...)' instead."
            ) from exc
        raise


# ---------------------------------------------------------------------------
# Private: twscrape helpers
# ---------------------------------------------------------------------------

def _import_twscrape():
    """Import twscrape.API or raise a clear install instruction."""
    try:
        from twscrape import API
        return API
    except ImportError as exc:
        raise TwitterScrapeError(
            "twscrape is not installed. "
            "Install with: pip install twscrape"
        ) from exc


async def _add_accounts(
    api: object,
    accounts: tuple[TwitterAccountConfig, ...],
) -> int:
    """
    Register accounts with the twscrape pool.

    Validation:
      - Skips accounts missing username or password (logs warning).
      - Raises TwitterScrapeError for missing email / email_password.
      - Duplicate accounts already in the DB are silently skipped.
      - Older twscrape versions that don't accept user_agent kwarg are
        handled with a logged retry without that argument.
    """
    added = 0
    for acc in accounts:
        if not acc.username or not acc.password:
            logger.warning(
                "twitter_twscrape: skipping account with missing "
                "username or password"
            )
            continue
        if not acc.email:
            logger.warning(
                "twitter_twscrape: skipping @%s (missing email)",
                acc.username,
            )
            continue
        if not acc.email_password:
            logger.warning(
                "twitter_twscrape: skipping @%s (missing email_password)",
                acc.username,
            )
            continue

        kwargs: dict[str, object] = {}
        if acc.user_agent:
            kwargs["user_agent"] = acc.user_agent

        try:
            await api.pool.add_account(  # type: ignore[attr-defined]
                acc.username,
                acc.password,
                acc.email,
                acc.email_password,
                **kwargs,
            )
            logger.debug("twitter_twscrape: registered @%s", acc.username)
            added += 1
        except TypeError:
            # Older twscrape versions don't accept user_agent kwarg
            if kwargs:
                logger.debug(
                    "twitter_twscrape: user_agent kwarg rejected for "
                    "@%s — retrying without it", acc.username,
                )
            try:
                await api.pool.add_account(  # type: ignore[attr-defined]
                    acc.username,
                    acc.password,
                    acc.email,
                    acc.email_password,
                )
                logger.debug(
                    "twitter_twscrape: registered @%s (no user_agent)",
                    acc.username,
                )
                added += 1
            except Exception as exc:
                logger.debug(
                    "twitter_twscrape: could not register @%s: %s",
                    acc.username, exc,
                )
        except Exception as exc:
            # Account already in DB — not an error
            logger.debug(
                "twitter_twscrape: skipping @%s (already in pool?): %s",
                acc.username, exc,
            )
            added += 1
    return added


def _tweet_to_post(tweet: object) -> TwitterPost:
    """
    Convert a twscrape Tweet object to a typed TwitterPost.

    Does NOT call tweet.dict() / model_dump() / __dict__ — those can
    return enormous nested structures.  All fields are extracted
    explicitly via getattr.
    """
    user = getattr(tweet, "user", None)

    author_username = (
        getattr(user, "username", None)
        or getattr(user, "userName", None)
    ) if user else None

    author_display = (
        getattr(user, "displayname", None)
        or getattr(user, "name", None)
    ) if user else None

    author_id_raw = getattr(user, "id", None) if user else None
    author_id     = str(author_id_raw) if author_id_raw is not None else None

    tweet_id = str(getattr(tweet, "id", "") or "")

    url = getattr(tweet, "url", None)
    if not url and tweet_id and author_username:
        url = f"https://x.com/{author_username}/status/{tweet_id}"

    content = (
        getattr(tweet, "rawContent", None)
        or getattr(tweet, "content",   None)
        or getattr(tweet, "fullText",  None)
        or getattr(tweet, "text",      None)
    )

    return TwitterPost(
        post_id=tweet_id,
        url=url,
        content_text=str(content).strip() if content else None,
        author_username=author_username,
        author_display_name=author_display,
        author_id=author_id,
        created_at_utc=_to_utc_iso(
            getattr(tweet, "date",      None)
            or getattr(tweet, "createdAt", None)
        ),
        lang=getattr(tweet, "lang", None),
        like_count=_as_int(getattr(tweet, "likeCount",    None)),
        retweet_count=_as_int(getattr(tweet, "retweetCount", None)),
        reply_count=_as_int(getattr(tweet, "replyCount",   None)),
        quote_count=_as_int(getattr(tweet, "quoteCount",   None)),
        view_count=_as_int(getattr(tweet, "viewCount",    None)),
    )


# ---------------------------------------------------------------------------
# Private: utilities
# ---------------------------------------------------------------------------

def _normalise_queries(query: str | Iterable[str]) -> tuple[str, ...]:
    if isinstance(query, str):
        q = query.strip()
        return (q,) if q else ()
    result: list[str] = []
    for item in query:
        q = str(item).strip()
        if q:
            result.append(q)
    return tuple(result)


def _to_utc_iso(value: object) -> str | None:
    """
    Convert a datetime or string timestamp to UTC ISO-8601.

    Handles:
      - datetime objects (with or without tzinfo)
      - RFC 2822 strings (standard Twitter format)
      - ISO-8601 strings (including 'Z' suffix on Python < 3.11)
    """
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).replace(microsecond=0).isoformat()
    if isinstance(value, str):
        # Try RFC 2822 first (most common in Twitter responses)
        try:
            dt = parsedate_to_datetime(value)
            return dt.astimezone(UTC).replace(microsecond=0).isoformat()
        except Exception:
            pass
        # Try ISO-8601 — handle 'Z' suffix for Python < 3.11
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC).replace(microsecond=0).isoformat()
        except Exception:
            return value   # return as-is when unparseable
    return None


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _pool_has_active_accounts(pool: object) -> bool:
    for name in ("get_stats", "stats", "getStats"):
        attr = getattr(pool, name, None)
        if attr is None:
            continue
        try:
            stats = attr() if callable(attr) else attr
            if asyncio.iscoroutine(stats):
                stats = await stats
        except Exception:
            continue
        if isinstance(stats, dict):
            for key in ("active", "active_accounts", "logged", "logged_in", "ok", "available"):
                if key in stats:
                    try:
                        return int(stats[key]) > 0
                    except Exception:
                        pass
        for key in ("active", "active_accounts", "logged", "logged_in", "ok", "available"):
            if hasattr(stats, key):
                try:
                    return int(getattr(stats, key)) > 0
                except Exception:
                    pass
    return False


def _fetch_public_rss(*, queries: tuple[str, ...], limit: int) -> TwitterSearchResponse:
    posts: list[TwitterPost] = []
    seen: set[str] = set()
    for q in queries:
        for post in _fetch_nitter_rss(q, limit=limit):
            if post.post_id not in seen:
                seen.add(post.post_id)
                posts.append(post)
    return TwitterSearchResponse(
        queries=queries,
        posts=tuple(posts),
        limit=limit,
        product="public_rss",
        total_returned=len(posts),
    )


def _fetch_nitter_rss(query: str, *, limit: int) -> list[TwitterPost]:
    bases = (
        "https://nitter.net/search/rss",
        "https://nitter.privacydev.net/search/rss",
        "https://nitter.poast.org/search/rss",
        "https://nitter.fdn.fr/search/rss",
    )
    raw = b""
    for base in bases:
        url = f"{base}?f=tweets&q={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={"User-Agent": "cryptogent/1.0 (twscrape)"})
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                raw = resp.read()
            if raw:
                break
        except Exception:
            continue
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    items = root.findall("./channel/item")
    posts: list[TwitterPost] = []
    for item in items[: max(0, int(limit))]:
        link = (item.findtext("link") or "").strip()
        title = (item.findtext("title") or "").strip()
        desc = (item.findtext("description") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        created_at_utc = None
        if pub:
            try:
                created_at_utc = parsedate_to_datetime(pub).astimezone(UTC).replace(microsecond=0).isoformat()
            except Exception:
                created_at_utc = None
        username = None
        post_id = ""
        if link:
            parts = link.rstrip("/").split("/")
            if len(parts) >= 2:
                post_id = parts[-1]
                username = parts[-3] if len(parts) >= 3 else None
        content = desc or title
        posts.append(
            TwitterPost(
                post_id=post_id or link or title,
                url=link or None,
                content_text=content or None,
                author_username=username,
                author_display_name=None,
                author_id=None,
                created_at_utc=created_at_utc,
                lang=None,
                like_count=None,
                retweet_count=None,
                reply_count=None,
                quote_count=None,
                view_count=None,
            )
        )
    return posts


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main() -> int:
    import sys

    from cryptogent.config.io import ConfigPaths, ensure_default_config, load_config

    paths       = ConfigPaths.from_cli(config_path=None, db_path=None)
    config_path = ensure_default_config(paths.config_path)
    cfg         = load_config(config_path)

    query = (
        sys.argv[1].strip()
        if len(sys.argv) > 1 and sys.argv[1].strip()
        else "bitcoin"
    )

    resp = fetch_twitter_search(
        query=query,
        accounts=cfg.twitter_accounts,
        db_path=cfg.twitter_db_path,
    )

    print(
        f"returned={resp.total_returned}  "
        f"query={query!r}  "
        f"product={resp.product}"
    )
    for post in resp.posts[:10]:
        eng = (
            f" [👍{post.like_count} 🔁{post.retweet_count}]"
            if post.like_count is not None else ""
        )
        print(
            f"\n  {post.created_at_utc or '?'} "
            f"@{post.author_username or '?'}{eng}"
        )
        if post.content_text:
            preview = post.content_text[:280].rstrip()
            suffix  = "…" if len(post.content_text) > 280 else ""
            print(f"  {preview}{suffix}")
        if post.url:
            print(f"  {post.url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
