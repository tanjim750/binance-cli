from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable

from cryptogent.exchange.interfaces import Balance
from cryptogent.util.time import utcnow_iso
from cryptogent.validation.trade_request import ValidatedTradeRequest


@dataclass(frozen=True)
class OrderRow:
    exchange_order_id: str | None
    symbol: str
    side: str
    type: str
    status: str
    time_in_force: str | None
    price: str | None
    quantity: str
    filled_quantity: str
    executed_quantity: str | None
    created_at_utc: str
    updated_at_utc: str


class StateManager:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def record_sync_run_start(self, *, kind: str) -> int:
        self.ensure_system_state()
        started_at = utcnow_iso()
        cur = self._conn.execute(
            "INSERT INTO sync_runs(kind, started_at_utc, status) VALUES(?, ?, ?)",
            (kind, started_at, "running"),
        )
        return int(cur.lastrowid)

    def record_sync_run_finish(self, *, sync_run_id: int, status: str, error_msg: str | None = None) -> None:
        self.ensure_system_state()
        finished_at = utcnow_iso()
        self._conn.execute(
            "UPDATE sync_runs SET finished_at_utc = ?, status = ?, error_msg = ? WHERE id = ?",
            (finished_at, status, error_msg, sync_run_id),
        )
        if status == "ok":
            self._conn.execute(
                "UPDATE system_state SET last_successful_sync_time_utc = ?, updated_at_utc = ? WHERE id = 1",
                (finished_at, finished_at),
            )

    def ensure_system_state(self) -> None:
        now = utcnow_iso()
        self._conn.execute(
            """
            INSERT OR IGNORE INTO system_state(id, created_at_utc, updated_at_utc)
            VALUES(1, ?, ?)
            """,
            (now, now),
        )
        # Best-effort backfill for existing DBs created before system_state existed.
        self._conn.execute(
            """
            UPDATE system_state
            SET last_successful_sync_time_utc = (
              SELECT MAX(finished_at_utc) FROM sync_runs WHERE status = 'ok' AND finished_at_utc IS NOT NULL
            ),
            updated_at_utc = ?
            WHERE id = 1 AND last_successful_sync_time_utc IS NULL
            """,
            (now,),
        )

    def set_automation_paused(self, *, paused: bool, reason: str | None, status: str | None = None) -> None:
        now = utcnow_iso()
        self.ensure_system_state()
        self._conn.execute(
            """
            UPDATE system_state
            SET automation_paused = ?,
                pause_reason = ?,
                paused_at_utc = ?,
                last_reconciliation_status = COALESCE(?, last_reconciliation_status),
                updated_at_utc = ?
            WHERE id = 1
            """,
            (1 if paused else 0, reason, now if paused else None, status, now),
        )

    def update_reconciliation_status(self, *, status: str) -> None:
        now = utcnow_iso()
        self.ensure_system_state()
        self._conn.execute(
            "UPDATE system_state SET last_reconciliation_status = ?, updated_at_utc = ? WHERE id = 1",
            (status, now),
        )

    def update_system_start(self, *, current_mode: str) -> None:
        now = utcnow_iso()
        self.ensure_system_state()
        self._conn.execute(
            """
            UPDATE system_state
            SET last_start_time_utc = ?,
                current_mode = ?,
                updated_at_utc = ?
            WHERE id = 1
            """,
            (now, current_mode, now),
        )

    def update_system_shutdown(self) -> None:
        now = utcnow_iso()
        self.ensure_system_state()
        self._conn.execute(
            "UPDATE system_state SET last_shutdown_time_utc = ?, updated_at_utc = ? WHERE id = 1",
            (now, now),
        )

    def get_system_state(self) -> dict | None:
        self.ensure_system_state()
        cur = self._conn.execute("SELECT * FROM system_state WHERE id = 1")
        row = cur.fetchone()
        return dict(row) if row else None

    def append_audit(self, *, level: str, event: str, details: dict | None = None) -> None:
        self._conn.execute(
            "INSERT INTO audit_logs(created_at_utc, level, event, details_json) VALUES(?, ?, ?, ?)",
            (utcnow_iso(), level, event, json.dumps(details or {}, separators=(",", ":"))),
        )

    def save_account_snapshot(self, *, payload: dict) -> None:
        self._conn.execute(
            "INSERT INTO account_snapshots(created_at_utc, payload_json) VALUES(?, ?)",
            (utcnow_iso(), json.dumps(payload, separators=(",", ":"))),
        )

    def upsert_fear_greed(
        self,
        *,
        value: int,
        value_classification: str,
        timestamp_utc: str,
        time_until_update_s: int | None,
        source: str,
        raw_json: dict,
    ) -> int:
        now = utcnow_iso()
        cur = self._conn.execute(
            """
            INSERT INTO fear_greed_index(
              value, value_classification, timestamp_utc, time_until_update_s,
              source, raw_json, created_at_utc, updated_at_utc
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, timestamp_utc) DO UPDATE SET
              value = excluded.value,
              value_classification = excluded.value_classification,
              time_until_update_s = excluded.time_until_update_s,
              raw_json = excluded.raw_json,
              updated_at_utc = excluded.updated_at_utc
            """,
            (
                str(value),
                value_classification,
                timestamp_utc,
                time_until_update_s,
                source,
                json.dumps(raw_json, separators=(",", ":")),
                now,
                now,
            ),
        )
        return int(cur.rowcount or 0)

    def upsert_news_articles(
        self,
        *,
        provider: str,
        request_kind: str,
        request_params: dict | None,
        articles: Iterable[dict],
    ) -> int:
        articles_list = list(articles)
        if not articles_list:
            return 0
        now = utcnow_iso()
        request_params_json = json.dumps(request_params or {}, separators=(",", ":")) if request_params else None
        rows: list[tuple] = []
        for a in articles_list:
            provider_article_id = str(a.get("provider_article_id") or "").strip()
            title = str(a.get("title") or "").strip()
            url = str(a.get("url") or "").strip()
            published_at_utc = str(a.get("published_at_utc") or "").strip()
            if not (provider_article_id and title and url and published_at_utc):
                continue
            rows.append(
                (
                    provider,
                    provider_article_id,
                    request_kind,
                    request_params_json,
                    title,
                    a.get("description"),
                    a.get("content"),
                    url,
                    a.get("image_url"),
                    published_at_utc,
                    a.get("lang"),
                    a.get("source_id"),
                    a.get("source_name"),
                    a.get("source_url"),
                    a.get("source_country"),
                    now,
                    json.dumps(a.get("raw_json") or {}, separators=(",", ":")),
                    now,
                    now,
                )
            )
        if not rows:
            return 0
        self._conn.executemany(
            """
            INSERT INTO news_articles(
              provider, provider_article_id, request_kind, request_params_json,
              title, description, content, url, image_url,
              published_at_utc, lang, source_id, source_name, source_url, source_country,
              fetched_at_utc, raw_json, created_at_utc, updated_at_utc
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, provider_article_id) DO UPDATE SET
              request_kind = excluded.request_kind,
              request_params_json = excluded.request_params_json,
              title = excluded.title,
              description = excluded.description,
              content = excluded.content,
              url = excluded.url,
              image_url = excluded.image_url,
              published_at_utc = excluded.published_at_utc,
              lang = excluded.lang,
              source_id = excluded.source_id,
              source_name = excluded.source_name,
              source_url = excluded.source_url,
              source_country = excluded.source_country,
              fetched_at_utc = excluded.fetched_at_utc,
              raw_json = excluded.raw_json,
              updated_at_utc = excluded.updated_at_utc
            """,
            rows,
        )
        return len(rows)

    def get_telegram_channel_state(self, *, channel: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM telegram_channel_state WHERE channel = ?",
            (channel,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def upsert_telegram_channel_state(
        self,
        *,
        channel: str,
        last_message_id: int | None,
        last_synced_at_utc: str,
    ) -> None:
        now = utcnow_iso()
        self._conn.execute(
            """
            INSERT INTO telegram_channel_state(
              channel, last_message_id, last_synced_at_utc, created_at_utc, updated_at_utc
            )
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(channel) DO UPDATE SET
              last_message_id = excluded.last_message_id,
              last_synced_at_utc = excluded.last_synced_at_utc,
              updated_at_utc = excluded.updated_at_utc
            """,
            (channel, last_message_id, last_synced_at_utc, now, now),
        )

    def list_existing_telegram_event_hashes(self, hashes: Iterable[str]) -> set[str]:
        hashes_list = [h for h in hashes if h]
        if not hashes_list:
            return set()
        placeholders = ",".join("?" for _ in hashes_list)
        cur = self._conn.execute(
            f"SELECT event_hash FROM telegram_messages WHERE event_hash IN ({placeholders})",
            hashes_list,
        )
        return {row[0] for row in cur.fetchall() if row and row[0]}

    def upsert_telegram_messages(self, *, messages: Iterable[dict]) -> int:
        messages_list = list(messages)
        if not messages_list:
            return 0
        now = utcnow_iso()
        rows: list[tuple] = []
        for msg in messages_list:
            channel = str(msg.get("channel") or "").strip()
            message_id = msg.get("message_id")
            published_at_utc = str(msg.get("published_at_utc") or "").strip()
            if not (channel and message_id is not None and published_at_utc):
                continue
            rows.append(
                (
                    channel,
                    int(message_id),
                    published_at_utc,
                    msg.get("text"),
                    msg.get("views"),
                    msg.get("forwards"),
                    1 if msg.get("has_media") else 0,
                    msg.get("source_type"),
                    msg.get("sentiment_score"),
                    msg.get("impact_score"),
                    msg.get("event_hash"),
                    json.dumps(msg.get("raw_json") or {}, separators=(",", ":")),
                    now,
                    now,
                )
            )
        if not rows:
            return 0
        self._conn.executemany(
            """
            INSERT INTO telegram_messages(
              channel, message_id, published_at_utc, text,
              views, forwards, has_media, source_type,
              sentiment_score, impact_score, event_hash,
              raw_json, created_at_utc, updated_at_utc
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel, message_id) DO UPDATE SET
              published_at_utc = excluded.published_at_utc,
              text = excluded.text,
              views = excluded.views,
              forwards = excluded.forwards,
              has_media = excluded.has_media,
              source_type = excluded.source_type,
              sentiment_score = excluded.sentiment_score,
              impact_score = excluded.impact_score,
              event_hash = excluded.event_hash,
              raw_json = excluded.raw_json,
              updated_at_utc = excluded.updated_at_utc
            """,
            rows,
        )
        return len(rows)

    def get_youtube_channel_state(self, *, channel_id: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM youtube_channel_state WHERE channel_id = ?",
            (channel_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def upsert_youtube_channel_state(
        self,
        *,
        channel_id: str,
        channel_name: str | None,
        last_video_published_at_utc: str | None,
        last_synced_at_utc: str,
    ) -> None:
        now = utcnow_iso()
        self._conn.execute(
            """
            INSERT INTO youtube_channel_state(
              channel_id, channel_name, last_video_published_at_utc, last_synced_at_utc,
              created_at_utc, updated_at_utc
            )
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
              channel_name = excluded.channel_name,
              last_video_published_at_utc = excluded.last_video_published_at_utc,
              last_synced_at_utc = excluded.last_synced_at_utc,
              updated_at_utc = excluded.updated_at_utc
            """,
            (channel_id, channel_name, last_video_published_at_utc, last_synced_at_utc, now, now),
        )

    def get_youtube_discovery_state(self, *, discovery_key: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM youtube_discovery_state WHERE discovery_key = ?",
            (discovery_key,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def upsert_youtube_discovery_state(
        self,
        *,
        discovery_key: str,
        last_published_at_utc: str | None,
        last_synced_at_utc: str,
    ) -> None:
        now = utcnow_iso()
        self._conn.execute(
            """
            INSERT INTO youtube_discovery_state(
              discovery_key, last_published_at_utc, last_synced_at_utc,
              created_at_utc, updated_at_utc
            )
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(discovery_key) DO UPDATE SET
              last_published_at_utc = excluded.last_published_at_utc,
              last_synced_at_utc = excluded.last_synced_at_utc,
              updated_at_utc = excluded.updated_at_utc
            """,
            (discovery_key, last_published_at_utc, last_synced_at_utc, now, now),
        )

    def upsert_youtube_videos(self, *, videos: Iterable[dict]) -> int:
        videos_list = list(videos)
        if not videos_list:
            return 0
        now = utcnow_iso()
        rows: list[tuple] = []
        for v in videos_list:
            video_id = str(v.get("video_id") or "").strip()
            channel_id = str(v.get("channel_id") or "").strip()
            title = str(v.get("title") or "").strip()
            published_at_utc = str(v.get("published_at_utc") or "").strip()
            if not (video_id and channel_id and title and published_at_utc):
                continue
            rows.append(
                (
                    video_id,
                    channel_id,
                    v.get("channel_title"),
                    title,
                    v.get("description"),
                    published_at_utc,
                    json.dumps(v.get("tags") or [], separators=(",", ":")),
                    v.get("view_count"),
                    v.get("like_count"),
                    v.get("comment_count"),
                    json.dumps(v.get("topic_labels") or [], separators=(",", ":")),
                    v.get("sentiment_score"),
                    v.get("impact_score"),
                    v.get("source_type"),
                    json.dumps(v.get("raw_json") or {}, separators=(",", ":")),
                    now,
                    now,
                )
            )
        if not rows:
            return 0
        self._conn.executemany(
            """
            INSERT INTO youtube_videos(
              video_id, channel_id, channel_title, title, description,
              published_at_utc, tags_json, view_count, like_count, comment_count,
              topic_labels_json, sentiment_score, impact_score, source_type,
              raw_json, created_at_utc, updated_at_utc
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
              channel_id = excluded.channel_id,
              channel_title = excluded.channel_title,
              title = excluded.title,
              description = excluded.description,
              published_at_utc = excluded.published_at_utc,
              tags_json = excluded.tags_json,
              view_count = excluded.view_count,
              like_count = excluded.like_count,
              comment_count = excluded.comment_count,
              topic_labels_json = excluded.topic_labels_json,
              sentiment_score = excluded.sentiment_score,
              impact_score = excluded.impact_score,
              source_type = excluded.source_type,
              raw_json = excluded.raw_json,
              updated_at_utc = excluded.updated_at_utc
            """,
            rows,
        )
        return len(rows)

    def upsert_youtube_comments(self, *, comments: Iterable[dict]) -> int:
        comments_list = list(comments)
        if not comments_list:
            return 0
        now = utcnow_iso()
        rows: list[tuple] = []
        for c in comments_list:
            video_id = str(c.get("video_id") or "").strip()
            comment_id = str(c.get("comment_id") or "").strip()
            published_at_utc = str(c.get("published_at_utc") or "").strip()
            if not (video_id and comment_id and published_at_utc):
                continue
            rows.append(
                (
                    video_id,
                    comment_id,
                    published_at_utc,
                    c.get("text"),
                    c.get("like_count"),
                    c.get("reply_count"),
                    c.get("author_channel_id"),
                    c.get("source_type"),
                    json.dumps(c.get("topic_labels") or [], separators=(",", ":")),
                    c.get("sentiment_score"),
                    c.get("impact_score"),
                    json.dumps(c.get("raw_json") or {}, separators=(",", ":")),
                    now,
                    now,
                )
            )
        if not rows:
            return 0
        self._conn.executemany(
            """
            INSERT INTO youtube_comments(
              video_id, comment_id, published_at_utc, text, like_count,
              reply_count, author_channel_id, source_type, topic_labels_json,
              sentiment_score, impact_score, raw_json, created_at_utc, updated_at_utc
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(comment_id) DO UPDATE SET
              video_id = excluded.video_id,
              published_at_utc = excluded.published_at_utc,
              text = excluded.text,
              like_count = excluded.like_count,
              reply_count = excluded.reply_count,
              author_channel_id = excluded.author_channel_id,
              source_type = excluded.source_type,
              topic_labels_json = excluded.topic_labels_json,
              sentiment_score = excluded.sentiment_score,
              impact_score = excluded.impact_score,
              raw_json = excluded.raw_json,
              updated_at_utc = excluded.updated_at_utc
            """,
            rows,
        )
        return len(rows)

    def upsert_balances(self, balances: Iterable[Balance]) -> None:
        now = utcnow_iso()
        self._conn.executemany(
            """
            INSERT INTO balances(asset, free, locked, snapshot_time_utc, updated_at_utc)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(asset) DO UPDATE SET
              free = excluded.free,
              locked = excluded.locked,
              snapshot_time_utc = excluded.snapshot_time_utc,
              updated_at_utc = excluded.updated_at_utc
            """,
            [(b.asset, b.free, b.locked, now, now) for b in balances],
        )

    def upsert_orders(self, orders: Iterable[OrderRow]) -> None:
        orders_list = list(orders)
        if not orders_list:
            return
        self._conn.executemany(
            """
            INSERT INTO orders(
              exchange_order_id, symbol, side, type, status, time_in_force,
              price, quantity, filled_quantity, executed_quantity,
              created_at_utc, updated_at_utc
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(exchange_order_id) DO UPDATE SET
              symbol = excluded.symbol,
              side = excluded.side,
              type = excluded.type,
              status = excluded.status,
              time_in_force = excluded.time_in_force,
              price = excluded.price,
              quantity = excluded.quantity,
              filled_quantity = excluded.filled_quantity,
              executed_quantity = excluded.executed_quantity,
              updated_at_utc = excluded.updated_at_utc
            """,
            [
                (
                    o.exchange_order_id,
                    o.symbol,
                    o.side,
                    o.type,
                    o.status,
                    o.time_in_force,
                    o.price,
                    o.quantity,
                    o.filled_quantity,
                    o.executed_quantity,
                    o.created_at_utc,
                    o.updated_at_utc,
                )
                for o in orders_list
                if o.exchange_order_id is not None
            ],
        )

    def sync_open_orders(self, open_orders: Iterable[OrderRow], *, symbol: str | None = None) -> None:
        """
        Exchange is source of truth.
        Mark previously open orders as CLOSED, then upsert currently open ones.
        """
        now = utcnow_iso()
        if symbol:
            self._conn.execute(
                "UPDATE orders SET status = ?, updated_at_utc = ? WHERE symbol = ? AND status IN ('NEW','PARTIALLY_FILLED')",
                ("CLOSED", now, symbol),
            )
        else:
            self._conn.execute(
                "UPDATE orders SET status = ?, updated_at_utc = ? WHERE status IN ('NEW','PARTIALLY_FILLED')",
                ("CLOSED", now),
            )
        self.upsert_orders(open_orders)
        # After upsert, classify order source for the currently open set.
        try:
            order_ids = [o.exchange_order_id for o in open_orders if getattr(o, "exchange_order_id", None)]
            self.set_order_sources_for_exchange_order_ids(order_ids)
        except Exception:
            return

    def set_order_sources_for_exchange_order_ids(self, exchange_order_ids: Iterable[str | None]) -> None:
        """
        Set orders.order_source for the given exchange order ids.

        Values:
          - execution: orderId exists in executions.binance_order_id
          - manual: orderId exists in manual_orders.binance_order_id
          - manual: orderId exists in loop_legs.binance_order_id (manual loop trading)
          - external: default when unknown
        """
        ids = [str(x).strip() for x in exchange_order_ids if x not in (None, "", "None")]
        if not ids:
            return

        # Default: external.
        self._conn.executemany("UPDATE orders SET order_source = 'external' WHERE exchange_order_id = ?", [(i,) for i in ids])

        q_marks = ",".join(["?"] * len(ids))
        # manual takes precedence over external.
        manual_rows = self._conn.execute(
            f"SELECT DISTINCT binance_order_id FROM manual_orders WHERE binance_order_id IN ({q_marks})",
            ids,
        ).fetchall()
        for r in manual_rows:
            oid = str(r["binance_order_id"])
            if oid:
                self._conn.execute("UPDATE orders SET order_source = 'manual' WHERE exchange_order_id = ?", (oid,))

        loop_rows = self._conn.execute(
            f"SELECT DISTINCT binance_order_id FROM loop_legs WHERE binance_order_id IN ({q_marks})",
            ids,
        ).fetchall()
        for r in loop_rows:
            oid = str(r["binance_order_id"])
            if oid:
                self._conn.execute("UPDATE orders SET order_source = 'manual' WHERE exchange_order_id = ?", (oid,))

        # execution takes precedence over manual/external.
        exec_rows = self._conn.execute(
            f"SELECT DISTINCT binance_order_id FROM executions WHERE binance_order_id IN ({q_marks})",
            ids,
        ).fetchall()
        for r in exec_rows:
            oid = str(r["binance_order_id"])
            if oid:
                self._conn.execute("UPDATE orders SET order_source = 'execution' WHERE exchange_order_id = ?", (oid,))

    def get_last_sync(self) -> dict | None:
        cur = self._conn.execute(
            "SELECT kind, started_at_utc, finished_at_utc, status, error_msg FROM sync_runs ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_balance_count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) AS n FROM balances")
        return int(cur.fetchone()["n"])

    def get_open_order_count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) AS n FROM orders WHERE status IN ('NEW','PARTIALLY_FILLED')")
        return int(cur.fetchone()["n"])

    def list_open_orders_for_reconcile(self, *, limit: int = 200) -> list[dict]:
        cur = self._conn.execute(
            """
            SELECT exchange_order_id, symbol, status, updated_at_utc
            FROM orders
            WHERE status IN ('NEW','PARTIALLY_FILLED') AND exchange_order_id IS NOT NULL
            ORDER BY updated_at_utc DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        return [dict(r) for r in cur.fetchall()]

    def list_open_orders_for_reconcile_by_source(self, *, order_source: str, limit: int = 200) -> list[dict]:
        cur = self._conn.execute(
            """
            SELECT exchange_order_id, symbol, status, updated_at_utc
            FROM orders
            WHERE status IN ('NEW','PARTIALLY_FILLED')
              AND exchange_order_id IS NOT NULL
              AND order_source = ?
            ORDER BY updated_at_utc DESC
            LIMIT ?
            """,
            (str(order_source), int(limit)),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_execution_by_binance_order_id(self, *, binance_order_id: str) -> dict | None:
        cur = self._conn.execute("SELECT * FROM executions WHERE binance_order_id = ?", (str(binance_order_id),))
        row = cur.fetchone()
        return dict(row) if row else None

    def create_trade_request(self, req: ValidatedTradeRequest) -> int:
        now = utcnow_iso()
        cur = self._conn.execute(
            """
            INSERT INTO trade_requests(
              request_id, status, preferred_symbol, exit_asset, label, notes,
              budget_mode, budget_asset, budget_amount,
              profit_target_pct, stop_loss_pct, deadline_hours, deadline_utc,
              created_at_utc, updated_at_utc
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                None,
                "DRAFT",
                req.preferred_symbol,
                req.exit_asset,
                req.label,
                req.notes,
                req.budget_mode,
                req.budget_asset,
                str(req.budget_amount) if req.budget_amount is not None else None,
                str(req.profit_target_pct),
                str(req.stop_loss_pct),
                req.deadline_hours,
                req.deadline_utc.replace(microsecond=0).isoformat(),
                now,
                now,
            ),
        )
        row_id = int(cur.lastrowid)
        # Generate a stable request_id for display/auditing.
        request_id = f"tr_{row_id:06d}"
        self._conn.execute("UPDATE trade_requests SET request_id = ?, updated_at_utc = ? WHERE id = ?", (request_id, now, row_id))
        return row_id

    def list_trade_requests(self, *, limit: int | None = None) -> list[dict]:
        sql = (
            "SELECT id, request_id, status, preferred_symbol, budget_mode, budget_asset, budget_amount, "
            "profit_target_pct, stop_loss_pct, deadline_utc, validation_status, created_at_utc, updated_at_utc "
            "FROM trade_requests ORDER BY id DESC"
        )
        params: list[object] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        cur = self._conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def get_trade_request(self, trade_request_id: int) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM trade_requests WHERE id = ?",
            (int(trade_request_id),),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def cancel_trade_request(self, trade_request_id: int) -> bool:
        now = utcnow_iso()
        cur = self._conn.execute(
            "UPDATE trade_requests SET status = ?, updated_at_utc = ? WHERE id = ? AND status IN ('NEW','DRAFT','VALIDATED')",
            ("CANCELLED", now, int(trade_request_id)),
        )
        return int(cur.rowcount) > 0

    def set_trade_request_validation(
        self,
        *,
        trade_request_id: int,
        validation_status: str,
        validation_error: str | None,
        last_price: str | None,
        estimated_qty: str | None,
        symbol_base_asset: str | None,
        symbol_quote_asset: str | None,
    ) -> bool:
        now = utcnow_iso()
        cur = self._conn.execute(
            """
            UPDATE trade_requests
            SET validation_status = ?,
                validation_error = ?,
                validated_at_utc = ?,
                last_price = ?,
                estimated_qty = ?,
                symbol_base_asset = ?,
                symbol_quote_asset = ?,
                updated_at_utc = ?
            WHERE id = ? AND status IN ('NEW','DRAFT','VALIDATED')
            """,
            (
                validation_status,
                validation_error,
                now,
                last_price,
                estimated_qty,
                symbol_base_asset,
                symbol_quote_asset,
                now,
                int(trade_request_id),
            ),
        )
        return int(cur.rowcount) > 0

    def list_balances(self, *, include_zero: bool, limit: int | None = None) -> list[dict]:
        cur = self._conn.execute("SELECT asset, free, locked, updated_at_utc FROM balances ORDER BY asset ASC")
        rows = [dict(r) for r in cur.fetchall()]
        if include_zero:
            return rows[:limit] if limit is not None else rows

        def _is_zero(s: object) -> bool:
            try:
                return Decimal(str(s)) == 0
            except (InvalidOperation, ValueError):
                return False

        filtered = [r for r in rows if not (_is_zero(r.get("free")) and _is_zero(r.get("locked")))]
        return filtered[:limit] if limit is not None else filtered

    def list_open_orders(self, *, symbol: str | None = None, limit: int | None = None) -> list[dict]:
        sql = (
            "SELECT exchange_order_id, order_source, symbol, side, type, status, price, quantity, filled_quantity, "
            "created_at_utc, updated_at_utc "
            "FROM orders WHERE status IN ('NEW','PARTIALLY_FILLED')"
        )
        params: list[object] = []
        if symbol:
            sql += " AND symbol = ?"
            params.append(symbol)
        sql += " ORDER BY updated_at_utc DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        cur = self._conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def list_fear_greed(self, *, limit: int = 20) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM fear_greed_index ORDER BY timestamp_utc DESC LIMIT ?",
            (int(limit),),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_latest_fear_greed(self) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM fear_greed_index ORDER BY timestamp_utc DESC LIMIT 1"
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def list_news_articles(self, *, provider: str | None = None, limit: int = 50) -> list[dict]:
        params: list[object] = []
        sql = "SELECT * FROM news_articles"
        if provider:
            sql += " WHERE provider = ?"
            params.append(provider)
        sql += " ORDER BY published_at_utc DESC, id DESC LIMIT ?"
        params.append(int(limit))
        cur = self._conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def get_latest_news_request(
        self,
        *,
        provider: str,
        request_kind: str,
        request_params_json: str,
    ) -> dict | None:
        cur = self._conn.execute(
            """
            SELECT * FROM news_articles
            WHERE provider = ? AND request_kind = ? AND request_params_json = ?
            ORDER BY fetched_at_utc DESC, id DESC
            LIMIT 1
            """,
            (provider, request_kind, request_params_json),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_cached_balance_free(self, *, asset: str) -> Decimal | None:
        cur = self._conn.execute("SELECT free FROM balances WHERE asset = ?", (asset.strip().upper(),))
        row = cur.fetchone()
        if not row:
            return None
        try:
            return Decimal(str(row["free"]))
        except (InvalidOperation, ValueError):
            return None

    def create_trade_plan(
        self,
        *,
        trade_request_id: int,
        request_id: str | None,
        status: str,
        feasibility_category: str,
        warnings_json: str | None,
        rejection_reason: str | None,
        market_data_environment: str,
        execution_environment: str,
        symbol: str,
        price: str,
        bid: str | None,
        ask: str | None,
        spread_pct: str | None,
        volume_24h_quote: str | None,
        volatility_pct: str | None,
        momentum_pct: str | None,
        budget_mode: str,
        approved_budget_asset: str,
        approved_budget_amount: str | None,
        usable_budget_amount: str | None,
        raw_quantity: str | None,
        rounded_quantity: str | None,
        expected_notional: str | None,
        rules_snapshot_json: str,
        market_summary_json: str,
        candidate_list_json: str | None,
        signal: str,
        signal_reasons_json: str | None,
        created_at_utc: str,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO trade_plans(
              trade_request_id, request_id, status, feasibility_category, warnings_json, rejection_reason,
              market_data_environment, execution_environment,
              symbol, price, bid, ask, spread_pct,
              volume_24h_quote, volatility_pct, momentum_pct,
              budget_mode, approved_budget_asset, approved_budget_amount, usable_budget_amount,
              raw_quantity, rounded_quantity, expected_notional,
              rules_snapshot_json, market_summary_json, candidate_list_json,
              signal, signal_reasons_json, created_at_utc
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(trade_request_id),
                request_id,
                status,
                feasibility_category,
                warnings_json,
                rejection_reason,
                market_data_environment,
                execution_environment,
                symbol,
                price,
                bid,
                ask,
                spread_pct,
                volume_24h_quote,
                volatility_pct,
                momentum_pct,
                budget_mode,
                approved_budget_asset,
                approved_budget_amount,
                usable_budget_amount,
                raw_quantity,
                rounded_quantity,
                expected_notional,
                rules_snapshot_json,
                market_summary_json,
                candidate_list_json,
                signal,
                signal_reasons_json,
                created_at_utc,
            ),
        )
        return int(cur.lastrowid)

    def list_trade_plans(self, *, limit: int = 20) -> list[dict]:
        cur = self._conn.execute(
            """
            SELECT
              id,
              trade_request_id,
              request_id,
              symbol,
              feasibility_category,
              approved_budget_asset,
              approved_budget_amount,
              status,
              warnings_json,
              created_at_utc
            FROM trade_plans
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_trade_plan(self, *, plan_id: int) -> dict | None:
        cur = self._conn.execute("SELECT * FROM trade_plans WHERE id = ?", (int(plan_id),))
        row = cur.fetchone()
        return dict(row) if row else None

    def create_execution_candidate(
        self,
        *,
        trade_plan_id: int,
        trade_request_id: int,
        request_id: str | None,
        symbol: str,
        side: str,
        order_type: str,
        limit_price: str | None,
        execution_environment: str,
        position_id: int | None,
        validation_status: str,
        risk_status: str,
        approved_budget_asset: str,
        approved_budget_amount: str,
        approved_quantity: str,
        execution_ready: bool,
        summary: str,
        details_json: str | None,
    ) -> int:
        now = utcnow_iso()
        cur = self._conn.execute(
            """
            INSERT INTO execution_candidates(
              trade_plan_id, trade_request_id, request_id,
              symbol, side, order_type, limit_price, execution_environment, position_id,
              validation_status, risk_status,
              approved_budget_asset, approved_budget_amount, approved_quantity,
              execution_ready, summary, details_json,
              created_at_utc, updated_at_utc
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(trade_plan_id),
                int(trade_request_id),
                request_id,
                symbol,
                side,
                order_type,
                limit_price,
                execution_environment,
                int(position_id) if position_id is not None else None,
                validation_status,
                risk_status,
                approved_budget_asset,
                approved_budget_amount,
                approved_quantity,
                1 if execution_ready else 0,
                summary,
                details_json,
                now,
                now,
            ),
        )
        return int(cur.lastrowid)

    def get_execution_candidate(self, *, candidate_id: int) -> dict | None:
        cur = self._conn.execute("SELECT * FROM execution_candidates WHERE id = ?", (int(candidate_id),))
        row = cur.fetchone()
        return dict(row) if row else None

    def create_execution(
        self,
        *,
        candidate_id: int,
        plan_id: int,
        trade_request_id: int,
        symbol: str,
        side: str,
        order_type: str,
        execution_environment: str,
        client_order_id: str,
        position_id: int | None = None,
        quote_order_qty: str | None,
        limit_price: str | None = None,
        time_in_force: str | None = None,
        requested_quantity: str | None = None,
    ) -> int:
        now = utcnow_iso()
        cur = self._conn.execute(
            """
            INSERT INTO executions(
              candidate_id, plan_id, trade_request_id,
              symbol, side, order_type, execution_environment, position_id,
              client_order_id, binance_order_id,
              quote_order_qty, limit_price, time_in_force, requested_quantity,
              executed_quantity, avg_fill_price, total_quote_spent,
              commission_total, commission_asset, fills_count,
              local_status, raw_status, retry_count,
              submitted_at_utc, reconciled_at_utc,
              expired_at_utc, created_at_utc, updated_at_utc
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(candidate_id),
                int(plan_id),
                int(trade_request_id),
                symbol,
                side,
                order_type,
                execution_environment,
                int(position_id) if position_id is not None else None,
                client_order_id,
                None,
                quote_order_qty,
                limit_price,
                time_in_force,
                requested_quantity,
                None,
                None,
                None,
                None,
                None,
                None,
                "submitting",
                None,
                0,
                None,
                None,
                None,
                now,
                now,
            ),
        )
        return int(cur.lastrowid)

    def update_execution(
        self,
        *,
        execution_id: int,
        local_status: str,
        raw_status: str | None,
        binance_order_id: str | None,
        executed_quantity: str | None,
        avg_fill_price: str | None,
        total_quote_spent: str | None,
        commission_total: str | None,
        commission_asset: str | None,
        fee_breakdown_json: str | None = None,
        realized_pnl_quote: str | None = None,
        realized_pnl_quote_asset: str | None = None,
        pnl_warnings_json: str | None = None,
        fills_count: int | None,
        retry_count: int,
        message: str,
        details_json: str | None,
        submitted_at_utc: str | None,
        reconciled_at_utc: str | None,
        expired_at_utc: str | None = None,
    ) -> None:
        now = utcnow_iso()
        self._conn.execute(
            """
            UPDATE executions
            SET local_status = ?,
                raw_status = ?,
                binance_order_id = ?,
                executed_quantity = ?,
                avg_fill_price = ?,
                total_quote_spent = ?,
                commission_total = ?,
                commission_asset = ?,
                fee_breakdown_json = COALESCE(?, fee_breakdown_json),
                realized_pnl_quote = COALESCE(?, realized_pnl_quote),
                realized_pnl_quote_asset = COALESCE(?, realized_pnl_quote_asset),
                pnl_warnings_json = COALESCE(?, pnl_warnings_json),
                fills_count = ?,
                retry_count = ?,
                submitted_at_utc = COALESCE(submitted_at_utc, ?),
                reconciled_at_utc = ?,
                expired_at_utc = COALESCE(expired_at_utc, ?),
                updated_at_utc = ?
            WHERE execution_id = ?
            """,
            (
                local_status,
                raw_status,
                binance_order_id,
                executed_quantity,
                avg_fill_price,
                total_quote_spent,
                commission_total,
                commission_asset,
                fee_breakdown_json,
                realized_pnl_quote,
                realized_pnl_quote_asset,
                pnl_warnings_json,
                fills_count,
                int(retry_count),
                submitted_at_utc,
                reconciled_at_utc,
                expired_at_utc,
                now,
                int(execution_id),
            ),
        )
        # Store human-readable message/details in audit log (executions table stays normalized).
        self.append_audit(
            level="INFO" if local_status in ("filled", "submitted", "partially_filled") else "WARN",
            event="execution_update",
            details={
                "execution_id": int(execution_id),
                "local_status": local_status,
                "message": message,
                "details": details_json,
            },
        )

    def mark_execution_expired(self, *, execution_id: int, reason: str) -> None:
        now = utcnow_iso()
        self._conn.execute(
            """
            UPDATE executions
            SET local_status = 'expired',
                expired_at_utc = COALESCE(expired_at_utc, ?),
                reconciled_at_utc = ?,
                updated_at_utc = ?
            WHERE execution_id = ?
            """,
            (now, now, now, int(execution_id)),
        )
        self.append_audit(level="WARN", event="execution_expired", details={"execution_id": int(execution_id), "reason": reason})

    def list_executions(self, *, limit: int = 20) -> list[dict]:
        cur = self._conn.execute(
            """
            SELECT
              execution_id,
              candidate_id,
              plan_id,
              trade_request_id,
              symbol,
              side,
              order_type,
              execution_environment,
              client_order_id,
              binance_order_id,
              quote_order_qty,
              limit_price,
              time_in_force,
              executed_quantity,
              avg_fill_price,
              total_quote_spent,
              commission_total,
              commission_asset,
              fills_count,
              local_status,
              raw_status,
              retry_count,
              submitted_at_utc,
              reconciled_at_utc,
              expired_at_utc,
              created_at_utc,
              updated_at_utc
            FROM executions
            ORDER BY execution_id DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_execution(self, *, execution_id: int) -> dict | None:
        cur = self._conn.execute("SELECT * FROM executions WHERE execution_id = ?", (int(execution_id),))
        row = cur.fetchone()
        return dict(row) if row else None

    def has_nonterminal_execution_for_candidate(self, *, candidate_id: int) -> bool:
        """
        Returns True if this candidate already has an execution attempt.
        This prevents accidental duplicate executions from the same approved candidate.
        """
        cur = self._conn.execute(
            """
            SELECT 1
            FROM executions
            WHERE candidate_id = ?
            LIMIT 1
            """,
            (int(candidate_id),),
        )
        return cur.fetchone() is not None

    def list_reconcilable_executions(self, *, limit: int = 50) -> list[dict]:
        cur = self._conn.execute(
            """
            SELECT
              execution_id,
              candidate_id,
              plan_id,
              trade_request_id,
              symbol,
              order_type,
              execution_environment,
              client_order_id,
              local_status,
              raw_status,
              retry_count,
              submitted_at_utc,
              limit_price,
              time_in_force
            FROM executions
            WHERE local_status IN ('uncertain_submitted','submitted','open','partially_filled')
            ORDER BY execution_id DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        return [dict(r) for r in cur.fetchall()]

    def create_position(
        self,
        *,
        symbol: str,
        base_asset: str | None,
        quote_asset: str | None,
        market_data_environment: str,
        execution_environment: str,
        entry_price: str,
        quantity: str,
        source_execution_id: int | None = None,
        gross_quantity: str | None = None,
        fee_amount: str | None = None,
        fee_asset: str | None = None,
        stop_loss_price: str,
        profit_target_price: str,
        deadline_utc: str,
    ) -> int:
        now = utcnow_iso()
        cur = self._conn.execute(
            """
            INSERT INTO positions(
              symbol, base_asset, quote_asset,
              market_data_environment, execution_environment,
              entry_price, quantity, locked_qty,
              source_execution_id, gross_quantity, fee_amount, fee_asset,
              stop_loss_price, profit_target_price, deadline_utc,
              status, opened_at_utc, created_at_utc, updated_at_utc
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol,
                base_asset,
                quote_asset,
                market_data_environment,
                execution_environment,
                entry_price,
                quantity,
                "0",
                int(source_execution_id) if source_execution_id is not None else None,
                gross_quantity,
                fee_amount,
                fee_asset,
                stop_loss_price,
                profit_target_price,
                deadline_utc,
                "OPEN",
                now,
                now,
                now,
            ),
        )
        return int(cur.lastrowid)

    def set_position_locked_qty(self, *, position_id: int, locked_qty: str) -> bool:
        now = utcnow_iso()
        cur = self._conn.execute(
            "UPDATE positions SET locked_qty = ?, updated_at_utc = ? WHERE id = ?",
            (locked_qty, now, int(position_id)),
        )
        return int(cur.rowcount) > 0

    def get_position_reserved_sell_qty(self, *, position_id: int) -> Decimal:
        cur = self._conn.execute(
            """
            SELECT requested_quantity, executed_quantity
            FROM executions
            WHERE position_id = ?
              AND side = 'SELL'
              AND local_status IN ('submitting','submitted','open','partially_filled','uncertain_submitted')
            """,
            (int(position_id),),
        )
        total = Decimal("0")
        for r in cur.fetchall():
            try:
                requested = Decimal(str(r["requested_quantity"] or "0"))
            except Exception:
                requested = Decimal("0")
            try:
                executed = Decimal(str(r["executed_quantity"] or "0"))
            except Exception:
                executed = Decimal("0")
            remaining = requested - executed
            if remaining < 0:
                remaining = Decimal("0")
            if remaining > 0:
                total += remaining
        return total

    def recompute_locked_qty_for_open_positions(self) -> int:
        cur = self._conn.execute("SELECT id, quantity FROM positions WHERE status = 'OPEN'")
        updated = 0
        for r in cur.fetchall():
            pos_id = int(r["id"])
            reserved = self.get_position_reserved_sell_qty(position_id=pos_id)
            try:
                pos_qty = Decimal(str(r["quantity"] or "0"))
            except Exception:
                pos_qty = None
            if pos_qty is not None and pos_qty >= 0:
                reserved = min(reserved, pos_qty)
            if self.set_position_locked_qty(position_id=pos_id, locked_qty=str(reserved)):
                updated += 1
        return updated

    def get_open_position_qty_by_asset(self) -> dict[str, Decimal]:
        cur = self._conn.execute("SELECT base_asset, quantity FROM positions WHERE status = 'OPEN' AND base_asset IS NOT NULL")
        out: dict[str, Decimal] = {}
        for r in cur.fetchall():
            asset = str(r["base_asset"] or "").strip().upper()
            if not asset:
                continue
            try:
                qty = Decimal(str(r["quantity"]))
            except (InvalidOperation, ValueError):
                continue
            out[asset] = out.get(asset, Decimal("0")) + qty
        return out

    def get_dust(self, *, asset: str) -> dict | None:
        cur = self._conn.execute("SELECT * FROM dust_ledger WHERE asset = ?", (asset.strip().upper(),))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_dust(self, *, limit: int = 200) -> list[dict]:
        cur = self._conn.execute("SELECT * FROM dust_ledger ORDER BY updated_at_utc DESC LIMIT ?", (int(limit),))
        return [dict(r) for r in cur.fetchall()]

    def add_dust(
        self,
        *,
        asset: str,
        dust_qty: str,
        avg_cost_price: str,
        needs_reconcile: bool = True,
    ) -> None:
        """
        Add dust to the per-asset dust ledger using weighted average cost.

        Dust ledger is accounting-only and must not be used as a tradable source of truth.
        """
        asset_u = asset.strip().upper()
        if not asset_u:
            return
        try:
            add_qty = Decimal(str(dust_qty))
            add_cost = Decimal(str(avg_cost_price))
        except (InvalidOperation, ValueError):
            return
        if add_qty <= 0 or add_cost <= 0:
            return

        now = utcnow_iso()
        row = self.get_dust(asset=asset_u)
        if not row:
            self._conn.execute(
                """
                INSERT INTO dust_ledger(asset, dust_qty, avg_cost_price, needs_reconcile, created_at_utc, updated_at_utc)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (asset_u, str(add_qty), str(add_cost), 1 if needs_reconcile else 0, now, now),
            )
            return

        try:
            prev_qty = Decimal(str(row.get("dust_qty") or "0"))
            prev_cost = Decimal(str(row.get("avg_cost_price") or "0"))
        except (InvalidOperation, ValueError):
            prev_qty = Decimal("0")
            prev_cost = Decimal("0")

        new_total = prev_qty + add_qty
        if new_total <= 0:
            new_total = Decimal("0")
            new_cost = add_cost
        elif prev_qty <= 0 or prev_cost <= 0:
            new_cost = add_cost
        else:
            new_cost = ((prev_qty * prev_cost) + (add_qty * add_cost)) / new_total

        self._conn.execute(
            """
            UPDATE dust_ledger
            SET dust_qty = ?,
                avg_cost_price = ?,
                needs_reconcile = ?,
                updated_at_utc = ?
            WHERE asset = ?
            """,
            (str(new_total), str(new_cost), 1 if needs_reconcile else int(row.get("needs_reconcile") or 0), now, asset_u),
        )

    def reconcile_dust_ledger(self, *, balances: Iterable[Balance]) -> None:
        """
        Clamp dust ledger against Binance free balances minus open-position quantities.
        """
        free_by_asset: dict[str, Decimal] = {}
        for b in balances:
            try:
                free_by_asset[str(b.asset).strip().upper()] = Decimal(str(b.free))
            except (InvalidOperation, ValueError):
                continue
        open_pos_qty = self.get_open_position_qty_by_asset()

        for row in self.list_dust(limit=500):
            asset = str(row.get("asset") or "").strip().upper()
            if not asset:
                continue
            try:
                dust_qty = Decimal(str(row.get("dust_qty") or "0"))
            except (InvalidOperation, ValueError):
                continue
            free = free_by_asset.get(asset, Decimal("0"))
            reserved = open_pos_qty.get(asset, Decimal("0"))
            allowed = free - reserved
            if allowed < 0:
                allowed = Decimal("0")
            effective = dust_qty if dust_qty <= allowed else allowed

            needs = int(row.get("needs_reconcile") or 0)
            if effective != dust_qty or needs:
                now = utcnow_iso()
                self._conn.execute(
                    "UPDATE dust_ledger SET dust_qty = ?, needs_reconcile = 0, updated_at_utc = ? WHERE asset = ?",
                    (str(effective), now, asset),
                )
                if effective != dust_qty:
                    self.append_audit(
                        level="WARN",
                        event="dust_ledger_clamped",
                        details={
                            "asset": asset,
                            "prev_dust_qty": str(dust_qty),
                            "new_dust_qty": str(effective),
                            "binance_free": str(free),
                            "open_position_qty": str(reserved),
                        },
                    )

    def get_active_position(self, *, symbol: str | None = None) -> dict | None:
        if symbol:
            cur = self._conn.execute(
                "SELECT * FROM positions WHERE status = 'OPEN' AND symbol = ? ORDER BY id DESC LIMIT 1",
                (symbol,),
            )
        else:
            cur = self._conn.execute("SELECT * FROM positions WHERE status = 'OPEN' ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        return dict(row) if row else None

    def get_position(self, *, position_id: int) -> dict | None:
        cur = self._conn.execute("SELECT * FROM positions WHERE id = ?", (int(position_id),))
        row = cur.fetchone()
        return dict(row) if row else None

    def close_position(self, *, position_id: int, status: str = "CLOSED") -> bool:
        now = utcnow_iso()
        cur = self._conn.execute(
            """
            UPDATE positions
            SET status = ?, closed_at_utc = ?, updated_at_utc = ?
            WHERE id = ? AND status = 'OPEN'
            """,
            (status, now, now, int(position_id)),
        )
        return int(cur.rowcount) > 0

    def update_position_quantity(self, *, position_id: int, quantity: str) -> bool:
        now = utcnow_iso()
        cur = self._conn.execute(
            """
            UPDATE positions
            SET quantity = ?, updated_at_utc = ?
            WHERE id = ? AND status = 'OPEN'
            """,
            (quantity, now, int(position_id)),
        )
        return int(cur.rowcount) > 0

    def update_position_last_monitored(self, *, position_id: int, at_utc: str) -> None:
        self._conn.execute(
            "UPDATE positions SET last_monitored_at_utc = ?, updated_at_utc = ? WHERE id = ?",
            (at_utc, utcnow_iso(), int(position_id)),
        )

    def list_positions(self, *, status: str | None = None, limit: int = 50) -> list[dict]:
        if status:
            cur = self._conn.execute(
                "SELECT * FROM positions WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, int(limit)),
            )
        else:
            cur = self._conn.execute("SELECT * FROM positions ORDER BY id DESC LIMIT ?", (int(limit),))
        return [dict(r) for r in cur.fetchall()]

    def create_market_snapshot(
        self,
        *,
        symbol: str,
        timeframe: str,
        captured_at_utc: str,
        last_price: str,
        bid: str | None,
        ask: str | None,
        spread_pct: str | None,
        change_percent: str | None,
        volume_quote: str | None,
        indicators_json: str | None,
        condition_summary: str | None,
        enabled_flags: str | None,
        config_hash: str | None,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO market_snapshots(
              symbol, timeframe, captured_at_utc,
              last_price, bid, ask, spread_pct, change_percent, volume_quote,
              indicators_json, condition_summary, enabled_flags, config_hash
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol,
                timeframe,
                captured_at_utc,
                last_price,
                bid,
                ask,
                spread_pct,
                change_percent,
                volume_quote,
                indicators_json,
                condition_summary,
                enabled_flags,
                config_hash,
            ),
        )
        return int(cur.lastrowid)

    def get_latest_market_snapshot(self, *, symbol: str, timeframe: str) -> dict | None:
        cur = self._conn.execute(
            """
            SELECT *
            FROM market_snapshots
            WHERE symbol = ? AND timeframe = ?
            ORDER BY captured_at_utc DESC, id DESC
            LIMIT 1
            """,
            (symbol, timeframe),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def list_market_snapshots(
        self,
        *,
        limit: int = 50,
        symbol: str | None = None,
        timeframe: str | None = None,
    ) -> list[dict]:
        if symbol and timeframe:
            cur = self._conn.execute(
                """
                SELECT *
                FROM market_snapshots
                WHERE symbol = ? AND timeframe = ?
                ORDER BY captured_at_utc DESC, id DESC
                LIMIT ?
                """,
                (symbol, timeframe, int(limit)),
            )
        elif symbol:
            cur = self._conn.execute(
                """
                SELECT *
                FROM market_snapshots
                WHERE symbol = ?
                ORDER BY captured_at_utc DESC, id DESC
                LIMIT ?
                """,
                (symbol, int(limit)),
            )
        elif timeframe:
            cur = self._conn.execute(
                """
                SELECT *
                FROM market_snapshots
                WHERE timeframe = ?
                ORDER BY captured_at_utc DESC, id DESC
                LIMIT ?
                """,
                (timeframe, int(limit)),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM market_snapshots ORDER BY captured_at_utc DESC, id DESC LIMIT ?",
                (int(limit),),
            )
        return [dict(r) for r in cur.fetchall()]

    def get_market_snapshot(self, *, snapshot_id: int) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM market_snapshots WHERE id = ?",
            (int(snapshot_id),),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def list_audit_logs(self, *, limit: int = 50) -> list[dict]:
        cur = self._conn.execute(
            "SELECT created_at_utc, level, event, details_json FROM audit_logs ORDER BY id DESC LIMIT ?",
            (int(limit),),
        )
        return [dict(r) for r in cur.fetchall()]

    def create_reconciliation_event(
        self,
        *,
        event_type: str,
        status: str,
        summary: str,
        details: dict | None = None,
    ) -> int:
        now = utcnow_iso()
        cur = self._conn.execute(
            """
            INSERT INTO reconciliation_events(
              event_type, status, summary, details_json, created_at_utc, updated_at_utc
            )
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (event_type, status, summary, json.dumps(details or {}, separators=(",", ":")), now, now),
        )
        return int(cur.lastrowid)

    def list_reconciliation_events(self, *, limit: int = 50) -> list[dict]:
        cur = self._conn.execute(
            """
            SELECT reconciliation_event_id, event_type, status, summary, created_at_utc
            FROM reconciliation_events
            ORDER BY reconciliation_event_id DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        return [dict(r) for r in cur.fetchall()]

    def set_pause(self, *, scope_type: str, scope_key: str, reason: str | None) -> None:
        now = utcnow_iso()
        self._conn.execute(
            """
            INSERT INTO automation_pauses(scope_type, scope_key, status, reason, created_at_utc, updated_at_utc)
            VALUES(?, ?, 'active', ?, ?, ?)
            """,
            (scope_type, scope_key, reason, now, now),
        )

    def clear_pause(self, *, scope_type: str, scope_key: str) -> None:
        now = utcnow_iso()
        self._conn.execute(
            """
            UPDATE automation_pauses
            SET status = 'cleared', updated_at_utc = ?
            WHERE scope_type = ? AND scope_key = ? AND status = 'active'
            """,
            (now, scope_type, scope_key),
        )

    def clear_all_scoped_pauses(self) -> None:
        now = utcnow_iso()
        self._conn.execute(
            "UPDATE automation_pauses SET status = 'cleared', updated_at_utc = ? WHERE status = 'active'",
            (now,),
        )

    def list_active_pauses(self) -> list[dict]:
        cur = self._conn.execute(
            """
            SELECT pause_id, scope_type, scope_key, reason, created_at_utc
            FROM automation_pauses
            WHERE status = 'active'
            ORDER BY pause_id DESC
            """
        )
        return [dict(r) for r in cur.fetchall()]

    def is_loop_paused(self, *, loop_id: int) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM automation_pauses WHERE scope_type = 'loop' AND scope_key = ? AND status = 'active' LIMIT 1",
            (str(loop_id),),
        )
        return cur.fetchone() is not None

    def is_symbol_paused(self, *, symbol: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM automation_pauses WHERE scope_type = 'symbol' AND scope_key = ? AND status = 'active' LIMIT 1",
            (str(symbol).upper(),),
        )
        return cur.fetchone() is not None

    def close_position_external(self, *, position_id: int, reason: str | None = None) -> None:
        now = utcnow_iso()
        self._conn.execute(
            """
            UPDATE positions
            SET status = 'CLOSED',
                closed_at_utc = ?,
                updated_at_utc = ?
            WHERE id = ? AND status = 'OPEN'
            """,
            (now, now, int(position_id)),
        )

    def create_monitoring_event(
        self,
        *,
        position_id: int,
        symbol: str,
        entry_price: str | None,
        current_price: str | None,
        pnl_percent: str | None,
        decision: str,
        exit_reason: str | None,
        deadline_utc: str | None,
        position_status: str | None,
        error_code: str | None,
        error_message: str | None,
    ) -> int:
        now = utcnow_iso()
        cur = self._conn.execute(
            """
            INSERT INTO monitoring_events(
              position_id, created_at_utc, symbol,
              entry_price, current_price, pnl_percent,
              decision, exit_reason, deadline_utc, position_status,
              error_code, error_message
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(position_id),
                now,
                symbol,
                entry_price,
                current_price,
                pnl_percent,
                decision,
                exit_reason,
                deadline_utc,
                position_status,
                error_code,
                error_message,
            ),
        )
        return int(cur.lastrowid)

    def list_monitoring_events(self, *, limit: int = 50) -> list[dict]:
        cur = self._conn.execute(
            """
            SELECT monitoring_event_id, position_id, created_at_utc, symbol,
                   entry_price, current_price, pnl_percent,
                   decision, exit_reason, deadline_utc, position_status,
                   error_code, error_message
            FROM monitoring_events
            ORDER BY monitoring_event_id DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        return [dict(r) for r in cur.fetchall()]

    def create_manual_order(
        self,
        *,
        dry_run: bool,
        execution_environment: str,
        base_url: str,
        symbol: str,
        side: str,
        order_type: str,
        time_in_force: str | None,
        limit_price: str | None,
        quote_order_qty: str | None,
        quantity: str | None,
        client_order_id: str,
        message: str | None,
        details_json: str | None,
    ) -> int:
        now = utcnow_iso()
        cur = self._conn.execute(
            """
            INSERT INTO manual_orders(
              created_at_utc, updated_at_utc,
              dry_run, execution_environment, base_url,
              symbol, side, order_type, time_in_force, limit_price,
              quote_order_qty, quantity, client_order_id,
              binance_order_id, local_status, raw_status, retry_count,
              executed_quantity, avg_fill_price, total_quote_value,
              fee_breakdown_json, message, details_json
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                now,
                1 if dry_run else 0,
                execution_environment,
                base_url,
                symbol,
                side,
                order_type,
                time_in_force,
                limit_price,
                quote_order_qty,
                quantity,
                client_order_id,
                None,
                "created",
                None,
                0,
                None,
                None,
                None,
                None,
                message,
                details_json,
            ),
        )
        return int(cur.lastrowid)

    def update_manual_order(
        self,
        *,
        manual_order_id: int,
        local_status: str,
        raw_status: str | None,
        binance_order_id: str | None,
        retry_count: int,
        executed_quantity: str | None,
        avg_fill_price: str | None,
        total_quote_value: str | None,
        fee_breakdown_json: str | None,
        message: str | None,
        details_json: str | None,
    ) -> None:
        now = utcnow_iso()
        self._conn.execute(
            """
            UPDATE manual_orders
            SET updated_at_utc = ?,
                local_status = ?,
                raw_status = ?,
                binance_order_id = ?,
                retry_count = ?,
                executed_quantity = ?,
                avg_fill_price = ?,
                total_quote_value = ?,
                fee_breakdown_json = COALESCE(?, fee_breakdown_json),
                message = COALESCE(?, message),
                details_json = COALESCE(?, details_json)
            WHERE manual_order_id = ?
            """,
            (
                now,
                local_status,
                raw_status,
                binance_order_id,
                int(retry_count),
                executed_quantity,
                avg_fill_price,
                total_quote_value,
                fee_breakdown_json,
                message,
                details_json,
                int(manual_order_id),
            ),
        )

    def list_manual_orders(self, *, limit: int = 20) -> list[dict]:
        cur = self._conn.execute(
            """
            SELECT manual_order_id, created_at_utc, dry_run, execution_environment,
                   symbol, side, order_type, local_status, raw_status,
                   quote_order_qty, quantity, limit_price, binance_order_id, client_order_id
            FROM manual_orders
            ORDER BY manual_order_id DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_manual_order(self, *, manual_order_id: int) -> dict | None:
        cur = self._conn.execute("SELECT * FROM manual_orders WHERE manual_order_id = ?", (int(manual_order_id),))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_manual_orders_for_reconcile(self, *, limit: int = 50) -> list[dict]:
        cur = self._conn.execute(
            """
            SELECT * FROM manual_orders
            WHERE local_status IN ('created','submitting','submitted','open','partially_filled','uncertain_submitted','retry_submitted')
            ORDER BY manual_order_id DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        return [dict(r) for r in cur.fetchall()]

    # ---- Manual loop trading mode (Phase 12) ----

    def create_loop_session(
        self,
        *,
        dry_run: bool,
        status: str,
        execution_environment: str,
        base_url: str,
        preset_id: int | None,
        symbol: str,
        quote_qty: str,
        entry_order_type: str,
        entry_limit_price: str | None,
        take_profit_kind: str,
        take_profit_value: str,
        rebuy_kind: str | None,
        rebuy_value: str | None,
        stop_loss_kind: str | None,
        stop_loss_value: str | None,
        stop_loss_action: str,
        cleanup_policy: str,
        max_cycles: int,
        state: str,
        pnl_quote_asset: str | None,
    ) -> int:
        now = utcnow_iso()
        cur = self._conn.execute(
            """
            INSERT INTO loop_sessions(
              created_at_utc, updated_at_utc,
              dry_run, status, execution_environment, base_url,
              preset_id, symbol, quote_qty,
              entry_order_type, entry_limit_price,
              take_profit_kind, take_profit_value,
              rebuy_kind, rebuy_value,
              stop_loss_kind, stop_loss_value, stop_loss_action, cleanup_policy,
              max_cycles, cycles_completed, state,
              cumulative_realized_pnl_quote, pnl_quote_asset,
              stopped_at_utc, last_error, last_warning,
              last_buy_leg_id, last_sell_leg_id,
              last_buy_avg_price, last_sell_avg_price,
              last_buy_executed_qty, last_sell_executed_qty
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)
            """,
            (
                now,
                now,
                1 if dry_run else 0,
                status,
                execution_environment,
                base_url,
                int(preset_id) if preset_id is not None else None,
                symbol,
                quote_qty,
                entry_order_type,
                entry_limit_price,
                take_profit_kind,
                take_profit_value,
                rebuy_kind,
                rebuy_value,
                stop_loss_kind,
                stop_loss_value,
                stop_loss_action,
                cleanup_policy,
                int(max_cycles),
                state,
                pnl_quote_asset,
            ),
        )
        return int(cur.lastrowid)

    def update_loop_session(
        self,
        *,
        loop_id: int,
        status: str | None = None,
        state: str | None = None,
        cycles_completed: int | None = None,
        last_buy_leg_id: int | None = None,
        last_sell_leg_id: int | None = None,
        last_buy_avg_price: str | None = None,
        last_sell_avg_price: str | None = None,
        last_buy_executed_qty: str | None = None,
        last_sell_executed_qty: str | None = None,
        cumulative_realized_pnl_quote: str | None = None,
        stopped_at_utc: str | None = None,
        last_error: str | None = None,
        last_warning: str | None = None,
    ) -> None:
        now = utcnow_iso()
        # COALESCE keeps previous value when None.
        self._conn.execute(
            """
            UPDATE loop_sessions
            SET updated_at_utc = ?,
                status = COALESCE(?, status),
                state = COALESCE(?, state),
                cycles_completed = COALESCE(?, cycles_completed),
                last_buy_leg_id = COALESCE(?, last_buy_leg_id),
                last_sell_leg_id = COALESCE(?, last_sell_leg_id),
                last_buy_avg_price = COALESCE(?, last_buy_avg_price),
                last_sell_avg_price = COALESCE(?, last_sell_avg_price),
                last_buy_executed_qty = COALESCE(?, last_buy_executed_qty),
                last_sell_executed_qty = COALESCE(?, last_sell_executed_qty),
                cumulative_realized_pnl_quote = COALESCE(?, cumulative_realized_pnl_quote),
                stopped_at_utc = COALESCE(?, stopped_at_utc),
                last_error = COALESCE(?, last_error),
                last_warning = COALESCE(?, last_warning)
            WHERE loop_id = ?
            """,
            (
                now,
                status,
                state,
                cycles_completed,
                last_buy_leg_id,
                last_sell_leg_id,
                last_buy_avg_price,
                last_sell_avg_price,
                last_buy_executed_qty,
                last_sell_executed_qty,
                cumulative_realized_pnl_quote,
                stopped_at_utc,
                last_error,
                last_warning,
                int(loop_id),
            ),
        )

    def get_loop_session(self, *, loop_id: int) -> dict | None:
        cur = self._conn.execute("SELECT * FROM loop_sessions WHERE loop_id = ?", (int(loop_id),))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_loop_sessions(self, *, limit: int = 20) -> list[dict]:
        cur = self._conn.execute(
            """
            SELECT loop_id, created_at_utc, dry_run, status, execution_environment, symbol,
                   quote_qty, entry_order_type, max_cycles, cycles_completed,
                   cumulative_realized_pnl_quote, pnl_quote_asset, last_error
            FROM loop_sessions
            ORDER BY loop_id DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_latest_loop_session(self, *, status: str | None = None) -> dict | None:
        if status:
            cur = self._conn.execute(
                "SELECT * FROM loop_sessions WHERE status = ? ORDER BY loop_id DESC LIMIT 1",
                (status,),
            )
        else:
            cur = self._conn.execute("SELECT * FROM loop_sessions ORDER BY loop_id DESC LIMIT 1")
        row = cur.fetchone()
        return dict(row) if row else None

    def create_loop_leg(
        self,
        *,
        loop_id: int,
        cycle_index: int,
        leg_role: str,
        side: str,
        order_type: str,
        time_in_force: str | None,
        limit_price: str | None,
        quote_order_qty: str | None,
        quantity: str | None,
        client_order_id: str,
        message: str | None,
    ) -> int:
        now = utcnow_iso()
        cur = self._conn.execute(
            """
            INSERT INTO loop_legs(
              loop_id, created_at_utc, updated_at_utc,
              cycle_index, leg_role, side, order_type,
              time_in_force, limit_price, quote_order_qty, quantity,
              client_order_id, binance_order_id,
              local_status, raw_status, retry_count,
              executed_quantity, avg_fill_price, total_quote_value,
              fee_breakdown_json, message, submitted_at_utc, reconciled_at_utc, filled_at_utc
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL, ?, NULL, NULL, NULL)
            """,
            (
                int(loop_id),
                now,
                now,
                int(cycle_index),
                leg_role,
                side,
                order_type,
                time_in_force,
                limit_price,
                quote_order_qty,
                quantity,
                client_order_id,
                None,
                "created",
                None,
                message,
            ),
        )
        return int(cur.lastrowid)

    def update_loop_leg(
        self,
        *,
        leg_id: int,
        local_status: str,
        raw_status: str | None,
        binance_order_id: str | None,
        retry_count: int,
        executed_quantity: str | None,
        avg_fill_price: str | None,
        total_quote_value: str | None,
        fee_breakdown_json: str | None,
        message: str | None,
        submitted_at_utc: str | None = None,
        reconciled_at_utc: str | None = None,
        filled_at_utc: str | None = None,
    ) -> None:
        now = utcnow_iso()
        self._conn.execute(
            """
            UPDATE loop_legs
            SET updated_at_utc = ?,
                local_status = ?,
                raw_status = ?,
                binance_order_id = COALESCE(?, binance_order_id),
                retry_count = ?,
                executed_quantity = ?,
                avg_fill_price = ?,
                total_quote_value = ?,
                fee_breakdown_json = COALESCE(?, fee_breakdown_json),
                message = COALESCE(?, message),
                submitted_at_utc = COALESCE(?, submitted_at_utc),
                reconciled_at_utc = COALESCE(?, reconciled_at_utc),
                filled_at_utc = COALESCE(?, filled_at_utc)
            WHERE leg_id = ?
            """,
            (
                now,
                local_status,
                raw_status,
                binance_order_id,
                int(retry_count),
                executed_quantity,
                avg_fill_price,
                total_quote_value,
                fee_breakdown_json,
                message,
                submitted_at_utc,
                reconciled_at_utc,
                filled_at_utc,
                int(leg_id),
            ),
        )

    def list_loop_legs(self, *, loop_id: int, limit: int = 50) -> list[dict]:
        cur = self._conn.execute(
            """
            SELECT * FROM loop_legs
            WHERE loop_id = ?
            ORDER BY leg_id DESC
            LIMIT ?
            """,
            (int(loop_id), int(limit)),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_loop_leg(self, *, leg_id: int) -> dict | None:
        cur = self._conn.execute("SELECT * FROM loop_legs WHERE leg_id = ?", (int(leg_id),))
        row = cur.fetchone()
        return dict(row) if row else None

    def get_latest_loop_leg(self, *, loop_id: int) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM loop_legs WHERE loop_id = ? ORDER BY leg_id DESC LIMIT 1",
            (int(loop_id),),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def list_loop_legs_for_reconcile(self, *, loop_id: int, limit: int = 50) -> list[dict]:
        cur = self._conn.execute(
            """
            SELECT * FROM loop_legs
            WHERE loop_id = ?
              AND local_status IN ('created','submitting','submitted','open','partially_filled','uncertain_submitted','retry_submitted')
            ORDER BY leg_id DESC
            LIMIT ?
            """,
            (int(loop_id), int(limit)),
        )
        return [dict(r) for r in cur.fetchall()]

    def list_loop_legs_open(self, *, loop_id: int, limit: int = 500) -> list[dict]:
        cur = self._conn.execute(
            """
            SELECT * FROM loop_legs
            WHERE loop_id = ?
              AND local_status IN ('created','submitting','submitted','open','partially_filled','uncertain_submitted','retry_submitted')
            ORDER BY leg_id ASC
            LIMIT ?
            """,
            (int(loop_id), int(limit)),
        )
        return [dict(r) for r in cur.fetchall()]

    def append_loop_event(
        self,
        *,
        loop_id: int,
        event_type: str,
        preset_id: int | None = None,
        symbol: str | None = None,
        side: str | None = None,
        cycle_number: int | None = None,
        client_order_id: str | None = None,
        binance_order_id: str | None = None,
        price: str | None = None,
        quantity: str | None = None,
        message: str | None = None,
        details: dict | None = None,
    ) -> int:
        now = utcnow_iso()
        cur = self._conn.execute(
            """
            INSERT INTO loop_events(
              loop_id, created_at_utc, event_type,
              preset_id, symbol, side, cycle_number,
              client_order_id, binance_order_id, price, quantity,
              message, details_json
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(loop_id),
                now,
                event_type,
                int(preset_id) if preset_id is not None else None,
                symbol,
                side,
                int(cycle_number) if cycle_number is not None else None,
                client_order_id,
                binance_order_id,
                price,
                quantity,
                message,
                json.dumps(details or {}, separators=(",", ":")),
            ),
        )
        return int(cur.lastrowid)

    # ---- Manual loop presets (saved configs) ----

    def create_loop_preset(
        self,
        *,
        name: str | None,
        notes: str | None,
        symbol: str,
        quote_qty: str,
        entry_order_type: str,
        entry_limit_price: str | None,
        take_profit_kind: str,
        take_profit_value: str,
        rebuy_kind: str | None,
        rebuy_value: str | None,
        stop_loss_kind: str | None,
        stop_loss_value: str | None,
        stop_loss_action: str,
        cleanup_policy: str,
    ) -> int:
        now = utcnow_iso()
        cur = self._conn.execute(
            """
            INSERT INTO loop_presets(
              created_at_utc, updated_at_utc, name, notes,
              symbol, quote_qty,
              entry_order_type, entry_limit_price,
              take_profit_kind, take_profit_value,
              rebuy_kind, rebuy_value,
              stop_loss_kind, stop_loss_value, stop_loss_action, cleanup_policy
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                now,
                name,
                notes,
                symbol,
                quote_qty,
                entry_order_type,
                entry_limit_price,
                take_profit_kind,
                take_profit_value,
                rebuy_kind,
                rebuy_value,
                stop_loss_kind,
                stop_loss_value,
                stop_loss_action,
                cleanup_policy,
            ),
        )
        return int(cur.lastrowid)

    def get_loop_preset(self, *, preset_id: int) -> dict | None:
        cur = self._conn.execute("SELECT * FROM loop_presets WHERE preset_id = ?", (int(preset_id),))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_loop_presets(self, *, limit: int = 50) -> list[dict]:
        cur = self._conn.execute(
            """
            SELECT preset_id, created_at_utc, name, symbol, quote_qty, entry_order_type
            FROM loop_presets
            ORDER BY preset_id DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        return [dict(r) for r in cur.fetchall()]

    def list_loop_events_since(self, *, loop_id: int, after_event_id: int = 0, limit: int = 200) -> list[dict]:
        cur = self._conn.execute(
            """
            SELECT loop_event_id, created_at_utc, event_type, preset_id, symbol, side, cycle_number,
                   client_order_id, binance_order_id, price, quantity, message, details_json
            FROM loop_events
            WHERE loop_id = ? AND loop_event_id > ?
            ORDER BY loop_event_id ASC
            LIMIT ?
            """,
            (int(loop_id), int(after_event_id), int(limit)),
        )
        return [dict(r) for r in cur.fetchall()]
