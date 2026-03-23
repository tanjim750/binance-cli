"""
cryptogent.market.news.cointelegraph.cointelegraph
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Cointelegraph RSS + full-article scraping helpers.

This is a thin wrapper around the generic RSS + article scraping modules:
- RSS feed: uses ``fetch_rss(feed="cointelegraph")``
- Full body: uses ``fetch_article_content(url=...)``
"""
from __future__ import annotations

from pathlib import Path

try:  # Allows running as a script without package context
    from ..coindesk.coindesk_rss import RSSResponse, fetch_rss
    from ..article_scrape import ArticleContent, fetch_article_content
    from ..enriched_article import EnrichedArticle, enrich_rss_articles
except ImportError:  # pragma: no cover - fallback for direct execution
    from cryptogent.market.news.coindesk.coindesk_rss import RSSResponse, fetch_rss
    from cryptogent.market.news.article_scrape import ArticleContent, fetch_article_content
    from cryptogent.market.news.enriched_article import EnrichedArticle, enrich_rss_articles


_COINTELEGRAPH_FEED = "cointelegraph"


def fetch_cointelegraph_rss(
    *,
    timeout_s: float = 10.0,
    ca_bundle: Path | None = None,
    insecure: bool = False,
    user_agent: str | None = None,
    max_items: int | None = None,
) -> RSSResponse:
    return fetch_rss(
        feed=_COINTELEGRAPH_FEED,
        timeout_s=timeout_s,
        ca_bundle=ca_bundle,
        insecure=insecure,
        user_agent=user_agent,
        max_items=max_items,
    )


def fetch_cointelegraph_article(
    *,
    url: str,
    timeout_s: float = 15.0,
    ca_bundle: Path | None = None,
    insecure: bool = False,
    user_agent: str | None = None,
    min_word_count: int = 50,
    delay_s: float = 0.0,
) -> ArticleContent:
    return fetch_article_content(
        url=url,
        timeout_s=timeout_s,
        ca_bundle=ca_bundle,
        insecure=insecure,
        user_agent=user_agent,
        min_word_count=min_word_count,
        delay_s=delay_s,
    )


def enrich_cointelegraph_rss(
    *,
    max_items: int | None = 10,
    max_articles: int | None = 5,
    timeout_s: float = 15.0,
    delay_s: float = 1.0,
    ca_bundle: Path | None = None,
    insecure: bool = False,
    user_agent: str | None = None,
) -> list[EnrichedArticle]:
    rss = fetch_cointelegraph_rss(
        timeout_s=timeout_s,
        ca_bundle=ca_bundle,
        insecure=insecure,
        user_agent=user_agent,
        max_items=max_items,
    )
    return enrich_rss_articles(
        rss.articles,
        max_articles=max_articles,
        timeout_s=timeout_s,
        delay_s=delay_s,
        ca_bundle=ca_bundle,
        insecure=insecure,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main() -> int:
    import sys

    if len(sys.argv) > 1 and sys.argv[1].strip().startswith("http"):
        content = fetch_cointelegraph_article(url=sys.argv[1].strip())
        print(f"status={content.fetch_status} words={content.word_count} url={content.url}")
        if content.body:
            preview = content.body[:600].rstrip()
            suffix = "…" if len(content.body) > 600 else ""
            print(f"\n{preview}{suffix}")
        return 0

    enriched = enrich_cointelegraph_rss(max_items=10, max_articles=5)
    print(f"enriched={len(enriched)} feed=cointelegraph")
    for a in enriched:
        print(f"  {a.published_at_utc or '?'} [{a.source_name or '?'}] {a.title}")
        print(f"    {a.url}")
        if a.full_body:
            preview = a.full_body[:400].rstrip()
            suffix = "…" if len(a.full_body) > 400 else ""
            print(f"    body: {preview}{suffix}")
        elif a.description:
            preview = a.description[:200].rstrip()
            suffix = "…" if len(a.description) > 200 else ""
            print(f"    desc: {preview}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
