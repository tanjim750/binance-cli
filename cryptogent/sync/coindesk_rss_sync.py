from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from cryptogent.market.news.coindesk.coindesk_rss import CoinDeskRSSParseError, fetch_coindesk_rss
from cryptogent.market.news.article_scrape import fetch_article_content
from cryptogent.state.manager import StateManager
from cryptogent.util.time import parse_utc_iso, utcnow_iso


@dataclass(frozen=True)
class CoinDeskSyncResult:
    kind: str
    status: str
    articles_saved: int = 0


def sync_coindesk_rss(
    *,
    conn,
    feed_url: str | None = None,
    ca_bundle: Path | None = None,
    insecure: bool = False,
    timeout_s: float = 10.0,
    cache_ttl_s: int = 900,
    fetch_full: bool = True,
    detail_timeout_s: float = 8.0,
) -> CoinDeskSyncResult:
    state = StateManager(conn)
    request_params = {"feed_url": str(feed_url or "").strip()} if feed_url else {}

    if cache_ttl_s > 0:
        try:
            request_params_json = json.dumps(request_params, separators=(",", ":"))
            latest = state.get_latest_news_request(
                provider="coindesk",
                request_kind="rss",
                request_params_json=request_params_json,
            )
            if latest and latest.get("fetched_at_utc"):
                updated_at = parse_utc_iso(str(latest["fetched_at_utc"]))
                now = parse_utc_iso(utcnow_iso())
                if (now - updated_at).total_seconds() < cache_ttl_s:
                    state.append_audit(
                        level="INFO",
                        event="sync_coindesk_rss_cached",
                        details={"cache_ttl_s": cache_ttl_s, "fetched_at_utc": latest.get("fetched_at_utc")},
                    )
                    return CoinDeskSyncResult(kind="coindesk_rss", status="ok", articles_saved=0)
        except Exception:
            pass

    sync_id = state.record_sync_run_start(kind="coindesk_rss")
    try:
        resp = fetch_coindesk_rss(
            feed_url=feed_url,
            timeout_s=timeout_s,
            ca_bundle=ca_bundle,
            insecure=insecure,
        )
        articles = _coindesk_enrich_articles(
            resp.articles,
            fetch_full=fetch_full,
            detail_timeout_s=detail_timeout_s,
            ca_bundle=ca_bundle,
            insecure=insecure,
        )
        saved = state.upsert_news_articles(
            provider="coindesk",
            request_kind=resp.request_kind,
            request_params=resp.request_params,
            articles=articles,
        )
        state.append_audit(
            level="INFO",
            event="sync_coindesk_rss_ok",
            details={"saved": saved},
        )
        state.record_sync_run_finish(sync_run_id=sync_id, status="ok", error_msg=None)
        return CoinDeskSyncResult(kind="coindesk_rss", status="ok", articles_saved=saved)
    except CoinDeskRSSParseError as exc:
        state.append_audit(level="ERROR", event="sync_coindesk_rss_error", details={"error": str(exc)})
        state.record_sync_run_finish(sync_run_id=sync_id, status="error", error_msg=str(exc))
        return CoinDeskSyncResult(kind="coindesk_rss", status="error")
    except Exception as exc:
        state.append_audit(level="ERROR", event="sync_coindesk_rss_error", details={"error": str(exc)})
        state.record_sync_run_finish(sync_run_id=sync_id, status="error", error_msg=str(exc))
        return CoinDeskSyncResult(kind="coindesk_rss", status="error")


def _coindesk_enrich_articles(
    articles: tuple[dict, ...],
    *,
    fetch_full: bool,
    detail_timeout_s: float,
    ca_bundle: Path | None,
    insecure: bool,
) -> list[dict]:
    out: list[dict] = []
    for a in articles:
        content = a.get("content")
        content_source = "rss"
        url = str(a.get("url") or "").strip()
        if fetch_full and url:
            try:
                detail = fetch_article_content(
                    url=url,
                    timeout_s=detail_timeout_s,
                    ca_bundle=ca_bundle,
                    insecure=insecure,
                )
                if detail.body:
                    content = detail.body
                    content_source = detail.fetch_status
            except Exception:
                pass
        merged = dict(a)
        raw_json = dict(a.get("raw_json") or {})
        raw_json["content_source"] = content_source
        merged["content"] = content
        merged["raw_json"] = raw_json
        out.append(merged)
    return out
