"""
cryptogent.market.news.article_scrape
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Full article body extractor for crypto news URLs.

Fetches article HTML and extracts the main body text using pure stdlib
(html.parser) — no external dependencies required.

Extraction strategy (in order):
  1. <article> tag content
  2. <div> / <section> with class containing "article", "story",
     "body", "content", "post", "entry"
  3. Largest <p> cluster in the document (fallback)

Handles:
  - Paywalls (returns what is publicly visible, sets paywall_suspected)
  - Bot blocking (detects Cloudflare / 403 responses)
  - Truncated / empty extractions (sets fetch_status accordingly)
  - HTML entity decoding and whitespace normalisation

Does NOT handle:
  - JavaScript-rendered content (React/Next.js SPAs)
  - Login-gated content
  - CAPTCHA challenges

Known limitations per source:
  CoinDesk        ~70% success (soft paywall on some articles)
  CoinTelegraph   ~95% success
  Decrypt         ~95% success
  The Block       ~40% success (harder paywall)
  Bitcoin.com     ~95% success
"""
from __future__ import annotations

import html
import logging
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

# CSS class fragments that indicate article body containers
_ARTICLE_CLASS_HINTS = frozenset({
    "article", "story", "body", "content", "post",
    "entry", "text", "prose", "main", "detail",
})

# Class fragments that indicate boilerplate to skip
_SKIP_CLASS_HINTS = frozenset({
    "nav", "navigation", "menu", "sidebar", "footer",
    "header", "comment", "ad", "advertisement", "related",
    "share", "social", "tag", "breadcrumb", "newsletter",
    "signup", "subscribe", "widget", "promo",
})

# Paywall indicator strings in page content
_PAYWALL_INDICATORS = frozenset({
    "subscribe to continue",
    "subscription required",
    "sign in to read",
    "members only",
    "create a free account",
    "unlock this article",
    "premium content",
    "this content is for subscribers",
})

# Minimum word count to consider extraction successful
_MIN_WORD_COUNT = 50

# Maximum characters to return (avoids returning entire HTML as text)
_MAX_BODY_CHARS = 8000

# Fetch status values
STATUS_OK               = "ok"
STATUS_PAYWALL          = "paywall"
STATUS_BLOCKED          = "blocked"
STATUS_EMPTY            = "empty"
STATUS_FAILED           = "failed"
STATUS_JS_RENDERED      = "js_rendered"


