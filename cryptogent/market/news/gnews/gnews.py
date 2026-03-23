"""
cryptogent.sentiment.gnews
~~~~~~~~~~~~~~~~~~~~~~~~~~
GNews API client for crypto news and sentiment.

API docs: https://gnews.io/docs/v4
Free tier: 100 articles/day, content truncated to ~260 chars

Supported endpoints:
  /search         — keyword search across all news
  /top-headlines  — top headlines by category / language / country

Usage notes:
  - Cache responses — free tier has a daily article limit
  - GNews content is truncated on free tier; ``content_truncated``
    flag is set when truncation marker detected
  - API key is passed via X-Api-Key header (not query param)
    to prevent key leakage in logs and URLs
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
from typing import Iterable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_BASE_URL = "https://gnews.io/api/v4"

# GNews free tier truncates content and appends this marker
_TRUNCATION_MARKER = "... ["

# Valid sortby values
_VALID_SORTBY = frozenset({"publishedAt", "relevance"})

# Valid category values for top-headlines
_VALID_CATEGORIES = frozenset({
    "breaking-news", "world", "nation", "business",
    "technology", "entertainment", "sports", "science", "health",
})


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------

class GNewsAPIError(RuntimeError):
    """Raised on any HTTP error, network failure, or unexpected response shape."""


# ---------------------------------------------------------------------------
# Public data contracts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GNewsArticle:
    """
    A single normalised GNews article.

    All string fields are ``None`` when the API did not provide the value.
    Empty strings from the API are coerced to ``None``.

    Attributes
    ----------
    title:
        Article headline.  ``None`` when missing — articles with no title
        are filtered out before being returned to callers.
    url:
        Canonical article URL.  ``None`` when missing — filtered out.
    description:
        Short summary / lead paragraph.
    content:
        Article body (truncated to ~260 chars on free tier).
    content_truncated:
        ``True`` when GNews truncated the content (free tier behaviour).
    published_at_utc:
        ISO-8601 publication timestamp.  ``None`` when missing.
    source_name:
        Publisher name (e.g. "CoinDesk", "Reuters").
    source_url:
        Publisher homepage URL.
    image_url:
        Article thumbnail URL.
    """
    title: str
    url: str
    description: str | None
    content: str | None
    content_truncated: bool
    published_at_utc: str | None
    source_name: str | None
    source_url: str | None
    image_url: str | None

    @property
    def short_summary(self) -> str:
        """First available text: description → content → title."""
        return self.description or self.content or self.title


@dataclass(frozen=True)
class GNewsResponse:
    """
    Response from a GNews API call.

    Attributes
    ----------
    total_articles:
        Total matching articles reported by the API
        (may be much larger than ``len(articles)``).
    articles:
        Normalised articles returned in this response.
    request_kind:
        ``"search"`` or ``"top-headlines"``.
    query:
        The ``q`` parameter used, or ``None`` for top-headlines without query.
    request_params:
        Dict of request parameters used for the API call.
    """
    total_articles: int
    articles: tuple[GNewsArticle, ...]
    request_kind: str
    query: str | None
    request_params: dict | None

    @property
    def has_results(self) -> bool:
        return len(self.articles) > 0

    @property
    def headlines(self) -> list[str]:
        """Convenience list of all article titles."""
        return [a.title for a in self.articles]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class GNewsClient:
    """
    Thin wrapper around the GNews REST API.

    The API key is sent via ``X-Api-Key`` request header — never
    appended to the URL — to prevent accidental key exposure in logs.

    Parameters
    ----------
    api_key:
        GNews API key.  Raises ``GNewsAPIError`` when missing.
    timeout_s:
        HTTP request timeout in seconds.
    ca_bundle:
        Path to a custom CA bundle for TLS verification.
    insecure:
        Disable TLS verification.  Never use in production.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout_s: float = 10.0,
        ca_bundle: Path | None = None,
        insecure: bool = False,
    ) -> None:
        if not api_key or not str(api_key).strip():
            raise GNewsAPIError(
                "Missing GNews API key. "
                "Set [gnews].api_key in cryptogent.toml."
            )
        self._api_key   = str(api_key).strip()
        self._timeout_s = float(timeout_s)
        self._ssl_ctx   = _build_ssl_context(ca_bundle=ca_bundle, insecure=insecure)

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def search(
        self,
        *,
        q: str | Iterable[str],
        lang: str | None = None,
        country: str | None = None,
        max_results: int | None = None,
        in_fields: str | None = None,
        from_iso: str | None = None,
        to_iso: str | None = None,
        sortby: str | None = None,
        page: int | None = None,
    ) -> GNewsResponse:
        """
        Search GNews for articles matching *q*.

        When *q* is a list/iterable of strings, this executes one
        search per query and returns the combined results.

        Parameters
        ----------
        q:
            Search query (required, non-empty), or an iterable of queries.
        lang:
            ISO 639-1 language code (e.g. ``"en"``).
        country:
            ISO 3166-1 alpha-2 country code (e.g. ``"us"``).
        max_results:
            Articles to return per page (1–100, free tier max 10).
        in_fields:
            Fields to search: ``"title"``, ``"description"``,
            ``"content"`` (comma-separated).
        from_iso / to_iso:
            ISO-8601 date range filters.
        sortby:
            ``"publishedAt"`` (default) or ``"relevance"``.
        page:
            Pagination page number.
        """
        if _is_iterable_query(q):
            queries = _normalise_queries(q)
            if not queries:
                raise GNewsAPIError("search() requires a non-empty 'q' parameter")
            return self._search_many(
                queries=queries,
                lang=lang,
                country=country,
                max_results=max_results,
                in_fields=in_fields,
                from_iso=from_iso,
                to_iso=to_iso,
                sortby=sortby,
                page=page,
            )

        q = str(q).strip()
        if not q:
            raise GNewsAPIError("search() requires a non-empty 'q' parameter")
        return self._search_single(
            q=q,
            lang=lang,
            country=country,
            max_results=max_results,
            in_fields=in_fields,
            from_iso=from_iso,
            to_iso=to_iso,
            sortby=sortby,
            page=page,
        )

    def _search_single(
        self,
        *,
        q: str,
        lang: str | None = None,
        country: str | None = None,
        max_results: int | None = None,
        in_fields: str | None = None,
        from_iso: str | None = None,
        to_iso: str | None = None,
        sortby: str | None = None,
        page: int | None = None,
    ) -> GNewsResponse:
        q = str(q).strip()
        if not q:
            raise GNewsAPIError("search() requires a non-empty 'q' parameter")

        if sortby is not None and sortby not in _VALID_SORTBY:
            raise GNewsAPIError(
                f"Invalid sortby={sortby!r}. Valid: {sorted(_VALID_SORTBY)}"
            )

        params: dict[str, str] = {"q": q}
        _add_opt(params, "lang",   lang)
        _add_opt(params, "country", country)
        _add_opt(params, "max",    str(int(max_results)) if max_results is not None else None)
        _add_opt(params, "in",     in_fields)
        _add_opt(params, "from",   from_iso)
        _add_opt(params, "to",     to_iso)
        _add_opt(params, "sortby", sortby)
        _add_opt(params, "page",   str(int(page)) if page is not None else None)

        payload = self._request(endpoint="search", params=params)
        return _parse_response(payload, request_kind="search", query=q, request_params=params)

    def _search_many(
        self,
        *,
        queries: list[str],
        lang: str | None = None,
        country: str | None = None,
        max_results: int | None = None,
        in_fields: str | None = None,
        from_iso: str | None = None,
        to_iso: str | None = None,
        sortby: str | None = None,
        page: int | None = None,
    ) -> GNewsResponse:
        seen_urls: set[str] = set()
        merged: list[GNewsArticle] = []
        total = 0
        for query in queries:
            resp = self._search_single(
                q=query,
                lang=lang,
                country=country,
                max_results=max_results,
                in_fields=in_fields,
                from_iso=from_iso,
                to_iso=to_iso,
                sortby=sortby,
                page=page,
            )
            total += int(resp.total_articles)
            for article in resp.articles:
                if article.url in seen_urls:
                    continue
                seen_urls.add(article.url)
                merged.append(article)

        combined_query = " OR ".join(queries)
        request_params = {
            "q": queries,
            "lang": lang,
            "country": country,
            "max": str(int(max_results)) if max_results is not None else None,
            "in": in_fields,
            "from": from_iso,
            "to": to_iso,
            "sortby": sortby,
            "page": str(int(page)) if page is not None else None,
        }
        request_params = {k: v for k, v in request_params.items() if v not in (None, "")}
        return GNewsResponse(
            total_articles=total,
            articles=tuple(merged),
            request_kind="search",
            query=combined_query,
            request_params=request_params,
        )

    def top_headlines(
        self,
        *,
        category: str | None = None,
        lang: str | None = None,
        country: str | None = None,
        max_results: int | None = None,
        q: str | None = None,
        from_iso: str | None = None,
        to_iso: str | None = None,
        page: int | None = None,
    ) -> GNewsResponse:
        """
        Fetch top headlines, optionally filtered by category or query.

        Parameters
        ----------
        category:
            One of: ``"breaking-news"``, ``"world"``, ``"business"``,
            ``"technology"``, ``"science"``, ``"health"``, etc.
        q:
            Optional keyword filter within top headlines.
        """
        if category is not None and category not in _VALID_CATEGORIES:
            logger.warning(
                "Unrecognised category=%r — API may reject this. "
                "Valid: %s", category, sorted(_VALID_CATEGORIES),
            )

        params: dict[str, str] = {}
        _add_opt(params, "category", category)
        _add_opt(params, "lang",     lang)
        _add_opt(params, "country",  country)
        _add_opt(params, "max",      str(int(max_results)) if max_results is not None else None)
        _add_opt(params, "q",        q)
        _add_opt(params, "from",     from_iso)
        _add_opt(params, "to",       to_iso)
        _add_opt(params, "page",     str(int(page)) if page is not None else None)

        if not params:
            logger.warning(
                "top_headlines() called with no filters — "
                "response will be generic global headlines."
            )

        payload = self._request(endpoint="top-headlines", params=params)
        return _parse_response(payload, request_kind="top-headlines", query=q, request_params=params)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _request(self, *, endpoint: str, params: dict[str, str]) -> dict:
        """
        Execute GET request.

        API key is sent in the ``X-Api-Key`` header — NOT in the URL —
        to prevent accidental exposure in logs, proxy logs, or browser history.
        """
        url = f"{_BASE_URL}/{endpoint}?{urllib.parse.urlencode(params)}"
        logger.debug("GET %s/%s params=%s", _BASE_URL, endpoint, params)

        req = urllib.request.Request(
            url=url,
            method="GET",
            headers={
                "Accept":    "application/json",
                "X-Api-Key": self._api_key,
            },
        )

        try:
            with urllib.request.urlopen(
                req, timeout=self._timeout_s, context=self._ssl_ctx
            ) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            raise GNewsAPIError(
                f"HTTP {exc.code} from GNews /{endpoint}: {body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise GNewsAPIError(
                f"Network error fetching GNews /{endpoint}: {exc.reason}"
            ) from exc

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GNewsAPIError(
                f"Non-JSON response from GNews /{endpoint}"
            ) from exc

        if not isinstance(payload, dict):
            raise GNewsAPIError(
                f"Unexpected top-level type from GNews: "
                f"expected dict, got {type(payload).__name__}"
            )

        # GNews returns errors as {"errors": {...}} with 2xx status sometimes
        if "errors" in payload:
            raise GNewsAPIError(
                f"GNews API error: {payload['errors']}"
            )

        return payload


