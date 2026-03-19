from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from cryptogent.exchange.binance_errors import BinanceAPIError
from cryptogent.exchange.binance_spot import BinanceSpotClient
from cryptogent.state.manager import StateManager
from cryptogent.util.time import utcnow_iso
from cryptogent.validation.binance_rules import quantize_down


class SafetyError(RuntimeError):
    pass


def _d(value: object, name: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as e:
        raise SafetyError(f"Invalid decimal for {name}") from e
    if d.is_nan() or d.is_infinite():
        raise SafetyError(f"Invalid decimal for {name}")
    return d


def _parse_iso_utc(s: str, name: str) -> datetime:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError as e:
        raise SafetyError(f"Invalid {name}") from e


def _pct(numer: Decimal, denom: Decimal) -> Decimal:
    if denom == 0:
        return Decimal("0")
    return (numer / denom) * Decimal("100")


def _json_load(s: object, name: str) -> dict:
    if not s:
        raise SafetyError(f"Missing {name}")
    try:
        obj = json.loads(str(s))
    except Exception as e:
        raise SafetyError(f"Invalid JSON for {name}") from e
    if not isinstance(obj, dict):
        raise SafetyError(f"Expected object for {name}")
    return obj


@dataclass(frozen=True)
class SafetyDecision:
    category: str  # safe | safe_with_warning | unsafe | expired
    validation_status: str  # passed | failed | passed_with_adjustments
    risk_status: str  # approved | approved_with_warning | rejected
    approved_budget_asset: str
    approved_budget_amount: Decimal
    approved_quantity: Decimal
    summary: str
    warnings: list[str]
    errors: list[str]
    details: dict
    created_at_utc: str


def evaluate_safety(
    *,
    state: StateManager,
    execution_client: BinanceSpotClient,
    plan: dict,
    trade_request: dict,
    order_type: str,
    limit_price: Decimal | None,
    position_id: int | None,
    close_mode: str,
    close_amount: Decimal | None,
    close_percent: Decimal | None,
    max_plan_age_minutes: int,
    max_price_drift_warning_pct: Decimal,
    max_price_drift_unsafe_pct: Decimal,
    max_position_pct: Decimal,
    max_stop_loss_pct: Decimal,
) -> SafetyDecision:
    errors: list[str] = []
    warnings: list[str] = []

    created_at_utc_s = str(plan.get("created_at_utc") or "")
    created_at = _parse_iso_utc(created_at_utc_s, "plan.created_at_utc")
    age_min = Decimal(str((datetime.now(UTC) - created_at).total_seconds())) / Decimal("60")
    if age_min > Decimal(str(max_plan_age_minutes)):
        return SafetyDecision(
            category="expired",
            validation_status="failed",
            risk_status="rejected",
            approved_budget_asset=str(plan.get("approved_budget_asset") or ""),
            approved_budget_amount=_d(plan.get("approved_budget_amount") or "0", "approved_budget_amount"),
            approved_quantity=_d(plan.get("rounded_quantity") or "0", "rounded_quantity"),
            summary=f"Plan expired (age_minutes={age_min:.2f} > {max_plan_age_minutes})",
            warnings=[],
            errors=["plan_expired"],
            details={"plan_age_minutes": str(age_min), "max_plan_age_minutes": max_plan_age_minutes},
            created_at_utc=utcnow_iso(),
        )

    ot = (order_type or "").strip().upper()
    is_buy = ot in ("MARKET_BUY", "LIMIT_BUY")
    is_sell = ot in ("MARKET_SELL", "LIMIT_SELL")

    # Fail-closed required fields.
    symbol = str(plan.get("symbol") or "").strip().upper()
    if not symbol:
        errors.append("missing_symbol")
    details: dict = {"symbol": symbol}
    approved_budget_asset = str(plan.get("approved_budget_asset") or "").strip().upper()
    if not approved_budget_asset:
        errors.append("missing_approved_budget_asset")

    qty_s = plan.get("rounded_quantity")
    budget_s = plan.get("approved_budget_amount")
    if is_buy:
        if qty_s in (None, ""):
            errors.append("missing_approved_quantity")
        if budget_s in (None, ""):
            errors.append("missing_approved_budget_amount")
    rules_snapshot = None
    try:
        rules_snapshot = _json_load(plan.get("rules_snapshot_json"), "rules_snapshot_json")
    except SafetyError as e:
        errors.append(str(e))

    if errors:
        return SafetyDecision(
            category="unsafe",
            validation_status="failed",
            risk_status="rejected",
            approved_budget_asset=approved_budget_asset or "-",
            approved_budget_amount=Decimal("0"),
            approved_quantity=Decimal("0"),
            summary="Unsafe: missing required plan fields",
            warnings=warnings,
            errors=errors,
            details={"plan_id": plan.get("id"), "errors": errors},
            created_at_utc=utcnow_iso(),
        )

    if not (is_buy or is_sell):
        errors.append("invalid_order_type")

    quote_asset = str(rules_snapshot.get("quote_asset") or "").strip().upper() if isinstance(rules_snapshot, dict) else ""
    base_asset = str(rules_snapshot.get("base_asset") or "").strip().upper() if isinstance(rules_snapshot, dict) else ""
    if not quote_asset:
        errors.append("missing_quote_asset_in_rules_snapshot")
    if not base_asset:
        errors.append("missing_base_asset_in_rules_snapshot")

    approved_budget_amount = _d(budget_s or "0", "approved_budget_amount") if budget_s not in (None, "") else Decimal("0")
    approved_quantity = _d(qty_s or "0", "rounded_quantity") if qty_s not in (None, "") else Decimal("0")

    # Deterministic validation against stored rules snapshot.
    step_size = _d(rules_snapshot.get("step_size"), "step_size")
    min_qty = _d(rules_snapshot.get("min_qty"), "min_qty")
    min_notional = _d(rules_snapshot.get("min_notional"), "min_notional")
    price = _d(plan.get("price"), "plan.price")
    expected_price_for_notional = price
    if is_buy or is_sell:
        if ot in ("LIMIT_BUY", "LIMIT_SELL"):
            if limit_price is None:
                errors.append("missing_limit_price")
            else:
                if limit_price <= 0:
                    errors.append("invalid_limit_price")
                else:
                    expected_price_for_notional = limit_price

    # Active position policy: multiple active positions per symbol allowed; SELL closes a specific position.
    active: dict | None
    if is_sell and position_id is not None:
        active = state.get_position(position_id=int(position_id))
        if not active:
            errors.append("position_not_found")
        else:
            if str(active.get("status") or "").upper() != "OPEN":
                errors.append("position_not_open")
            if str(active.get("symbol") or "").strip().upper() != symbol:
                errors.append("position_symbol_mismatch")
    else:
        active = state.get_active_position(symbol=symbol)
    if is_buy and active:
        # Multiple active positions per symbol are allowed. If the active position is dust-sized, close it locally.
        try:
            active_qty = _d(active.get("quantity"), "position.quantity")
        except SafetyError:
            warnings.append("active_position_exists")
        else:
            if min_qty > 0 and active_qty < min_qty:
                try:
                    # Move leftover into dust ledger immediately (accounting-only), then close.
                    if base_asset and active_qty > 0:
                        try:
                            entry_price = str(active.get("entry_price") or "").strip()
                            if entry_price:
                                state.add_dust(asset=base_asset, dust_qty=str(active_qty), avg_cost_price=entry_price, needs_reconcile=True)
                        except Exception:
                            pass
                    try:
                        state.update_position_quantity(position_id=int(active["id"]), quantity="0")
                    except Exception:
                        pass
                    state.close_position(position_id=int(active["id"]), status="CLOSED")
                    warnings.append(f"active_position_dust_closed:{active_qty}<{min_qty}")
                    active = None
                except Exception:
                    warnings.append("active_position_exists")
            else:
                warnings.append("active_position_exists")
    if is_sell and not active:
        errors.append("no_active_position")
    requested_sell_qty: Decimal | None = None
    pos_qty: Decimal | None = None
    reserved_qty: Decimal | None = None
    if is_sell and active:
        details["position_id"] = int(active.get("id") or 0)
        try:
            pos_qty = _d(active.get("quantity"), "position.quantity")
        except SafetyError as e:
            errors.append(f"invalid_active_position_quantity:{e}")
        else:
            cm = (close_mode or "all").strip().lower()
            details["close_mode"] = cm
            if cm not in ("amount", "percent", "all"):
                errors.append("invalid_close_mode")
            elif cm == "all":
                requested_sell_qty = pos_qty
            elif cm == "amount":
                if close_amount is None:
                    errors.append("missing_close_amount")
                else:
                    if close_amount <= 0:
                        errors.append("invalid_close_amount")
                    else:
                        requested_sell_qty = close_amount
            else:
                if close_percent is None:
                    errors.append("missing_close_percent")
                else:
                    if close_percent <= 0 or close_percent > 100:
                        errors.append("invalid_close_percent")
                    else:
                        requested_sell_qty = (pos_qty * close_percent) / Decimal("100")

            if requested_sell_qty is not None and requested_sell_qty > pos_qty:
                errors.append("insufficient_position_balance")

        try:
            reserved_qty = state.get_position_reserved_sell_qty(position_id=int(active.get("id")))
        except Exception:
            reserved_qty = None
        if reserved_qty is not None:
            details["reserved_sell_qty"] = str(reserved_qty)
            try:
                state.set_position_locked_qty(position_id=int(active.get("id")), locked_qty=str(reserved_qty))
            except Exception:
                pass

        try:
            fee_asset = str(active.get("fee_asset") or "").strip().upper()
            fee_amount = str(active.get("fee_amount") or "").strip()
            if fee_asset:
                warnings.append(f"position_fee_asset:{fee_asset}")
                if fee_amount:
                    warnings.append(f"position_fee_amount:{fee_amount}")
        except Exception:
            pass

        # For SELL candidates, minNotional must use:
        # - LIMIT_SELL: limit_price
        # - MARKET_SELL: live price (set later)
        if ot == "MARKET_SELL":
            expected_price_for_notional = None
        else:
            expected_price_for_notional = limit_price

    if step_size <= 0:
        errors.append("invalid_step_size")
    if min_qty <= 0:
        errors.append("invalid_min_qty")
    if min_notional <= 0:
        errors.append("invalid_min_notional")
    if is_buy:
        if approved_quantity <= 0:
            errors.append("quantity_non_positive")
        if approved_quantity < min_qty:
            errors.append("min_qty_failed")
        expected_notional = approved_quantity * expected_price_for_notional
        if expected_notional < min_notional:
            errors.append("min_notional_failed")

    # Step alignment check: BUY qty must already be aligned (from plan snapshot).
    if is_buy and step_size > 0 and approved_quantity > 0:
        steps = (approved_quantity / step_size).to_integral_value()
        if steps * step_size != approved_quantity:
            errors.append("quantity_step_mismatch")

    # Risk rules from trade request (BUY open only).
    if is_buy:
        pt = _d(trade_request.get("profit_target_pct"), "profit_target_pct")
        sl = _d(trade_request.get("stop_loss_pct"), "stop_loss_pct")
        if sl <= 0:
            errors.append("missing_or_invalid_stop_loss")
        if sl > max_stop_loss_pct:
            errors.append("stop_loss_too_large")
        if sl >= pt:
            warnings.append("stop_loss_ge_profit_target")

    # Live rechecks (fail-closed).
    try:
        info = execution_client.get_symbol_info(symbol=symbol)
        if not info:
            errors.append("symbol_not_found_live")
        else:
            status = str(info.get("status") or "")
            if status != "TRADING":
                errors.append(f"symbol_not_trading:{status}")
    except (BinanceAPIError, ValueError) as e:
        errors.append(f"live_symbol_check_failed:{e}")

    live_price: Decimal | None = None
    try:
        live_price = _d(execution_client.get_ticker_price(symbol=symbol), "live_price")
        drift = _pct(abs(live_price - price), price if price != 0 else live_price)
        if drift >= max_price_drift_unsafe_pct:
            errors.append(f"price_drift_unsafe:{drift}")
        elif drift >= max_price_drift_warning_pct:
            warnings.append(f"price_drift_warning:{drift}")

        if ot == "LIMIT_SELL" and limit_price is not None and limit_price <= live_price:
            warnings.append("limit_sell_price_le_live_price")
        if ot == "LIMIT_BUY" and limit_price is not None and limit_price >= live_price:
            warnings.append("limit_buy_price_ge_live_price")
    except (BinanceAPIError, SafetyError) as e:
        errors.append(f"live_price_check_failed:{e}")

    try:
        acct = execution_client.get_account()
        balances = acct.get("balances", [])
        if is_buy:
            free = Decimal("0")
            if isinstance(balances, list):
                for b in balances:
                    if isinstance(b, dict) and str(b.get("asset") or "").upper() == approved_budget_asset:
                        free = _d(b.get("free") or "0", "account.free")
                        break
            if approved_budget_amount > free:
                errors.append("insufficient_free_balance")
            if free > 0:
                pct = _pct(approved_budget_amount, free)
                if pct > max_position_pct:
                    errors.append("max_position_pct_exceeded")
        elif is_sell:
            free_base = Decimal("0")
            if isinstance(balances, list):
                for b in balances:
                    if isinstance(b, dict) and str(b.get("asset") or "").upper() == base_asset:
                        free_base = _d(b.get("free") or "0", "account.free_base")
                        break
            if pos_qty is not None and free_base < pos_qty:
                warnings.append(f"free_base_lt_position:{free_base}<{pos_qty}")

            if requested_sell_qty is None:
                errors.append("missing_sell_quantity")
            else:
                max_tradable = free_base
                if pos_qty is not None:
                    max_tradable = min(max_tradable, pos_qty)
                available_tradable = max_tradable
                if reserved_qty is not None:
                    available_tradable = max(Decimal("0"), max_tradable - reserved_qty)
                    details["available_to_sell"] = str(available_tradable)
                if requested_sell_qty > max_tradable:
                    if cm == "all":
                        requested_sell_qty = max_tradable
                        warnings.append("sell_qty_clamped_to_tradable")
                    else:
                        errors.append("insufficient_free_base_balance")
                if requested_sell_qty > available_tradable:
                    if cm == "all":
                        requested_sell_qty = available_tradable
                        warnings.append("sell_qty_clamped_to_available")
                    else:
                        errors.append("insufficient_available_to_sell")
                if not errors:
                    approved_quantity = quantize_down(requested_sell_qty, step_size) if step_size > 0 else Decimal("0")
                    if approved_quantity <= 0:
                        errors.append("sell_qty_rounded_to_zero")
                    elif approved_quantity != requested_sell_qty:
                        warnings.append(f"sell_qty_rounded_down:{approved_quantity}<{requested_sell_qty}")

                    if approved_quantity > free_base:
                        errors.append("insufficient_free_base_balance")

                ref_price = limit_price if ot == "LIMIT_SELL" else live_price
                if ref_price is None or ref_price <= 0:
                    errors.append("missing_reference_price_for_sell")
                else:
                    if approved_quantity < min_qty:
                        errors.append("min_qty_failed")
                    expected_notional = approved_quantity * ref_price
                    if expected_notional < min_notional:
                        errors.append("min_notional_failed")

                    # For SELL candidates, approved budget is an estimate (quote proceeds).
                    approved_budget_asset = quote_asset or approved_budget_asset
                    approved_budget_amount = approved_quantity * ref_price
    except (BinanceAPIError, SafetyError) as e:
        errors.append(f"live_balance_check_failed:{e}")

    if errors:
        details_out = {**details, "warnings": warnings, "errors": errors}
        return SafetyDecision(
            category="unsafe",
            validation_status="failed",
            risk_status="rejected",
            approved_budget_asset=approved_budget_asset,
            approved_budget_amount=approved_budget_amount,
            approved_quantity=approved_quantity,
            summary="Unsafe: one or more safety checks failed",
            warnings=warnings,
            errors=errors,
            details=details_out,
            created_at_utc=utcnow_iso(),
        )

    if warnings:
        details_out = {**details, "warnings": warnings}
        return SafetyDecision(
            category="safe_with_warning",
            validation_status="passed",
            risk_status="approved_with_warning",
            approved_budget_asset=approved_budget_asset,
            approved_budget_amount=approved_budget_amount,
            approved_quantity=approved_quantity,
            summary="Safe with warnings",
            warnings=warnings,
            errors=[],
            details=details_out,
            created_at_utc=utcnow_iso(),
        )

    details_out = {**details}
    return SafetyDecision(
        category="safe",
        validation_status="passed",
        risk_status="approved",
        approved_budget_asset=approved_budget_asset,
        approved_budget_amount=approved_budget_amount,
        approved_quantity=approved_quantity,
        summary="Safe to proceed to execution",
        warnings=[],
        errors=[],
        details=details_out,
        created_at_utc=utcnow_iso(),
    )