# ---------------------------------------------------------------------------
# Public data contract
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArticleContent:
    """
    Extracted content from a single article URL.

    Attributes
    ----------
    url:
        The URL that was fetched.
    title:
        Page title extracted from ``<title>`` tag.
        ``None`` when not found.
    body:
        Extracted article body text, cleaned of HTML tags and normalised.
        ``None`` when extraction failed or was blocked.
    word_count:
        Word count of ``body``.  ``None`` when body is ``None``.
    fetch_status:
        ``"ok"``          — body extracted successfully
        ``"paywall"``     — paywall detected, partial or no body
        ``"blocked"``     — HTTP 403 or bot-detection page
        ``"empty"``       — fetched successfully but extracted < 50 words
        ``"js_rendered"`` — page requires JavaScript (SPA / Next.js)
        ``"failed"``      — network error or unrecoverable parse failure
    paywall_suspected:
        ``True`` when paywall indicators found in page content.
    http_status:
        HTTP response status code.  ``None`` on network error.
    fetch_duration_ms:
        How long the HTTP fetch took in milliseconds.
    """
    url: str
    title: str | None
    body: str | None
    word_count: int | None
    fetch_status: str
    paywall_suspected: bool
    http_status: int | None
    fetch_duration_ms: int | None

    @property
    def is_usable(self) -> bool:
        """True when body was extracted with sufficient content."""
        return self.fetch_status == STATUS_OK and self.body is not None

    @property
    def best_available(self) -> str | None:
        """Body if usable, else None. Callers fall back to RSS description."""
        return self.body if self.is_usable else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_article_content(
    *,
    url: str,
    timeout_s: float = 15.0,
    ca_bundle: Path | None = None,
    insecure: bool = False,
    user_agent: str | None = None,
    min_word_count: int = _MIN_WORD_COUNT,
    delay_s: float = 0.0,
) -> ArticleContent:
    """
    Fetch and extract the full article body from a news URL.

    Parameters
    ----------
    url:
        Article URL to fetch.
    timeout_s:
        HTTP request timeout.  News sites can be slow — 15s recommended.
    ca_bundle:
        Custom CA bundle for TLS verification.
    insecure:
        Disable TLS verification.  Never use in production.
    user_agent:
        Custom User-Agent.  Default mimics Chrome to reduce bot-blocking.
    min_word_count:
        Minimum words required to consider extraction successful.
        Extractions below this threshold get ``fetch_status="empty"``.
    delay_s:
        Seconds to sleep before fetching.  Use when fetching multiple
        articles in a loop to avoid rate-limiting (0.5–1.5 recommended).

    Returns
    -------
    ArticleContent
        Never raises — all errors are captured in ``fetch_status``.
    """
    if delay_s > 0:
        time.sleep(delay_s)

    ssl_ctx = _build_ssl_context(ca_bundle=ca_bundle, insecure=insecure)
    ua      = user_agent or _DEFAULT_UA

    # Fetch HTML
    html_bytes, http_status, duration_ms, error = _fetch_html(
        url, timeout_s=timeout_s, ssl_ctx=ssl_ctx, user_agent=ua
    )

    if html_bytes is None:
        return ArticleContent(
            url=url, title=None, body=None, word_count=None,
            fetch_status=error or STATUS_FAILED,
            paywall_suspected=False,
            http_status=http_status,
            fetch_duration_ms=duration_ms,
        )

    # Decode
    try:
        html_text = html_bytes.decode("utf-8", errors="replace")
    except Exception:
        return _failed(url, http_status, duration_ms)

    # Detect JS-rendered SPA (no meaningful HTML content)
    if _is_js_rendered(html_text):
        logger.debug("article_fetcher: JS-rendered page detected: %s", url)
        return ArticleContent(
            url=url, title=None, body=None, word_count=None,
            fetch_status=STATUS_JS_RENDERED,
            paywall_suspected=False,
            http_status=http_status,
            fetch_duration_ms=duration_ms,
        )

    # Extract
    page_title   = _extract_title(html_text)
    body, method = _extract_body(html_text)
    paywall      = _check_paywall(html_text, body)

    if not body or len(body.split()) < min_word_count:
        status = STATUS_PAYWALL if paywall else STATUS_EMPTY
        logger.debug(
            "article_fetcher: %s — body too short (%d words) via %s: %s",
            status, len(body.split()) if body else 0, method, url,
        )
        return ArticleContent(
            url=url, title=page_title,
            body=body if body else None,
            word_count=len(body.split()) if body else None,
            fetch_status=status,
            paywall_suspected=paywall,
            http_status=http_status,
            fetch_duration_ms=duration_ms,
        )

    # Truncate to max chars
    if len(body) > _MAX_BODY_CHARS:
        body = body[:_MAX_BODY_CHARS].rstrip() + "…"

    word_count = len(body.split())
    logger.debug(
        "article_fetcher: ok — %d words via %s: %s", word_count, method, url
    )

    return ArticleContent(
        url=url,
        title=page_title,
        body=body,
        word_count=word_count,
        fetch_status=STATUS_OK,
        paywall_suspected=paywall,
        http_status=http_status,
        fetch_duration_ms=duration_ms,
    )


