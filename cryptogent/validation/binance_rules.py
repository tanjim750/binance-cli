from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class LotSizeRule:
    min_qty: Decimal
    max_qty: Decimal
    step_size: Decimal


@dataclass(frozen=True)
class MinNotionalRule:
    min_notional: Decimal


@dataclass(frozen=True)
class PriceFilterRule:
    tick_size: Decimal


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    status: str
    base_asset: str
    quote_asset: str
    lot_size: LotSizeRule | None
    min_notional: MinNotionalRule | None
    price_filter: PriceFilterRule | None


def _d(value: object, name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as e:
        raise RuleError(f"Invalid decimal for {name}") from e


def parse_symbol_rules(symbol_info: dict) -> SymbolRules:
    symbol = str(symbol_info.get("symbol") or "")
    status = str(symbol_info.get("status") or "")
    base_asset = str(symbol_info.get("baseAsset") or "")
    quote_asset = str(symbol_info.get("quoteAsset") or "")
    if not (symbol and status and base_asset and quote_asset):
        raise RuleError("Missing required symbol fields in exchangeInfo")

    lot_size: LotSizeRule | None = None
    min_notional: MinNotionalRule | None = None
    price_filter: PriceFilterRule | None = None

    filters = symbol_info.get("filters") or []
    if isinstance(filters, list):
        for f in filters:
            if not isinstance(f, dict):
                continue
            ftype = f.get("filterType")
            if ftype == "LOT_SIZE":
                lot_size = LotSizeRule(
                    min_qty=_d(f.get("minQty"), "minQty"),
                    max_qty=_d(f.get("maxQty"), "maxQty"),
                    step_size=_d(f.get("stepSize"), "stepSize"),
                )
            elif ftype in ("MIN_NOTIONAL", "NOTIONAL"):
                key = "minNotional" if "minNotional" in f else "minNotional"
                min_notional = MinNotionalRule(min_notional=_d(f.get(key), key))
            elif ftype == "PRICE_FILTER":
                price_filter = PriceFilterRule(tick_size=_d(f.get("tickSize"), "tickSize"))

    return SymbolRules(
        symbol=symbol,
        status=status,
        base_asset=base_asset,
        quote_asset=quote_asset,
        lot_size=lot_size,
        min_notional=min_notional,
        price_filter=price_filter,
    )


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise RuleError("Invalid step size")
    # number of steps = floor(value / step)
    steps = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return steps * step


@dataclass(frozen=True)
class TradePrecheckResult:
    ok: bool
    error: str | None
    estimated_qty: Decimal | None
    notional: Decimal | None


def precheck_market_buy(
    *,
    rules: SymbolRules,
    budget_asset: str,
    budget_amount: Decimal,
    last_price: Decimal,
) -> TradePrecheckResult:
    if rules.status != "TRADING":
        return TradePrecheckResult(ok=False, error=f"symbol not TRADING (status={rules.status})", estimated_qty=None, notional=None)

    if budget_asset.upper() != rules.quote_asset.upper():
        return TradePrecheckResult(
            ok=False,
            error=f"budget asset {budget_asset} must match symbol quote asset {rules.quote_asset}",
            estimated_qty=None,
            notional=None,
        )

    if last_price <= 0:
        return TradePrecheckResult(ok=False, error="last price must be > 0", estimated_qty=None, notional=None)

    qty_raw = budget_amount / last_price
    if qty_raw <= 0:
        return TradePrecheckResult(ok=False, error="budget too small for any quantity", estimated_qty=None, notional=None)

    qty = qty_raw
    if rules.lot_size:
        qty = quantize_down(qty_raw, rules.lot_size.step_size)
        if qty < rules.lot_size.min_qty:
            return TradePrecheckResult(
                ok=False,
                error=f"qty {qty} < minQty {rules.lot_size.min_qty}",
                estimated_qty=qty,
                notional=qty * last_price,
            )
        if qty > rules.lot_size.max_qty:
            return TradePrecheckResult(
                ok=False,
                error=f"qty {qty} > maxQty {rules.lot_size.max_qty}",
                estimated_qty=qty,
                notional=qty * last_price,
            )

    notional = qty * last_price
    if rules.min_notional and notional < rules.min_notional.min_notional:
        return TradePrecheckResult(
            ok=False,
            error=f"notional {notional} < minNotional {rules.min_notional.min_notional}",
            estimated_qty=qty,
            notional=notional,
        )

    if qty <= 0:
        return TradePrecheckResult(ok=False, error="qty rounded down to 0", estimated_qty=qty, notional=notional)

    return TradePrecheckResult(ok=True, error=None, estimated_qty=qty, notional=notional)
