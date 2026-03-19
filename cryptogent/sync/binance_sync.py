from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from cryptogent.exchange.binance_errors import BinanceAPIError
from cryptogent.exchange.binance_spot import BinanceSpotClient
from cryptogent.state.manager import OrderRow, StateManager
from cryptogent.util.time import ms_to_utc_iso


@dataclass(frozen=True)
class SyncResult:
    kind: str
    status: str
    balances_upserted: int = 0
    open_orders_seen: int = 0


def _orders_from_open_orders(open_orders: list[dict]) -> list[OrderRow]:
    out: list[OrderRow] = []
    for o in open_orders:
        symbol = str(o.get("symbol") or "")
        side = str(o.get("side") or "")
        otype = str(o.get("type") or "")
        status = str(o.get("status") or "")
        if not (symbol and side and otype and status):
            continue

        exchange_order_id = str(o.get("orderId")) if o.get("orderId") is not None else None
        time_in_force = str(o.get("timeInForce")) if o.get("timeInForce") is not None else None
        price = str(o.get("price")) if o.get("price") is not None else None
        quantity = str(o.get("origQty") or "0")
        filled_quantity = str(o.get("executedQty") or "0")
        executed_quantity = filled_quantity

        created_ms = int(o.get("time") or 0)
        updated_ms = int(o.get("updateTime") or created_ms or 0)
        created_at_utc = ms_to_utc_iso(created_ms) if created_ms else ms_to_utc_iso(updated_ms) if updated_ms else ""
        updated_at_utc = ms_to_utc_iso(updated_ms) if updated_ms else created_at_utc

        if not created_at_utc:
            # Fallback to something valid; StateManager doesn't require exactness, just ordering.
            created_at_utc = updated_at_utc or "1970-01-01T00:00:00+00:00"
            updated_at_utc = created_at_utc

        out.append(
            OrderRow(
                exchange_order_id=exchange_order_id,
                symbol=symbol,
                side=side,
                type=otype,
                status=status,
                time_in_force=time_in_force,
                price=price,
                quantity=quantity,
                filled_quantity=filled_quantity,
                executed_quantity=executed_quantity,
                created_at_utc=created_at_utc,
                updated_at_utc=updated_at_utc,
            )
        )
    return out


def sync_balances(*, client: BinanceSpotClient, conn: sqlite3.Connection) -> SyncResult:
    state = StateManager(conn)
    sync_id = state.record_sync_run_start(kind="balances")
    try:
        account = client.get_account()
        balances = client.get_balances()
        state.save_account_snapshot(payload={"kind": "account", "account": account})
        state.upsert_balances(balances)
        try:
            state.reconcile_dust_ledger(balances=balances)
        except Exception:
            pass
        state.append_audit(level="INFO", event="sync_balances_ok", details={"count": len(balances)})
        state.record_sync_run_finish(sync_run_id=sync_id, status="ok", error_msg=None)
        return SyncResult(kind="balances", status="ok", balances_upserted=len(balances))
    except BinanceAPIError as e:
        state.append_audit(level="ERROR", event="sync_balances_error", details={"error": str(e)})
        state.record_sync_run_finish(sync_run_id=sync_id, status="error", error_msg=str(e))
        return SyncResult(kind="balances", status="error", balances_upserted=0)


def sync_open_orders(*, client: BinanceSpotClient, conn: sqlite3.Connection, symbol: str | None = None) -> SyncResult:
    state = StateManager(conn)
    sync_id = state.record_sync_run_start(kind="open_orders")
    try:
        open_orders = client.get_open_orders(symbol=symbol)
        order_rows = _orders_from_open_orders(open_orders)
        state.sync_open_orders(order_rows, symbol=symbol)
        state.append_audit(level="INFO", event="sync_open_orders_ok", details={"count": len(order_rows)})
        state.record_sync_run_finish(sync_run_id=sync_id, status="ok", error_msg=None)
        return SyncResult(kind="open_orders", status="ok", open_orders_seen=len(order_rows))
    except BinanceAPIError as e:
        state.append_audit(level="ERROR", event="sync_open_orders_error", details={"error": str(e)})
        state.record_sync_run_finish(sync_run_id=sync_id, status="error", error_msg=str(e))
        return SyncResult(kind="open_orders", status="error", open_orders_seen=0)


def startup_sync(*, client: BinanceSpotClient, conn: sqlite3.Connection) -> SyncResult:
    state = StateManager(conn)
    sync_id = state.record_sync_run_start(kind="startup")
    try:
        account = client.get_account()
        balances = client.get_balances()
        open_orders = client.get_open_orders()
        state.save_account_snapshot(payload={"kind": "startup", "account": account, "open_orders": open_orders})
        state.upsert_balances(balances)
        try:
            state.reconcile_dust_ledger(balances=balances)
        except Exception:
            pass
        state.sync_open_orders(_orders_from_open_orders(open_orders), symbol=None)
        state.append_audit(
            level="INFO",
            event="startup_sync_ok",
            details={"balances": len(balances), "open_orders": len(open_orders)},
        )
        state.record_sync_run_finish(sync_run_id=sync_id, status="ok", error_msg=None)
        return SyncResult(kind="startup", status="ok", balances_upserted=len(balances), open_orders_seen=len(open_orders))
    except BinanceAPIError as e:
        state.append_audit(level="ERROR", event="startup_sync_error", details={"error": str(e)})
        state.record_sync_run_finish(sync_run_id=sync_id, status="error", error_msg=str(e))
        return SyncResult(kind="startup", status="error", balances_upserted=0, open_orders_seen=0)
