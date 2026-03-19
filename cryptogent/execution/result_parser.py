from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class ExecutionParseError(RuntimeError):
    pass


def _d(value: object, name: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as e:
        raise ExecutionParseError(f"Invalid decimal for {name}") from e
    if d.is_nan() or d.is_infinite():
        raise ExecutionParseError(f"Invalid decimal for {name}")
    return d


@dataclass(frozen=True)
class FillSummary:
    executed_qty: Decimal
    total_quote_spent: Decimal
    avg_fill_price: Decimal | None
    fills_count: int
    commission_total: Decimal | None
    commission_asset: str | None
    commission_breakdown: dict[str, str]


def parse_fills(order_resp: dict) -> FillSummary:
    fills = order_resp.get("fills")
    if not isinstance(fills, list) or not fills:
        executed_qty = _d(order_resp.get("executedQty") or "0", "executedQty")
        total_quote = _d(order_resp.get("cummulativeQuoteQty") or "0", "cummulativeQuoteQty")
        avg = (total_quote / executed_qty) if executed_qty > 0 else None
        return FillSummary(
            executed_qty=executed_qty,
            total_quote_spent=total_quote,
            avg_fill_price=avg,
            fills_count=0,
            commission_total=None,
            commission_asset=None,
            commission_breakdown={},
        )

    total_qty = Decimal("0")
    total_quote = Decimal("0")
    commission_by_asset: dict[str, Decimal] = {}

    for f in fills:
        if not isinstance(f, dict):
            continue
        price = _d(f.get("price") or "0", "fill.price")
        qty = _d(f.get("qty") or "0", "fill.qty")
        total_qty += qty
        total_quote += price * qty

        c = f.get("commission")
        a = str(f.get("commissionAsset") or "").strip().upper()
        if c is not None and a:
            commission_by_asset[a] = commission_by_asset.get(a, Decimal("0")) + _d(c, "fill.commission")

    avg = (total_quote / total_qty) if total_qty > 0 else None

    commission_breakdown = {k: str(v) for k, v in sorted(commission_by_asset.items(), key=lambda kv: kv[0])}
    commission_total: Decimal | None = None
    commission_asset: str | None = None
    if len(commission_by_asset) == 1:
        (commission_asset, commission_total) = next(iter(commission_by_asset.items()))
    elif len(commission_by_asset) > 1:
        commission_asset = "MIXED"
        commission_total = None

    return FillSummary(
        executed_qty=total_qty,
        total_quote_spent=total_quote,
        avg_fill_price=avg,
        fills_count=len([f for f in fills if isinstance(f, dict)]),
        commission_total=commission_total,
        commission_asset=commission_asset,
        commission_breakdown=commission_breakdown,
    )

