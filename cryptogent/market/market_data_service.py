from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from cryptogent.exchange.binance_spot import BinanceSpotClient
from cryptogent.market.candles import CandleError, CandleMetrics, compute_candle_metrics
from cryptogent.market.tickers import TickerError, quote_volume_24h, spread_pct_from_book


class MarketDataError(RuntimeError):
    pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def _d(value: object, name: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as e:
        raise MarketDataError(f"Invalid decimal for {name}") from e
    if d.is_nan() or d.is_infinite():
        raise MarketDataError(f"Invalid decimal for {name}")
    return d


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    price: Decimal
    price_time_ms: int
    stats_24h: dict
    stats_time_ms: int
    klines: list[list]
    klines_time_ms: int
    book_ticker: dict | None
    book_time_ms: int | None
    candles: CandleMetrics
    volume_24h_quote: Decimal
    bid: Decimal | None
    ask: Decimal | None
    spread_pct: Decimal | None


def fetch_market_snapshot(
    *,
    client: BinanceSpotClient,
    symbol: str,
    candle_interval: str,
    candle_count: int,
    fetch_book_ticker: bool,
) -> MarketSnapshot:
    price_time_ms = _now_ms()
    price_s = client.get_ticker_price(symbol=symbol)
    price = _d(price_s, "price")

    stats_time_ms = _now_ms()
    stats = client.get_ticker_24hr(symbol=symbol)
    if not isinstance(stats, dict):
        raise MarketDataError("Unexpected 24h stats response")

    klines_time_ms = _now_ms()
    klines = client.get_klines(symbol=symbol, interval=candle_interval, limit=candle_count)

    book: dict | None = None
    book_time_ms: int | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    spread_pct: Decimal | None = None
    if fetch_book_ticker:
        book_time_ms = _now_ms()
        book = client.get_book_ticker(symbol=symbol)
        if isinstance(book, dict):
            try:
                bid, ask, spread_pct = spread_pct_from_book(book)
            except TickerError:
                bid, ask, spread_pct = None, None, None

    try:
        candles = compute_candle_metrics(klines)
    except CandleError as e:
        raise MarketDataError(str(e)) from e

    try:
        vol_q = quote_volume_24h(stats)
    except TickerError as e:
        raise MarketDataError(str(e)) from e

    return MarketSnapshot(
        symbol=symbol,
        price=price,
        price_time_ms=price_time_ms,
        stats_24h=stats,
        stats_time_ms=stats_time_ms,
        klines=klines,
        klines_time_ms=klines_time_ms,
        book_ticker=book,
        book_time_ms=book_time_ms,
        candles=candles,
        volume_24h_quote=vol_q,
        bid=bid,
        ask=ask,
        spread_pct=spread_pct,
    )