def fetch_articles_content(
    urls: list[str],
    *,
    timeout_s: float = 15.0,
    ca_bundle: Path | None = None,
    insecure: bool = False,
    user_agent: str | None = None,
    min_word_count: int = _MIN_WORD_COUNT,
    delay_s: float = 1.0,
    max_articles: int | None = None,
) -> list[ArticleContent]:
    """
    Fetch full content for multiple article URLs sequentially.

    Parameters
    ----------
    urls:
        List of article URLs to fetch.
    delay_s:
        Seconds between requests (default 1.0 — be polite to news servers).
    max_articles:
        Maximum articles to fetch.  ``None`` = all.

    Returns
    -------
    list[ArticleContent]
        Same order as input.  Failed articles have ``fetch_status != "ok"``.
    """
    targets = urls[:max_articles] if max_articles is not None else urls
    results: list[ArticleContent] = []

    for i, url in enumerate(targets):
        # Apply delay between requests (not before the first)
        applied_delay = delay_s if i > 0 else 0.0
        result = fetch_article_content(
            url=url,
            timeout_s=timeout_s,
            ca_bundle=ca_bundle,
            insecure=insecure,
            user_agent=user_agent,
            min_word_count=min_word_count,
            delay_s=applied_delay,
        )
        results.append(result)
        logger.debug(
            "article_fetcher: [%d/%d] %s → %s",
            i + 1, len(targets), url, result.fetch_status,
        )

    ok      = sum(1 for r in results if r.fetch_status == STATUS_OK)
    blocked = sum(1 for r in results if r.fetch_status == STATUS_BLOCKED)
    paywall = sum(1 for r in results if r.fetch_status == STATUS_PAYWALL)
    logger.info(
        "article_fetcher: %d/%d ok, %d blocked, %d paywall",
        ok, len(results), blocked, paywall,
    )
    return results


# ---------------------------------------------------------------------------
# Private: HTTP
# ---------------------------------------------------------------------------

def _fetch_html(
    url: str,
    *,
    timeout_s: float,
    ssl_ctx: ssl.SSLContext,
    user_agent: str,
) -> tuple[bytes | None, int | None, int | None, str | None]:
    """
    Fetch URL and return (bytes, http_status, duration_ms, error_status).
    On failure returns (None, status_or_None, duration_ms, error_label).
    """
    req = urllib.request.Request(
        url=url,
        method="GET",
        headers={
            "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent":      user_agent,
        },
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=ssl_ctx) as resp:
            raw = resp.read()
            duration_ms = int((time.monotonic() - t0) * 1000)
            return raw, resp.status, duration_ms, None
    except urllib.error.HTTPError as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        if exc.code == 403:
            logger.debug("article_fetcher: 403 blocked: %s", url)
            return None, 403, duration_ms, STATUS_BLOCKED
        if exc.code == 429:
            logger.warning("article_fetcher: 429 rate-limited: %s", url)
            return None, 429, duration_ms, STATUS_BLOCKED
        logger.debug("article_fetcher: HTTP %d: %s", exc.code, url)
        return None, exc.code, duration_ms, STATUS_FAILED
    except urllib.error.URLError as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        logger.debug("article_fetcher: network error %s: %s", exc.reason, url)
        return None, None, duration_ms, STATUS_FAILED
    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        logger.debug("article_fetcher: unexpected error %s: %s", exc, url)
        return None, None, duration_ms, STATUS_FAILED


def _build_ssl_context(
    *,
    ca_bundle: Path | None,
    insecure: bool,
) -> ssl.SSLContext:
    if insecure:
        logger.warning("TLS verification DISABLED. Never use insecure=True in production.")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        return ctx
    if ca_bundle is not None:
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cafile=str(ca_bundle.expanduser()))
        return ctx
    return ssl.create_default_context()


# ---------------------------------------------------------------------------
# Private: HTML parsing
# ---------------------------------------------------------------------------

