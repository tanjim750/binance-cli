from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation

from cryptogent.market.candles import pct as pct_candles
from cryptogent.market.market_data_service import MarketSnapshot
from cryptogent.models.feasibility_result import FeasibilityResult


class FeasibilityError(RuntimeError):
    pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def _d(value: object, name: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as e:
        raise FeasibilityError(f"Invalid decimal for {name}") from e
    if d.is_nan() or d.is_infinite():
        raise FeasibilityError(f"Invalid decimal for {name}")
    return d


def freshness_and_consistency_checks(
    *,
    snapshot: MarketSnapshot,
    candle_interval: str,
    candle_count: int,
) -> tuple[list[str], str | None]:
    warnings: list[str] = []
    now_ms = _now_ms()

    # 24h stats freshness via closeTime when available.
    close_time = None
    if isinstance(snapshot.stats_24h, dict) and "closeTime" in snapshot.stats_24h:
        try:
            close_time = int(snapshot.stats_24h["closeTime"])
        except Exception:
            close_time = None
    if close_time is not None:
        age_s = (now_ms - close_time) / 1000.0
        if age_s > 300:
            return warnings, "stale_24h_stats"
        if age_s > 60:
            warnings.append("stale_24h_stats")

    # Candle freshness for deterministic MVP baseline.
    max_age_s = 10 * 60 if candle_interval == "5m" else 0
    if max_age_s:
        age_s = (now_ms - snapshot.candles.last_close_time_ms) / 1000.0
        if age_s > max_age_s * 2:
            return warnings, "stale_candles"
        if age_s > max_age_s:
            warnings.append("stale_candles")

    # Basic checks.
    if snapshot.price <= 0:
        return warnings, "invalid_price"
    if len(snapshot.klines) < candle_count:
        return warnings, "insufficient_candles"

    # High/low sanity.
    if isinstance(snapshot.stats_24h, dict):
        try:
            hi = _d(snapshot.stats_24h.get("highPrice"), "highPrice")
            lo = _d(snapshot.stats_24h.get("lowPrice"), "lowPrice")
            if hi < lo:
                return warnings, "invalid_24h_high_low"
        except FeasibilityError:
            pass

    # Ticker vs last candle close consistency.
    last_close = snapshot.candles.last_close
    diff_pct = pct_candles(abs(snapshot.price - last_close), snapshot.price)
    if diff_pct > Decimal("3"):
        return warnings, "price_candle_inconsistent_hard"
    if diff_pct > Decimal("1"):
        warnings.append("price_candle_inconsistent")

    return warnings, None


def evaluate_feasibility(
    *,
    profit_target_pct: Decimal,
    stop_loss_pct: Decimal,
    deadline_hours: int,
    volume_24h_quote: Decimal,
    volatility_pct: Decimal,
    spread_pct: Decimal | None,
    spread_available: bool,
    warnings: list[str] | None = None,
) -> FeasibilityResult:
    warnings = list(warnings or [])

    # Profit/stop constraints (planning-only; deterministic heuristics).
    if profit_target_pct >= Decimal("50"):
        return FeasibilityResult(category="not_feasible", rejection_reason="profit_target_extreme", warnings=warnings)
    if deadline_hours <= 6 and profit_target_pct >= Decimal("5"):
        return FeasibilityResult(category="not_feasible", rejection_reason="profit_target_deadline_mismatch", warnings=warnings)
    if deadline_hours <= 24 and profit_target_pct >= Decimal("20"):
        return FeasibilityResult(category="not_feasible", rejection_reason="profit_target_deadline_mismatch", warnings=warnings)

    if stop_loss_pct >= profit_target_pct:
        warnings.append("stop_loss_ge_profit_target")

    vol_floor = volatility_pct if volatility_pct >= Decimal("0.10") else Decimal("0.10")
    ratio = profit_target_pct / vol_floor
    if ratio >= Decimal("10"):
        return FeasibilityResult(category="not_feasible", rejection_reason="profit_target_vs_volatility", warnings=warnings)
    if ratio >= Decimal("5"):
        warnings.append("profit_target_vs_volatility_high")
        category = "high_risk"
    elif ratio >= Decimal("3"):
        warnings.append("profit_target_vs_volatility_warning")
        category = "feasible_with_warning"
    else:
        category = "feasible"

    # Liquidity thresholds (quote volume).
    if volume_24h_quote < Decimal("1000000"):
        return FeasibilityResult(category="not_feasible", rejection_reason="liquidity_reject", warnings=warnings)
    if volume_24h_quote < Decimal("5000000"):
        warnings.append("liquidity_warning")

    # Volatility thresholds.
    if volatility_pct >= Decimal("8"):
        return FeasibilityResult(category="not_feasible", rejection_reason="volatility_reject", warnings=warnings)
    if volatility_pct >= Decimal("5"):
        category = "high_risk"
        warnings.append("volatility_high_risk")
    elif volatility_pct >= Decimal("3"):
        if category == "feasible":
            category = "feasible_with_warning"
        warnings.append("volatility_warning")

    # Spread thresholds (requires bid/ask).
    if not spread_available:
        if category == "feasible":
            category = "feasible_with_warning"
        warnings.append("spread_check_skipped_missing_bid_ask")
    else:
        if spread_pct is not None:
            if spread_pct >= Decimal("1.0"):
                return FeasibilityResult(category="not_feasible", rejection_reason="spread_reject", warnings=warnings)
            if spread_pct >= Decimal("0.50"):
                if category == "feasible":
                    category = "feasible_with_warning"
                warnings.append("spread_warning")

    # Deadline policy.
    if deadline_hours > 168:
        category = "high_risk"
        warnings.append("deadline_high_risk")
    elif deadline_hours > 24:
        if category == "feasible":
            category = "feasible_with_warning"
        warnings.append("deadline_warning")

    return FeasibilityResult(category=category, warnings=warnings, rejection_reason=None)
