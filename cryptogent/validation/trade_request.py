from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedTradeRequest:
    profit_target_pct: Decimal
    stop_loss_pct: Decimal
    deadline_utc: datetime
    deadline_hours: int | None
    budget_mode: str
    budget_asset: str
    budget_amount: Decimal | None
    preferred_symbol: str | None
    exit_asset: str
    label: str | None
    notes: str | None


def _parse_decimal(name: str, value: str) -> Decimal:
    try:
        d = Decimal(value)
    except (InvalidOperation, ValueError) as e:
        raise ValidationError(f"{name} must be a number") from e
    if d.is_nan() or d.is_infinite():
        raise ValidationError(f"{name} must be finite")
    return d


def _parse_deadline(
    deadline: str | None, deadline_minutes: int | None, deadline_hours: int | None
) -> tuple[datetime, int | None]:
    provided = [v is not None for v in (deadline, deadline_minutes, deadline_hours)]
    if sum(provided) != 1:
        raise ValidationError("Provide exactly one of --deadline, --deadline-minutes, or --deadline-hours")
    if deadline_hours is not None:
        if deadline_hours <= 0:
            raise ValidationError("--deadline-hours must be > 0")
        if deadline_hours > 24 * 365:
            raise ValidationError("--deadline-hours is too large for MVP")
        return datetime.now(UTC) + timedelta(hours=int(deadline_hours)), int(deadline_hours)
    if deadline_minutes is not None:
        if deadline_minutes <= 0:
            raise ValidationError("--deadline-minutes must be > 0")
        return datetime.now(UTC) + timedelta(minutes=int(deadline_minutes)), None
    if not deadline:
        raise ValidationError("Missing deadline")

    d = deadline.strip()
    if d.endswith("Z"):
        d = d[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(d)
    except ValueError as e:
        raise ValidationError("--deadline must be ISO8601 (e.g. 2026-03-14T12:00:00+00:00)") from e
    if dt.tzinfo is None:
        raise ValidationError("--deadline must include a timezone offset (e.g. +00:00)")
    return dt.astimezone(UTC), None


def validate_trade_request(
    *,
    profit_target_pct: str,
    stop_loss_pct: str,
    deadline: str | None,
    deadline_minutes: int | None,
    deadline_hours: int | None,
    budget_mode: str,
    budget_asset: str,
    budget_amount: str | None,
    preferred_symbol: str | None,
    exit_asset: str,
    label: str | None = None,
    notes: str | None = None,
) -> ValidatedTradeRequest:
    pt = _parse_decimal("--profit-target-pct", profit_target_pct)
    sl = _parse_decimal("--stop-loss-pct", stop_loss_pct)
    mode = (budget_mode or "").strip().lower()
    if mode not in ("manual", "auto"):
        raise ValidationError("--budget-mode must be 'manual' or 'auto'")

    budget: Decimal | None = None
    if mode == "manual":
        if budget_amount is None:
            raise ValidationError("--budget is required when --budget-mode=manual")
        budget = _parse_decimal("--budget", budget_amount)
        if budget <= 0:
            raise ValidationError("--budget must be > 0")
    else:
        if budget_amount not in (None, ""):
            raise ValidationError("--budget must be omitted when --budget-mode=auto")

    dl, dl_hours = _parse_deadline(deadline, deadline_minutes, deadline_hours)

    if pt <= 0:
        raise ValidationError("--profit-target-pct must be > 0")
    if sl <= 0:
        raise ValidationError("--stop-loss-pct must be > 0")
    if sl >= 100:
        raise ValidationError("--stop-loss-pct must be < 100")
    if pt >= 500:
        raise ValidationError("--profit-target-pct is unreasonably large (>= 500)")
    now = datetime.now(UTC)
    if dl <= now + timedelta(seconds=30):
        raise ValidationError("deadline must be at least 30 seconds in the future")

    asset = budget_asset.strip().upper()
    if not asset.isalnum() or len(asset) < 2 or len(asset) > 16:
        raise ValidationError("--budget-asset must be an alphanumeric asset code (e.g. USDT)")
    if not any(ch.isalpha() for ch in asset):
        raise ValidationError("--budget-asset must include at least one letter (e.g. USDT)")

    sym = preferred_symbol.strip().upper() if preferred_symbol else None
    if sym is not None:
        if not sym.isalnum() or len(sym) < 6 or len(sym) > 20:
            raise ValidationError("--symbol must be alphanumeric (e.g. BTCUSDT)")
        if not any(ch.isalpha() for ch in sym):
            raise ValidationError("--symbol must include letters (e.g. BTCUSDT)")

    exit_a = exit_asset.strip().upper()
    if not exit_a.isalnum() or len(exit_a) < 2 or len(exit_a) > 16 or not any(ch.isalpha() for ch in exit_a):
        raise ValidationError("--exit-asset must be an alphanumeric asset code (e.g. USDT)")

    lbl = label.strip() if label else None
    nts = notes.strip() if notes else None
    if lbl is not None and len(lbl) > 80:
        raise ValidationError("--label is too long (max 80 chars)")
    if nts is not None and len(nts) > 500:
        raise ValidationError("--notes is too long (max 500 chars)")

    return ValidatedTradeRequest(
        profit_target_pct=pt,
        stop_loss_pct=sl,
        deadline_utc=dl,
        deadline_hours=dl_hours,
        budget_mode=mode,
        budget_asset=asset,
        budget_amount=budget,
        preferred_symbol=sym,
        exit_asset=exit_a,
        label=lbl,
        notes=nts,
    )
