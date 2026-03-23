"""
cryptogent.sync.telegram_sync
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Incremental Telegram channel ingestion.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from cryptogent.config.io import ConfigPaths, ensure_default_config, load_config
from cryptogent.db.connection import connect
from cryptogent.market.news.telegram.telegram_client import TelegramClientError, build_telegram_client, ensure_authorized
from cryptogent.market.news.telegram.telegram_config import build_telegram_config
from cryptogent.market.news.telegram.telegram_fetcher import fetch_channel_messages
from cryptogent.market.news.telegram.telegram_parser import build_keyword_patterns, parse_messages
from cryptogent.market.news.telegram.telegram_state import get_channel_state, update_channel_state
from cryptogent.market.news.telegram.telegram_storage import persist_messages
from cryptogent.state.manager import StateManager
from cryptogent.util.time import utcnow_iso


@dataclass(frozen=True)
class TelegramSyncResult:
    status: str
    channels_total: int
    messages_saved: int


async def _sync_async(*, config_path: str | None, db_path: str | None) -> TelegramSyncResult:
    paths = ConfigPaths.from_cli(
        config_path=Path(config_path) if config_path else None,
        db_path=Path(db_path) if db_path else None,
    )
    config_path = ensure_default_config(paths.config_path)
    cfg = load_config(config_path)
    telegram_cfg = build_telegram_config(cfg)

    if not telegram_cfg.api_id or not telegram_cfg.api_hash:
        return TelegramSyncResult(status="error", channels_total=0, messages_saved=0)

    client = build_telegram_client(
        api_id=telegram_cfg.api_id,
        api_hash=telegram_cfg.api_hash,
        session_path=telegram_cfg.session_path,
    )

    keyword_patterns = build_keyword_patterns(telegram_cfg.keywords)

    messages_saved = 0
    async with client:
        with connect(paths.db_path or cfg.db_path) as conn:
            state = StateManager(conn)
            try:
                await ensure_authorized(client, phone=telegram_cfg.phone)
            except TelegramClientError as exc:
                state.append_audit(
                    level="ERROR",
                    event="telegram_session_error",
                    details={"error": str(exc)},
                )
                return TelegramSyncResult(status="error", channels_total=0, messages_saved=0)

            for channel_cfg in telegram_cfg.channels:
                channel_name = channel_cfg.username
                state_row = get_channel_state(state, channel=channel_name)
                min_id = state_row.last_message_id if state_row else None
                limit = telegram_cfg.backfill_limit if state_row is None else max(50, telegram_cfg.backfill_limit // 2)

                result = await fetch_channel_messages(
                    client,
                    username=channel_name,
                    limit=limit,
                    min_id=min_id,
                    join_channels=telegram_cfg.join_channels,
                )
                if result.error:
                    if "FLOOD_WAIT" in result.error or "FloodWait" in result.error:
                        state.append_audit(
                            level="WARN",
                            event="telegram_rate_limit",
                            details={"channel": channel_name, "error": result.error},
                        )
                    state.append_audit(
                        level="WARN",
                        event="telegram_channel_error",
                        details={"channel": channel_name, "error": result.error},
                    )
                    update_channel_state(state, channel=channel_name, last_message_id=min_id)
                    continue

                fetched = list(result.messages)
                if not fetched:
                    update_channel_state(state, channel=channel_name, last_message_id=min_id)
                    continue

                parsed = parse_messages(
                    fetched,
                    channel=channel_name,
                    source_type=channel_cfg.source_type,
                    keyword_patterns=keyword_patterns,
                )

                messages_saved += persist_messages(state, messages=parsed)

                max_id = max(int(getattr(m, "id", 0) or 0) for m in fetched)
                update_channel_state(state, channel=channel_name, last_message_id=max_id or min_id)

            state.append_audit(
                level="INFO",
                event="telegram_sync_complete",
                details={"at": utcnow_iso(), "messages_saved": messages_saved},
            )
    return TelegramSyncResult(
        status="ok",
        channels_total=len(telegram_cfg.channels),
        messages_saved=messages_saved,
    )


def sync_telegram_channels(*, config_path: str | None = None, db_path: str | None = None) -> TelegramSyncResult:
    return asyncio.run(_sync_async(config_path=config_path, db_path=db_path))
