"""
cryptogent.sentiment.rss
~~~~~~~~~~~~~~~~~~~~~~~~
Generic crypto RSS feed client.

Fetches and parses RSS 2.0 / Atom feeds from any crypto news source.
Pre-configured feed URLs provided for common sources.

Supported sources (all free, no API key):
  CoinDesk       https://www.coindesk.com/arc/outboundfeeds/rss/
  CoinTelegraph  https://cointelegraph.com/rss
  Decrypt        https://decrypt.co/feed
  The Block      https://www.theblock.co/rss.xml
  Bitcoin.com    https://news.bitcoin.com/feed/

Design:
  - Returns typed ``RSSArticle`` dataclasses (not raw dicts)
  - No raw XML stored in output — all fields extracted explicitly
  - Missing ``published_at_utc`` does NOT filter the article (breaking
    news may lack a timestamp temporarily)
  - Feed source name inferred from channel <title> or caller-supplied label
"""
from __future__ import annotations

import logging
import ssl
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pre-configured feed URLs
# ---------------------------------------------------------------------------
FEEDS: dict[str, str] = {
    "coindesk":      "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",
    "decrypt":       "https://decrypt.co/feed",
    "theblock":      "https://www.theblock.co/rss.xml",
    "bitcoincom":    "https://news.bitcoin.com/feed/",
}

_DEFAULT_FEED    = "coindesk"
_DEFAULT_UA      = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------

class RSSFetchError(RuntimeError):
    """Raised on HTTP errors, network failures, or unparseable XML."""


# ---------------------------------------------------------------------------
# Public data contracts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RSSArticle:
    """
    A single normalised RSS article.

    Attributes
    ----------
    title:
        Article headline.
    url:
        Canonical article URL.
    description:
        Short summary or lead paragraph (plain text, may contain HTML).
    content:
        Full article body when available via ``content:encoded``.
        ``None`` when the feed provides only a summary.
    published_at_utc:
        ISO-8601 UTC publication timestamp.
        ``None`` when the feed item has no date (rare but possible
        for freshly published breaking news).
    source_name:
        Publisher name from the RSS channel title.
    source_url:
        Publisher homepage URL.
    guid:
        Feed-provided unique identifier.  Falls back to ``url`` when absent.
    image_url:
        Article thumbnail from ``media:thumbnail`` or ``media:content``.
    categories:
        List of category/tag strings from ``<category>`` elements.
    """
    title: str
    url: str
    description: str | None
    content: str | None
    published_at_utc: str | None
    source_name: str
    source_url: str | None
    guid: str
    image_url: str | None
    categories: tuple[str, ...]

    @property
    def short_summary(self) -> str:
        """First available text: description → content → title."""
        return self.description or self.content or self.title

    @property
    def has_full_content(self) -> bool:
        """True when full article body is available (not just a summary)."""
        return self.content is not None and len(self.content) > len(self.description or "")


@dataclass(frozen=True)
class RSSResponse:
    """
    Response from an RSS feed fetch.

    Attributes
    ----------
    feed_name:
        Caller-supplied label or inferred from channel title.
    feed_url:
        The URL that was fetched.
    channel_title:
        RSS channel <title> value.
    articles:
        Parsed articles, ordered as they appear in the feed
        (newest first for most crypto feeds).
    skipped_count:
        Number of items skipped due to missing title or URL.
    """
    feed_name: str
    feed_url: str
    channel_title: str
    articles: tuple[RSSArticle, ...]
    skipped_count: int

    @property
    def has_results(self) -> bool:
        return len(self.articles) > 0

    @property
    def headlines(self) -> list[str]:
        """Convenience list of all article titles."""
        return [a.title for a in self.articles]

    @property
    def latest(self) -> RSSArticle | None:
        """Most recent article (first in feed order)."""
        return self.articles[0] if self.articles else None


# ---------------------------------------------------------------------------
# Public API: Multi-feed convenience
# ---------------------------------------------------------------------------

