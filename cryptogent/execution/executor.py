from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from cryptogent.exchange.binance_errors import BinanceAPIError
from cryptogent.exchange.binance_spot import BinanceSpotClient
from cryptogent.execution.result_parser import ExecutionParseError, FillSummary, parse_fills
from cryptogent.state.manager import StateManager
from cryptogent.util.time import utcnow_iso
from cryptogent.validation.binance_rules import quantize_down


class ExecutionError(RuntimeError):
    pass


def _d(value: object, name: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except Exception as e:
        raise ExecutionError(f"Invalid decimal for {name}") from e
    if d.is_nan() or d.is_infinite():
        raise ExecutionError(f"Invalid decimal for {name}")
    return d


def _utc_ts_compact() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def generate_client_order_id(*, candidate_id: int) -> str:
    # Keep short to avoid exchange length limits.
    rand = secrets.token_hex(2)  # 4 chars
    return f"cg_{candidate_id}_{_utc_ts_compact()}_{rand}"


@dataclass(frozen=True)
class ExecutionOutcome:
    local_status: str
    raw_status: str | None
    binance_order_id: str | None
    fills: FillSummary | None
    message: str
    details: dict


def _safe_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _commission_assets(fills: FillSummary | None) -> set[str]:
    if not fills:
        return set()
    out: set[str] = set()
    for a in (fills.commission_breakdown or {}).keys():
        a = str(a or "").strip().upper()
        if a:
            out.add(a)
    return out


def _compute_realized_pnl_for_sell(
    *,
    fills: FillSummary | None,
    avg_entry_price: Decimal,
    quote_asset: str,
    base_asset: str,
) -> tuple[Decimal | None, list[str]]:
    """
    Locked rules:
    - realized PnL only from executed SELL fills
    - subtract quote fees directly
    - if fee asset is non-base/non-quote: warn + store separately (no conversion)
    """
    warnings: list[str] = []
    if not fills or fills.executed_qty <= 0:
        return None, warnings
    proceeds_quote = fills.total_quote_spent
    cost_basis_quote = fills.executed_qty * avg_entry_price

    quote_fee = Decimal("0")
    try:
        qf = fills.commission_breakdown.get(quote_asset) if fills.commission_breakdown else None
        if qf not in (None, ""):
            quote_fee = _d(qf, "commission.quote")
    except Exception:
        quote_fee = Decimal("0")

    realized = proceeds_quote - cost_basis_quote - quote_fee

    assets = _commission_assets(fills)
    for a in sorted(assets):
        if a not in (quote_asset, base_asset):
            warnings.append("realized_pnl_excludes_non_quote_fee_conversion")
            warnings.append(f"fee_asset_non_base_non_quote:{a}")
            break

    return realized, warnings


def _maybe_open_position_from_buy(
    *,
    state: StateManager,
    trade_request_id: int,
    symbol: str,
    source_execution_id: int,
    fills: FillSummary | None,
    entry_price_fallback: Decimal,
    market_data_environment: str,
    execution_environment: str,
    rules_snapshot: dict,
) -> None:
    if not fills or fills.executed_qty <= 0:
        return
    if state.get_active_position(symbol=symbol):
        return

    base_asset = str(rules_snapshot.get("base_asset") or "").strip().upper()
    if not base_asset:
        return

    # Use fee-adjusted average cost as the stored position entry price (Average Cost, Decimal-safe).
    entry_price = fills.avg_fill_price if fills.avg_fill_price is not None else entry_price_fallback
    gross_qty = fills.executed_qty
    qty = gross_qty

    fee_asset: str | None = None
    fee_amount: str | None = None
    if fills.commission_asset and fills.commission_total is not None:
        fee_asset = fills.commission_asset
        fee_amount = str(fills.commission_total)

    try:
        base_comm = fills.commission_breakdown.get(base_asset)
        if base_comm not in (None, ""):
            qty = qty - _d(base_comm, "base_commission")
    except Exception:
        pass

    # If BUY commission is charged in quote asset, include it in cost basis (no quantity change).
    quote_asset = str(rules_snapshot.get("quote_asset") or "").strip().upper()
    quote_fee = Decimal("0")
    try:
        if quote_asset and fills.commission_breakdown:
            qf = fills.commission_breakdown.get(quote_asset)
            if qf not in (None, ""):
                quote_fee = _d(qf, "quote_commission")
    except Exception:
        quote_fee = Decimal("0")

    # Recompute entry_price from cost basis and *net* quantity so realized/unrealized PnL is fee-aware.
    try:
        if qty > 0 and fills.total_quote_spent > 0:
            cost_basis_quote = fills.total_quote_spent + quote_fee
            entry_price = cost_basis_quote / qty
    except Exception:
        pass

    if qty <= 0 or entry_price <= 0:
        return

    tr = state.get_trade_request(trade_request_id)
    if not tr:
        return
    try:
        pt_pct = _d(tr.get("profit_target_pct"), "profit_target_pct")
        sl_pct = _d(tr.get("stop_loss_pct"), "stop_loss_pct")
        deadline_utc = str(tr.get("deadline_utc") or "").strip()
    except ExecutionError:
        return
    if not deadline_utc:
        return

    stop_loss_price = entry_price * (Decimal("1") - (sl_pct / Decimal("100")))
    profit_target_price = entry_price * (Decimal("1") + (pt_pct / Decimal("100")))

    state.create_position(
        symbol=symbol,
        base_asset=base_asset,
        quote_asset=str(rules_snapshot.get("quote_asset") or "").strip().upper() or None,
        market_data_environment=market_data_environment,
        execution_environment=execution_environment,
        entry_price=str(entry_price),
        quantity=str(qty),
        source_execution_id=source_execution_id,
        gross_quantity=str(gross_qty),
        fee_amount=fee_amount,
        fee_asset=fee_asset,
        stop_loss_price=str(stop_loss_price),
        profit_target_price=str(profit_target_price),
        deadline_utc=deadline_utc,
    )


def _maybe_update_position_from_sell(
    *,
    state: StateManager,
    symbol: str,
    fills: FillSummary | None,
    rules_snapshot: dict,
) -> None:
    if not fills or fills.executed_qty <= 0:
        return
    pos = state.get_active_position(symbol=symbol)
    if not pos:
        return
    try:
        current_qty = _d(pos.get("quantity"), "position.quantity")
    except ExecutionError:
        return
    # If SELL commission is charged in base asset, reflect it through reduced net quantity.
    base_asset = str(rules_snapshot.get("base_asset") or "").strip().upper()
    base_fee = Decimal("0")
    try:
        if base_asset and fills and fills.commission_breakdown:
            bf = fills.commission_breakdown.get(base_asset)
            if bf not in (None, ""):
                base_fee = _d(bf, "commission.base")
    except Exception:
        base_fee = Decimal("0")

    new_qty = current_qty - fills.executed_qty - base_fee
    try:
        min_qty = _d(rules_snapshot.get("min_qty"), "min_qty") if rules_snapshot.get("min_qty") else None
    except ExecutionError:
        min_qty = None

    if new_qty <= 0 or (min_qty is not None and new_qty < min_qty):
        leftover = new_qty if new_qty > 0 else Decimal("0")
        if leftover > 0 and base_asset:
            try:
                entry_price = str(pos.get("entry_price") or "").strip()
                if entry_price:
                    state.add_dust(asset=base_asset, dust_qty=str(leftover), avg_cost_price=entry_price, needs_reconcile=True)
            except Exception:
                pass
        # Avoid double-counting: position is closed and its quantity becomes 0; any leftover is tracked in dust_ledger.
        try:
            state.update_position_quantity(position_id=int(pos["id"]), quantity="0")
        except Exception:
            pass
        state.close_position(position_id=int(pos["id"]), status="CLOSED")
        state.append_audit(
            level="INFO",
            event="position_closed_after_sell",
            details={
                "symbol": symbol,
                "position_id": int(pos["id"]),
                "remaining_qty": str(new_qty),
                "dust_moved": str(leftover),
                "reason": "dust_or_zero",
            },
        )
        return

    state.update_position_quantity(position_id=int(pos["id"]), quantity=str(new_qty))


def execute_market_buy_quote(
    *,
    execution_client: BinanceSpotClient,
    state: StateManager,
    candidate: dict,
    plan: dict,
    rules_snapshot: dict,
    runtime_environment: str,
) -> tuple[int, ExecutionOutcome]:
    candidate_id = int(candidate["id"])
    plan_id = int(candidate["trade_plan_id"])
    trade_request_id = int(candidate["trade_request_id"])
    symbol = str(candidate["symbol"] or "").strip().upper()
    approved_budget_asset = str(candidate["approved_budget_asset"] or "").strip().upper()
    approved_budget_amount = str(candidate["approved_budget_amount"] or "").strip()

    quote_asset = str(rules_snapshot.get("quote_asset") or "").strip().upper()
    if not quote_asset:
        raise ExecutionError("missing_quote_asset_in_rules_snapshot")
    if approved_budget_asset != quote_asset:
        raise ExecutionError(f"approved_budget_asset_mismatch: {approved_budget_asset} != {quote_asset}")

    client_order_id = generate_client_order_id(candidate_id=candidate_id)
    execution_id = state.create_execution(
        candidate_id=candidate_id,
        plan_id=plan_id,
        trade_request_id=trade_request_id,
        symbol=symbol,
        side="BUY",
        order_type="MARKET_BUY",
        execution_environment=runtime_environment,
        client_order_id=client_order_id,
        quote_order_qty=approved_budget_amount,
    )

    state.append_audit(
        level="INFO",
        event="execution_submitting",
        details={
            "execution_id": execution_id,
            "candidate_id": candidate_id,
            "plan_id": plan_id,
            "client_order_id": client_order_id,
            "symbol": symbol,
            "side": "BUY",
            "order_type": "MARKET",
            "execution_environment": runtime_environment,
            "approved_budget": f"{approved_budget_amount} {approved_budget_asset}",
        },
    )

    def _update_uncertain(reason: str, *, retry_count: int) -> None:
        state.update_execution(
            execution_id=execution_id,
            local_status="uncertain_submitted",
            raw_status=None,
            binance_order_id=None,
            executed_quantity=None,
            avg_fill_price=None,
            total_quote_spent=None,
            commission_total=None,
            commission_asset=None,
            fills_count=None,
            retry_count=retry_count,
            message=reason,
            details_json=_safe_json({"reason": reason}),
            submitted_at_utc=utcnow_iso(),
            reconciled_at_utc=None,
        )

    def _finalize_from_order(order: dict, *, retry_count: int, note: str) -> ExecutionOutcome:
        raw_status = str(order.get("status") or "") or None
        order_id = str(order.get("orderId") or "") or None
        try:
            fills = parse_fills(order)
        except ExecutionParseError as e:
            fills = None
            note = f"{note}; fill_parse_error={e}"

        local_status = "submitted"
        if raw_status == "FILLED":
            local_status = "filled"
        elif raw_status == "PARTIALLY_FILLED":
            local_status = "partially_filled"

        fee_breakdown_json = _safe_json(fills.commission_breakdown) if fills and fills.commission_breakdown else None
        state.update_execution(
            execution_id=execution_id,
            local_status=local_status,
            raw_status=raw_status,
            binance_order_id=order_id,
            executed_quantity=str(fills.executed_qty) if fills else None,
            avg_fill_price=str(fills.avg_fill_price) if fills and fills.avg_fill_price is not None else None,
            total_quote_spent=str(fills.total_quote_spent) if fills else None,
            commission_total=str(fills.commission_total) if fills and fills.commission_total is not None else None,
            commission_asset=(fills.commission_asset if fills else None),
            fee_breakdown_json=fee_breakdown_json,
            fills_count=(fills.fills_count if fills else None),
            retry_count=retry_count,
            message=note,
            details_json=_safe_json(
                {
                    "raw_status": raw_status,
                    "commission_breakdown": fills.commission_breakdown if fills else {},
                    "source": "exchange",
                }
            ),
            submitted_at_utc=utcnow_iso(),
            reconciled_at_utc=utcnow_iso(),
        )

        return ExecutionOutcome(
            local_status=local_status,
            raw_status=raw_status,
            binance_order_id=order_id,
            fills=fills,
            message=note,
            details={"commission_breakdown": fills.commission_breakdown if fills else {}},
        )

    def _reconcile_or_not_found(*, retry_count: int) -> tuple[bool, dict | None]:
        try:
            order = execution_client.get_order_by_client_order_id(symbol=symbol, client_order_id=client_order_id)
            return True, order
        except BinanceAPIError as e:
            # -2013 is commonly "Order does not exist."
            if e.code == -2013:
                return False, None
            raise

    # First attempt.
    try:
        order = execution_client.create_order_market_buy_quote(
            symbol=symbol, quote_order_qty=approved_budget_amount, client_order_id=client_order_id
        )
        outcome = _finalize_from_order(order, retry_count=0, note="submitted")
        if outcome.local_status in ("filled", "partially_filled"):
            _maybe_open_position_from_buy(
                state=state,
                trade_request_id=trade_request_id,
                symbol=symbol,
                source_execution_id=execution_id,
                fills=outcome.fills,
                entry_price_fallback=_d(plan.get("price"), "plan.price"),
                market_data_environment=str(plan.get("market_data_environment") or "mainnet_public"),
                execution_environment=str(plan.get("execution_environment") or runtime_environment),
                rules_snapshot=rules_snapshot,
            )
        return execution_id, outcome
    except BinanceAPIError as e:
        if e.status != 0:
            state.update_execution(
                execution_id=execution_id,
                local_status="failed",
                raw_status=None,
                binance_order_id=None,
                executed_quantity=None,
                avg_fill_price=None,
                total_quote_spent=None,
                commission_total=None,
                commission_asset=None,
                fills_count=None,
                retry_count=0,
                message=str(e),
                details_json=_safe_json({"error": str(e)}),
                submitted_at_utc=utcnow_iso(),
                reconciled_at_utc=utcnow_iso(),
            )
            return execution_id, ExecutionOutcome(
                local_status="failed",
                raw_status=None,
                binance_order_id=None,
                fills=None,
                message=str(e),
                details={"error": str(e)},
            )

        # Transport-level unknown: uncertain + reconcile + (maybe) retry once.
        _update_uncertain(str(e), retry_count=0)

    # Reconcile before retry.
    try:
        found, order = _reconcile_or_not_found(retry_count=0)
        if found and order is not None:
            outcome = _finalize_from_order(order, retry_count=0, note="reconciled_after_timeout")
            if outcome.local_status in ("filled", "partially_filled"):
                _maybe_open_position_from_buy(
                    state=state,
                    trade_request_id=trade_request_id,
                    symbol=symbol,
                    source_execution_id=execution_id,
                    fills=outcome.fills,
                    entry_price_fallback=_d(plan.get("price"), "plan.price"),
                    market_data_environment=str(plan.get("market_data_environment") or "mainnet_public"),
                    execution_environment=str(plan.get("execution_environment") or runtime_environment),
                    rules_snapshot=rules_snapshot,
                )
            return execution_id, outcome
    except BinanceAPIError as e:
        _update_uncertain(f"reconcile_failed: {e}", retry_count=0)
        return execution_id, ExecutionOutcome(
            local_status="uncertain_submitted",
            raw_status=None,
            binance_order_id=None,
            fills=None,
            message=f"reconcile_failed: {e}",
            details={"error": str(e)},
        )

    # Retry once with same client_order_id.
    try:
        order = execution_client.create_order_market_buy_quote(
            symbol=symbol, quote_order_qty=approved_budget_amount, client_order_id=client_order_id
        )
        outcome = _finalize_from_order(order, retry_count=1, note="submitted_after_retry")
        if outcome.local_status in ("filled", "partially_filled"):
            _maybe_open_position_from_buy(
                state=state,
                trade_request_id=trade_request_id,
                symbol=symbol,
                source_execution_id=execution_id,
                fills=outcome.fills,
                entry_price_fallback=_d(plan.get("price"), "plan.price"),
                market_data_environment=str(plan.get("market_data_environment") or "mainnet_public"),
                execution_environment=str(plan.get("execution_environment") or runtime_environment),
                rules_snapshot=rules_snapshot,
            )
        return execution_id, outcome
    except BinanceAPIError as e:
        # After retry limit, remain uncertain (fail-closed).
        _update_uncertain(f"retry_failed: {e}", retry_count=1)
        return execution_id, ExecutionOutcome(
            local_status="uncertain_submitted",
            raw_status=None,
            binance_order_id=None,
            fills=None,
            message=f"retry_failed: {e}",
            details={"error": str(e)},
        )


def execute_limit_buy(
    *,
    execution_client: BinanceSpotClient,
    state: StateManager,
    candidate: dict,
    plan: dict,
    rules_snapshot: dict,
    runtime_environment: str,
) -> tuple[int, ExecutionOutcome]:
    candidate_id = int(candidate["id"])
    plan_id = int(candidate["trade_plan_id"])
    trade_request_id = int(candidate["trade_request_id"])
    symbol = str(candidate["symbol"] or "").strip().upper()
    approved_budget_asset = str(candidate["approved_budget_asset"] or "").strip().upper()
    approved_budget_amount = Decimal(str(candidate["approved_budget_amount"] or "0"))

    limit_price_s = candidate.get("limit_price")
    if limit_price_s in (None, ""):
        raise ExecutionError("missing_limit_price")
    limit_price_raw = Decimal(str(limit_price_s))

    quote_asset = str(rules_snapshot.get("quote_asset") or "").strip().upper()
    if not quote_asset:
        raise ExecutionError("missing_quote_asset_in_rules_snapshot")
    if approved_budget_asset != quote_asset:
        raise ExecutionError(f"approved_budget_asset_mismatch: {approved_budget_asset} != {quote_asset}")

    tick_size_s = rules_snapshot.get("tick_size")
    step_size_s = rules_snapshot.get("step_size")
    min_qty_s = rules_snapshot.get("min_qty")
    max_qty_s = rules_snapshot.get("max_qty")
    min_notional_s = rules_snapshot.get("min_notional")
    if not (tick_size_s and step_size_s and min_qty_s and max_qty_s and min_notional_s):
        raise ExecutionError("missing_rules_snapshot_fields_for_limit_buy")

    tick_size = Decimal(str(tick_size_s))
    step_size = Decimal(str(step_size_s))
    min_qty = Decimal(str(min_qty_s))
    max_qty = Decimal(str(max_qty_s))
    min_notional = Decimal(str(min_notional_s))
    if tick_size <= 0 or step_size <= 0:
        raise ExecutionError("invalid_tick_or_step_size")

    limit_price = quantize_down(limit_price_raw, tick_size)
    if limit_price <= 0:
        raise ExecutionError("limit_price_rounded_non_positive")

    raw_qty = approved_budget_amount / limit_price
    qty = quantize_down(raw_qty, step_size)
    if qty <= 0:
        raise ExecutionError("quantity_rounded_to_zero")
    if qty < min_qty:
        raise ExecutionError(f"qty_below_minQty:{qty}<{min_qty}")
    if qty > max_qty:
        raise ExecutionError(f"qty_above_maxQty:{qty}>{max_qty}")
    notional = qty * limit_price
    if notional < min_notional:
        raise ExecutionError(f"min_notional_failed:{notional}<{min_notional}")

    client_order_id = generate_client_order_id(candidate_id=candidate_id)
    time_in_force = "GTC"
    execution_id = state.create_execution(
        candidate_id=candidate_id,
        plan_id=plan_id,
        trade_request_id=trade_request_id,
        symbol=symbol,
        side="BUY",
        order_type="LIMIT_BUY",
        execution_environment=runtime_environment,
        client_order_id=client_order_id,
        quote_order_qty=str(approved_budget_amount),
        limit_price=str(limit_price),
        time_in_force=time_in_force,
        requested_quantity=str(qty),
    )

    state.append_audit(
        level="INFO",
        event="execution_submitting",
        details={
            "execution_id": execution_id,
            "candidate_id": candidate_id,
            "plan_id": plan_id,
            "client_order_id": client_order_id,
            "symbol": symbol,
            "side": "BUY",
            "order_type": "LIMIT",
            "execution_environment": runtime_environment,
            "price": str(limit_price),
            "quantity": str(qty),
            "approved_budget": f"{approved_budget_amount} {approved_budget_asset}",
        },
    )

    def _update_uncertain(reason: str, *, retry_count: int) -> None:
        state.update_execution(
            execution_id=execution_id,
            local_status="uncertain_submitted",
            raw_status=None,
            binance_order_id=None,
            executed_quantity=None,
            avg_fill_price=None,
            total_quote_spent=None,
            commission_total=None,
            commission_asset=None,
            fills_count=None,
            retry_count=retry_count,
            message=reason,
            details_json=_safe_json({"reason": reason}),
            submitted_at_utc=utcnow_iso(),
            reconciled_at_utc=None,
        )

    def _finalize_from_order(order: dict, *, retry_count: int, note: str) -> ExecutionOutcome:
        raw_status = str(order.get("status") or "") or None
        order_id = str(order.get("orderId") or "") or None
        try:
            fills = parse_fills(order)
        except ExecutionParseError as e:
            fills = None
            note = f"{note}; fill_parse_error={e}"

        local_status = "submitted"
        if raw_status in ("NEW",):
            local_status = "open"
        elif raw_status == "FILLED":
            local_status = "filled"
        elif raw_status == "PARTIALLY_FILLED":
            local_status = "partially_filled"
        elif raw_status in ("CANCELED", "CANCELLED"):
            local_status = "cancelled"
        elif raw_status in ("EXPIRED",):
            local_status = "expired"

        fee_breakdown_json = _safe_json(fills.commission_breakdown) if fills and fills.commission_breakdown else None
        state.update_execution(
            execution_id=execution_id,
            local_status=local_status,
            raw_status=raw_status,
            binance_order_id=order_id,
            executed_quantity=str(fills.executed_qty) if fills else None,
            avg_fill_price=str(fills.avg_fill_price) if fills and fills.avg_fill_price is not None else None,
            total_quote_spent=str(fills.total_quote_spent) if fills else None,
            commission_total=str(fills.commission_total) if fills and fills.commission_total is not None else None,
            commission_asset=(fills.commission_asset if fills else None),
            fee_breakdown_json=fee_breakdown_json,
            fills_count=(fills.fills_count if fills else None),
            retry_count=retry_count,
            message=note,
            details_json=_safe_json(
                {
                    "raw_status": raw_status,
                    "commission_breakdown": fills.commission_breakdown if fills else {},
                    "source": "exchange",
                }
            ),
            submitted_at_utc=utcnow_iso(),
            reconciled_at_utc=utcnow_iso(),
        )

        return ExecutionOutcome(
            local_status=local_status,
            raw_status=raw_status,
            binance_order_id=order_id,
            fills=fills,
            message=note,
            details={"commission_breakdown": fills.commission_breakdown if fills else {}},
        )

    def _reconcile_or_not_found() -> tuple[bool, dict | None]:
        try:
            order = execution_client.get_order_by_client_order_id(symbol=symbol, client_order_id=client_order_id)
            return True, order
        except BinanceAPIError as e:
            if e.code == -2013:
                return False, None
            raise

    try:
        order = execution_client.create_order_limit_buy(
            symbol=symbol,
            price=str(limit_price),
            quantity=str(qty),
            client_order_id=client_order_id,
            time_in_force=time_in_force,
        )
        outcome = _finalize_from_order(order, retry_count=0, note="submitted")
        if outcome.local_status in ("filled", "partially_filled"):
            _maybe_open_position_from_buy(
                state=state,
                trade_request_id=trade_request_id,
                symbol=symbol,
                source_execution_id=execution_id,
                fills=outcome.fills,
                entry_price_fallback=_d(plan.get("price"), "plan.price"),
                market_data_environment=str(plan.get("market_data_environment") or "mainnet_public"),
                execution_environment=str(plan.get("execution_environment") or runtime_environment),
                rules_snapshot=rules_snapshot,
            )
        return execution_id, outcome
    except BinanceAPIError as e:
        if e.status != 0:
            state.update_execution(
                execution_id=execution_id,
                local_status="failed",
                raw_status=None,
                binance_order_id=None,
                executed_quantity=None,
                avg_fill_price=None,
                total_quote_spent=None,
                commission_total=None,
                commission_asset=None,
                fills_count=None,
                retry_count=0,
                message=str(e),
                details_json=_safe_json({"error": str(e)}),
                submitted_at_utc=utcnow_iso(),
                reconciled_at_utc=utcnow_iso(),
            )
            return execution_id, ExecutionOutcome(
                local_status="failed",
                raw_status=None,
                binance_order_id=None,
                fills=None,
                message=str(e),
                details={"error": str(e)},
            )
        _update_uncertain(str(e), retry_count=0)

    try:
        found, order = _reconcile_or_not_found()
        if found and order is not None:
            outcome = _finalize_from_order(order, retry_count=0, note="reconciled_after_timeout")
            if outcome.local_status in ("filled", "partially_filled"):
                _maybe_open_position_from_buy(
                    state=state,
                    trade_request_id=trade_request_id,
                    symbol=symbol,
                    source_execution_id=execution_id,
                    fills=outcome.fills,
                    entry_price_fallback=_d(plan.get("price"), "plan.price"),
                    market_data_environment=str(plan.get("market_data_environment") or "mainnet_public"),
                    execution_environment=str(plan.get("execution_environment") or runtime_environment),
                    rules_snapshot=rules_snapshot,
                )
            return execution_id, outcome
    except BinanceAPIError as e:
        _update_uncertain(f"reconcile_failed: {e}", retry_count=0)
        return execution_id, ExecutionOutcome(
            local_status="uncertain_submitted",
            raw_status=None,
            binance_order_id=None,
            fills=None,
            message=f"reconcile_failed: {e}",
            details={"error": str(e)},
        )

    try:
        order = execution_client.create_order_limit_buy(
            symbol=symbol,
            price=str(limit_price),
            quantity=str(qty),
            client_order_id=client_order_id,
            time_in_force=time_in_force,
        )
        outcome = _finalize_from_order(order, retry_count=1, note="submitted_after_retry")
        if outcome.local_status in ("filled", "partially_filled"):
            _maybe_open_position_from_buy(
                state=state,
                trade_request_id=trade_request_id,
                symbol=symbol,
                source_execution_id=execution_id,
                fills=outcome.fills,
                entry_price_fallback=_d(plan.get("price"), "plan.price"),
                market_data_environment=str(plan.get("market_data_environment") or "mainnet_public"),
                execution_environment=str(plan.get("execution_environment") or runtime_environment),
                rules_snapshot=rules_snapshot,
            )
        return execution_id, outcome
    except BinanceAPIError as e:
        _update_uncertain(f"retry_failed: {e}", retry_count=1)
        return execution_id, ExecutionOutcome(
            local_status="uncertain_submitted",
            raw_status=None,
            binance_order_id=None,
            fills=None,
            message=f"retry_failed: {e}",
            details={"error": str(e)},
        )


def execute_market_sell_qty(
    *,
    execution_client: BinanceSpotClient,
    state: StateManager,
    candidate: dict,
    plan: dict,
    rules_snapshot: dict,
    runtime_environment: str,
) -> tuple[int, ExecutionOutcome]:
    candidate_id = int(candidate["id"])
    plan_id = int(candidate["trade_plan_id"])
    trade_request_id = int(candidate["trade_request_id"])
    symbol = str(candidate["symbol"] or "").strip().upper()

    step_size_s = rules_snapshot.get("step_size")
    min_qty_s = rules_snapshot.get("min_qty")
    max_qty_s = rules_snapshot.get("max_qty")
    min_notional_s = rules_snapshot.get("min_notional")
    if not (step_size_s and min_qty_s and max_qty_s and min_notional_s):
        raise ExecutionError("missing_rules_snapshot_fields_for_market_sell")
    step_size = Decimal(str(step_size_s))
    min_qty = Decimal(str(min_qty_s))
    max_qty = Decimal(str(max_qty_s))
    min_notional = Decimal(str(min_notional_s))
    price = _d(plan.get("price"), "plan.price")

    qty_raw = _d(candidate.get("approved_quantity") or "0", "approved_quantity")
    qty = quantize_down(qty_raw, step_size)
    if qty <= 0:
        raise ExecutionError("quantity_rounded_to_zero")
    if qty < min_qty:
        raise ExecutionError(f"qty_below_minQty:{qty}<{min_qty}")
    if qty > max_qty:
        raise ExecutionError(f"qty_above_maxQty:{qty}>{max_qty}")
    # MARKET SELL minNotional should use a realistic reference price.
    # Prefer the safety-time proceeds estimate (approved_budget_amount / qty) to avoid drift from stale plan.price.
    ref_price = price
    try:
        proceeds = candidate.get("approved_budget_amount")
        if proceeds not in (None, "") and qty > 0:
            ref_price = _d(proceeds, "approved_budget_amount") / qty
    except Exception:
        ref_price = price
    if (qty * ref_price) < min_notional:
        raise ExecutionError("min_notional_failed")

    client_order_id = generate_client_order_id(candidate_id=candidate_id)
    position_id = candidate.get("position_id")
    execution_id = state.create_execution(
        candidate_id=candidate_id,
        plan_id=plan_id,
        trade_request_id=trade_request_id,
        symbol=symbol,
        side="SELL",
        order_type="MARKET_SELL",
        execution_environment=runtime_environment,
        client_order_id=client_order_id,
        position_id=int(position_id) if position_id not in (None, "") else None,
        quote_order_qty=None,
        requested_quantity=str(qty),
    )

    state.append_audit(
        level="INFO",
        event="execution_submitting",
        details={
            "execution_id": execution_id,
            "candidate_id": candidate_id,
            "plan_id": plan_id,
            "client_order_id": client_order_id,
            "symbol": symbol,
            "side": "SELL",
            "order_type": "MARKET",
            "execution_environment": runtime_environment,
            "quantity": str(qty),
        },
    )

    def _update_uncertain(reason: str, *, retry_count: int) -> None:
        state.update_execution(
            execution_id=execution_id,
            local_status="uncertain_submitted",
            raw_status=None,
            binance_order_id=None,
            executed_quantity=None,
            avg_fill_price=None,
            total_quote_spent=None,
            commission_total=None,
            commission_asset=None,
            fills_count=None,
            retry_count=retry_count,
            message=reason,
            details_json=_safe_json({"reason": reason}),
            submitted_at_utc=utcnow_iso(),
            reconciled_at_utc=None,
        )

    def _finalize_from_order(order: dict, *, retry_count: int, note: str) -> ExecutionOutcome:
        raw_status = str(order.get("status") or "") or None
        order_id = str(order.get("orderId") or "") or None
        try:
            fills = parse_fills(order)
        except ExecutionParseError as e:
            fills = None
            note = f"{note}; fill_parse_error={e}"

        local_status = "submitted"
        if raw_status == "FILLED":
            local_status = "filled"
        elif raw_status == "PARTIALLY_FILLED":
            local_status = "partially_filled"

        fee_breakdown_json = _safe_json(fills.commission_breakdown) if fills and fills.commission_breakdown else None
        realized_pnl_quote = None
        realized_pnl_quote_asset = None
        pnl_warnings_json = None
        if fills and fills.executed_qty > 0:
            pos = state.get_active_position(symbol=symbol)
            if pos:
                try:
                    avg_entry = _d(pos.get("entry_price"), "position.entry_price")
                    quote_asset = str(rules_snapshot.get("quote_asset") or "").strip().upper()
                    base_asset = str(rules_snapshot.get("base_asset") or "").strip().upper()
                    if quote_asset and base_asset and avg_entry > 0:
                        realized, pnl_warns = _compute_realized_pnl_for_sell(
                            fills=fills,
                            avg_entry_price=avg_entry,
                            quote_asset=quote_asset,
                            base_asset=base_asset,
                        )
                        if realized is not None:
                            realized_pnl_quote = str(realized)
                            realized_pnl_quote_asset = quote_asset
                        if pnl_warns:
                            pnl_warnings_json = _safe_json(pnl_warns)
                except Exception:
                    pass

        state.update_execution(
            execution_id=execution_id,
            local_status=local_status,
            raw_status=raw_status,
            binance_order_id=order_id,
            executed_quantity=str(fills.executed_qty) if fills else None,
            avg_fill_price=str(fills.avg_fill_price) if fills and fills.avg_fill_price is not None else None,
            total_quote_spent=str(fills.total_quote_spent) if fills else None,
            commission_total=str(fills.commission_total) if fills and fills.commission_total is not None else None,
            commission_asset=(fills.commission_asset if fills else None),
            fee_breakdown_json=fee_breakdown_json,
            realized_pnl_quote=realized_pnl_quote,
            realized_pnl_quote_asset=realized_pnl_quote_asset,
            pnl_warnings_json=pnl_warnings_json,
            fills_count=(fills.fills_count if fills else None),
            retry_count=retry_count,
            message=note,
            details_json=_safe_json({"raw_status": raw_status, "source": "exchange"}),
            submitted_at_utc=utcnow_iso(),
            reconciled_at_utc=utcnow_iso(),
        )

        return ExecutionOutcome(
            local_status=local_status,
            raw_status=raw_status,
            binance_order_id=order_id,
            fills=fills,
            message=note,
            details={},
        )

    def _reconcile_or_not_found() -> tuple[bool, dict | None]:
        try:
            order = execution_client.get_order_by_client_order_id(symbol=symbol, client_order_id=client_order_id)
            return True, order
        except BinanceAPIError as e:
            if e.code == -2013:
                return False, None
            raise

    try:
        order = execution_client.create_order_market_sell_qty(symbol=symbol, quantity=str(qty), client_order_id=client_order_id)
        outcome = _finalize_from_order(order, retry_count=0, note="submitted")
        if outcome.local_status in ("filled", "partially_filled"):
            _maybe_update_position_from_sell(state=state, symbol=symbol, fills=outcome.fills, rules_snapshot=rules_snapshot)
        return execution_id, outcome
    except BinanceAPIError as e:
        if e.status != 0:
            state.update_execution(
                execution_id=execution_id,
                local_status="failed",
                raw_status=None,
                binance_order_id=None,
                executed_quantity=None,
                avg_fill_price=None,
                total_quote_spent=None,
                commission_total=None,
                commission_asset=None,
                fills_count=None,
                retry_count=0,
                message=str(e),
                details_json=_safe_json({"error": str(e)}),
                submitted_at_utc=utcnow_iso(),
                reconciled_at_utc=utcnow_iso(),
            )
            return execution_id, ExecutionOutcome(
                local_status="failed",
                raw_status=None,
                binance_order_id=None,
                fills=None,
                message=str(e),
                details={"error": str(e)},
            )
        _update_uncertain(str(e), retry_count=0)

    try:
        found, order = _reconcile_or_not_found()
        if found and order is not None:
            outcome = _finalize_from_order(order, retry_count=0, note="reconciled_after_timeout")
            if outcome.local_status in ("filled", "partially_filled"):
                _maybe_update_position_from_sell(state=state, symbol=symbol, fills=outcome.fills, rules_snapshot=rules_snapshot)
            return execution_id, outcome
    except BinanceAPIError as e:
        _update_uncertain(f"reconcile_failed: {e}", retry_count=0)
        return execution_id, ExecutionOutcome(
            local_status="uncertain_submitted",
            raw_status=None,
            binance_order_id=None,
            fills=None,
            message=f"reconcile_failed: {e}",
            details={"error": str(e)},
        )

    try:
        order = execution_client.create_order_market_sell_qty(symbol=symbol, quantity=str(qty), client_order_id=client_order_id)
        outcome = _finalize_from_order(order, retry_count=1, note="submitted_after_retry")
        if outcome.local_status in ("filled", "partially_filled"):
            _maybe_update_position_from_sell(state=state, symbol=symbol, fills=outcome.fills, rules_snapshot=rules_snapshot)
        return execution_id, outcome
    except BinanceAPIError as e:
        _update_uncertain(f"retry_failed: {e}", retry_count=1)
        return execution_id, ExecutionOutcome(
            local_status="uncertain_submitted",
            raw_status=None,
            binance_order_id=None,
            fills=None,
            message=f"retry_failed: {e}",
            details={"error": str(e)},
        )


def execute_limit_sell(
    *,
    execution_client: BinanceSpotClient,
    state: StateManager,
    candidate: dict,
    plan: dict,
    rules_snapshot: dict,
    runtime_environment: str,
) -> tuple[int, ExecutionOutcome]:
    candidate_id = int(candidate["id"])
    plan_id = int(candidate["trade_plan_id"])
    trade_request_id = int(candidate["trade_request_id"])
    symbol = str(candidate["symbol"] or "").strip().upper()

    limit_price_s = candidate.get("limit_price")
    if limit_price_s in (None, ""):
        raise ExecutionError("missing_limit_price")
    limit_price_raw = _d(limit_price_s, "limit_price")

    tick_size_s = rules_snapshot.get("tick_size")
    step_size_s = rules_snapshot.get("step_size")
    min_qty_s = rules_snapshot.get("min_qty")
    max_qty_s = rules_snapshot.get("max_qty")
    min_notional_s = rules_snapshot.get("min_notional")
    if not (tick_size_s and step_size_s and min_qty_s and max_qty_s and min_notional_s):
        raise ExecutionError("missing_rules_snapshot_fields_for_limit_sell")

    tick_size = Decimal(str(tick_size_s))
    step_size = Decimal(str(step_size_s))
    min_qty = Decimal(str(min_qty_s))
    max_qty = Decimal(str(max_qty_s))
    min_notional = Decimal(str(min_notional_s))
    if tick_size <= 0 or step_size <= 0:
        raise ExecutionError("invalid_tick_or_step_size")

    limit_price = quantize_down(limit_price_raw, tick_size)
    if limit_price <= 0:
        raise ExecutionError("limit_price_rounded_non_positive")

    qty_raw = _d(candidate.get("approved_quantity") or "0", "approved_quantity")
    qty = quantize_down(qty_raw, step_size)
    if qty <= 0:
        raise ExecutionError("quantity_rounded_to_zero")
    if qty < min_qty:
        raise ExecutionError(f"qty_below_minQty:{qty}<{min_qty}")
    if qty > max_qty:
        raise ExecutionError(f"qty_above_maxQty:{qty}>{max_qty}")
    notional = qty * limit_price
    if notional < min_notional:
        raise ExecutionError(f"min_notional_failed:{notional}<{min_notional}")

    client_order_id = generate_client_order_id(candidate_id=candidate_id)
    time_in_force = "GTC"
    position_id = candidate.get("position_id")
    execution_id = state.create_execution(
        candidate_id=candidate_id,
        plan_id=plan_id,
        trade_request_id=trade_request_id,
        symbol=symbol,
        side="SELL",
        order_type="LIMIT_SELL",
        execution_environment=runtime_environment,
        client_order_id=client_order_id,
        position_id=int(position_id) if position_id not in (None, "") else None,
        quote_order_qty=None,
        limit_price=str(limit_price),
        time_in_force=time_in_force,
        requested_quantity=str(qty),
    )

    state.append_audit(
        level="INFO",
        event="execution_submitting",
        details={
            "execution_id": execution_id,
            "candidate_id": candidate_id,
            "plan_id": plan_id,
            "client_order_id": client_order_id,
            "symbol": symbol,
            "side": "SELL",
            "order_type": "LIMIT",
            "execution_environment": runtime_environment,
            "price": str(limit_price),
            "quantity": str(qty),
        },
    )

    def _update_uncertain(reason: str, *, retry_count: int) -> None:
        state.update_execution(
            execution_id=execution_id,
            local_status="uncertain_submitted",
            raw_status=None,
            binance_order_id=None,
            executed_quantity=None,
            avg_fill_price=None,
            total_quote_spent=None,
            commission_total=None,
            commission_asset=None,
            fills_count=None,
            retry_count=retry_count,
            message=reason,
            details_json=_safe_json({"reason": reason}),
            submitted_at_utc=utcnow_iso(),
            reconciled_at_utc=None,
        )

    def _finalize_from_order(order: dict, *, retry_count: int, note: str) -> ExecutionOutcome:
        raw_status = str(order.get("status") or "") or None
        order_id = str(order.get("orderId") or "") or None
        try:
            fills = parse_fills(order)
        except ExecutionParseError as e:
            fills = None
            note = f"{note}; fill_parse_error={e}"

        local_status = "submitted"
        if raw_status in ("NEW",):
            local_status = "open"
        elif raw_status == "FILLED":
            local_status = "filled"
        elif raw_status == "PARTIALLY_FILLED":
            local_status = "partially_filled"
        elif raw_status in ("CANCELED", "CANCELLED"):
            local_status = "cancelled"
        elif raw_status in ("EXPIRED",):
            local_status = "expired"

        fee_breakdown_json = _safe_json(fills.commission_breakdown) if fills and fills.commission_breakdown else None
        realized_pnl_quote = None
        realized_pnl_quote_asset = None
        pnl_warnings_json = None
        if fills and fills.executed_qty > 0:
            pos = state.get_active_position(symbol=symbol)
            if pos:
                try:
                    avg_entry = _d(pos.get("entry_price"), "position.entry_price")
                    quote_asset = str(rules_snapshot.get("quote_asset") or "").strip().upper()
                    base_asset = str(rules_snapshot.get("base_asset") or "").strip().upper()
                    if quote_asset and base_asset and avg_entry > 0:
                        realized, pnl_warns = _compute_realized_pnl_for_sell(
                            fills=fills,
                            avg_entry_price=avg_entry,
                            quote_asset=quote_asset,
                            base_asset=base_asset,
                        )
                        if realized is not None:
                            realized_pnl_quote = str(realized)
                            realized_pnl_quote_asset = quote_asset
                        if pnl_warns:
                            pnl_warnings_json = _safe_json(pnl_warns)
                except Exception:
                    pass

        state.update_execution(
            execution_id=execution_id,
            local_status=local_status,
            raw_status=raw_status,
            binance_order_id=order_id,
            executed_quantity=str(fills.executed_qty) if fills else None,
            avg_fill_price=str(fills.avg_fill_price) if fills and fills.avg_fill_price is not None else None,
            total_quote_spent=str(fills.total_quote_spent) if fills else None,
            commission_total=str(fills.commission_total) if fills and fills.commission_total is not None else None,
            commission_asset=(fills.commission_asset if fills else None),
            fee_breakdown_json=fee_breakdown_json,
            realized_pnl_quote=realized_pnl_quote,
            realized_pnl_quote_asset=realized_pnl_quote_asset,
            pnl_warnings_json=pnl_warnings_json,
            fills_count=(fills.fills_count if fills else None),
            retry_count=retry_count,
            message=note,
            details_json=_safe_json({"raw_status": raw_status, "source": "exchange"}),
            submitted_at_utc=utcnow_iso(),
            reconciled_at_utc=utcnow_iso(),
        )

        return ExecutionOutcome(
            local_status=local_status,
            raw_status=raw_status,
            binance_order_id=order_id,
            fills=fills,
            message=note,
            details={},
        )

    def _reconcile_or_not_found() -> tuple[bool, dict | None]:
        try:
            order = execution_client.get_order_by_client_order_id(symbol=symbol, client_order_id=client_order_id)
            return True, order
        except BinanceAPIError as e:
            if e.code == -2013:
                return False, None
            raise

    try:
        order = execution_client.create_order_limit_sell(
            symbol=symbol,
            price=str(limit_price),
            quantity=str(qty),
            client_order_id=client_order_id,
            time_in_force=time_in_force,
        )
        outcome = _finalize_from_order(order, retry_count=0, note="submitted")
        if outcome.local_status in ("filled", "partially_filled"):
            _maybe_update_position_from_sell(state=state, symbol=symbol, fills=outcome.fills, rules_snapshot=rules_snapshot)
        return execution_id, outcome
    except BinanceAPIError as e:
        if e.status != 0:
            state.update_execution(
                execution_id=execution_id,
                local_status="failed",
                raw_status=None,
                binance_order_id=None,
                executed_quantity=None,
                avg_fill_price=None,
                total_quote_spent=None,
                commission_total=None,
                commission_asset=None,
                fills_count=None,
                retry_count=0,
                message=str(e),
                details_json=_safe_json({"error": str(e)}),
                submitted_at_utc=utcnow_iso(),
                reconciled_at_utc=utcnow_iso(),
            )
            return execution_id, ExecutionOutcome(
                local_status="failed",
                raw_status=None,
                binance_order_id=None,
                fills=None,
                message=str(e),
                details={"error": str(e)},
            )
        _update_uncertain(str(e), retry_count=0)

    try:
        found, order = _reconcile_or_not_found()
        if found and order is not None:
            outcome = _finalize_from_order(order, retry_count=0, note="reconciled_after_timeout")
            if outcome.local_status in ("filled", "partially_filled"):
                _maybe_update_position_from_sell(state=state, symbol=symbol, fills=outcome.fills, rules_snapshot=rules_snapshot)
            return execution_id, outcome
    except BinanceAPIError as e:
        _update_uncertain(f"reconcile_failed: {e}", retry_count=0)
        return execution_id, ExecutionOutcome(
            local_status="uncertain_submitted",
            raw_status=None,
            binance_order_id=None,
            fills=None,
            message=f"reconcile_failed: {e}",
            details={"error": str(e)},
        )

    try:
        order = execution_client.create_order_limit_sell(
            symbol=symbol,
            price=str(limit_price),
            quantity=str(qty),
            client_order_id=client_order_id,
            time_in_force=time_in_force,
        )
        outcome = _finalize_from_order(order, retry_count=1, note="submitted_after_retry")
        if outcome.local_status in ("filled", "partially_filled"):
            _maybe_update_position_from_sell(state=state, symbol=symbol, fills=outcome.fills, rules_snapshot=rules_snapshot)
        return execution_id, outcome
    except BinanceAPIError as e:
        _update_uncertain(f"retry_failed: {e}", retry_count=1)
        return execution_id, ExecutionOutcome(
            local_status="uncertain_submitted",
            raw_status=None,
            binance_order_id=None,
            fills=None,
            message=f"retry_failed: {e}",
            details={"error": str(e)},
        )
