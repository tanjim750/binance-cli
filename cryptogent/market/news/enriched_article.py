"""
cryptogent.market.news.enriched_article
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Combines RSS / GNews article metadata with full body extraction.

Usage
-----
    from cryptogent.market.news.enriched_article import enrich_gnews_articles
    from cryptogent.market.news.enriched_article import enrich_rss_articles

    # GNews articles
    gnews_resp = client.search(q="bitcoin", lang="en", max_results=5)
    enriched   = enrich_gnews_articles(gnews_resp.articles, delay_s=1.0)

    # RSS articles
    rss_resp = fetch_rss(feed="cointelegraph")
    enriched = enrich_rss_articles(rss_resp.articles, max_articles=5)

    # Use in LLM prompt
    for e in enriched:
        print(e.body_for_llm)         # best available text
        print(e.fetch_status)         # "ok" | "paywall" | "blocked" | ...
        print(e.word_count)           # None when body unavailable
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .article_scrape import ArticleContent, fetch_articles_content
from .gnews import GNewsArticle
from .coindesk.coindesk_rss import RSSArticle
from .binance.binance_announcements import BinanceAnnouncement

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public data contract
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EnrichedArticle:
    """
    A news article with both metadata (from GNews/RSS) and full body text.

    Attributes
    ----------
    title:
        Article headline.
    url:
        Canonical article URL.
    description:
        Short summary from the news API / RSS feed (~1-2 sentences).
    full_body:
        Full article text extracted from the URL.
        ``None`` when blocked, paywalled, or extraction failed.
    word_count:
        Word count of ``full_body``.  ``None`` when unavailable.
    published_at_utc:
        ISO-8601 publication timestamp.
    source_name:
        Publisher name.
    fetch_status:
        ``"ok"`` | ``"paywall"`` | ``"blocked"`` | ``"empty"``
        | ``"js_rendered"`` | ``"failed"`` | ``"skipped"``
        ``"skipped"`` means full body fetch was not attempted.
    paywall_suspected:
        ``True`` when paywall indicators were found on the page.
    categories:
        Article category/tag labels (from RSS only; empty for GNews).
    """
    title: str
    url: str
    description: str | None
    full_body: str | None
    word_count: int | None
    published_at_utc: str | None
    source_name: str | None
    fetch_status: str
    paywall_suspected: bool
    categories: tuple[str, ...]

    @property
    def body_for_llm(self) -> str:
        """
        Best available text for LLM consumption.

        Priority:
          1. full_body   — when successfully extracted (≥ 50 words)
          2. description — when body unavailable (paywall / blocked)
          3. title       — last resort

        The LLM should be informed which level of detail is available
        so it can weight its confidence accordingly.
        """
        if self.full_body and self.fetch_status == "ok":
            return self.full_body
        if self.description:
            return self.description
        return self.title

    @property
    def body_source(self) -> str:
        """
        Which source ``body_for_llm`` comes from.

        Returns: ``"full_body"`` | ``"description"`` | ``"title"``
        """
        if self.full_body and self.fetch_status == "ok":
            return "full_body"
        if self.description:
            return "description"
        return "title"

    @property
    def is_fully_extracted(self) -> bool:
        return self.fetch_status == "ok" and self.full_body is not None

    def llm_context_block(self) -> str:
        """
        Format this article as a single LLM context block.

        Example output:
            [CoinDesk | 2026-03-20 12:45]
            Iran war volatility is driving oil trading boom on Hyperliquid
            Source: full_body (342 words)

            Round-the-clock oil trading on Hyperliquid is drawing investors...
        """
        ts = (self.published_at_utc or "?")[:16].replace("T", " ")
        src = self.source_name or "?"
        words = f"{self.word_count} words" if self.word_count else self.fetch_status
        header = f"[{src} | {ts}] {self.title}"
        meta   = f"Source: {self.body_source} ({words})"
        body   = self.body_for_llm
        return f"{header}\n{meta}\n\n{body}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enrich_gnews_articles(
    articles: tuple[GNewsArticle, ...] | list[GNewsArticle],
    *,
    fetch_full_body: bool = True,
    timeout_s: float = 15.0,
    delay_s: float = 1.0,
    max_articles: int | None = None,
    ca_bundle: Path | None = None,
    insecure: bool = False,
) -> list[EnrichedArticle]:
    """
    Enrich GNews articles with full body text.

    Parameters
    ----------
    articles:
        From ``GNewsResponse.articles``.
    fetch_full_body:
        When ``False``, skips article fetching and returns articles with
        description-only content.  Useful when rate limits are a concern.
    delay_s:
        Seconds between article fetches (default 1.0 — be polite).
    max_articles:
        Limit how many articles to enrich.  ``None`` = all.
    """
    targets = list(articles)[:max_articles] if max_articles else list(articles)

    if not fetch_full_body:
        return [_from_gnews(a, content=None) for a in targets]

    urls     = [a.url for a in targets]
    contents = fetch_articles_content(
        urls,
        timeout_s=timeout_s,
        delay_s=delay_s,
        ca_bundle=ca_bundle,
        insecure=insecure,
    )

    return [
        _from_gnews(article, content=content)
        for article, content in zip(targets, contents)
    ]


def enrich_rss_articles(
    articles: tuple[RSSArticle, ...] | list[RSSArticle],
    *,
    fetch_full_body: bool = True,
    timeout_s: float = 15.0,
    delay_s: float = 1.0,
    max_articles: int | None = None,
    ca_bundle: Path | None = None,
    insecure: bool = False,
) -> list[EnrichedArticle]:
    """
    Enrich RSS articles with full body text.

    Parameters
    ----------
    articles:
        From ``RSSResponse.articles``.
    """
    targets = list(articles)[:max_articles] if max_articles else list(articles)

    if not fetch_full_body:
        return [_from_rss(a, content=None) for a in targets]

    urls     = [a.url for a in targets]
    contents = fetch_articles_content(
        urls,
        timeout_s=timeout_s,
        delay_s=delay_s,
        ca_bundle=ca_bundle,
        insecure=insecure,
    )

    return [
        _from_rss(article, content=content)
        for article, content in zip(targets, contents)
    ]


def enrich_binance_announcements(
    announcements: tuple[BinanceAnnouncement, ...] | list[BinanceAnnouncement],
    *,
    fetch_full_body: bool = True,
    timeout_s: float = 15.0,
    delay_s: float = 1.0,
    max_articles: int | None = None,
    ca_bundle: Path | None = None,
    insecure: bool = False,
) -> list[EnrichedArticle]:
    """
    Enrich Binance announcements with full body text.
    """
    targets = list(announcements)[:max_articles] if max_articles else list(announcements)

    if not fetch_full_body:
        return [_from_binance(a, content=None) for a in targets]

    urls = [a.url for a in targets]
    contents = fetch_articles_content(
        urls,
        timeout_s=timeout_s,
        delay_s=delay_s,
        ca_bundle=ca_bundle,
        insecure=insecure,
    )

    return [
        _from_binance(article, content=content)
        for article, content in zip(targets, contents)
    ]


def build_llm_news_context(
    enriched: list[EnrichedArticle],
    *,
    max_articles: int = 5,
    max_words_per_article: int = 300,
) -> str:
    """
    Build a formatted news context block for LLM prompt injection.

    Selects the most informative articles (full body preferred over
    description-only) and formats them as a numbered list.

    Parameters
    ----------
    enriched:
        From ``enrich_gnews_articles`` or ``enrich_rss_articles``.
    max_articles:
        Maximum articles to include in the context block.
    max_words_per_article:
        Truncate individual article bodies to this word count.

    Returns
    -------
    str
        Ready to insert into an LLM prompt under "RECENT NEWS:" heading.
    """
    # Sort: full body first, then by recency
    sorted_articles = sorted(
        enriched,
        key=lambda a: (
            0 if a.is_fully_extracted else 1,       # full body first
            -(a.published_at_utc or ""),              # then newest first
        ),
    )[:max_articles]

    if not sorted_articles:
        return "RECENT NEWS:\n  No articles available."

    lines = ["RECENT NEWS:"]
    for i, a in enumerate(sorted_articles, 1):
        ts  = (a.published_at_utc or "?")[:16].replace("T", " ")
        src = a.source_name or "?"

        # Truncate body to max_words_per_article
        body = a.body_for_llm
        words = body.split()
        if len(words) > max_words_per_article:
            body = " ".join(words[:max_words_per_article]) + "…"

        detail = f"[{a.body_source}]" if a.body_source != "full_body" else ""
        lines.append(f"\n  [{i}] {ts} | {src} {detail}")
        lines.append(f"  {a.title}")
        lines.append(f"  {body}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Private: constructors
# ---------------------------------------------------------------------------

def _from_gnews(
    article: GNewsArticle,
    content: ArticleContent | None,
) -> EnrichedArticle:
    if content is None:
        return EnrichedArticle(
            title=article.title,
            url=article.url,
            description=article.description or article.content,
            full_body=None,
            word_count=None,
            published_at_utc=article.published_at_utc,
            source_name=article.source_name,
            fetch_status="skipped",
            paywall_suspected=False,
            categories=(),
        )
    return EnrichedArticle(
        title=article.title,
        url=article.url,
        description=article.description or article.content,
        full_body=content.body,
        word_count=content.word_count,
        published_at_utc=article.published_at_utc,
        source_name=article.source_name,
        fetch_status=content.fetch_status,
        paywall_suspected=content.paywall_suspected,
        categories=(),
    )


def _from_rss(
    article: RSSArticle,
    content: ArticleContent | None,
) -> EnrichedArticle:
    if content is None:
        return EnrichedArticle(
            title=article.title,
            url=article.url,
            description=article.description or article.content,
            full_body=None,
            word_count=None,
            published_at_utc=article.published_at_utc,
            source_name=article.source_name,
            fetch_status="skipped",
            paywall_suspected=False,
            categories=article.categories,
        )
    return EnrichedArticle(
        title=article.title,
        url=article.url,
        description=article.description or article.content,
        full_body=content.body,
        word_count=content.word_count,
        published_at_utc=article.published_at_utc,
        source_name=article.source_name,
        fetch_status=content.fetch_status,
        paywall_suspected=content.paywall_suspected,
        categories=article.categories,
    )


def _from_binance(
    announcement: BinanceAnnouncement,
    content: ArticleContent | None,
) -> EnrichedArticle:
    categories = (announcement.catalog_name,) if announcement.catalog_name else ()
    if content is None:
        return EnrichedArticle(
            title=announcement.title,
            url=announcement.url,
            description=None,
            full_body=None,
            word_count=None,
            published_at_utc=announcement.release_date_utc,
            source_name="Binance",
            fetch_status="skipped",
            paywall_suspected=False,
            categories=categories,
        )
    return EnrichedArticle(
        title=announcement.title,
        url=announcement.url,
        description=None,
        full_body=content.body,
        word_count=content.word_count,
        published_at_utc=announcement.release_date_utc,
        source_name="Binance",
        fetch_status=content.fetch_status,
        paywall_suspected=content.paywall_suspected,
        categories=categories,
    )
