from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class StrategySignal:
    signal: str
    reasons: list[str]
    confidence: Decimal


def generate_signal(
    *,
    feasibility_category: str,
    momentum_pct: Decimal,
    volatility_pct: Decimal,
    volume_24h_quote: Decimal,
) -> StrategySignal:
    reasons: list[str] = []
    confidence = Decimal("0.50")

    if feasibility_category == "not_feasible":
        return StrategySignal(signal="avoid", reasons=["not_feasible"], confidence=Decimal("0"))

    if momentum_pct >= Decimal("0.5"):
        reasons.append("momentum_positive")
        confidence += Decimal("0.15")
    elif momentum_pct <= Decimal("-0.5"):
        reasons.append("momentum_negative")
        confidence -= Decimal("0.15")

    if volatility_pct >= Decimal("5"):
        reasons.append("volatility_elevated")
        confidence -= Decimal("0.10")
    elif volatility_pct <= Decimal("3"):
        reasons.append("volatility_acceptable")
        confidence += Decimal("0.05")

    if volume_24h_quote >= Decimal("5000000"):
        reasons.append("liquidity_strong")
        confidence += Decimal("0.05")
    else:
        reasons.append("liquidity_borderline")
        confidence -= Decimal("0.05")

    if feasibility_category == "high_risk":
        reasons.append("high_risk_category")
        confidence -= Decimal("0.10")
    if feasibility_category == "feasible_with_warning":
        reasons.append("has_warnings")
        confidence -= Decimal("0.05")

    if confidence < Decimal("0"):
        confidence = Decimal("0")
    if confidence > Decimal("0.99"):
        confidence = Decimal("0.99")

    if confidence >= Decimal("0.65") and momentum_pct >= 0:
        signal = "favorable"
    elif confidence >= Decimal("0.45"):
        signal = "neutral"
    elif confidence >= Decimal("0.25"):
        signal = "weak"
    else:
        signal = "avoid"

    return StrategySignal(signal=signal, reasons=reasons, confidence=confidence)

