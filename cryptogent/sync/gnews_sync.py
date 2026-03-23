from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from cryptogent.market.news.gnews.gnews import GNewsAPIError, GNewsClient, GNewsArticle
from cryptogent.market.news.article_scrape import fetch_article_content
from cryptogent.state.manager import StateManager
from cryptogent.util.time import parse_utc_iso, utcnow_iso


@dataclass(frozen=True)
class GNewsSyncResult:
    kind: str
    status: str
    articles_saved: int = 0
    total_articles: int = 0


def sync_gnews_search(
    *,
    conn,
    q: str,
    api_key: str | None,
    lang: str | None = None,
    country: str | None = None,
    max_results: int | None = None,
    in_fields: str | None = None,
    nullable: str | None = None,
    from_iso: str | None = None,
    to_iso: str | None = None,
    sortby: str | None = None,
    page: int | None = None,
    truncate: str | None = None,
    ca_bundle: Path | None = None,
    insecure: bool = False,
    timeout_s: float = 10.0,
    cache_ttl_s: int = 3600,
    fetch_full: bool = True,
    detail_timeout_s: float = 8.0,
) -> GNewsSyncResult:
    state = StateManager(conn)
    params_preview: dict[str, str] = {"q": str(q).strip()}
    if lang:
        params_preview["lang"] = str(lang).strip()
    if country:
        params_preview["country"] = str(country).strip()
    if max_results is not None:
        params_preview["max"] = str(int(max_results))
    if in_fields:
        params_preview["in"] = str(in_fields).strip()
    if nullable:
        params_preview["nullable"] = str(nullable).strip()
    if from_iso:
        params_preview["from"] = str(from_iso).strip()
    if to_iso:
        params_preview["to"] = str(to_iso).strip()
    if sortby:
        params_preview["sortby"] = str(sortby).strip()
    if page is not None:
        params_preview["page"] = str(int(page))
    if truncate:
        params_preview["truncate"] = str(truncate).strip()

    if cache_ttl_s > 0:
        try:
            request_params_json = json.dumps(params_preview, separators=(",", ":"))
            latest = state.get_latest_news_request(
                provider="gnews",
                request_kind="search",
                request_params_json=request_params_json,
            )
            if latest and latest.get("fetched_at_utc"):
                updated_at = parse_utc_iso(str(latest["fetched_at_utc"]))
                now = parse_utc_iso(utcnow_iso())
                if (now - updated_at).total_seconds() < cache_ttl_s:
                    state.append_audit(
                        level="INFO",
                        event="sync_gnews_search_cached",
                        details={"cache_ttl_s": cache_ttl_s, "fetched_at_utc": latest.get("fetched_at_utc")},
                    )
                    return GNewsSyncResult(kind="gnews_search", status="ok", articles_saved=0, total_articles=0)
        except Exception:
            pass
    sync_id = state.record_sync_run_start(kind="gnews_search")
    try:
        client = GNewsClient(api_key=api_key, timeout_s=timeout_s, ca_bundle=ca_bundle, insecure=insecure)
        resp = client.search(
            q=q,
            lang=lang,
            country=country,
            max_results=max_results,
            in_fields=in_fields,
            nullable=nullable,
            from_iso=from_iso,
            to_iso=to_iso,
            sortby=sortby,
            page=page,
            truncate=truncate,
        )
        articles = _gnews_to_records(
            resp.articles,
            fetch_full=fetch_full,
            detail_timeout_s=detail_timeout_s,
            ca_bundle=ca_bundle,
            insecure=insecure,
        )
        saved = state.upsert_news_articles(
            provider="gnews",
            request_kind=resp.request_kind,
            request_params=resp.request_params,
            articles=articles,
        )
        state.append_audit(
            level="INFO",
            event="sync_gnews_search_ok",
            details={"saved": saved, "total": resp.total_articles},
        )
        state.record_sync_run_finish(sync_run_id=sync_id, status="ok", error_msg=None)
        return GNewsSyncResult(
            kind="gnews_search",
            status="ok",
            articles_saved=saved,
            total_articles=resp.total_articles,
        )
    except GNewsAPIError as exc:
        state.append_audit(level="ERROR", event="sync_gnews_search_error", details={"error": str(exc)})
        state.record_sync_run_finish(sync_run_id=sync_id, status="error", error_msg=str(exc))
        return GNewsSyncResult(kind="gnews_search", status="error")
    except Exception as exc:
        state.append_audit(level="ERROR", event="sync_gnews_search_error", details={"error": str(exc)})
        state.record_sync_run_finish(sync_run_id=sync_id, status="error", error_msg=str(exc))
        return GNewsSyncResult(kind="gnews_search", status="error")


