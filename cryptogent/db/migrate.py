from __future__ import annotations

from pathlib import Path

from cryptogent.config.io import DEFAULT_DB_PATH, load_config
from cryptogent.db.connection import connect

TARGET_SCHEMA_VERSION = 34


def _read_schema_sql() -> str:
    schema_path = Path(__file__).with_name("schema.sql")
    return schema_path.read_text(encoding="utf-8")


def _get_schema_version(conn) -> int:
    try:
        cur = conn.execute("SELECT value FROM app_meta WHERE key = ?", ("schema_version",))
        row = cur.fetchone()
        return int(row["value"]) if row else 0
    except Exception:
        return 0


def _column_exists(conn, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(r["name"] == column for r in cur.fetchall())


def _column_notnull(conn, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    for r in cur.fetchall():
        if r["name"] == column:
            return bool(r["notnull"])
    return False


def _add_column_if_missing(conn, table: str, column: str, ddl_fragment: str) -> None:
    if _column_exists(conn, table, column):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl_fragment}")


def _migrate_to_v4(conn) -> None:
    _add_column_if_missing(conn, "trade_requests", "validation_status", "validation_status TEXT")
    _add_column_if_missing(conn, "trade_requests", "validation_error", "validation_error TEXT")
    _add_column_if_missing(conn, "trade_requests", "validated_at_utc", "validated_at_utc TEXT")
    _add_column_if_missing(conn, "trade_requests", "last_price", "last_price TEXT")
    _add_column_if_missing(conn, "trade_requests", "estimated_qty", "estimated_qty TEXT")
    _add_column_if_missing(conn, "trade_requests", "symbol_base_asset", "symbol_base_asset TEXT")
    _add_column_if_missing(conn, "trade_requests", "symbol_quote_asset", "symbol_quote_asset TEXT")


def _migrate_to_v5(conn) -> None:
    # Phase 3 Local State refinements
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS system_state (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          last_start_time_utc TEXT,
          last_shutdown_time_utc TEXT,
          last_successful_sync_time_utc TEXT,
          current_mode TEXT,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL
        )
        """
    )

    _add_column_if_missing(conn, "balances", "snapshot_time_utc", "snapshot_time_utc TEXT")

    _add_column_if_missing(conn, "orders", "time_in_force", "time_in_force TEXT")
    _add_column_if_missing(conn, "orders", "executed_quantity", "executed_quantity TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_exchange_order_id ON orders(exchange_order_id)")

    _add_column_if_missing(conn, "positions", "opened_at_utc", "opened_at_utc TEXT")
    _add_column_if_missing(conn, "positions", "closed_at_utc", "closed_at_utc TEXT")


def _migrate_to_v6(conn) -> None:
    # Phase 4 Trade Input refinements
    # If an older DB has budget_amount as NOT NULL, we rebuild the table to allow NULL (needed for budget_mode=auto).
    if _column_exists(conn, "trade_requests", "budget_amount") and _column_notnull(conn, "trade_requests", "budget_amount"):
        conn.execute(
            """
            CREATE TABLE trade_requests_new (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              request_id TEXT,
              status TEXT NOT NULL,
              preferred_symbol TEXT,
              exit_asset TEXT,
              label TEXT,
              notes TEXT,
              budget_mode TEXT NOT NULL,
              budget_asset TEXT NOT NULL,
              budget_amount TEXT,
              profit_target_pct TEXT NOT NULL,
              stop_loss_pct TEXT NOT NULL,
              deadline_hours INTEGER,
              deadline_utc TEXT NOT NULL,
              validation_status TEXT,
              validation_error TEXT,
              validated_at_utc TEXT,
              last_price TEXT,
              estimated_qty TEXT,
              symbol_base_asset TEXT,
              symbol_quote_asset TEXT,
              created_at_utc TEXT NOT NULL,
              updated_at_utc TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO trade_requests_new(
              id, request_id, status, preferred_symbol, exit_asset, label, notes,
              budget_mode, budget_asset, budget_amount,
              profit_target_pct, stop_loss_pct, deadline_hours, deadline_utc,
              validation_status, validation_error, validated_at_utc, last_price, estimated_qty,
              symbol_base_asset, symbol_quote_asset,
              created_at_utc, updated_at_utc
            )
            SELECT
              id,
              NULL,
              status,
              preferred_symbol,
              NULL,
              NULL,
              NULL,
              COALESCE(budget_mode, 'manual'),
              budget_asset,
              budget_amount,
              profit_target_pct,
              stop_loss_pct,
              NULL,
              deadline_utc,
              validation_status,
              validation_error,
              validated_at_utc,
              last_price,
              estimated_qty,
              symbol_base_asset,
              symbol_quote_asset,
              created_at_utc,
              updated_at_utc
            FROM trade_requests
            """
        )
        conn.execute("DROP TABLE trade_requests")
        conn.execute("ALTER TABLE trade_requests_new RENAME TO trade_requests")

    _add_column_if_missing(conn, "trade_requests", "request_id", "request_id TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_requests_request_id ON trade_requests(request_id)")

    _add_column_if_missing(conn, "trade_requests", "exit_asset", "exit_asset TEXT")


def _migrate_to_v18(conn) -> None:
    # Track source of each cached order row (execution/manual/external).
    _add_column_if_missing(conn, "orders", "order_source", "order_source TEXT NOT NULL DEFAULT 'external'")


def _migrate_to_v19(conn) -> None:
    # Dust ledger for leftover untradable quantities (accounting only).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dust_ledger (
          dust_id INTEGER PRIMARY KEY AUTOINCREMENT,
          asset TEXT NOT NULL UNIQUE,
          dust_qty TEXT NOT NULL,
          avg_cost_price TEXT NOT NULL,
          needs_reconcile INTEGER NOT NULL DEFAULT 1,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL
        )
        """
    )
    # Store base/quote asset on positions so dust ledger can avoid double-counting against open positions.
    _add_column_if_missing(conn, "positions", "base_asset", "base_asset TEXT")
    _add_column_if_missing(conn, "positions", "quote_asset", "quote_asset TEXT")
    _add_column_if_missing(conn, "trade_requests", "label", "label TEXT")
    _add_column_if_missing(conn, "trade_requests", "notes", "notes TEXT")
    _add_column_if_missing(conn, "trade_requests", "budget_mode", "budget_mode TEXT")
    _add_column_if_missing(conn, "trade_requests", "deadline_hours", "deadline_hours INTEGER")


def _migrate_to_v20(conn) -> None:
    # Phase 12 Manual Loop Trading Mode (human-only)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS loop_sessions (
          loop_id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL,
          dry_run INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL,
          execution_environment TEXT NOT NULL,
          base_url TEXT NOT NULL,
          symbol TEXT NOT NULL,
          quote_qty TEXT NOT NULL,
          entry_order_type TEXT NOT NULL,
          entry_limit_price TEXT,
          take_profit_kind TEXT NOT NULL,
          take_profit_value TEXT NOT NULL,
          rebuy_kind TEXT,
          rebuy_value TEXT,
          stop_loss_kind TEXT,
          stop_loss_value TEXT,
          max_cycles INTEGER NOT NULL,
          cycles_completed INTEGER NOT NULL DEFAULT 0,
          state TEXT NOT NULL,
          last_buy_leg_id INTEGER,
          last_sell_leg_id INTEGER,
          last_buy_avg_price TEXT,
          last_sell_avg_price TEXT,
          last_buy_executed_qty TEXT,
          last_sell_executed_qty TEXT,
          cumulative_realized_pnl_quote TEXT,
          pnl_quote_asset TEXT,
          stopped_at_utc TEXT,
          last_error TEXT,
          last_warning TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_loop_sessions_status ON loop_sessions(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_loop_sessions_symbol ON loop_sessions(symbol)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS loop_legs (
          leg_id INTEGER PRIMARY KEY AUTOINCREMENT,
          loop_id INTEGER NOT NULL,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL,
          cycle_index INTEGER NOT NULL,
          leg_role TEXT NOT NULL,
          side TEXT NOT NULL,
          order_type TEXT NOT NULL,
          time_in_force TEXT,
          limit_price TEXT,
          quote_order_qty TEXT,
          quantity TEXT,
          client_order_id TEXT NOT NULL,
          binance_order_id TEXT,
          local_status TEXT NOT NULL,
          raw_status TEXT,
          retry_count INTEGER NOT NULL DEFAULT 0,
          executed_quantity TEXT,
          avg_fill_price TEXT,
          total_quote_value TEXT,
          fee_breakdown_json TEXT,
          message TEXT,
          submitted_at_utc TEXT,
          reconciled_at_utc TEXT,
          filled_at_utc TEXT,
          FOREIGN KEY(loop_id) REFERENCES loop_sessions(loop_id)
        )
        """
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_loop_legs_client_order_id ON loop_legs(client_order_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_loop_legs_loop_id ON loop_legs(loop_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_loop_legs_status ON loop_legs(local_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_loop_legs_binance_order_id ON loop_legs(binance_order_id)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS loop_events (
          loop_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
          loop_id INTEGER NOT NULL,
          created_at_utc TEXT NOT NULL,
          event_type TEXT NOT NULL,
          details_json TEXT,
          FOREIGN KEY(loop_id) REFERENCES loop_sessions(loop_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_loop_events_loop_id ON loop_events(loop_id)")


def _migrate_to_v21(conn) -> None:
    # Phase 12 Manual Loop Trading presets (saved configurations).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS loop_presets (
          preset_id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL,
          name TEXT,
          notes TEXT,
          symbol TEXT NOT NULL,
          quote_qty TEXT NOT NULL,
          entry_order_type TEXT NOT NULL,
          entry_limit_price TEXT,
          take_profit_kind TEXT NOT NULL,
          take_profit_value TEXT NOT NULL,
          rebuy_kind TEXT,
          rebuy_value TEXT,
          stop_loss_kind TEXT,
          stop_loss_value TEXT,
          max_cycles INTEGER NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_loop_presets_symbol ON loop_presets(symbol)")


def _migrate_to_v22(conn) -> None:
    """
    Loop presets should not store max_cycles (chosen at start-time).
    SQLite cannot DROP COLUMN directly, so rebuild loop_presets without max_cycles.
    """
    try:
        cur = conn.execute("PRAGMA table_info(loop_presets)")
        cols = [r["name"] for r in cur.fetchall()]
    except Exception:
        cols = []
    if "loop_presets" not in [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]:
        return
    if "max_cycles" not in cols:
        return

    conn.execute(
        """
        CREATE TABLE loop_presets_new (
          preset_id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL,
          name TEXT,
          notes TEXT,
          symbol TEXT NOT NULL,
          quote_qty TEXT NOT NULL,
          entry_order_type TEXT NOT NULL,
          entry_limit_price TEXT,
          take_profit_kind TEXT NOT NULL,
          take_profit_value TEXT NOT NULL,
          rebuy_kind TEXT,
          rebuy_value TEXT,
          stop_loss_kind TEXT,
          stop_loss_value TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO loop_presets_new(
          preset_id, created_at_utc, updated_at_utc, name, notes, symbol, quote_qty,
          entry_order_type, entry_limit_price,
          take_profit_kind, take_profit_value,
          rebuy_kind, rebuy_value,
          stop_loss_kind, stop_loss_value
        )
        SELECT
          preset_id, created_at_utc, updated_at_utc, name, notes, symbol, quote_qty,
          entry_order_type, entry_limit_price,
          take_profit_kind, take_profit_value,
          rebuy_kind, rebuy_value,
          stop_loss_kind, stop_loss_value
        FROM loop_presets
        """
    )
    conn.execute("DROP TABLE loop_presets")
    conn.execute("ALTER TABLE loop_presets_new RENAME TO loop_presets")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_loop_presets_symbol ON loop_presets(symbol)")


def _migrate_to_v23(conn) -> None:
    # Link loop_sessions to a stored preset (or an auto-created preset).
    _add_column_if_missing(conn, "loop_sessions", "preset_id", "preset_id INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_loop_sessions_preset_id ON loop_sessions(preset_id)")

    # Structured audit fields for loop_events (details_json remains).
    _add_column_if_missing(conn, "loop_events", "preset_id", "preset_id INTEGER")
    _add_column_if_missing(conn, "loop_events", "symbol", "symbol TEXT")
    _add_column_if_missing(conn, "loop_events", "side", "side TEXT")
    _add_column_if_missing(conn, "loop_events", "cycle_number", "cycle_number INTEGER")
    _add_column_if_missing(conn, "loop_events", "client_order_id", "client_order_id TEXT")
    _add_column_if_missing(conn, "loop_events", "binance_order_id", "binance_order_id TEXT")
    _add_column_if_missing(conn, "loop_events", "price", "price TEXT")
    _add_column_if_missing(conn, "loop_events", "quantity", "quantity TEXT")
    _add_column_if_missing(conn, "loop_events", "message", "message TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_loop_events_event_type ON loop_events(event_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_loop_events_preset_id ON loop_events(preset_id)")


def _migrate_to_v24(conn) -> None:
    # Manual loop stop-loss action (default stop_only; optional stop_and_exit).
    _add_column_if_missing(conn, "loop_presets", "stop_loss_action", "stop_loss_action TEXT NOT NULL DEFAULT 'stop_only'")
    _add_column_if_missing(conn, "loop_sessions", "stop_loss_action", "stop_loss_action TEXT NOT NULL DEFAULT 'stop_only'")


def _migrate_to_v25(conn) -> None:
    # Manual loop order cleanup policy (default cancel-open).
    _add_column_if_missing(conn, "loop_presets", "cleanup_policy", "cleanup_policy TEXT NOT NULL DEFAULT 'cancel-open'")
    _add_column_if_missing(conn, "loop_sessions", "cleanup_policy", "cleanup_policy TEXT NOT NULL DEFAULT 'cancel-open'")


def _migrate_to_v26(conn) -> None:
    # Reliability fields on system_state.
    _add_column_if_missing(conn, "system_state", "automation_paused", "automation_paused INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "system_state", "pause_reason", "pause_reason TEXT")
    _add_column_if_missing(conn, "system_state", "paused_at_utc", "paused_at_utc TEXT")
    _add_column_if_missing(conn, "system_state", "last_reconciliation_status", "last_reconciliation_status TEXT")

    # Reconciliation events table.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reconciliation_events (
          reconciliation_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
          event_type TEXT NOT NULL,
          status TEXT NOT NULL,
          summary TEXT NOT NULL,
          details_json TEXT,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reconciliation_events_created ON reconciliation_events(created_at_utc)")


def _migrate_to_v27(conn) -> None:
    # Scoped automation pauses (loop/symbol).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS automation_pauses (
          pause_id INTEGER PRIMARY KEY AUTOINCREMENT,
          scope_type TEXT NOT NULL,
          scope_key TEXT NOT NULL,
          status TEXT NOT NULL,
          reason TEXT,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_automation_pauses_scope ON automation_pauses(scope_type, scope_key, status)")


def _migrate_to_v7(conn) -> None:
    # Repair migration: ensure trade_requests.budget_amount can be NULL (needed for budget_mode=auto).
    if _column_exists(conn, "trade_requests", "budget_amount") and _column_notnull(conn, "trade_requests", "budget_amount"):
        conn.execute(
            """
            CREATE TABLE trade_requests_new (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              request_id TEXT,
              status TEXT NOT NULL,
              preferred_symbol TEXT,
              exit_asset TEXT,
              label TEXT,
              notes TEXT,
              budget_mode TEXT NOT NULL,
              budget_asset TEXT NOT NULL,
              budget_amount TEXT,
              profit_target_pct TEXT NOT NULL,
              stop_loss_pct TEXT NOT NULL,
              deadline_hours INTEGER,
              deadline_utc TEXT NOT NULL,
              validation_status TEXT,
              validation_error TEXT,
              validated_at_utc TEXT,
              last_price TEXT,
              estimated_qty TEXT,
              symbol_base_asset TEXT,
              symbol_quote_asset TEXT,
              created_at_utc TEXT NOT NULL,
              updated_at_utc TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO trade_requests_new(
              id, request_id, status, preferred_symbol, exit_asset, label, notes,
              budget_mode, budget_asset, budget_amount,
              profit_target_pct, stop_loss_pct, deadline_hours, deadline_utc,
              validation_status, validation_error, validated_at_utc, last_price, estimated_qty,
              symbol_base_asset, symbol_quote_asset,
              created_at_utc, updated_at_utc
            )
            SELECT
              id,
              request_id,
              status,
              preferred_symbol,
              exit_asset,
              label,
              notes,
              COALESCE(budget_mode, 'manual'),
              budget_asset,
              budget_amount,
              profit_target_pct,
              stop_loss_pct,
              deadline_hours,
              deadline_utc,
              validation_status,
              validation_error,
              validated_at_utc,
              last_price,
              estimated_qty,
              symbol_base_asset,
              symbol_quote_asset,
              created_at_utc,
              updated_at_utc
            FROM trade_requests
            """
        )
        conn.execute("DROP TABLE trade_requests")
        conn.execute("ALTER TABLE trade_requests_new RENAME TO trade_requests")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_requests_request_id ON trade_requests(request_id)")


def _migrate_to_v8(conn) -> None:
    # Phase 5 Market and Planning: trade_plans table
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_plans (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          trade_request_id INTEGER NOT NULL,
          request_id TEXT,
          status TEXT NOT NULL,
          feasibility_category TEXT NOT NULL,
          warnings_json TEXT,
          rejection_reason TEXT,
          market_data_environment TEXT NOT NULL,
          execution_environment TEXT NOT NULL,
          symbol TEXT NOT NULL,
          price TEXT NOT NULL,
          bid TEXT,
          ask TEXT,
          spread_pct TEXT,
          volume_24h_quote TEXT,
          volatility_pct TEXT,
          momentum_pct TEXT,
          budget_mode TEXT NOT NULL,
          approved_budget_asset TEXT NOT NULL,
          approved_budget_amount TEXT,
          usable_budget_amount TEXT,
          raw_quantity TEXT,
          rounded_quantity TEXT,
          expected_notional TEXT,
          rules_snapshot_json TEXT NOT NULL,
          market_summary_json TEXT NOT NULL,
          candidate_list_json TEXT,
          signal TEXT NOT NULL,
          signal_reasons_json TEXT,
          created_at_utc TEXT NOT NULL,
          FOREIGN KEY(trade_request_id) REFERENCES trade_requests(id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_plans_trade_request_id ON trade_plans(trade_request_id)")


def _migrate_to_v9(conn) -> None:
    # Phase 6 Safety: execution candidates table
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_candidates (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          trade_plan_id INTEGER NOT NULL,
          trade_request_id INTEGER NOT NULL,
          request_id TEXT,
          symbol TEXT NOT NULL,
          side TEXT NOT NULL,
          validation_status TEXT NOT NULL,
          risk_status TEXT NOT NULL,
          approved_budget_asset TEXT NOT NULL,
          approved_budget_amount TEXT NOT NULL,
          approved_quantity TEXT NOT NULL,
          execution_ready INTEGER NOT NULL,
          summary TEXT NOT NULL,
          details_json TEXT,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL,
          FOREIGN KEY(trade_plan_id) REFERENCES trade_plans(id),
          FOREIGN KEY(trade_request_id) REFERENCES trade_requests(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_candidates_trade_plan_id ON execution_candidates(trade_plan_id)"
    )


def _migrate_to_v10(conn) -> None:
    # Phase 7 Execution: executions table
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS executions (
          execution_id INTEGER PRIMARY KEY AUTOINCREMENT,
          candidate_id INTEGER NOT NULL,
          plan_id INTEGER NOT NULL,
          trade_request_id INTEGER NOT NULL,
          symbol TEXT NOT NULL,
          side TEXT NOT NULL,
          order_type TEXT NOT NULL,
          execution_environment TEXT NOT NULL,
          client_order_id TEXT NOT NULL,
          binance_order_id TEXT,
          quote_order_qty TEXT,
          requested_quantity TEXT,
          executed_quantity TEXT,
          avg_fill_price TEXT,
          total_quote_spent TEXT,
          commission_total TEXT,
          commission_asset TEXT,
          fills_count INTEGER,
          local_status TEXT NOT NULL,
          raw_status TEXT,
          retry_count INTEGER NOT NULL,
          submitted_at_utc TEXT,
          reconciled_at_utc TEXT,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL,
          FOREIGN KEY(candidate_id) REFERENCES execution_candidates(id),
          FOREIGN KEY(plan_id) REFERENCES trade_plans(id),
          FOREIGN KEY(trade_request_id) REFERENCES trade_requests(id)
        )
        """
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_executions_client_order_id ON executions(client_order_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_executions_candidate_id ON executions(candidate_id)")


def _migrate_to_v11(conn) -> None:
    # Phase 7 LIMIT BUY extension: add candidate + execution fields
    _add_column_if_missing(conn, "execution_candidates", "order_type", "order_type TEXT NOT NULL DEFAULT 'MARKET_BUY'")
    _add_column_if_missing(conn, "execution_candidates", "limit_price", "limit_price TEXT")

    _add_column_if_missing(conn, "executions", "limit_price", "limit_price TEXT")
    _add_column_if_missing(conn, "executions", "time_in_force", "time_in_force TEXT")
    _add_column_if_missing(conn, "executions", "expired_at_utc", "expired_at_utc TEXT")


def _migrate_to_v12(conn) -> None:
    # Phase 7 LIMIT BUY lock-ins: execution_candidates must store execution_environment.
    _add_column_if_missing(
        conn,
        "execution_candidates",
        "execution_environment",
        "execution_environment TEXT NOT NULL DEFAULT 'mainnet'",
    )
    # Best-effort backfill from linked plan.
    try:
        conn.execute(
            """
            UPDATE execution_candidates
            SET execution_environment = (
              SELECT execution_environment FROM trade_plans WHERE trade_plans.id = execution_candidates.trade_plan_id
            )
            WHERE execution_environment IS NULL OR execution_environment = ''
            """
        )
    except Exception:
        pass


def _migrate_to_v13(conn) -> None:
    # Phase 7/8: record execution/fee metadata on positions for auditability.
    _add_column_if_missing(conn, "positions", "source_execution_id", "source_execution_id INTEGER")
    _add_column_if_missing(conn, "positions", "gross_quantity", "gross_quantity TEXT")
    _add_column_if_missing(conn, "positions", "fee_amount", "fee_amount TEXT")
    _add_column_if_missing(conn, "positions", "fee_asset", "fee_asset TEXT")


def _migrate_to_v14(conn) -> None:
    # Phase 7: store realized PnL (sell only) and full fee breakdown for auditability.
    _add_column_if_missing(conn, "executions", "fee_breakdown_json", "fee_breakdown_json TEXT")
    _add_column_if_missing(conn, "executions", "realized_pnl_quote", "realized_pnl_quote TEXT")
    _add_column_if_missing(conn, "executions", "realized_pnl_quote_asset", "realized_pnl_quote_asset TEXT")
    _add_column_if_missing(conn, "executions", "pnl_warnings_json", "pnl_warnings_json TEXT")


def _migrate_to_v15(conn) -> None:
    # Phase 8 prep: persist environments on positions so monitoring uses the correct price environment.
    _add_column_if_missing(
        conn,
        "positions",
        "market_data_environment",
        "market_data_environment TEXT NOT NULL DEFAULT 'mainnet_public'",
    )
    _add_column_if_missing(
        conn,
        "positions",
        "execution_environment",
        "execution_environment TEXT NOT NULL DEFAULT 'mainnet'",
    )
    _add_column_if_missing(conn, "positions", "last_monitored_at_utc", "last_monitored_at_utc TEXT")

    # Best-effort backfill:
    # - execution_environment from executions via source_execution_id
    # - market_data_environment from linked plan via executions.plan_id
    try:
        conn.execute(
            """
            UPDATE positions
            SET execution_environment = (
              SELECT execution_environment FROM executions WHERE executions.execution_id = positions.source_execution_id
            )
            WHERE (execution_environment IS NULL OR execution_environment = '')
              AND source_execution_id IS NOT NULL
            """
        )
    except Exception:
        pass
    try:
        conn.execute(
            """
            UPDATE positions
            SET market_data_environment = (
              SELECT market_data_environment
              FROM trade_plans
              WHERE trade_plans.id = (
                SELECT plan_id FROM executions WHERE executions.execution_id = positions.source_execution_id
              )
            )
            WHERE (market_data_environment IS NULL OR market_data_environment = '')
              AND source_execution_id IS NOT NULL
            """
        )
    except Exception:
        pass


def _migrate_to_v16(conn) -> None:
    # Phase 8: monitoring events (decisions only; no order placement).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS monitoring_events (
          monitoring_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
          position_id INTEGER NOT NULL,
          created_at_utc TEXT NOT NULL,
          symbol TEXT NOT NULL,
          entry_price TEXT,
          current_price TEXT,
          pnl_percent TEXT,
          decision TEXT NOT NULL,
          exit_reason TEXT,
          deadline_utc TEXT,
          position_status TEXT,
          error_code TEXT,
          error_message TEXT,
          FOREIGN KEY(position_id) REFERENCES positions(id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_monitoring_events_position_id ON monitoring_events(position_id)")


def _migrate_to_v17(conn) -> None:
    # Manual direct order mode (human-only): local persistence for previews, idempotency, and audit.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS manual_orders (
          manual_order_id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL,
          dry_run INTEGER NOT NULL,
          execution_environment TEXT NOT NULL,
          base_url TEXT NOT NULL,
          symbol TEXT NOT NULL,
          side TEXT NOT NULL,
          order_type TEXT NOT NULL,
          time_in_force TEXT,
          limit_price TEXT,
          quote_order_qty TEXT,
          quantity TEXT,
          client_order_id TEXT NOT NULL,
          binance_order_id TEXT,
          local_status TEXT NOT NULL,
          raw_status TEXT,
          retry_count INTEGER NOT NULL,
          executed_quantity TEXT,
          avg_fill_price TEXT,
          total_quote_value TEXT,
          fee_breakdown_json TEXT,
          message TEXT,
          details_json TEXT
        )
        """
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_manual_orders_client_order_id ON manual_orders(client_order_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_manual_orders_created_at ON manual_orders(created_at_utc)")


def _migrate_to_v28(conn) -> None:
    _add_column_if_missing(conn, "positions", "locked_qty", "locked_qty TEXT NOT NULL DEFAULT '0'")
    _add_column_if_missing(conn, "execution_candidates", "position_id", "position_id INTEGER")
    _add_column_if_missing(conn, "executions", "position_id", "position_id INTEGER")


def _migrate_to_v29(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          symbol TEXT NOT NULL,
          timeframe TEXT NOT NULL,
          captured_at_utc TEXT NOT NULL,
          last_price TEXT NOT NULL,
          bid TEXT,
          ask TEXT,
          spread_pct TEXT,
          change_percent TEXT,
          volume_quote TEXT,
          indicators_json TEXT,
          condition_summary TEXT,
          enabled_flags TEXT,
          config_hash TEXT
        )
        """
    )


def _migrate_to_v30(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fear_greed_index (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          value TEXT NOT NULL,
          value_classification TEXT NOT NULL,
          timestamp_utc TEXT NOT NULL,
          time_until_update_s INTEGER,
          source TEXT NOT NULL DEFAULT 'alternative.me',
          raw_json TEXT NOT NULL,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_fear_greed_source_ts ON fear_greed_index(source, timestamp_utc)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fear_greed_created ON fear_greed_index(created_at_utc)")


def _migrate_to_v31(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS news_articles (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          provider TEXT NOT NULL,
          provider_article_id TEXT NOT NULL,
          request_kind TEXT NOT NULL,
          request_params_json TEXT,
          title TEXT NOT NULL,
          description TEXT,
          content TEXT,
          url TEXT NOT NULL,
          image_url TEXT,
          published_at_utc TEXT NOT NULL,
          lang TEXT,
          source_id TEXT,
          source_name TEXT,
          source_url TEXT,
          source_country TEXT,
          fetched_at_utc TEXT NOT NULL,
          raw_json TEXT NOT NULL,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_news_provider_article ON news_articles(provider, provider_article_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_published ON news_articles(published_at_utc)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_provider ON news_articles(provider)")


def _migrate_to_v32(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_channel_state (
          channel TEXT NOT NULL PRIMARY KEY,
          last_message_id INTEGER,
          last_synced_at_utc TEXT,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          channel TEXT NOT NULL,
          message_id INTEGER NOT NULL,
          published_at_utc TEXT NOT NULL,
          text TEXT,
          views INTEGER,
          forwards INTEGER,
          has_media INTEGER NOT NULL DEFAULT 0,
          source_type TEXT,
          sentiment_score REAL,
          impact_score REAL,
          event_hash TEXT,
          raw_json TEXT NOT NULL,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_telegram_channel_message ON telegram_messages(channel, message_id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_telegram_event_hash ON telegram_messages(event_hash)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_telegram_published ON telegram_messages(published_at_utc)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_telegram_channel ON telegram_messages(channel)"
    )


def _migrate_to_v33(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS youtube_channel_state (
          channel_id TEXT NOT NULL PRIMARY KEY,
          channel_name TEXT,
          last_video_published_at_utc TEXT,
          last_synced_at_utc TEXT,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS youtube_discovery_state (
          discovery_key TEXT NOT NULL PRIMARY KEY,
          last_published_at_utc TEXT,
          last_synced_at_utc TEXT,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS youtube_videos (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          video_id TEXT NOT NULL,
          channel_id TEXT NOT NULL,
          channel_title TEXT,
          title TEXT NOT NULL,
          description TEXT,
          published_at_utc TEXT NOT NULL,
          tags_json TEXT,
          view_count INTEGER,
          like_count INTEGER,
          comment_count INTEGER,
          topic_labels_json TEXT,
          sentiment_score REAL,
          impact_score REAL,
          source_type TEXT,
          raw_json TEXT NOT NULL,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS youtube_comments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          video_id TEXT NOT NULL,
          comment_id TEXT NOT NULL,
          published_at_utc TEXT NOT NULL,
          text TEXT,
          like_count INTEGER,
          reply_count INTEGER,
          author_channel_id TEXT,
          source_type TEXT,
          topic_labels_json TEXT,
          sentiment_score REAL,
          impact_score REAL,
          raw_json TEXT NOT NULL,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_youtube_video_id ON youtube_videos(video_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_youtube_channel_id ON youtube_videos(channel_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_youtube_published ON youtube_videos(published_at_utc)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_youtube_comment_id ON youtube_comments(comment_id)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_youtube_comment_video ON youtube_comments(video_id)")


def _migrate_to_v34(conn) -> None:
    _add_column_if_missing(conn, "youtube_videos", "topic_labels_json", "topic_labels_json TEXT")
    _add_column_if_missing(conn, "youtube_videos", "sentiment_score", "sentiment_score REAL")
    _add_column_if_missing(conn, "youtube_videos", "impact_score", "impact_score REAL")
    _add_column_if_missing(conn, "youtube_videos", "source_type", "source_type TEXT")
def ensure_db_initialized(*, config_path: Path, db_path: Path | None) -> Path:
    cfg = load_config(config_path)
    resolved_db_path = (db_path or cfg.db_path or DEFAULT_DB_PATH).expanduser()

    resolved_db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = _read_schema_sql()

    with connect(resolved_db_path) as conn:
        # Important: Only apply the full schema.sql on *new* databases.
        # On existing DBs, schema.sql may include indexes on columns introduced by later migrations,
        # which would fail with "no such column" before we get a chance to migrate.
        current = _get_schema_version(conn)
        if current <= 0:
            conn.executescript(schema_sql)
            current = _get_schema_version(conn)
        if current < 4:
            _migrate_to_v4(conn)
        if current < 5:
            _migrate_to_v5(conn)
        if current < 6:
            _migrate_to_v6(conn)
        if current < 7:
            _migrate_to_v7(conn)
        if current < 8:
            _migrate_to_v8(conn)
        if current < 9:
            _migrate_to_v9(conn)
        if current < 10:
            _migrate_to_v10(conn)
        if current < 11:
            _migrate_to_v11(conn)
        if current < 12:
            _migrate_to_v12(conn)
        if current < 13:
            _migrate_to_v13(conn)
        if current < 14:
            _migrate_to_v14(conn)
        if current < 15:
            _migrate_to_v15(conn)
        if current < 16:
            _migrate_to_v16(conn)
        if current < 17:
            _migrate_to_v17(conn)
        if current < 18:
            _migrate_to_v18(conn)
        if current < 19:
            _migrate_to_v19(conn)
        if current < 20:
            _migrate_to_v20(conn)
        if current < 21:
            _migrate_to_v21(conn)
        if current < 22:
            _migrate_to_v22(conn)
        if current < 23:
            _migrate_to_v23(conn)
        if current < 24:
            _migrate_to_v24(conn)
        if current < 25:
            _migrate_to_v25(conn)
        if current < 26:
            _migrate_to_v26(conn)
        if current < 27:
            _migrate_to_v27(conn)
        if current < 28:
            _migrate_to_v28(conn)
        if current < 29:
            _migrate_to_v29(conn)
        if current < 30:
            _migrate_to_v30(conn)
        if current < 31:
            _migrate_to_v31(conn)
        if current < 32:
            _migrate_to_v32(conn)
        if current < 33:
            _migrate_to_v33(conn)
        if current < 34:
            _migrate_to_v34(conn)
        conn.execute(
            "INSERT OR REPLACE INTO app_meta(key, value) VALUES(?, ?)",
            ("schema_version", str(TARGET_SCHEMA_VERSION)),
        )

    return resolved_db_path
