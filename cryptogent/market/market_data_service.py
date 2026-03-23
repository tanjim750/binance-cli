from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from cryptogent.exchange.binance_spot import BinanceSpotClient
from cryptogent.market.candles import CandleMetrics
from cryptogent.market.compute_engine import ComputeEngineError, compute_market_metrics


class MarketDataError(RuntimeError):
    pass


_CACHE: dict[tuple, tuple[float, MarketSnapshot]] = {}


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
    if fetch_book_ticker:
        book_time_ms = _now_ms()
        book = client.get_book_ticker(symbol=symbol)

    try:
        computed = compute_market_metrics(klines=klines, stats_24h=stats, book_ticker=book)
    except ComputeEngineError as e:
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
        candles=computed.candles,
        volume_24h_quote=computed.volume_24h_quote,
        bid=computed.bid,
        ask=computed.ask,
        spread_pct=computed.spread_pct,
    )


def fetch_market_snapshot_cached(
    *,
    client: BinanceSpotClient,
    symbol: str,
    candle_interval: str,
    candle_count: int,
    fetch_book_ticker: bool,
    cache_ttl_s: int,
    return_meta: bool = False,
) -> MarketSnapshot | tuple[MarketSnapshot, bool]:
    if cache_ttl_s <= 0:
        snap = fetch_market_snapshot(
            client=client,
            symbol=symbol,
            candle_interval=candle_interval,
            candle_count=candle_count,
            fetch_book_ticker=fetch_book_ticker,
        )
        return (snap, False) if return_meta else snap
    key = (
        str(client.base_url or ""),
        symbol,
        candle_interval,
        int(candle_count),
        bool(fetch_book_ticker),
    )
    now = time.time()
    hit = _CACHE.get(key)
    if hit:
        ts, snap = hit
        if (now - ts) <= cache_ttl_s:
            return (snap, True) if return_meta else snap
    snap = fetch_market_snapshot(
        client=client,
        symbol=symbol,
        candle_interval=candle_interval,
        candle_count=candle_count,
        fetch_book_ticker=fetch_book_ticker,
    )
    _CACHE[key] = (now, snap)
    return (snap, False) if return_meta else snap
