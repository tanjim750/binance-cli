from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class TradePlan:
    trade_request_id: int
    request_id: str | None
    status: str
    feasibility_category: str
    warnings: list[str]
    rejection_reason: str | None
    market_data_environment: str
    execution_environment: str
    symbol: str
    price: Decimal
    bid: Decimal | None
    ask: Decimal | None
    spread_pct: Decimal | None
    volume_24h_quote: Decimal
    volatility_pct: Decimal
    momentum_pct: Decimal
    budget_mode: str
    approved_budget_asset: str
    approved_budget_amount: Decimal | None
    usable_budget_amount: Decimal | None
    raw_quantity: Decimal | None
    rounded_quantity: Decimal | None
    expected_notional: Decimal | None
    rules_snapshot: dict
    market_summary: dict
    candidate_list: list[dict] | None
    signal: str
    signal_reasons: list[str]
    signal_confidence: Decimal
    created_at_utc: str

