from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class CandleError(ValueError):
    pass


def _d(value: object, name: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as e:
        raise CandleError(f"Invalid decimal for {name}") from e
    if d.is_nan() or d.is_infinite():
        raise CandleError(f"Invalid decimal for {name}")
    return d


def pct(numer: Decimal, denom: Decimal) -> Decimal:
    if denom == 0:
        return Decimal("0")
    return (numer / denom) * Decimal("100")


@dataclass(frozen=True)
class CandleMetrics:
    closes: list[Decimal]
    first_open_time_ms: int
    last_close_time_ms: int
    first_close: Decimal
    last_close: Decimal
    volatility_pct: Decimal
    momentum_pct: Decimal


def compute_candle_metrics(klines: list[list]) -> CandleMetrics:
    if not klines:
        raise CandleError("Missing candle data")

    closes: list[Decimal] = []
    first_open_time_ms: int | None = None
    last_close_time_ms: int | None = None

    last_open: int | None = None
    for i, row in enumerate(klines):
        if not isinstance(row, list) or len(row) < 7:
            raise CandleError("Corrupt kline row")
        try:
            open_t = int(row[0])
            close_s = row[4]
            close_t = int(row[6])
        except Exception as e:
            raise CandleError("Invalid kline fields") from e

        if last_open is not None and open_t <= last_open:
            raise CandleError("Corrupt kline timestamps (not increasing)")
        last_open = open_t

        if i == 0:
            first_open_time_ms = open_t
        last_close_time_ms = close_t
        closes.append(_d(close_s, "kline.close"))

    if first_open_time_ms is None or last_close_time_ms is None:
        raise CandleError("Missing kline timestamps")
    if not closes:
        raise CandleError("Missing candle closes")

    first_close = closes[0]
    last_close = closes[-1]
    max_close = max(closes)
    min_close = min(closes)

    volatility_pct = pct(max_close - min_close, last_close)
    momentum_pct = pct(last_close - first_close, first_close if first_close != 0 else last_close)

    return CandleMetrics(
        closes=closes,
        first_open_time_ms=first_open_time_ms,
        last_close_time_ms=last_close_time_ms,
        first_close=first_close,
        last_close=last_close,
        volatility_pct=volatility_pct,
        momentum_pct=momentum_pct,
    )

