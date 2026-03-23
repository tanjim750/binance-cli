"""
cryptogent.market.news.reddit_rss
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Reddit RSS fetcher for subreddits.

Example:
  https://www.reddit.com/r/bitcoin/new/.rss
"""
from __future__ import annotations

import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

from cryptogent.util.time import s_to_utc_iso

logger = logging.getLogger(__name__)

_DEFAULT_USER_AGENT = "cryptogent/1.0 (reddit rss)"


class RedditRSSError(RuntimeError):
    pass


@dataclass(frozen=True)
class RedditRSSPost:
    id: str
    title: str
    url: str
    author: str | None
    subreddit: str | None
    published_at_utc: str | None
    content_text: str | None
    source: str
    raw: dict


@dataclass(frozen=True)
class RedditRSSResponse:
    request_kind: str
    request_params: dict
    posts: tuple[RedditRSSPost, ...]


def fetch_reddit_rss(
    *,
    subreddit: str,
    sort: str = "new",
    limit: int = 20,
    user_agent: str | None = None,
    timeout_s: float = 10.0,
    ca_bundle: Path | None = None,
    insecure: bool = False,
) -> RedditRSSResponse:
    subreddit = str(subreddit).strip().lstrip("r/").strip("/")
    if not subreddit:
        raise RedditRSSError("subreddit is empty")
    sort = str(sort or "new").strip().lower()
    if sort not in ("new", "hot", "top", "rising"):
        sort = "new"

    url = f"https://www.reddit.com/r/{urllib.parse.quote(subreddit)}/{sort}/.rss?limit={int(limit)}"
    raw = _fetch_xml(
        url,
        timeout_s=timeout_s,
        user_agent=user_agent or _DEFAULT_USER_AGENT,
        ca_bundle=ca_bundle,
        insecure=insecure,
    )
    posts = _parse_feed(raw, subreddit=subreddit)
    return RedditRSSResponse(
        request_kind="reddit_rss",
        request_params={"subreddit": subreddit, "sort": sort, "limit": int(limit)},
        posts=tuple(posts),
    )