# ---------------------------------------------------------------------------
# Private: parsing
# ---------------------------------------------------------------------------

def _parse_response(
    payload: dict,
    *,
    request_kind: str,
    query: str | None,
    request_params: dict | None,
) -> GNewsResponse:
    total = _parse_int(payload.get("totalArticles", 0), field="totalArticles")

    raw_articles = payload.get("articles")
    if not isinstance(raw_articles, list):
        raise GNewsAPIError(
            f"GNews response missing 'articles' list "
            f"(got {type(raw_articles).__name__})"
        )

    articles: list[GNewsArticle] = []
    for idx, item in enumerate(raw_articles):
        if not isinstance(item, dict):
            logger.warning("Skipping articles[%d]: not a dict", idx)
            continue
        article = _normalise_article(item, idx=idx)
        if article is not None:
            articles.append(article)

    skipped = len(raw_articles) - len(articles)
    if skipped:
        logger.debug("Skipped %d articles with missing title or URL", skipped)

    return GNewsResponse(
        total_articles=total,
        articles=tuple(articles),
        request_kind=request_kind,
        query=query,
        request_params=request_params,
    )


def _normalise_article(item: dict, *, idx: int) -> GNewsArticle | None:
    """
    Normalise a raw GNews article dict into a typed ``GNewsArticle``.

    Returns ``None`` when the article is missing ``title`` or ``url``
    — these are considered malformed and are filtered out.

    Coerces empty strings to ``None`` for optional fields.
    Does NOT store the raw dict — all fields are extracted explicitly.
    """
    title = _str_or_none(item.get("title"))
    url   = _str_or_none(item.get("url"))

    if not title or not url:
        logger.debug(
            "articles[%d] filtered: missing title=%r url=%r", idx, title, url
        )
        return None

    description    = _str_or_none(item.get("description"))
    raw_content    = _str_or_none(item.get("content"))
    published_at   = _str_or_none(item.get("publishedAt"))
    image_url      = _str_or_none(item.get("image"))

    # Detect GNews free-tier content truncation
    content_truncated = (
        raw_content is not None and _TRUNCATION_MARKER in raw_content
    )

    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    source_name = _str_or_none(source.get("name"))
    source_url  = _str_or_none(source.get("url"))

    return GNewsArticle(
        title=title,
        url=url,
        description=description,
        content=raw_content,
        content_truncated=content_truncated,
        published_at_utc=published_at,
        source_name=source_name,
        source_url=source_url,
        image_url=image_url,
    )


