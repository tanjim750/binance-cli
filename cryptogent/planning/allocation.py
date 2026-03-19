from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from cryptogent.exchange.binance_errors import BinanceAPIError, BinanceAuthError
from cryptogent.exchange.binance_spot import BinanceSpotClient
from cryptogent.state.manager import StateManager
from cryptogent.validation.binance_rules import SymbolRules, precheck_market_buy


class AllocationError(RuntimeError):
    pass


def _d(value: object, name: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as e:
        raise AllocationError(f"Invalid decimal for {name}") from e
    if d.is_nan() or d.is_infinite():
        raise AllocationError(f"Invalid decimal for {name}")
    return d


@dataclass(frozen=True)
class AllocationResult:
    approved_budget_amount: Decimal
    usable_budget_amount: Decimal
    raw_quantity: Decimal
    rounded_quantity: Decimal
    expected_notional: Decimal
    warnings: list[str]
    balance_source: str
    fee_buffer_pct: Decimal
    safety_buffer_pct: Decimal


def allocate(
    *,
    state: StateManager,
    execution_client: BinanceSpotClient,
    rules: SymbolRules,
    price: Decimal,
    budget_mode: str,
    budget_asset: str,
    budget_amount: object | None,
    fee_buffer_pct: Decimal = Decimal("0.25"),
    safety_buffer_pct: Decimal = Decimal("1.0"),
    risk_cap_pct: Decimal = Decimal("5"),
) -> AllocationResult:
    mode = (budget_mode or "manual").strip().lower()
    asset = budget_asset.strip().upper()
    warnings: list[str] = []

    approved_budget_amount: Decimal
    balance_source = "request"

    if mode == "manual":
        approved_budget_amount = _d(budget_amount, "budget_amount")
    elif mode == "auto":
        available: Decimal | None = None
        try:
            acct = execution_client.get_account()
            balances = acct.get("balances", [])
            if isinstance(balances, list):
                for b in balances:
                    if isinstance(b, dict) and str(b.get("asset") or "").upper() == asset:
                        available = _d(b.get("free") or "0", "account.free")
                        break
            balance_source = "exchange"
        except (BinanceAuthError, BinanceAPIError, AllocationError):
            available = None

        if available is None:
            cached = state.get_cached_balance_free(asset=asset)
            available = cached or Decimal("0")
            balance_source = "cache"
            warnings.append("balance_source_cache_only")

        approved_budget_amount = (available * risk_cap_pct) / Decimal("100")
    else:
        raise AllocationError("Invalid budget_mode")

    usable_budget_amount = approved_budget_amount * (Decimal("1") - (fee_buffer_pct + safety_buffer_pct) / Decimal("100"))
    if usable_budget_amount <= 0:
        raise AllocationError("usable_budget_amount <= 0")

    res = precheck_market_buy(
        rules=rules,
        budget_asset=asset,
        budget_amount=usable_budget_amount,
        last_price=price,
    )
    if not res.ok or res.estimated_qty is None or res.notional is None:
        raise AllocationError(res.error or "allocation_failed")

    raw_quantity = usable_budget_amount / price
    rounded_quantity = res.estimated_qty
    expected_notional = res.notional

    return AllocationResult(
        approved_budget_amount=approved_budget_amount,
        usable_budget_amount=usable_budget_amount,
        raw_quantity=raw_quantity,
        rounded_quantity=rounded_quantity,
        expected_notional=expected_notional,
        warnings=warnings,
        balance_source=balance_source,
        fee_buffer_pct=fee_buffer_pct,
        safety_buffer_pct=safety_buffer_pct,
    )