def fetch_all_feeds(
    *,
    feeds: dict[str, str] | None = None,
    timeout_s: float = 10.0,
    ca_bundle: Path | None = None,
    insecure: bool = False,
    user_agent: str | None = None,
    max_items: int | None = None,
) -> list[RSSResponse]:
    """
    Fetch all configured feeds (or a caller-supplied feed dict).

    Parameters
    ----------
    feeds:
        Mapping of feed_name -> feed_url. Defaults to ``FEEDS``.
    timeout_s, ca_bundle, insecure, user_agent, max_items:
        Same as ``fetch_rss``.
    """
    feed_map = feeds or FEEDS
    responses: list[RSSResponse] = []
    for name, url in feed_map.items():
        resp = fetch_rss(
            feed_name=name,
            feed_url=url,
            timeout_s=timeout_s,
            ca_bundle=ca_bundle,
            insecure=insecure,
            user_agent=user_agent,
            max_items=max_items,
        )
        responses.append(resp)
    return responses


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_rss(
    *,
    feed: str = _DEFAULT_FEED,
    feed_url: str | None = None,
    feed_name: str | None = None,
    timeout_s: float = 10.0,
    ca_bundle: Path | None = None,
    insecure: bool = False,
    user_agent: str | None = None,
    max_items: int | None = None,
) -> RSSResponse:
    """
    Fetch and parse a crypto RSS feed.

    Parameters
    ----------
    feed:
        Pre-configured feed name: ``"coindesk"``, ``"cointelegraph"``,
        ``"decrypt"``, ``"theblock"``, ``"bitcoincom"``.
        Ignored when ``feed_url`` is provided explicitly.
    feed_url:
        Custom RSS feed URL.  Overrides ``feed`` when provided.
    feed_name:
        Label to use in the response.  Defaults to ``feed`` or inferred
        from the channel title.
    timeout_s:
        HTTP request timeout in seconds.
    ca_bundle:
        Path to a custom CA bundle for TLS verification.
    insecure:
        Disable TLS certificate verification.  Never use in production.
    user_agent:
        Custom User-Agent string.  The default mimics a browser to avoid
        bot-blocking by news sites.
    max_items:
        Maximum number of articles to return.  ``None`` = all items.

    Returns
    -------
    RSSResponse

    Raises
    ------
    RSSFetchError
        On HTTP error, network failure, or malformed XML.
    ValueError
        On unknown ``feed`` name when ``feed_url`` is not provided.
    """
    # Resolve URL
    if feed_url is not None:
        url = str(feed_url).strip()
        if not url:
            raise RSSFetchError("feed_url is empty")
    else:
        url = FEEDS.get(feed, "")
        if not url:
            raise ValueError(
                f"Unknown feed={feed!r}. "
                f"Valid: {sorted(FEEDS)} or pass feed_url explicitly."
            )

    resolved_name = feed_name or feed

    # Fetch
    ssl_ctx  = _build_ssl_context(ca_bundle=ca_bundle, insecure=insecure)
    raw_xml  = _fetch_xml(url, timeout_s=timeout_s, ssl_ctx=ssl_ctx,
                          user_agent=user_agent or _DEFAULT_UA)

    # Parse
    return _parse_feed(
        raw_xml,
        feed_name=resolved_name,
        feed_url=url,
        max_items=max_items,
    )


# ---------------------------------------------------------------------------
# Private: HTTP
# ---------------------------------------------------------------------------

