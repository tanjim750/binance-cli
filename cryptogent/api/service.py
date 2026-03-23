from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Generic, TypeVar

from cryptogent.config.io import ConfigPaths, ensure_default_config, load_config
from cryptogent.config.model import AppConfig
from cryptogent.db.connection import connect
from cryptogent.db.migrate import ensure_db_initialized
from cryptogent.exchange.binance_errors import BinanceAPIError
from cryptogent.exchange.binance_spot import BinanceSpotClient
from cryptogent.planning.trade_planner import PlanningError, build_trade_plan, persist_trade_plan
from cryptogent.state.manager import StateManager
from cryptogent.validation.binance_rules import RuleError, parse_symbol_rules, precheck_market_buy
from cryptogent.validation.trade_request import ValidationError, validate_trade_request

T = TypeVar("T")


@dataclass(frozen=True)
class ActionError:
    code: str
    message: str
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class ActionResult(Generic[T]):
    ok: bool
    data: T | None = None
    error: ActionError | None = None
    warnings: list[str] | None = None


@dataclass(frozen=True)
class ServiceConfig:
    config_path: Path | None = None
    db_path: Path | None = None
    testnet: bool | None = None
    base_url: str | None = None
    ca_bundle: Path | None = None
    insecure: bool = False


@dataclass(frozen=True)
class CreateTradeRequestInput:
    profit_target_pct: str
    stop_loss_pct: str | None
    deadline: str | None
    deadline_minutes: int | None
    deadline_hours: int | None
    budget_mode: str | None
    budget_asset: str
    budget: str | None
    symbol: str | None
    exit_asset: str | None
    label: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class CreateTradeRequestOutput:
    trade_request_id: int
    status: str


@dataclass(frozen=True)
class ValidateTradeRequestOutput:
    trade_request_id: int
    validation_status: str
    validation_error: str | None
    last_price: str | None
    estimated_qty: str | None
    symbol_base_asset: str | None
    symbol_quote_asset: str | None


@dataclass(frozen=True)
class BuildTradePlanOutput:
    plan_id: int
    trade_request_id: int
    status: str
    symbol: str
    feasibility_category: str
    signal: str
    warnings: list[str]