# ---------------------------------------------------------------------------
# Private: SSL and utilities
# ---------------------------------------------------------------------------

def _build_ssl_context(
    *,
    ca_bundle: Path | None,
    insecure: bool,
) -> ssl.SSLContext:
    """
    Always return an explicit SSLContext.

    When insecure=True: check_hostname must be disabled BEFORE
    setting verify_mode=CERT_NONE (Python raises ValueError otherwise).
    """
    if insecure:
        logger.warning(
            "TLS verification DISABLED for GNews API. "
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


def _add_opt(params: dict[str, str], key: str, value: str | None) -> None:
    """Add *key* to *params* only when *value* is non-empty."""
    if value is not None and str(value).strip():
        params[key] = str(value).strip()


def _str_or_none(value: object) -> str | None:
    """Return stripped string or None for empty / None values."""
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _parse_int(value: object, *, field: str) -> int:
    try:
        return int(str(value))
    except (ValueError, TypeError) as exc:
        raise GNewsAPIError(
            f"Cannot parse {field}={value!r} as integer"
        ) from exc


def _is_iterable_query(value: object) -> bool:
    return isinstance(value, Iterable) and not isinstance(value, (str, bytes))


def _normalise_queries(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main() -> int:
    import os
    import sys

    from cryptogent.config.io import DEFAULT_CONFIG_PATH, load_config

    cfg_path = Path(os.environ.get("CRYPTOGENT_CONFIG") or DEFAULT_CONFIG_PATH)
    if not cfg_path.exists():
        print(f"Config not found: {cfg_path}", file=sys.stderr)
        return 2

    cfg = load_config(cfg_path)
    if not cfg.gnews_api_key:
        print(
            "Missing GNews API key in cryptogent.toml [gnews].api_key",
            file=sys.stderr,
        )
        return 2

    query = sys.argv[1].strip() if len(sys.argv) > 1 and sys.argv[1].strip() else "bitcoin"

    client = GNewsClient(api_key=cfg.gnews_api_key, timeout_s=10.0)
    resp   = client.search(q=query, lang="en", max_results=10)

    print(f"total={resp.total_articles}  returned={len(resp.articles)}")
    for a in resp.articles:
        trunc = " [truncated]" if a.content_truncated else ""
        print(f"  [{a.source_name or '?'}] {a.title}{trunc}")
        print(f"    {a.url}")
        if a.description:
            print(f"    desc: {a.description}")
        if a.content:
            preview = a.content.strip()
            if len(preview) > 400:
                preview = preview[:400].rstrip() + "..."
            print(f"    content: {preview}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
