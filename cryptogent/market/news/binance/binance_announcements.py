"""
cryptogent.market.news.binance.binance_announcements
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Binance announcements fetcher (public CMS API, no auth required).

API endpoint:
  https://www.binance.com/bapi/composite/v1/public/cms/article/list/query

Known catalog IDs (most relevant for trading):
  48   New Cryptocurrency Listing    ← most important for price impact
  49   Latest Binance News
  51   Delisting
  161  New Futures Contracts
  182  New Margin Pairs
  93   API Updates

Usage:
  resp = fetch_binance_announcements(catalog_id=48, page_size=20)
  for a in resp.announcements:
      print(a.title, a.release_date_utc, a.url)

Announcement detail (full content):
  detail = fetch_binance_announcement_detail(code=a.code)
  print(detail.content_text)
"""
from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from cryptogent.util.time import ms_to_utc_iso

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.binance.com"
_LIST_PATH   = "/bapi/composite/v1/public/cms/article/list/query"
_DETAIL_PATH = "/bapi/composite/v1/public/cms/article/detail"

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

# Binance CMS API requires these XHR headers to return article detail content.
# The list endpoint works without them, but the detail endpoint returns
# success=true with empty data unless clienttype and lang are set.
_BINANCE_XHR_HEADERS: dict[str, str] = {
    "Accept":          "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "clienttype":      "web",
    "lang":            "en",
    "Origin":          "https://www.binance.com",
    "Referer":         "https://www.binance.com/en/support/announcement/",
}

# Known catalog IDs — document here so callers don't have to guess
CATALOG_NEW_LISTINGS   = 48
CATALOG_LATEST_NEWS    = 49
CATALOG_DELISTING      = 51
CATALOG_NEW_FUTURES    = 161
CATALOG_NEW_MARGIN     = 182
CATALOG_API_UPDATES    = 93


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------

class BinanceAnnouncementsError(RuntimeError):
    """Raised on HTTP errors, network failures, or unexpected response shape."""


# ---------------------------------------------------------------------------
# Public data contracts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BinanceAnnouncement:
    """
    A single Binance announcement from the list endpoint.

    Attributes
    ----------
    announcement_id:
        Numeric article ID.  ``None`` when not provided by the API.
    code:
        Unique article code used to fetch the detail.
    title:
        Announcement headline.
    release_date_utc:
        ISO-8601 UTC publication timestamp.
    catalog_id:
        Catalog this announcement belongs to (e.g. 48 = New Listings).
    catalog_name:
        Human-readable catalog name.
    url:
        Direct link to the announcement on Binance.
    article_type:
        Numeric type field from the API.  ``None`` when absent.
    """
    announcement_id: int | None
    code: str
    title: str
    release_date_utc: str
    catalog_id: int | None
    catalog_name: str | None
    url: str
    article_type: int | None

    @property
    def is_listing(self) -> bool:
        """True when this is a new listing announcement."""
        return self.catalog_id == CATALOG_NEW_LISTINGS

    @property
    def is_delisting(self) -> bool:
        return self.catalog_id == CATALOG_DELISTING


@dataclass(frozen=True)
class BinanceAnnouncementsResponse:
    """
    Response from the announcements list endpoint.

    Attributes
    ----------
    catalog_id:
        The catalog that was queried.
    page_no / page_size:
        Pagination parameters used.
    total:
        Total announcements available in this catalog.
    announcements:
        Parsed announcements returned in this page.
    """
    catalog_id: int
    page_no: int
    page_size: int
    total: int
    announcements: tuple[BinanceAnnouncement, ...]

    @property
    def has_more(self) -> bool:
        """True when there are more pages available."""
        return self.total > self.page_no * self.page_size