class ActionService:
    """
    Internal application-layer service.

    - Acts as the single entry point for agent/CLI actions.
    - Enforces config/db initialization.
    - Returns structured results and errors.
    """

    def __init__(self, cfg: ServiceConfig | None = None) -> None:
        self._cfg = cfg or ServiceConfig()

    def _paths(self) -> ConfigPaths:
        return ConfigPaths.from_cli(config_path=self._cfg.config_path, db_path=self._cfg.db_path)

    def _load_config(self) -> AppConfig:
        paths = self._paths()
        config_path = ensure_default_config(paths.config_path)
        return load_config(config_path)

    def _db_path(self) -> Path:
        paths = self._paths()
        config_path = ensure_default_config(paths.config_path)
        return ensure_db_initialized(config_path=config_path, db_path=paths.db_path)

    def _build_client(self) -> BinanceSpotClient:
        cfg = self._load_config()
        client = BinanceSpotClient.from_config(cfg)

        if self._cfg.testnet is True:
            client = BinanceSpotClient(**{**client.__dict__, "base_url": "https://testnet.binance.vision"})

        if self._cfg.base_url:
            client = BinanceSpotClient(**{**client.__dict__, "base_url": str(self._cfg.base_url).strip()})

        if self._cfg.ca_bundle:
            client = BinanceSpotClient(
                **{**client.__dict__, "ca_bundle_path": self._cfg.ca_bundle.expanduser(), "tls_verify": True}
            )
        if self._cfg.insecure:
            client = BinanceSpotClient(**{**client.__dict__, "tls_verify": False})

        return client

    def create_trade_request(self, payload: CreateTradeRequestInput) -> ActionResult[CreateTradeRequestOutput]:
        cfg = self._load_config()
        db_path = self._db_path()

        budget_mode = payload.budget_mode or cfg.trading_default_budget_mode
        exit_asset = payload.exit_asset or cfg.trading_default_exit_asset
        stop_loss_default = cfg.trading_default_stop_loss_pct

        try:
            req = validate_trade_request(
                profit_target_pct=payload.profit_target_pct,
                stop_loss_pct=payload.stop_loss_pct or stop_loss_default,
                deadline=payload.deadline,
                deadline_minutes=payload.deadline_minutes,
                deadline_hours=payload.deadline_hours,
                budget_mode=budget_mode,
                budget_asset=payload.budget_asset,
                budget_amount=payload.budget,
                preferred_symbol=payload.symbol,
                exit_asset=exit_asset,
                label=payload.label,
                notes=payload.notes,
            )
        except ValidationError as e:
            return ActionResult(ok=False, error=ActionError(code="invalid_request", message=str(e)))

        with connect(db_path) as conn:
            state = StateManager(conn)
            trade_id = state.create_trade_request(req)

        return ActionResult(
            ok=True,
            data=CreateTradeRequestOutput(trade_request_id=trade_id, status="NEW"),
        )

    def validate_trade_request(self, *, trade_request_id: int) -> ActionResult[ValidateTradeRequestOutput]:
        client = self._build_client()
        db_path = self._db_path()

        with connect(db_path) as conn:
            state = StateManager(conn)
            row = state.get_trade_request(trade_request_id)
            if not row:
                return ActionResult(ok=False, error=ActionError(code="not_found", message="trade_request not found"))
            if row.get("status") in ("CANCELLED",):
                return ActionResult(
                    ok=False, error=ActionError(code="invalid_state", message="trade_request is CANCELLED")
                )
            symbol = row.get("preferred_symbol")
            if not symbol:
                return ActionResult(
                    ok=False,
                    error=ActionError(code="missing_symbol", message="trade_request has no preferred_symbol"),
                )

            deadline_s = str(row.get("deadline_utc") or "")
            try:
                deadline = datetime.fromisoformat(deadline_s.replace("Z", "+00:00")).astimezone(UTC)
            except ValueError:
                return ActionResult(
                    ok=False, error=ActionError(code="invalid_deadline", message="Invalid stored deadline_utc")
                )
            if deadline <= datetime.now(UTC):
                err = "deadline already passed"
                state.set_trade_request_validation(
                    trade_request_id=trade_request_id,
                    validation_status="INVALID",
                    validation_error=err,
                    last_price=None,
                    estimated_qty=None,
                    symbol_base_asset=None,
                    symbol_quote_asset=None,
                )
                return ActionResult(
                    ok=False, error=ActionError(code="deadline_passed", message=err, details={"deadline": deadline_s})
                )

            budget_asset = str(row.get("budget_asset") or "")
            try:
                budget_amount = Decimal(str(row.get("budget_amount")))
            except (InvalidOperation, ValueError):
                return ActionResult(
                    ok=False, error=ActionError(code="invalid_budget", message="Invalid stored budget_amount")
                )

        try:
            info = client.get_symbol_info(symbol=str(symbol))
            if not info:
                err = "symbol not found in exchangeInfo"
                with connect(db_path) as conn:
                    StateManager(conn).set_trade_request_validation(
                        trade_request_id=trade_request_id,
                        validation_status="INVALID",
                        validation_error=err,
                        last_price=None,
                        estimated_qty=None,
                        symbol_base_asset=None,
                        symbol_quote_asset=None,
                    )
                return ActionResult(ok=False, error=ActionError(code="symbol_not_found", message=err))

            rules = parse_symbol_rules(info)
            price_s = client.get_ticker_price(symbol=rules.symbol)
            last_price = Decimal(price_s)
            res = precheck_market_buy(
                rules=rules,
                budget_asset=budget_asset,
                budget_amount=budget_amount,
                last_price=last_price,
            )
        except (BinanceAPIError, RuleError, InvalidOperation, ValueError) as e:
            err = str(e)
            with connect(db_path) as conn:
                StateManager(conn).set_trade_request_validation(
                    trade_request_id=trade_request_id,
                    validation_status="ERROR",
                    validation_error=err,
                    last_price=None,
                    estimated_qty=None,
                    symbol_base_asset=None,
                    symbol_quote_asset=None,
                )
            return ActionResult(ok=False, error=ActionError(code="validation_error", message=err))

        with connect(db_path) as conn:
            ok = StateManager(conn).set_trade_request_validation(
                trade_request_id=trade_request_id,
                validation_status="VALID" if res.ok else "INVALID",
                validation_error=res.error,
                last_price=str(last_price),
                estimated_qty=str(res.estimated_qty) if res.estimated_qty is not None else None,
                symbol_base_asset=rules.base_asset,
                symbol_quote_asset=rules.quote_asset,
            )
        if not ok:
            return ActionResult(
                ok=False, error=ActionError(code="invalid_state", message="trade_request not NEW or not found")
            )

        return ActionResult(
            ok=True,
            data=ValidateTradeRequestOutput(
                trade_request_id=trade_request_id,
                validation_status="VALID" if res.ok else "INVALID",
                validation_error=res.error,
                last_price=str(last_price),
                estimated_qty=str(res.estimated_qty) if res.estimated_qty is not None else None,
                symbol_base_asset=rules.base_asset,
                symbol_quote_asset=rules.quote_asset,
            ),
        )

    def build_trade_plan(self, *, trade_request_id: int, execution_environment: str) -> ActionResult[BuildTradePlanOutput]:
        cfg = self._load_config()
        db_path = self._db_path()
        market_client = self._build_client()
        execution_client = self._build_client()

        with connect(db_path) as conn:
            state = StateManager(conn)
            trade_request = state.get_trade_request(trade_request_id)
            if not trade_request:
                return ActionResult(ok=False, error=ActionError(code="not_found", message="trade_request not found"))

            try:
                plan = build_trade_plan(
                    cfg=cfg,
                    state=state,
                    trade_request=trade_request,
                    market_client=market_client,
                    execution_client=execution_client,
                    execution_environment=execution_environment,
                )
            except PlanningError as e:
                return ActionResult(ok=False, error=ActionError(code="planning_error", message=str(e)))

            plan_id = persist_trade_plan(state=state, plan=plan)

        return ActionResult(
            ok=True,
            data=BuildTradePlanOutput(
                plan_id=plan_id,
                trade_request_id=trade_request_id,
                status=plan.status,
                symbol=plan.symbol,
                feasibility_category=plan.feasibility_category,
                signal=plan.signal,
                warnings=plan.warnings,
            ),
        )