def _fetch_xml(
    url: str,
    *,
    timeout_s: float,
    ssl_ctx: ssl.SSLContext,
    user_agent: str,
) -> bytes:
    logger.debug("GET %s", url)
    req = urllib.request.Request(
        url=url,
        method="GET",
        headers={
            "Accept":     "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
            "User-Agent": user_agent,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=ssl_ctx) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        raise RSSFetchError(
            f"HTTP {exc.code} fetching RSS from {url}: {body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RSSFetchError(
            f"Network error fetching RSS from {url}: {exc.reason}"
        ) from exc


def _build_ssl_context(
    *,
    ca_bundle: Path | None,
    insecure: bool,
) -> ssl.SSLContext:
    """
    Always return an explicit SSLContext.

    check_hostname must be disabled BEFORE setting verify_mode=CERT_NONE
    (Python raises ValueError if done in the wrong order).
    """
    if insecure:
        logger.warning(
            "TLS verification DISABLED for RSS fetch. "
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


# ---------------------------------------------------------------------------
# Private: XML parsing
# ---------------------------------------------------------------------------

def _parse_feed(
    raw_xml: bytes,
    *,
    feed_name: str,
    feed_url: str,
    max_items: int | None,
) -> RSSResponse:
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as exc:
        raise RSSFetchError(f"Invalid XML in RSS feed from {feed_url}") from exc

    channel = _find_child(root, "channel")
    if channel is None:
        raise RSSFetchError(f"RSS feed from {feed_url} is missing <channel>")

    channel_title = _child_text(channel, "title") or feed_name
    channel_link  = _child_text(channel, "link")
    channel_lang  = _child_text(channel, "language")

    items = [c for c in channel if _local_name(c.tag) == "item"]
    if max_items is not None:
        items = items[:max_items]

    articles: list[RSSArticle] = []
    skipped = 0

    for idx, item in enumerate(items):
        article = _parse_item(
            item, idx=idx,
            channel_title=channel_title,
            channel_link=channel_link,
            channel_lang=channel_lang,
        )
        if article is not None:
            articles.append(article)
        else:
            skipped += 1

    if skipped:
        logger.debug(
            "RSS %s: skipped %d/%d items (missing title or URL)",
            feed_name, skipped, len(items),
        )

    return RSSResponse(
        feed_name=feed_name,
        feed_url=feed_url,
        channel_title=channel_title,
        articles=tuple(articles),
        skipped_count=skipped,
    )


def _parse_item(
    item: ET.Element,
    *,
    idx: int,
    channel_title: str,
    channel_link: str | None,
    channel_lang: str | None,
) -> RSSArticle | None:
    """
    Parse a single RSS <item> element into a typed RSSArticle.

    Returns None only when title or URL is missing — both are required
    for the article to be usable downstream.

    published_at_utc=None is allowed — breaking news may lack a timestamp.
    """
    title = _child_text(item, "title")
    url   = _child_text(item, "link")

    if not title or not url:
        logger.debug(
            "Skipping RSS item[%d]: missing title=%r url=%r", idx, title, url
        )
        return None

    guid = _child_text(item, "guid") or url
    description     = _child_text(item, "description")
    content         = _find_content_encoded(item)
    image_url       = _find_media_image(item)
    published_at    = _parse_pubdate(_child_text(item, "pubDate")
                                     or _child_text(item, "date"))

    # Collect all <category> values — multi-value field, must not skip duplicates
    categories = tuple(
        _text_or_none(child.text) or ""
        for child in item
        if _local_name(child.tag) == "category"
        and _text_or_none(child.text)
    )

    return RSSArticle(
        title=title,
        url=url,
        description=description,
        content=content,
        published_at_utc=published_at,
        source_name=channel_title,
        source_url=channel_link,
        guid=guid,
        image_url=image_url,
        categories=categories,
    )


# ---------------------------------------------------------------------------
# Private: field extractors
# ---------------------------------------------------------------------------

def _find_content_encoded(item: ET.Element) -> str | None:
    """
    Extract ``content:encoded`` (namespace: purl.org/rss/1.0/modules/content/).
    The namespace prefix is stripped by ``_local_name`` so we match on "encoded".
    """
    for child in item:
        if _local_name(child.tag) == "encoded":
            return _text_or_none(child.text)
    return None


def _find_media_image(item: ET.Element) -> str | None:
    """
    Extract image URL from ``media:thumbnail``, ``media:content``,
    or ``<enclosure type="image/...">``.

    ``media:content`` and ``media:thumbnail`` both have local name
    ``content`` / ``thumbnail`` after namespace stripping.
    We require the ``url`` attribute to be present to avoid matching
    non-image content elements.
    """
    for child in item:
        ln = _local_name(child.tag)
        if ln in ("thumbnail", "enclosure"):
            url = child.attrib.get("url") or child.attrib.get("href")
            if url:
                return str(url).strip()
        if ln == "content":
            # media:content — only use when it has a url attribute
            # (not to be confused with content:encoded which has no attribs)
            url = child.attrib.get("url")
            if url:
                return str(url).strip()
    return None


def _parse_pubdate(value: str | None) -> str | None:
    """
    Parse an RSS pubDate (RFC 2822) or ISO-8601 date string to UTC ISO-8601.

    ``parsedate_to_datetime`` handles RFC 2822 format used by most RSS feeds.
    ``datetime.fromisoformat`` handles ISO-8601 variants.

    Note: ``datetime.fromisoformat`` supports full ISO-8601 (including UTC 'Z'
    suffix) from Python 3.11+.  On 3.10 and below, 'Z' is not supported.
    The replace('Z', '+00:00') handles cross-version compatibility.
    """
    if not value:
        return None
    value = str(value).strip()

    # Try RFC 2822 first (standard RSS format)
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        dt = None

    # Fall back to ISO-8601
    if dt is None:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            logger.debug("Could not parse pubDate=%r — skipping timestamp", value)
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)

    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Private: XML utilities
# ---------------------------------------------------------------------------

def _find_child(parent: ET.Element, local_name: str) -> ET.Element | None:
    for child in parent:
        if _local_name(child.tag) == local_name:
            return child
    return None


def _child_text(parent: ET.Element, local_name: str) -> str | None:
    child = _find_child(parent, local_name)
    return _text_or_none(child.text) if child is not None else None


def _local_name(tag: str) -> str:
    """Strip XML namespace from a tag: ``{ns}localname`` → ``localname``."""
    return tag.split("}", 1)[1] if "}" in tag else tag


def _text_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main() -> int:
    import sys

    feed_arg = sys.argv[1].strip() if len(sys.argv) > 1 and sys.argv[1].strip() else _DEFAULT_FEED

    # Accept either a named feed or a URL
    if feed_arg.startswith("http"):
        resp = fetch_rss(feed_url=feed_arg)
    else:
        resp = fetch_rss(feed=feed_arg)

    print(
        f"feed={resp.feed_name!r}  "
        f"fetched={len(resp.articles)}  "
        f"skipped={resp.skipped_count}"
    )
    for a in resp.articles:
        trunc = " [no date]" if a.published_at_utc is None else ""
        cats  = f" [{', '.join(a.categories)}]" if a.categories else ""
        print(f"  {a.published_at_utc or '?'}{trunc} [{a.source_name}] {a.title}{cats}")
        print(f"    {a.url}")
        if a.description:
            preview = a.description[:150].rstrip()
            suffix  = "..." if len(a.description) > 150 else ""
            print(f"    {preview}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
