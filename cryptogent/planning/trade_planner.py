from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from cryptogent.config.model import AppConfig
from cryptogent.exchange.binance_spot import BinanceSpotClient
from cryptogent.models.trade_plan import TradePlan
from cryptogent.planning.allocation import AllocationError, allocate
from cryptogent.planning.asset_selector import AssetSelectionError, select_asset
from cryptogent.planning.feasibility import FeasibilityError, evaluate_feasibility, freshness_and_consistency_checks
from cryptogent.planning.strategy import StrategySignal, generate_signal
from cryptogent.state.manager import StateManager
from cryptogent.util.time import utcnow_iso
from cryptogent.validation.binance_rules import RuleError


class PlanningError(RuntimeError):
    pass


def _safe_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def build_trade_plan(
    *,
    cfg: AppConfig,
    state: StateManager,
    trade_request: dict,
    market_client: BinanceSpotClient,
    execution_client: BinanceSpotClient,
    execution_environment: str,
    candle_interval: str = "5m",
    candle_count: int = 288,
) -> TradePlan:
    trade_request_id = int(trade_request["id"])
    request_id = trade_request.get("request_id")

    market_data_environment = "mainnet_public"
    exec_env = execution_environment

    preferred_symbol = (trade_request.get("preferred_symbol") or "").strip().upper() or None
    budget_mode = str(trade_request.get("budget_mode") or "manual").strip().lower()
    budget_asset = str(trade_request.get("budget_asset") or "").strip().upper()
    budget_amount = trade_request.get("budget_amount")

    profit_target_pct_s = str(trade_request.get("profit_target_pct") or "")
    stop_loss_pct_s = str(trade_request.get("stop_loss_pct") or "")
    deadline_utc_s = str(trade_request.get("deadline_utc") or "")

    try:
        profit_target_pct = Decimal(profit_target_pct_s)
        stop_loss_pct = Decimal(stop_loss_pct_s)
    except Exception as e:
        raise PlanningError("Invalid stored profit_target_pct/stop_loss_pct") from e

    try:
        deadline = datetime.fromisoformat(deadline_utc_s.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError as e:
        raise PlanningError("Invalid stored deadline_utc") from e
    remaining_hours = max(0.0, (deadline - datetime.now(UTC)).total_seconds() / 3600.0)
    deadline_hours = int(trade_request.get("deadline_hours") or int(round(remaining_hours)) or 1)

    try:
        selected = select_asset(
            client=market_client,
            preferred_symbol=preferred_symbol,
            budget_asset=budget_asset,
            profit_target_pct=profit_target_pct,
            stop_loss_pct=stop_loss_pct,
            deadline_hours=deadline_hours,
            candle_interval=candle_interval,
            candle_count=candle_count,
        )
    except (AssetSelectionError, RuleError) as e:
        raise PlanningError(str(e)) from e

    # Final freshness + feasibility evaluation for chosen snapshot.
    try:
        md_warnings, hard = freshness_and_consistency_checks(
            snapshot=selected.snapshot, candle_interval=candle_interval, candle_count=candle_count
        )
    except FeasibilityError as e:
        raise PlanningError(str(e)) from e
    if hard:
        raise PlanningError(hard)

    spread_available = (
        selected.snapshot.bid is not None and selected.snapshot.ask is not None and selected.snapshot.spread_pct is not None
    )
    feas = evaluate_feasibility(
        profit_target_pct=profit_target_pct,
        stop_loss_pct=stop_loss_pct,
        deadline_hours=deadline_hours,
        volume_24h_quote=selected.snapshot.volume_24h_quote,
        volatility_pct=selected.snapshot.candles.volatility_pct,
        spread_pct=selected.snapshot.spread_pct,
        spread_available=spread_available,
        warnings=md_warnings,
    )
    if feas.category == "not_feasible":
        raise PlanningError(feas.rejection_reason or "not_feasible")

    try:
        alloc = allocate(
            state=state,
            execution_client=execution_client,
            rules=selected.rules,
            price=selected.snapshot.price,
            budget_mode=budget_mode,
            budget_asset=budget_asset,
            budget_amount=budget_amount,
        )
    except (AllocationError, RuleError) as e:
        raise PlanningError(str(e)) from e

    warnings = list(feas.warnings)
    warnings.extend([w for w in alloc.warnings if w not in warnings])

    signal: StrategySignal = generate_signal(
        feasibility_category=feas.category,
        momentum_pct=selected.snapshot.candles.momentum_pct,
        volatility_pct=selected.snapshot.candles.volatility_pct,
        volume_24h_quote=selected.snapshot.volume_24h_quote,
    )

    rules_snapshot = {
        "symbol": selected.rules.symbol,
        "status": selected.rules.status,
        "base_asset": selected.rules.base_asset,
        "quote_asset": selected.rules.quote_asset,
        "step_size": str(selected.rules.lot_size.step_size) if selected.rules.lot_size else None,
        "min_qty": str(selected.rules.lot_size.min_qty) if selected.rules.lot_size else None,
        "max_qty": str(selected.rules.lot_size.max_qty) if selected.rules.lot_size else None,
        "min_notional": str(selected.rules.min_notional.min_notional) if selected.rules.min_notional else None,
        "tick_size": str(selected.rules.price_filter.tick_size) if selected.rules.price_filter else None,
        "price_source": "ticker_price",
        "price_time_ms": selected.snapshot.price_time_ms,
        "candle_interval": candle_interval,
        "candle_count": candle_count,
        "candle_first_open_time_ms": selected.snapshot.candles.first_open_time_ms,
        "candle_last_close_time_ms": selected.snapshot.candles.last_close_time_ms,
        "planning_time_utc": utcnow_iso(),
        "market_data_environment": market_data_environment,
        "execution_environment": exec_env,
    }

    market_summary = {
        "profit_target_pct": profit_target_pct_s,
        "stop_loss_pct": stop_loss_pct_s,
        "deadline_utc": deadline_utc_s,
        "deadline_hours": deadline_hours,
        "volume_24h_quote": str(selected.snapshot.volume_24h_quote),
        "volatility_pct": str(selected.snapshot.candles.volatility_pct),
        "momentum_pct": str(selected.snapshot.candles.momentum_pct),
        "spread_pct": str(selected.snapshot.spread_pct) if selected.snapshot.spread_pct is not None else None,
        "bid": str(selected.snapshot.bid) if selected.snapshot.bid is not None else None,
        "ask": str(selected.snapshot.ask) if selected.snapshot.ask is not None else None,
        "balance_source": alloc.balance_source,
        "fee_buffer_pct": str(alloc.fee_buffer_pct),
        "safety_buffer_pct": str(alloc.safety_buffer_pct),
        "signal_confidence": str(signal.confidence),
    }

    created_at_utc = utcnow_iso()
    plan = TradePlan(
        trade_request_id=trade_request_id,
        request_id=request_id,
        status="ready_for_validation",
        feasibility_category=feas.category,
        warnings=warnings,
        rejection_reason=feas.rejection_reason,
        market_data_environment=market_data_environment,
        execution_environment=exec_env,
        symbol=selected.rules.symbol,
        price=selected.snapshot.price,
        bid=selected.snapshot.bid,
        ask=selected.snapshot.ask,
        spread_pct=selected.snapshot.spread_pct,
        volume_24h_quote=selected.snapshot.volume_24h_quote,
        volatility_pct=selected.snapshot.candles.volatility_pct,
        momentum_pct=selected.snapshot.candles.momentum_pct,
        budget_mode=budget_mode,
        approved_budget_asset=budget_asset,
        approved_budget_amount=alloc.approved_budget_amount,
        usable_budget_amount=alloc.usable_budget_amount,
        raw_quantity=alloc.raw_quantity,
        rounded_quantity=alloc.rounded_quantity,
        expected_notional=alloc.expected_notional,
        rules_snapshot=rules_snapshot,
        market_summary=market_summary,
        candidate_list=selected.candidates,
        signal=signal.signal,
        signal_reasons=signal.reasons,
        signal_confidence=signal.confidence,
        created_at_utc=created_at_utc,
    )

    state.append_audit(
        level="INFO",
        event="trade_plan_built",
        details={
            "trade_request_id": trade_request_id,
            "symbol": plan.symbol,
            "category": plan.feasibility_category,
            "signal": plan.signal,
            "market_data_environment": plan.market_data_environment,
            "execution_environment": plan.execution_environment,
        },
    )

    return plan


def persist_trade_plan(*, state: StateManager, plan: TradePlan) -> int:
    return state.create_trade_plan(
        trade_request_id=plan.trade_request_id,
        request_id=plan.request_id,
        status=plan.status,
        feasibility_category=plan.feasibility_category,
        warnings_json=_safe_json(plan.warnings),
        rejection_reason=plan.rejection_reason,
        market_data_environment=plan.market_data_environment,
        execution_environment=plan.execution_environment,
        symbol=plan.symbol,
        price=str(plan.price),
        bid=str(plan.bid) if plan.bid is not None else None,
        ask=str(plan.ask) if plan.ask is not None else None,
        spread_pct=str(plan.spread_pct) if plan.spread_pct is not None else None,
        volume_24h_quote=str(plan.volume_24h_quote),
        volatility_pct=str(plan.volatility_pct),
        momentum_pct=str(plan.momentum_pct),
        budget_mode=plan.budget_mode,
        approved_budget_asset=plan.approved_budget_asset,
        approved_budget_amount=str(plan.approved_budget_amount) if plan.approved_budget_amount is not None else None,
        usable_budget_amount=str(plan.usable_budget_amount) if plan.usable_budget_amount is not None else None,
        raw_quantity=str(plan.raw_quantity) if plan.raw_quantity is not None else None,
        rounded_quantity=str(plan.rounded_quantity) if plan.rounded_quantity is not None else None,
        expected_notional=str(plan.expected_notional) if plan.expected_notional is not None else None,
        rules_snapshot_json=_safe_json(plan.rules_snapshot),
        market_summary_json=_safe_json(plan.market_summary),
        candidate_list_json=_safe_json(plan.candidate_list) if plan.candidate_list is not None else None,
        signal=plan.signal,
        signal_reasons_json=_safe_json(plan.signal_reasons),
        created_at_utc=plan.created_at_utc,
    )