def _fetch_xml(
    url: str,
    *,
    timeout_s: float,
    user_agent: str,
    ca_bundle: Path | None,
    insecure: bool,
) -> bytes:
    ssl_ctx = _build_ssl_context(ca_bundle=ca_bundle, insecure=insecure)
    req = urllib.request.Request(
        url=url,
        method="GET",
        headers={
            "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
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
        raise RedditRSSError(f"HTTP {exc.code} fetching Reddit RSS: {body}") from exc
    except urllib.error.URLError as exc:
        raise RedditRSSError(f"Network error fetching Reddit RSS: {exc.reason}") from exc


def _parse_feed(raw_xml: bytes, *, subreddit: str) -> list[RedditRSSPost]:
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as exc:
        raise RedditRSSError("Invalid XML in Reddit RSS feed") from exc

    channel = _find_child(root, "channel")
    if channel is not None:
        return _parse_rss_channel(channel, subreddit=subreddit)
    # Fallback: Atom feed (common for Reddit .rss)
    if _local_name(root.tag) == "feed":
        return _parse_atom_feed(root, subreddit=subreddit)
    # Some feeds wrap <rss><channel> with namespaces; try deep search
    channel = _find_descendant(root, "channel")
    if channel is not None:
        return _parse_rss_channel(channel, subreddit=subreddit)
    raise RedditRSSError("Missing <channel> or <feed> in Reddit RSS")


def _parse_rss_channel(channel: ET.Element, *, subreddit: str) -> list[RedditRSSPost]:
    items = [c for c in channel if _local_name(c.tag) == "item"]
    posts: list[RedditRSSPost] = []
    for item in items:
        title = _child_text(item, "title")
        link = _child_text(item, "link")
        guid = _child_text(item, "guid") or link
        if not (title and link and guid):
            continue
        author = _child_text(item, "creator") or _child_text(item, "author")
        pub = _parse_pubdate(_child_text(item, "pubDate"))
        content_html = _child_text(item, "encoded") or _child_text(item, "description")
        content_text = _strip_tags(content_html) if content_html else None
        posts.append(
            RedditRSSPost(
                id=str(guid),
                title=title,
                url=link,
                author=author,
                subreddit=subreddit,
                published_at_utc=pub,
                content_text=content_text,
                source="rss",
                raw={"guid": guid, "content_html": content_html},
            )
        )
    return posts


def _parse_atom_feed(feed: ET.Element, *, subreddit: str) -> list[RedditRSSPost]:
    entries = [c for c in feed if _local_name(c.tag) == "entry"]
    posts: list[RedditRSSPost] = []
    for entry in entries:
        title = _child_text(entry, "title")
        link = _find_atom_link(entry)
        guid = _child_text(entry, "id") or link
        if not (title and link and guid):
            continue
        author = _find_atom_author(entry)
        pub = _child_text(entry, "updated") or _child_text(entry, "published")
        published_at = _parse_iso(pub) if pub else None
        content_html = _child_text(entry, "content") or _child_text(entry, "summary")
        if not content_html:
            content_html = _element_text(entry, "content") or _element_text(entry, "summary")
        content_text = _strip_tags(content_html) if content_html else None
        posts.append(
            RedditRSSPost(
                id=str(guid),
                title=title,
                url=link,
                author=author,
                subreddit=subreddit,
                published_at_utc=published_at,
                content_text=content_text,
                source="rss",
                raw={"guid": guid, "content_html": content_html},
            )
        )
    return posts


def _parse_pubdate(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=None)
    return dt.astimezone().replace(microsecond=0).isoformat()


def _strip_tags(value: str) -> str:
    out: list[str] = []
    in_tag = False
    for ch in value:
        if ch == "<":
            in_tag = True
            continue
        if ch == ">":
            in_tag = False
            continue
        if not in_tag:
            out.append(ch)
    return " ".join("".join(out).split())


def _find_child(parent: ET.Element, local_name: str) -> ET.Element | None:
    for child in parent:
        if _local_name(child.tag) == local_name:
            return child
    return None


def _child_text(parent: ET.Element, local_name: str) -> str | None:
    child = _find_child(parent, local_name)
    if child is None:
        return None
    if child.text and child.text.strip():
        return child.text.strip()
    return None


def _element_text(parent: ET.Element, local_name: str) -> str | None:
    child = _find_child(parent, local_name)
    if child is None:
        return None
    text = " ".join(t.strip() for t in child.itertext() if t and t.strip())
    return text.strip() if text else None


def _find_descendant(parent: ET.Element, local_name: str) -> ET.Element | None:
    for child in parent.iter():
        if _local_name(child.tag) == local_name:
            return child
    return None


def _find_atom_link(entry: ET.Element) -> str | None:
    for child in entry:
        if _local_name(child.tag) == "link":
            href = child.attrib.get("href")
            if href:
                return href.strip()
    return None


def _find_atom_author(entry: ET.Element) -> str | None:
    for child in entry:
        if _local_name(child.tag) == "author":
            name = _child_text(child, "name")
            return name or None
    return None


def _parse_iso(value: str) -> str | None:
    if not value:
        return None
    try:
        # Normalize Z suffix
        value = value.replace("Z", "+00:00")
        dt = parsedate_to_datetime(value)
    except Exception:
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(value)
        except Exception:
            return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone().replace(microsecond=0).isoformat()


def _local_name(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


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
    import sys

    subreddit = sys.argv[1].strip() if len(sys.argv) > 1 and sys.argv[1].strip() else "bitcoin"
    resp = fetch_reddit_rss(subreddit=subreddit)
    print(f"returned={len(resp.posts)} subreddit={subreddit}")
    for p in resp.posts[:10]:
        print(f"  {p.published_at_utc or '?'} [{p.subreddit}] {p.title}")
        print(f"    {p.url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
