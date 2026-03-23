from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cryptogent.market.news.fear_greed import FearGreedAPIError, fetch_fear_greed
from cryptogent.state.manager import StateManager
from cryptogent.util.time import parse_utc_iso, utcnow_iso


@dataclass(frozen=True)
class FearGreedSyncResult:
    kind: str
    status: str
    rows_upserted: int = 0


def sync_fear_greed(
    *,
    conn,
    ca_bundle: Path | None = None,
    insecure: bool = False,
    timeout_s: float = 10.0,
    cache_ttl_s: int = 3600,
) -> FearGreedSyncResult:
    state = StateManager(conn)
    if cache_ttl_s > 0:
        latest = state.get_latest_fear_greed()
        if latest and latest.get("updated_at_utc"):
            try:
                updated_at = parse_utc_iso(str(latest["updated_at_utc"]))
                now = parse_utc_iso(utcnow_iso())
                if (now - updated_at).total_seconds() < cache_ttl_s:
                    state.append_audit(
                        level="INFO",
                        event="sync_fear_greed_cached",
                        details={"cache_ttl_s": cache_ttl_s, "updated_at_utc": latest.get("updated_at_utc")},
                    )
                    return FearGreedSyncResult(kind="fear_greed", status="ok", rows_upserted=0)
            except Exception:
                pass
    sync_id = state.record_sync_run_start(kind="fear_greed")
    try:
        resp = fetch_fear_greed(limit=1, timeout_s=timeout_s, ca_bundle=ca_bundle, insecure=insecure)
        raw_payload = {
            "value": resp.reading.value,
            "value_classification": resp.reading.value_classification,
            "timestamp_utc": resp.reading.timestamp_utc,
            "time_until_update_s": resp.reading.time_until_update_s,
        }
        rows = state.upsert_fear_greed(
            value=resp.reading.value,
            value_classification=resp.reading.value_classification,
            timestamp_utc=resp.reading.timestamp_utc,
            time_until_update_s=resp.reading.time_until_update_s,
            source=resp.source,
            raw_json=raw_payload,
        )
        state.append_audit(
            level="INFO",
            event="sync_fear_greed_ok",
            details={"value": resp.reading.value, "classification": resp.reading.value_classification},
        )
        state.record_sync_run_finish(sync_run_id=sync_id, status="ok", error_msg=None)
        return FearGreedSyncResult(kind="fear_greed", status="ok", rows_upserted=rows)
    except FearGreedAPIError as exc:
        state.append_audit(level="ERROR", event="sync_fear_greed_error", details={"error": str(exc)})
        state.record_sync_run_finish(sync_run_id=sync_id, status="error", error_msg=str(exc))
        return FearGreedSyncResult(kind="fear_greed", status="error", rows_upserted=0)
    except Exception as exc:
        state.append_audit(level="ERROR", event="sync_fear_greed_error", details={"error": str(exc)})
        state.record_sync_run_finish(sync_run_id=sync_id, status="error", error_msg=str(exc))
        return FearGreedSyncResult(kind="fear_greed", status="error", rows_upserted=0)
