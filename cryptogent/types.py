from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class TradeRequest:
    profit_target_pct: Decimal
    stop_loss_pct: Decimal
    deadline_utc: datetime
    budget_quote: Decimal
    preferred_symbol: str | None = None