def sync_gnews_top_headlines(
    *,
    conn,
    api_key: str | None,
    category: str | None = None,
    lang: str | None = None,
    country: str | None = None,
    max_results: int | None = None,
    nullable: str | None = None,
    from_iso: str | None = None,
    to_iso: str | None = None,
    q: str | None = None,
    page: int | None = None,
    truncate: str | None = None,
    ca_bundle: Path | None = None,
    insecure: bool = False,
    timeout_s: float = 10.0,
    fetch_full: bool = True,
    detail_timeout_s: float = 8.0,
) -> GNewsSyncResult:
    state = StateManager(conn)
    sync_id = state.record_sync_run_start(kind="gnews_top_headlines")
    try:
        client = GNewsClient(api_key=api_key, timeout_s=timeout_s, ca_bundle=ca_bundle, insecure=insecure)
        resp = client.top_headlines(
            category=category,
            lang=lang,
            country=country,
            max_results=max_results,
            nullable=nullable,
            from_iso=from_iso,
            to_iso=to_iso,
            q=q,
            page=page,
            truncate=truncate,
        )
        articles = _gnews_to_records(
            resp.articles,
            fetch_full=fetch_full,
            detail_timeout_s=detail_timeout_s,
            ca_bundle=ca_bundle,
            insecure=insecure,
        )
        saved = state.upsert_news_articles(
            provider="gnews",
            request_kind=resp.request_kind,
            request_params=resp.request_params,
            articles=articles,
        )
        state.append_audit(
            level="INFO",
            event="sync_gnews_top_headlines_ok",
            details={"saved": saved, "total": resp.total_articles},
        )
        state.record_sync_run_finish(sync_run_id=sync_id, status="ok", error_msg=None)
        return GNewsSyncResult(
            kind="gnews_top_headlines",
            status="ok",
            articles_saved=saved,
            total_articles=resp.total_articles,
        )
    except GNewsAPIError as exc:
        state.append_audit(level="ERROR", event="sync_gnews_top_headlines_error", details={"error": str(exc)})
        state.record_sync_run_finish(sync_run_id=sync_id, status="error", error_msg=str(exc))
        return GNewsSyncResult(kind="gnews_top_headlines", status="error")
    except Exception as exc:
        state.append_audit(level="ERROR", event="sync_gnews_top_headlines_error", details={"error": str(exc)})
        state.record_sync_run_finish(sync_run_id=sync_id, status="error", error_msg=str(exc))
        return GNewsSyncResult(kind="gnews_top_headlines", status="error")


def _gnews_to_records(
    articles: tuple[GNewsArticle, ...],
    *,
    fetch_full: bool,
    detail_timeout_s: float,
    ca_bundle: Path | None,
    insecure: bool,
) -> list[dict]:
    records: list[dict] = []
    for a in articles:
        content = a.content
        content_source = "gnews"
        if fetch_full and a.url:
            try:
                detail = fetch_article_content(
                    url=a.url,
                    timeout_s=detail_timeout_s,
                    ca_bundle=ca_bundle,
                    insecure=insecure,
                )
                if detail.body:
                    content = detail.body
                    content_source = detail.fetch_status
            except Exception:
                pass
        records.append(
            {
                "provider_article_id": a.url,
                "title": a.title,
                "description": a.description,
                "content": content,
                "url": a.url,
                "image_url": a.image_url,
                "published_at_utc": a.published_at_utc,
                "lang": None,
                "source_id": None,
                "source_name": a.source_name,
                "source_url": a.source_url,
                "source_country": None,
                "raw_json": {
                    "content_truncated": a.content_truncated,
                    "content_source": content_source,
                },
            }
        )
    return records
