from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from cryptogent.market.candles import CandleError, CandleMetrics, compute_candle_metrics
from cryptogent.market.tickers import TickerError, quote_volume_24h, spread_pct_from_book


class ComputeEngineError(RuntimeError):
    pass


@dataclass(frozen=True)
class MarketComputed:
    candles: CandleMetrics
    volume_24h_quote: Decimal
    bid: Decimal | None
    ask: Decimal | None
    spread_pct: Decimal | None


def compute_market_metrics(
    *,
    klines: list[list],
    stats_24h: dict,
    book_ticker: dict | None,
) -> MarketComputed:
    try:
        candles = compute_candle_metrics(klines)
    except CandleError as e:
        raise ComputeEngineError(str(e)) from e

    try:
        vol_q = quote_volume_24h(stats_24h)
    except TickerError as e:
        raise ComputeEngineError(str(e)) from e

    bid: Decimal | None = None
    ask: Decimal | None = None
    spread_pct: Decimal | None = None
    if isinstance(book_ticker, dict):
        try:
            bid, ask, spread_pct = spread_pct_from_book(book_ticker)
        except TickerError:
            bid, ask, spread_pct = None, None, None

    return MarketComputed(
        candles=candles,
        volume_24h_quote=vol_q,
        bid=bid,
        ask=ask,
        spread_pct=spread_pct,
    )
