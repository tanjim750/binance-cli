"""
cryptogent.sentiment.twitter
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
X/Twitter search for crypto sentiment.

Primary path  — twscrape (requires Twitter accounts in cryptogent.toml)
Fallback path — Nitter RSS (public, no accounts needed, limited results)

twscrape installation:
  pip install twscrape

Account setup in cryptogent.toml:
  [[twitter.accounts]]
  username       = "your_handle"
  password       = "your_password"
  email          = "your@email.com"
  email_password = "your_email_password"

Usage:
  from cryptogent.sentiment.twitter import fetch_twitter_search

  resp = fetch_twitter_search(
      query="bitcoin OR BTC",
      accounts=cfg.twitter_accounts,
      limit=20,
  )
  for post in resp.posts:
      print(post.author_username, post.content_text)

Without accounts (Nitter RSS fallback):
  resp = fetch_twitter_search(query="bitcoin", limit=10)
  # resp.source == "nitter_rss"  (limited fields, no engagement counts)
"""
from __future__ import annotations

import asyncio
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Nitter RSS fallback instances
# These are public Nitter mirrors — configure additional ones if these go down
# ---------------------------------------------------------------------------
_NITTER_BASES: tuple[str, ...] = (
    "https://nitter.net/search/rss",
    "https://nitter.privacydev.net/search/rss",
    "https://nitter.poast.org/search/rss",
    "https://nitter.fdn.fr/search/rss",
)

_DEFAULT_UA = "Mozilla/5.0 (compatible; cryptogent/1.0)"

# Source label constants
SOURCE_TWSCRAPE = "twscrape"
SOURCE_NITTER   = "nitter_rss"


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------

class TwitterScrapeError(RuntimeError):
    """Raised on configuration errors or unrecoverable scrape failures."""