@dataclass(frozen=True)
class BinanceAnnouncementDetail:
    """
    Full content of a single Binance announcement.

    Attributes
    ----------
    code:
        Article code (same as in BinanceAnnouncement).
    title:
        Announcement title.
    content_text:
        Full announcement body as plain text (HTML stripped).
    release_date_utc:
        ISO-8601 UTC publication timestamp.
    url:
        Direct link to the announcement.
    """
    code: str
    title: str | None
    content_text: str | None
    release_date_utc: str | None
    url: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_binance_announcements(
    *,
    catalog_id: int = CATALOG_NEW_LISTINGS,
    article_type: int = 1,
    page_no: int = 1,
    page_size: int = 20,
    locale: str = "en",
    timeout_s: float = 10.0,
    ca_bundle: Path | None = None,
    insecure: bool = False,
    user_agent: str | None = None,
) -> BinanceAnnouncementsResponse:
    """
    Fetch a page of Binance announcements from a given catalog.

    Parameters
    ----------
    catalog_id:
        Catalog to query.  Use module-level constants:
        ``CATALOG_NEW_LISTINGS`` (48), ``CATALOG_DELISTING`` (51), etc.
    article_type:
        API type parameter (default 1).
    page_no:
        Page number (1-based).
    page_size:
        Articles per page (max 20 on free tier).
    locale:
        Language for announcement URLs (default ``"en"``).

    Returns
    -------
    BinanceAnnouncementsResponse

    Raises
    ------
    BinanceAnnouncementsError
        On HTTP error, network failure, or unexpected response shape.
    """
    params = {
        "type":      str(int(article_type)),
        "catalogId": str(int(catalog_id)),
        "pageNo":    str(int(page_no)),
        "pageSize":  str(int(page_size)),
    }
    url     = f"{_BASE_URL}{_LIST_PATH}?{urllib.parse.urlencode(params)}"
    payload = _request_json(
        url=url, timeout_s=timeout_s,
        ca_bundle=ca_bundle, insecure=insecure,
        user_agent=user_agent,
    )

    data = payload.get("data")
    if not isinstance(data, dict):
        raise BinanceAnnouncementsError(
            f"Unexpected response shape: 'data' is {type(data).__name__}"
        )

    catalogs = data.get("catalogs")
    if not isinstance(catalogs, list):
        raise BinanceAnnouncementsError(
            f"Unexpected response shape: 'catalogs' is {type(catalogs).__name__}"
        )

    announcements: list[BinanceAnnouncement] = []
    total = 0

    for cat in catalogs:
        if not isinstance(cat, dict):
            continue
        cat_id   = cat.get("catalogId")
        cat_name = cat.get("catalogName")
        total    = max(total, int(cat.get("total") or 0))

        articles = cat.get("articles")
        if isinstance(articles, list):
            parsed = _parse_articles(articles, cat_id=cat_id, cat_name=cat_name, locale=locale)
            announcements.extend(parsed)

        # Handle nested catalogs in the response
        nested = cat.get("catalogs")
        if isinstance(nested, list):
            for sub in nested:
                if not isinstance(sub, dict):
                    continue
                sub_articles = sub.get("articles")
                if isinstance(sub_articles, list):
                    parsed = _parse_articles(
                        sub_articles,
                        cat_id=sub.get("catalogId"),
                        cat_name=sub.get("catalogName"),
                        locale=locale,
                    )
                    announcements.extend(parsed)

    logger.debug(
        "fetch_binance_announcements: catalog=%d page=%d/%d "
        "returned=%d total=%d",
        catalog_id, page_no,
        max(1, (total + page_size - 1) // page_size),
        len(announcements), total,
    )

    return BinanceAnnouncementsResponse(
        catalog_id=catalog_id,
        page_no=page_no,
        page_size=page_size,
        total=total,
        announcements=tuple(announcements),
    )


def fetch_binance_announcement_detail(
    *,
    code: str,
    locale: str = "en",
    catalog_id: int | None = None,
    timeout_s: float = 12.0,
    ca_bundle: Path | None = None,
    insecure: bool = False,
    user_agent: str | None = None,
) -> BinanceAnnouncementDetail | None:
    """
    Fetch full content of a single announcement by its code.

    Tries multiple known Binance CMS endpoint patterns in order until
    one returns article content.  This is necessary because Binance's
    internal API structure varies by announcement type and has changed
    over time.

    Parameters
    ----------
    code:
        Article code from ``BinanceAnnouncement.code``.
    catalog_id:
        When provided, included in list-query fallbacks to improve
        matching accuracy.

    Returns
    -------
    BinanceAnnouncementDetail or ``None`` when not found.
    """
    code = str(code).strip()
    if not code:
        return None

    announcement_url = _build_url(code=code, locale=locale)

    # All known endpoint + param combinations, tried in order.
    # Binance's CMS API has several undocumented variants — we try the
    # most likely ones systematically and log each attempt.
    candidates: list[tuple[str, dict]] = [
        # 1. Dedicated detail endpoint (most direct)
        (_DETAIL_PATH, {"code": code, "locale": locale}),

        # 2. Detail endpoint with articleCode param variant
        (_DETAIL_PATH, {"articleCode": code, "locale": locale}),

        # 3. List endpoint filtered by articleCode (commonly works)
        (_LIST_PATH, {"articleCode": code, "type": "1", "pageNo": "1", "pageSize": "1"}),

        # 4. List endpoint filtered by code param
        (_LIST_PATH, {"code": code, "type": "1", "pageNo": "1", "pageSize": "1"}),

        # 5. List endpoint with locale and code
        (_LIST_PATH, {"code": code, "locale": locale, "type": "1"}),
    ]

    # 6. If catalog_id is known, add catalog-scoped list query
    if catalog_id is not None:
        candidates.append((
            _LIST_PATH,
            {
                "catalogId": str(int(catalog_id)),
                "articleCode": code,
                "type": "1",
                "pageNo": "1",
                "pageSize": "1",
            },
        ))

    for idx, (path, params) in enumerate(candidates, 1):
        url = f"{_BASE_URL}{path}?{urllib.parse.urlencode(params)}"
        logger.debug(
            "fetch_binance_announcement_detail: attempt %d/%d: %s",
            idx, len(candidates), url,
        )
        try:
            payload = _request_json(
                url=url, timeout_s=timeout_s,
                ca_bundle=ca_bundle, insecure=insecure,
                user_agent=user_agent,
                extra_headers=_BINANCE_XHR_HEADERS,
            )
        except BinanceAnnouncementsError as exc:
            logger.debug(
                "fetch_binance_announcement_detail: attempt %d failed: %s",
                idx, exc,
            )
            continue

        # Try to extract detail from this response
        detail = _extract_detail_dict(payload)
        if detail:
            logger.debug(
                "fetch_binance_announcement_detail: success on attempt %d", idx
            )
            return _build_detail(code=code, data=detail, url=announcement_url)

        # For list-style responses, walk catalogs to find articles
        article_data = _extract_article_from_list_response(payload, code=code)
        if article_data:
            logger.debug(
                "fetch_binance_announcement_detail: "
                "found article in list response on attempt %d", idx,
            )
            return _build_detail(code=code, data=article_data, url=announcement_url)

        logger.debug(
            "fetch_binance_announcement_detail: attempt %d returned no usable content",
            idx,
        )

    logger.warning(
        "fetch_binance_announcement_detail: exhausted all %d endpoint patterns "
        "for code=%r — falling back to HTML page extraction.",
        len(candidates), code,
    )

    # Final fallback — fetch the HTML announcement page directly
    # Binance support pages are Next.js but they include JSON-LD structured
    # data in <script type="application/ld+json"> which contains the article body
    return _fetch_detail_from_html_page(
        url=announcement_url,
        code=code,
        timeout_s=timeout_s,
        ca_bundle=ca_bundle,
        insecure=insecure,
        user_agent=user_agent,
    )


def build_announcement_url(*, code: str, locale: str = "en") -> str:
    """Return the canonical Binance announcement page URL for a given code."""
    return _build_url(code=str(code).strip(), locale=locale)


# ---------------------------------------------------------------------------
# Private: parsing
# ---------------------------------------------------------------------------

def _parse_articles(
    articles: list[dict],
    *,
    cat_id: object,
    cat_name: object,
    locale: str,
) -> list[BinanceAnnouncement]:
    out: list[BinanceAnnouncement] = []
    for idx, item in enumerate(articles):
        if not isinstance(item, dict):
            logger.debug("_parse_articles: skipping non-dict item[%d]", idx)
            continue

        code     = str(item.get("code") or "").strip()
        title    = str(item.get("title") or "").strip()
        rel_ms   = item.get("releaseDate")

        if not code:
            logger.debug("_parse_articles: item[%d] missing code — skipped", idx)
            continue
        if not title:
            logger.debug("_parse_articles: item[%d] (code=%r) missing title — skipped", idx, code)
            continue
        if not rel_ms:
            logger.debug("_parse_articles: item[%d] (code=%r) missing releaseDate — skipped", idx, code)
            continue

        try:
            release_date_utc = ms_to_utc_iso(int(rel_ms))
        except (TypeError, ValueError) as exc:
            logger.debug(
                "_parse_articles: item[%d] (code=%r) invalid releaseDate=%r: %s",
                idx, code, rel_ms, exc,
            )
            continue

        raw_id   = item.get("id")
        raw_type = item.get("type")

        out.append(BinanceAnnouncement(
            announcement_id=int(raw_id)   if raw_id   is not None else None,
            code=code,
            title=title,
            release_date_utc=release_date_utc,
            catalog_id=int(cat_id)        if cat_id   is not None else None,
            catalog_name=str(cat_name)    if cat_name is not None else None,
            url=_build_url(code=code, locale=locale),
            article_type=int(raw_type)    if raw_type is not None else None,
        ))
    return out


def _fetch_detail_from_html_page(
    *,
    url: str,
    code: str,
    timeout_s: float,
    ca_bundle: Path | None,
    insecure: bool,
    user_agent: str | None,
) -> BinanceAnnouncementDetail | None:
    """
    Last-resort fallback: fetch the Binance HTML announcement page and
    extract content from embedded JSON-LD or __NEXT_DATA__ script tags.

    Binance Next.js pages embed the article data as JSON in:
      <script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"article":{"title":...,"content":...}}}}
      </script>

    This is server-side rendered and available without JS execution.
    """
    import re as _re

    ssl_ctx = _build_ssl_context(ca_bundle=ca_bundle, insecure=insecure)
    logger.debug("fetch_binance_announcement_detail: HTML fallback: %s", url)

    req = urllib.request.Request(
        url=url,
        method="GET",
        headers={
            "Accept":     "text/html,application/xhtml+xml,*/*;q=0.8",
            "User-Agent": str(user_agent or _DEFAULT_USER_AGENT),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=ssl_ctx) as resp:
            html_bytes = resp.read()
    except Exception as exc:
        logger.debug("_fetch_detail_from_html_page: HTTP failed: %s", exc)
        return None

    try:
        html_text = html_bytes.decode("utf-8", errors="replace")
    except Exception:
        return None

    # Detect bot-challenge / Cloudflare interstitial pages
    # These are short HTML pages (< 5000 chars) with no meaningful content
    if len(html_text) < 5000 and html_text.count("<p") < 3:
        logger.debug(
            "_fetch_detail_from_html_page: "
            "bot-challenge or empty page detected for code=%r (len=%d)",
            code, len(html_text),
        )
        return None

    # Strategy 1 — __NEXT_DATA__ JSON embedded in page
    next_data_match = _re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html_text,
        _re.DOTALL | _re.IGNORECASE,
    )
    if next_data_match:
        try:
            next_data = json.loads(next_data_match.group(1))
            article = _walk_next_data(next_data)
            if article:
                logger.debug(
                    "_fetch_detail_from_html_page: "
                    "extracted from __NEXT_DATA__ for code=%r", code,
                )
                return _build_detail(code=code, data=article, url=url)
        except (json.JSONDecodeError, Exception) as exc:
            logger.debug("_fetch_detail_from_html_page: __NEXT_DATA__ parse failed: %s", exc)

    # Strategy 2 — JSON-LD structured data
    jsonld_matches = _re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text,
        _re.DOTALL | _re.IGNORECASE,
    )
    for raw_jsonld in jsonld_matches:
        try:
            ld = json.loads(raw_jsonld)
            if isinstance(ld, dict) and ld.get("@type") in ("NewsArticle", "Article"):
                content = ld.get("articleBody") or ld.get("description")
                if content:
                    logger.debug(
                        "_fetch_detail_from_html_page: "
                        "extracted from JSON-LD for code=%r", code,
                    )
                    return BinanceAnnouncementDetail(
                        code=code,
                        title=ld.get("headline") or ld.get("name"),
                        content_text=str(content).strip(),
                        release_date_utc=ld.get("datePublished"),
                        url=url,
                    )
        except Exception:
            continue

    logger.warning(
        "_fetch_detail_from_html_page: could not extract content "
        "from HTML page for code=%r", code,
    )
    return None