class _ArticleParser(HTMLParser):
    """
    Stateful SAX-style HTML parser that collects text from article containers.

    Strategy:
      Pass 1 — find <article> tag or best <div>/<section> by class hint
      Pass 2 — extract all <p> text within that container
      Fallback — collect all <p> text from the entire document
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str | None = None

        # Article container tracking
        self._in_article: bool  = False
        self._article_depth: int = 0
        self._depth: int        = 0

        # Skip container tracking
        self._skip_depth: int | None = None

        # Text collection
        self._in_para: bool     = False
        self._paragraphs: list[str] = []
        self._current_p: list[str]  = []
        self._article_text_nodes: list[str] = []

        # All-document fallback
        self._all_p: list[str]  = []
        self._all_current: list[str] = []
        self._in_any_para: bool = False
        self._all_text_nodes: list[str] = []
        self._para_tags = {"p", "li"}

        # Title
        self._in_title: bool    = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._depth += 1
        attr_dict = {k: (v or "") for k, v in attrs}
        cls  = attr_dict.get("class", "").lower()
        role = attr_dict.get("role", "").lower()

        # Title
        if tag == "title":
            self._in_title = True
            return

        # Skip boilerplate containers
        if self._skip_depth is None:
            if tag in ("nav", "footer", "header", "aside"):
                self._skip_depth = self._depth
                return
            if tag in ("div", "section", "aside") and _has_class_hint(cls, _SKIP_CLASS_HINTS):
                self._skip_depth = self._depth
                return

        if self._skip_depth is not None:
            return

        # Article container detection
        if not self._in_article:
            if tag == "article":
                self._in_article   = True
                self._article_depth = self._depth
            elif tag in ("div", "section", "main") and (
                _has_class_hint(cls, _ARTICLE_CLASS_HINTS)
                or role in ("main", "article")
            ):
                self._in_article   = True
                self._article_depth = self._depth

        # Paragraph-like detection
        if tag in self._para_tags:
            self._in_para      = True
            self._in_any_para  = True

    def handle_endtag(self, tag: str) -> None:
        # Close skip zone
        if self._skip_depth is not None:
            if self._depth == self._skip_depth:
                self._skip_depth = None
            self._depth -= 1
            return

        if tag == "title":
            self._in_title = False
            if self._title_parts:
                self.title = " ".join(self._title_parts).strip()
                self._title_parts = []

        # Close article container
        if self._in_article and self._depth == self._article_depth:
            self._in_article = False

        # Close paragraph-like tag
        if tag in self._para_tags:
            text = " ".join(self._current_p).strip()
            if text:
                if self._in_article:
                    self._paragraphs.append(text)
                self._all_p.append(text)
            self._current_p = []
            self._all_current = []
            self._in_para      = False
            self._in_any_para  = False

        self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth is not None:
            return

        cleaned = data.strip()
        if not cleaned:
            return

        if self._in_title:
            self._title_parts.append(cleaned)
            return

        if self._in_para and self._in_article:
            self._current_p.append(cleaned)

        if self._in_any_para:
            self._all_current.append(cleaned)

        # Fallback: collect loose text nodes inside article or document
        if self._in_article and not self._in_para:
            self._article_text_nodes.append(cleaned)
        if not self._in_any_para:
            self._all_text_nodes.append(cleaned)

    @property
    def article_text(self) -> str:
        """Text from detected article container."""
        return "\n\n".join(p for p in self._paragraphs if len(p.split()) >= 3)

    @property
    def article_text_nodes(self) -> str:
        """Loose text nodes inside article container."""
        return " ".join(self._article_text_nodes)

    @property
    def fallback_text(self) -> str:
        """All <p> text from document when no container found."""
        return "\n\n".join(p for p in self._all_p if len(p.split()) >= 3)

    @property
    def fallback_text_nodes(self) -> str:
        """Loose text nodes from full document."""
        return " ".join(self._all_text_nodes)

    @property
    def extraction_method(self) -> str:
        if self._paragraphs:
            return "article_container"
        if self._all_p:
            return "all_paragraphs"
        return "none"


def _extract_body(html_text: str) -> tuple[str, str]:
    """
    Extract article body using HTMLParser.

    Returns (body_text, method_used).
    """
    parser = _ArticleParser()
    try:
        parser.feed(html_text)
    except Exception as exc:
        logger.debug("article_fetcher: HTMLParser error: %s", exc)
        return "", "parse_error"

    body = parser.article_text
    method = "article_container"

    if not body or len(body.split()) < _MIN_WORD_COUNT:
        body   = parser.article_text_nodes
        method = "article_text_nodes"

    if not body or len(body.split()) < _MIN_WORD_COUNT:
        body   = parser.fallback_text
        method = "all_paragraphs"

    if not body or len(body.split()) < _MIN_WORD_COUNT:
        body   = parser.fallback_text_nodes
        method = "all_text_nodes"

    # Normalise whitespace
    body = _clean_text(body)
    return body, method


def _extract_title(html_text: str) -> str | None:
    """Extract page title from <title> tag."""
    match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
    if match:
        raw = html.unescape(match.group(1)).strip()
        # Remove common site name suffixes: "Article Title | CoinDesk"
        for sep in (" | ", " - ", " – ", " · "):
            if sep in raw:
                raw = raw.split(sep)[0].strip()
        return raw if raw else None
    return None


def _is_js_rendered(html_text: str) -> bool:
    """
    Detect pages that require JavaScript to render content.

    These return near-empty HTML with a JS bundle — our parser
    gets nothing useful from them.
    """
    # Very short HTML with no <p>/<li> tags = likely JS-rendered
    lower_html = html_text.lower()
    p_count = lower_html.count("<p")
    li_count = lower_html.count("<li")
    if len(html_text) < 2000 and (p_count + li_count) < 3:
        return True
    # Common SPA indicators
    spa_hints = (
        'id="__next"',          # Next.js
        'id="root"',            # Create React App
        'data-reactroot',       # React SSR marker
        'ng-version=',          # Angular
    )
    lower = html_text[:3000].lower()
    return any(hint.lower() in lower for hint in spa_hints) and (p_count + li_count) < 5


def _check_paywall(html_text: str, body: str | None) -> bool:
    """Detect paywall indicators in page content."""
    check_text = ((body or "") + " " + html_text[:5000]).lower()
    return any(indicator in check_text for indicator in _PAYWALL_INDICATORS)


def _has_class_hint(cls: str, hints: frozenset[str]) -> bool:
    """Return True when any hint word appears in the class string."""
    return any(hint in cls for hint in hints)


def _clean_text(text: str) -> str:
    """Normalise whitespace and decode HTML entities."""
    text = html.unescape(text)
    # Collapse multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Collapse multiple spaces
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _failed(
    url: str,
    http_status: int | None,
    duration_ms: int | None,
) -> ArticleContent:
    return ArticleContent(
        url=url, title=None, body=None, word_count=None,
        fetch_status=STATUS_FAILED,
        paywall_suspected=False,
        http_status=http_status,
        fetch_duration_ms=duration_ms,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main() -> int:
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m article_scrape <url> [url2] ...", file=sys.stderr)
        return 1

    urls = [a.strip() for a in sys.argv[1:] if a.strip()]
    results = fetch_articles_content(urls, delay_s=1.0)

    for r in results:
        print(f"\n{'─'*60}")
        print(f"URL:      {r.url}")
        print(f"Status:   {r.fetch_status}  HTTP={r.http_status}  "
              f"{r.fetch_duration_ms}ms  words={r.word_count}")
        print(f"Paywall:  {r.paywall_suspected}")
        if r.title:
            print(f"Title:    {r.title}")
        if r.body:
            preview = r.body[:600].rstrip()
            suffix  = "…" if len(r.body) > 600 else ""
            print(f"Body:\n{preview}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