# ---------------------------------------------------------------------------
# Public data contracts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TwitterPost:
    """
    A single tweet / X post.

    Fields marked 'None from Nitter' are unavailable via the RSS fallback.

    Attributes
    ----------
    post_id:
        Tweet ID as string.
    url:
        Direct link to the tweet.
    content_text:
        Full tweet text.
    author_username:
        Twitter handle without @.
    author_display_name:
        Display name.  None from Nitter RSS.
    created_at_utc:
        ISO-8601 UTC timestamp.
    lang:
        BCP-47 language tag.  None from Nitter RSS.
    like_count / retweet_count / reply_count / quote_count / view_count:
        Engagement metrics.  All None from Nitter RSS.
    source:
        "twscrape" or "nitter_rss".
    """
    post_id: str
    url: str | None
    content_text: str | None
    author_username: str | None
    author_display_name: str | None
    created_at_utc: str | None
    lang: str | None
    like_count: int | None
    retweet_count: int | None
    reply_count: int | None
    quote_count: int | None
    view_count: int | None
    source: str

    @property
    def engagement_total(self) -> int | None:
        """Sum of all engagement signals. None when all unavailable."""
        counts    = [self.like_count, self.retweet_count,
                     self.reply_count, self.quote_count]
        available = [c for c in counts if c is not None]
        return sum(available) if available else None

    @property
    def is_high_engagement(self) -> bool:
        """True when total engagement >= 100."""
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
        Retrieved posts, deduplicated by post_id.
    source:
        "twscrape" or "nitter_rss".
    limit:
        The per-query limit used.
    total_returned:
        Total posts after deduplication.
    """
    queries: tuple[str, ...]
    posts: tuple[TwitterPost, ...]
    source: str
    limit: int
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

def fetch_twitter_search(
    *,
    query: str | Iterable[str],
    accounts: Iterable[object] | None = None,
    limit: int = 20,
    product: str | None = None,
    db_path: Path | None = None,
    login: bool = True,
    nitter_bases: tuple[str, ...] = _NITTER_BASES,
    user_agent: str | None = None,
    timeout_s: float = 10.0,
) -> TwitterSearchResponse:
    """
    Search Twitter/X for posts matching *query*.

    Tries twscrape first when accounts are configured.
    Falls back to Nitter RSS when:
      - No accounts are provided
      - twscrape is not installed
      - All accounts fail to login

    Parameters
    ----------
    query:
        Search query string or list of query strings.
        Multiple queries are executed and deduplicated.
    accounts:
        Twitter account configs from cfg.twitter_accounts.
        Optional — Nitter RSS is used when omitted.
    limit:
        Maximum posts to return per query.
    product:
        twscrape search product: "Top", "Latest", "People".
    db_path:
        Path to twscrape account database.
    login:
        Whether to call login_all() before searching.
        Set False when accounts are already logged in.
    nitter_bases:
        Nitter RSS base URLs to try for fallback.
    user_agent:
        Custom User-Agent for Nitter RSS requests.
    timeout_s:
        HTTP timeout for Nitter RSS requests.

    Returns
    -------
    TwitterSearchResponse
    """
    queries      = _normalise_queries(query)
    if not queries:
        raise TwitterScrapeError("query must be a non-empty string or list of strings")

    accounts_list = list(accounts or [])

    # Try twscrape when accounts are available
    if accounts_list:
        try:
            _import_twscrape()
            return asyncio.run(
                _fetch_twscrape(
                    queries=queries,
                    accounts=accounts_list,
                    limit=limit,
                    product=product,
                    db_path=db_path,
                    login=login,
                )
            )
        except TwitterScrapeError as exc:
            logger.warning(
                "twitter: twscrape failed (%s) — falling back to Nitter RSS", exc
            )
        except RuntimeError as exc:
            if "cannot be called from a running event loop" in str(exc):
                logger.warning(
                    "twitter: asyncio.run() called from a running event loop. "
                    "Use fetch_twitter_search_async() in async contexts."
                )
            else:
                logger.warning(
                    "twitter: twscrape runtime error (%s) — falling back to Nitter RSS", exc
                )

    # Nitter RSS fallback
    logger.debug("twitter: using Nitter RSS fallback for queries=%s", queries)
    return _fetch_nitter_rss(
        queries=queries,
        limit=limit,
        nitter_bases=nitter_bases,
        user_agent=user_agent or _DEFAULT_UA,
        timeout_s=timeout_s,
    )


async def fetch_twitter_search_async(
    *,
    query: str | Iterable[str],
    accounts: Iterable[object],
    limit: int = 20,
    product: str | None = None,
    db_path: Path | None = None,
    login: bool = True,
) -> TwitterSearchResponse:
    """
    Async version for use inside FastAPI, async scripts, or Jupyter notebooks.

    Avoids the 'asyncio.run() called from a running event loop' error
    that fetch_twitter_search() produces in async contexts.
    """
    queries = _normalise_queries(query)
    if not queries:
        raise TwitterScrapeError("query must be non-empty")
    _import_twscrape()
    return await _fetch_twscrape(
        queries=queries,
        accounts=list(accounts),
        limit=limit,
        product=product,
        db_path=db_path,
        login=login,
    )


# ---------------------------------------------------------------------------
# Private: twscrape
# ---------------------------------------------------------------------------

def _import_twscrape() -> None:
    """Import twscrape or raise a clear install instruction."""
    try:
        import twscrape  # noqa: F401
    except ImportError as exc:
        raise TwitterScrapeError(
            "twscrape is not installed. Install with: pip install twscrape"
        ) from exc


async def _fetch_twscrape(
    *,
    queries: tuple[str, ...],
    accounts: list[object],
    limit: int,
    product: str | None,
    db_path: Path | None,
    login: bool,
) -> TwitterSearchResponse:
    from twscrape import API

    api = API(str(db_path) if db_path else None)
    await _add_accounts(api, accounts)

    if login:
        try:
            await api.pool.login_all()
        except Exception as exc:
            raise TwitterScrapeError(f"Twitter login failed: {exc}") from exc

    posts: list[TwitterPost] = []
    seen:  set[str]          = set()

    for q in queries:
        logger.debug("twitter: twscrape search q=%r limit=%d", q, limit)
        try:
            kv = {"product": product} if product else {}
            async for tweet in api.search(q, limit=limit, kv=kv):
                post = _tweet_to_post(tweet)
                if post.post_id not in seen:
                    seen.add(post.post_id)
                    posts.append(post)
        except Exception as exc:
            raise TwitterScrapeError(
                f"twscrape search failed for q={q!r}: {exc}"
            ) from exc

    logger.debug("twitter: twscrape returned %d posts", len(posts))
    return TwitterSearchResponse(
        queries=queries,
        posts=tuple(posts),
        source=SOURCE_TWSCRAPE,
        limit=limit,
        total_returned=len(posts),
    )


async def _add_accounts(api: object, accounts: list[object]) -> None:
    """
    Register accounts with the twscrape pool.

    Accounts already in the pool DB raise on duplicate add — this is
    not an error and is caught silently with a debug log.
    """
    for acc in accounts:
        username       = getattr(acc, "username",       None)
        password       = getattr(acc, "password",       None)
        email          = getattr(acc, "email",          None)
        email_password = getattr(acc, "email_password", None)
        user_agent_acc = getattr(acc, "user_agent",     None)

        if not username or not password:
            logger.warning("twitter: skipping account with missing username/password")
            continue
        if not email:
            raise TwitterScrapeError(
                f"Twitter account '{username}' missing 'email' — required by twscrape."
            )
        if not email_password:
            raise TwitterScrapeError(
                f"Twitter account '{username}' missing 'email_password' — required by twscrape."
            )

        kwargs: dict[str, object] = {}
        if user_agent_acc:
            kwargs["user_agent"] = user_agent_acc

        try:
            await api.pool.add_account(  # type: ignore[attr-defined]
                username, password, email, email_password, **kwargs
            )
            logger.debug("twitter: registered @%s in pool", username)
        except TypeError:
            # Older twscrape versions don't accept user_agent kwarg — retry without
            if kwargs:
                logger.debug(
                    "twitter: user_agent not supported by this twscrape version "
                    "for @%s — retrying without it", username,
                )
            try:
                await api.pool.add_account(  # type: ignore[attr-defined]
                    username, password, email, email_password
                )
            except Exception as exc:
                logger.debug("twitter: could not register @%s: %s", username, exc)
        except Exception as exc:
            # Duplicate account in DB — not an error
            logger.debug("twitter: skipping @%s (already in pool?): %s", username, exc)


def _tweet_to_post(tweet: object) -> TwitterPost:
    """Convert a twscrape Tweet object to a typed TwitterPost."""
    user = getattr(tweet, "user", None)

    author_username = (
        getattr(user, "username", None) or getattr(user, "userName", None)
    ) if user else None

    author_display = (
        getattr(user, "displayname", None) or getattr(user, "name", None)
    ) if user else None

    tweet_id = str(getattr(tweet, "id", "") or "")

    url = getattr(tweet, "url", None)
    if not url and tweet_id and author_username:
        url = f"https://x.com/{author_username}/status/{tweet_id}"

    content = (
        getattr(tweet, "rawContent", None)
        or getattr(tweet, "content",    None)
        or getattr(tweet, "fullText",   None)
        or getattr(tweet, "text",       None)
    )

    return TwitterPost(
        post_id=tweet_id,
        url=url,
        content_text=str(content).strip() if content else None,
        author_username=author_username,
        author_display_name=author_display,
        created_at_utc=_to_utc_iso(
            getattr(tweet, "date", None) or getattr(tweet, "createdAt", None)
        ),
        lang=getattr(tweet, "lang", None),
        like_count=_as_int(getattr(tweet, "likeCount",    None)),
        retweet_count=_as_int(getattr(tweet, "retweetCount", None)),
        reply_count=_as_int(getattr(tweet, "replyCount",   None)),
        quote_count=_as_int(getattr(tweet, "quoteCount",   None)),
        view_count=_as_int(getattr(tweet, "viewCount",    None)),
        source=SOURCE_TWSCRAPE,
    )


# ---------------------------------------------------------------------------
# Private: Nitter RSS fallback
# ---------------------------------------------------------------------------

def _fetch_nitter_rss(
    *,
    queries: tuple[str, ...],
    limit: int,
    nitter_bases: tuple[str, ...],
    user_agent: str,
    timeout_s: float,
) -> TwitterSearchResponse:
    ssl_ctx = ssl.create_default_context()
    posts:    list[TwitterPost] = []
    seen:     set[str]          = set()

    for q in queries:
        items = _query_nitter(
            query=q,
            limit=limit,
            nitter_bases=nitter_bases,
            user_agent=user_agent,
            timeout_s=timeout_s,
            ssl_ctx=ssl_ctx,
        )
        for post in items:
            if post.post_id not in seen:
                seen.add(post.post_id)
                posts.append(post)

    logger.debug("twitter: Nitter RSS returned %d posts", len(posts))
    return TwitterSearchResponse(
        queries=queries,
        posts=tuple(posts),
        source=SOURCE_NITTER,
        limit=limit,
        total_returned=len(posts),
    )


def _query_nitter(
    *,
    query: str,
    limit: int,
    nitter_bases: tuple[str, ...],
    user_agent: str,
    timeout_s: float,
    ssl_ctx: ssl.SSLContext,
) -> list[TwitterPost]:
    """Try each Nitter base URL until one responds."""
    raw_xml:    bytes              = b""
    last_error: Exception | None   = None

    for base in nitter_bases:
        url = f"{base}?f=tweets&q={urllib.parse.quote(query)}"
        req = urllib.request.Request(
            url,
            headers={
                "Accept":     "application/rss+xml,*/*",
                "User-Agent": user_agent,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s, context=ssl_ctx) as resp:
                raw_xml = resp.read()
            if raw_xml:
                logger.debug("twitter: Nitter OK from %s q=%r", base, query)
                break
        except urllib.error.HTTPError as exc:
            logger.debug("twitter: Nitter %s → HTTP %d q=%r", base, exc.code, query)
            last_error = exc
        except Exception as exc:
            logger.debug("twitter: Nitter %s failed q=%r: %s", base, query, exc)
            last_error = exc

    if not raw_xml:
        logger.warning(
            "twitter: all %d Nitter instances failed for q=%r. Last error: %s",
            len(nitter_bases), query, last_error,
        )
        return []

    return _parse_nitter_xml(raw_xml, limit=limit)


def _parse_nitter_xml(raw_xml: bytes, *, limit: int) -> list[TwitterPost]:
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as exc:
        logger.debug("twitter: Nitter XML parse error: %s", exc)
        return []

    posts: list[TwitterPost] = []
    for item in root.findall("./channel/item")[:max(0, limit)]:
        link  = _xml_text(item, "link")
        title = _xml_text(item, "title")
        desc  = _xml_text(item, "description")
        pub   = _xml_text(item, "pubDate")

        created_at_utc: str | None = None
        if pub:
            try:
                created_at_utc = (
                    parsedate_to_datetime(pub)
                    .astimezone(UTC)
                    .replace(microsecond=0)
                    .isoformat()
                )
            except Exception:
                pass

        # Extract tweet_id and username from Nitter URL structure:
        # https://nitter.net/{username}/status/{tweet_id}
        tweet_id = ""
        username = None
        if link:
            parts = link.rstrip("/").split("/")
            if len(parts) >= 1:
                tweet_id = parts[-1]
            if len(parts) >= 4:
                username = parts[-3]

        post_id = tweet_id or link or title or ""
        if not post_id:
            continue

        posts.append(TwitterPost(
            post_id=post_id,
            url=link or None,
            content_text=(desc or title) or None,
            author_username=username,
            author_display_name=None,
            created_at_utc=created_at_utc,
            lang=None,
            like_count=None,
            retweet_count=None,
            reply_count=None,
            quote_count=None,
            view_count=None,
            source=SOURCE_NITTER,
        ))

    return posts


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
    """Convert datetime or string to UTC ISO-8601."""
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).replace(microsecond=0).isoformat()
    if isinstance(value, str):
        # Try RFC 2822 first (most common in RSS / Twitter)
        try:
            dt = parsedate_to_datetime(value)
            return dt.astimezone(UTC).replace(microsecond=0).isoformat()
        except Exception:
            pass
        # Try ISO-8601 (Python 3.11+ handles Z suffix natively)
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC).replace(microsecond=0).isoformat()
        except Exception:
            return value    # return as-is if unparseable
    return None


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _xml_text(element: ET.Element, tag: str) -> str | None:
    child = element.find(tag)
    if child is None:
        return None
    text = (child.text or "").strip()
    return text if text else None


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
        accounts=getattr(cfg, "twitter_accounts", None),
        limit=10,
        db_path=getattr(cfg, "twitter_db_path", None),
        user_agent=getattr(cfg, "twitter_user_agent", None),
    )

    print(
        f"source={resp.source}  "
        f"returned={resp.total_returned}  "
        f"query={query!r}"
    )
    for post in resp.posts[:10]:
        eng = (
            f" [👍{post.like_count} 🔁{post.retweet_count}]"
            if post.like_count is not None else ""
        )
        print(f"\n  {post.created_at_utc or '?'} @{post.author_username or '?'}{eng}")
        if post.content_text:
            preview = post.content_text[:280].rstrip()
            suffix  = "…" if len(post.content_text) > 280 else ""
            print(f"  {preview}{suffix}")
        if post.url:
            print(f"  {post.url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())