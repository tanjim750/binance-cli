from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from cryptogent.market.news.binance.binance_announcements import (
    BinanceAnnouncementsError,
    BinanceAnnouncement,
    fetch_binance_announcements,
    fetch_binance_announcement_detail,
)
from cryptogent.state.manager import StateManager
from cryptogent.util.time import parse_utc_iso, utcnow_iso


@dataclass(frozen=True)
class BinanceAnnouncementsSyncResult:
    kind: str
    status: str
    articles_saved: int = 0


def sync_binance_announcements(
    *,
    conn,
    catalog_id: int = 48,
    type: int = 1,
    page_no: int = 1,
    page_size: int = 20,
    locale: str = "en",
    ca_bundle: Path | None = None,
    insecure: bool = False,
    timeout_s: float = 10.0,
    cache_ttl_s: int = 1800,
    fetch_full: bool = True,
    detail_timeout_s: float = 12.0,
) -> BinanceAnnouncementsSyncResult:
    state = StateManager(conn)
    params_preview = {
        "catalogId": str(int(catalog_id)),
        "type": str(int(type)),
        "pageNo": str(int(page_no)),
        "pageSize": str(int(page_size)),
        "locale": str(locale),
    }

    if cache_ttl_s > 0:
        try:
            request_params_json = json.dumps(params_preview, separators=(",", ":"))
            latest = state.get_latest_news_request(
                provider="binance",
                request_kind="binance_announcements",
                request_params_json=request_params_json,
            )
            if latest and latest.get("fetched_at_utc"):
                updated_at = parse_utc_iso(str(latest["fetched_at_utc"]))
                now = parse_utc_iso(utcnow_iso())
                if (now - updated_at).total_seconds() < cache_ttl_s:
                    state.append_audit(
                        level="INFO",
                        event="sync_binance_announcements_cached",
                        details={"cache_ttl_s": cache_ttl_s, "fetched_at_utc": latest.get("fetched_at_utc")},
                    )
                    return BinanceAnnouncementsSyncResult(kind="binance_announcements", status="ok", articles_saved=0)
        except Exception:
            pass

    sync_id = state.record_sync_run_start(kind="binance_announcements")
    try:
        resp = fetch_binance_announcements(
            catalog_id=catalog_id,
            type=type,
            page_no=page_no,
            page_size=page_size,
            locale=locale,
            timeout_s=timeout_s,
            ca_bundle=ca_bundle,
            insecure=insecure,
        )
        articles = _binance_to_records(
            resp.announcements,
            fetch_full=fetch_full,
            detail_timeout_s=detail_timeout_s,
            ca_bundle=ca_bundle,
            insecure=insecure,
        )
        saved = state.upsert_news_articles(
            provider="binance",
            request_kind=resp.request_kind,
            request_params=resp.request_params,
            articles=articles,
        )
        state.append_audit(
            level="INFO",
            event="sync_binance_announcements_ok",
            details={"saved": saved},
        )
        state.record_sync_run_finish(sync_run_id=sync_id, status="ok", error_msg=None)
        return BinanceAnnouncementsSyncResult(kind="binance_announcements", status="ok", articles_saved=saved)
    except BinanceAnnouncementsError as exc:
        state.append_audit(level="ERROR", event="sync_binance_announcements_error", details={"error": str(exc)})
        state.record_sync_run_finish(sync_run_id=sync_id, status="error", error_msg=str(exc))
        return BinanceAnnouncementsSyncResult(kind="binance_announcements", status="error")
    except Exception as exc:
        state.append_audit(level="ERROR", event="sync_binance_announcements_error", details={"error": str(exc)})
        state.record_sync_run_finish(sync_run_id=sync_id, status="error", error_msg=str(exc))
        return BinanceAnnouncementsSyncResult(kind="binance_announcements", status="error")


def _binance_to_records(
    announcements: tuple[BinanceAnnouncement, ...],
    *,
    fetch_full: bool,
    detail_timeout_s: float,
    ca_bundle: Path | None,
    insecure: bool,
) -> list[dict]:
    records: list[dict] = []
    for a in announcements:
        content = None
        content_source = "none"
        if fetch_full:
            try:
                detail = fetch_binance_announcement_detail(
                    code=a.code,
                    catalog_id=a.catalog_id,
                    locale="en",
                    timeout_s=detail_timeout_s,
                    ca_bundle=ca_bundle,
                    insecure=insecure,
                )
                if detail and detail.content_text:
                    content = detail.content_text
                    content_source = "detail_api"
            except Exception:
                pass
        records.append(
            {
                "provider_article_id": a.code,
                "title": a.title,
                "description": None,
                "content": content,
                "url": a.url,
                "image_url": None,
                "published_at_utc": a.release_date_utc,
                "lang": "en",
                "source_id": None,
                "source_name": "Binance",
                "source_url": "https://www.binance.com/en/support/announcement/",
                "source_country": None,
                "raw_json": {
                    "catalog_id": a.catalog_id,
                    "catalog_name": a.catalog_name,
                    "content_source": content_source,
                },
            }
        )
    return records