def _walk_next_data(data: object) -> dict | None:
    """
    Recursively walk __NEXT_DATA__ to find the article dict.

    Binance embeds article detail under various paths depending on
    the page version — walk the tree looking for a dict that contains
    'content' or 'body' alongside 'title'.
    """
    if not isinstance(data, dict):
        return None

    # Direct match — this dict looks like an article
    has_content = any(data.get(k) for k in ("content", "body", "articleContent"))
    has_title   = bool(data.get("title"))
    if has_content and has_title:
        return data

    # Walk known structural paths first for performance
    for key in ("article", "articleDetail", "detail", "pageProps", "props", "data"):
        child = data.get(key)
        if child is not None:
            result = _walk_next_data(child)
            if result:
                return result

    # Generic walk of all dict values
    for value in data.values():
        if isinstance(value, dict):
            result = _walk_next_data(value)
            if result:
                return result

    return None


def _extract_article_from_list_response(
    payload: dict,
    *,
    code: str,
) -> dict | None:
    """
    Walk a list-style API response to find an article matching *code*.

    Some Binance list endpoints return the full article detail inside
    the articles array when filtered by code/articleCode.  This extracts
    that article dict so it can be used as a detail source.
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None

    catalogs = data.get("catalogs")
    if not isinstance(catalogs, list):
        return None

    for cat in catalogs:
        if not isinstance(cat, dict):
            continue
        for article_list_key in ("articles",):
            articles = cat.get(article_list_key)
            if not isinstance(articles, list):
                continue
            for item in articles:
                if not isinstance(item, dict):
                    continue
                item_code = str(item.get("code") or item.get("articleCode") or "").strip()
                if item_code == code:
                    # Check whether this item has body content
                    # (not just list metadata like title + releaseDate)
                    has_body = any(
                        item.get(k)
                        for k in ("content", "body", "text", "articleContent")
                    )
                    if has_body:
                        return item
                    # Item found but no body — return it anyway
                    # so caller gets at minimum title + date
                    return item
        # Check nested catalogs
        nested = cat.get("catalogs")
        if isinstance(nested, list):
            for sub in nested:
                if not isinstance(sub, dict):
                    continue
                sub_articles = sub.get("articles")
                if not isinstance(sub_articles, list):
                    continue
                for item in sub_articles:
                    if not isinstance(item, dict):
                        continue
                    item_code = str(item.get("code") or "").strip()
                    if item_code == code:
                        return item
    return None


def _extract_detail_dict(payload: dict) -> dict | None:
    """
    Navigate the API response to find the article detail dict.

    Tries known key names in order; logs which key was found.
    Returns the raw data dict without modification.
    """
    if not isinstance(payload, dict):
        return None

    data = payload.get("data")
    if not isinstance(data, dict):
        return None

    # Try known detail container keys
    for key in ("article", "articleDetail", "articleDetailVo", "detail"):
        candidate = data.get(key)
        if isinstance(candidate, dict) and candidate:
            logger.debug("_extract_detail_dict: found detail under key=%r", key)
            return candidate

    # If data itself looks like a detail dict (has title/content), use it
    if data.get("title") or data.get("content") or data.get("body"):
        logger.debug("_extract_detail_dict: using data dict directly")
        return data

    logger.debug(
        "_extract_detail_dict: no detail found. Available keys: %s",
        list(data.keys()),
    )
    return None


def _build_detail(
    *,
    code: str,
    data: dict,
    url: str,
) -> BinanceAnnouncementDetail:
    """Construct a BinanceAnnouncementDetail from a raw detail dict."""
    content_html = (
        data.get("content")
        or data.get("body")
        or data.get("text")
    )
    content_text = _html_to_text(content_html) if isinstance(content_html, str) else None

    rel_ms = data.get("releaseDate") or data.get("releaseTime")
    release_date_utc: str | None = None
    if rel_ms is not None:
        try:
            release_date_utc = ms_to_utc_iso(int(rel_ms))
        except (TypeError, ValueError):
            logger.debug("_build_detail: invalid releaseDate=%r for code=%r", rel_ms, code)

    title = data.get("title")

    return BinanceAnnouncementDetail(
        code=code,
        title=str(title).strip() if isinstance(title, str) else None,
        content_text=content_text,
        release_date_utc=release_date_utc,
        url=url,
    )


# ---------------------------------------------------------------------------
# Private: HTTP
# ---------------------------------------------------------------------------

def _request_json(
    *,
    url: str,
    timeout_s: float,
    ca_bundle: Path | None,
    insecure: bool,
    user_agent: str | None,
    extra_headers: dict[str, str] | None = None,
) -> dict:
    ssl_ctx = _build_ssl_context(ca_bundle=ca_bundle, insecure=insecure)
    logger.debug("GET %s", url)

    headers: dict[str, str] = {
        "Accept":     "application/json",
        "User-Agent": str(user_agent or _DEFAULT_USER_AGENT),
    }
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(
        url=url,
        method="GET",
        headers=headers,
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=ssl_ctx) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        raise BinanceAnnouncementsError(
            f"HTTP {exc.code} fetching Binance announcements: {body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise BinanceAnnouncementsError(
            f"Network error fetching Binance announcements: {exc.reason}"
        ) from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BinanceAnnouncementsError(
            "Non-JSON response from Binance announcements API"
        ) from exc

    if not isinstance(payload, dict):
        raise BinanceAnnouncementsError(
            f"Unexpected top-level type: expected dict, "
            f"got {type(payload).__name__}"
        )

    if payload.get("success") is False:
        raise BinanceAnnouncementsError(
            f"Binance announcements API returned success=false: "
            f"{payload.get('message', payload)}"
        )

    return payload


def _build_ssl_context(
    *,
    ca_bundle: Path | None,
    insecure: bool,
) -> ssl.SSLContext:
    """
    Always return an explicit SSLContext.
    check_hostname must be False BEFORE setting CERT_NONE.
    """
    if insecure:
        logger.warning(
            "TLS verification DISABLED for Binance announcements. "
            "Never use insecure=True in production."
        )
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        return ctx

    if ca_bundle is not None:
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cafile=str(ca_bundle.expanduser()))
        return ctx

    return ssl.create_default_context()


def _build_url(*, code: str, locale: str) -> str:
    return f"{_BASE_URL}/{locale}/support/announcement/detail/{code}"


# ---------------------------------------------------------------------------
# Private: HTML → plain text
# ---------------------------------------------------------------------------

class _HTMLToText(HTMLParser):
    """
    Minimal HTML stripper that preserves paragraph structure.

    Block-level elements (p, div, br, h1-h6, li) insert newlines
    so the output retains readable paragraph breaks rather than
    collapsing the whole article into a single line.
    """

    _BLOCK_TAGS = frozenset({
        "p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "tr", "blockquote", "section", "article",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag.lower() in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        s = data.strip()
        if s:
            self._chunks.append(s)

    def text(self) -> str:
        import re
        raw = " ".join(self._chunks)
        # Collapse excess whitespace while preserving paragraph breaks
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _html_to_text(value: str | None) -> str | None:
    if not value:
        return None
    parser = _HTMLToText()
    try:
        parser.feed(value)
        parser.close()
    except Exception as exc:
        logger.debug("_html_to_text: parse error: %s", exc)
        return None
    text = parser.text()
    return text if text else None


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main() -> int:
    import sys

    cat_id = int(sys.argv[1]) if len(sys.argv) > 1 else CATALOG_NEW_LISTINGS

    resp = fetch_binance_announcements(catalog_id=cat_id, page_size=10)
    print(
        f"catalog={cat_id}  total={resp.total}  "
        f"returned={len(resp.announcements)}  "
        f"has_more={resp.has_more}"
    )

    for a in resp.announcements:
        cat = f" [{a.catalog_name}]" if a.catalog_name else ""
        flag = " [LISTING]" if a.is_listing else " [DELIST]" if a.is_delisting else ""
        print(f"\n  {a.release_date_utc}{cat}{flag}")
        print(f"  {a.title}")
        print(f"  {a.url}")

        detail = fetch_binance_announcement_detail(code=a.code, timeout_s=12.0)
        if detail and detail.content_text:
            preview = detail.content_text[:400].rstrip()
            suffix  = "…" if len(detail.content_text) > 400 else ""
            print(f"  body: {preview}{suffix}")
        elif detail is None:
            print("  body: [not available]")

    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
