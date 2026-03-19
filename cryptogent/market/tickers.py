from __future__ import annotations

from decimal import Decimal, InvalidOperation


class TickerError(ValueError):
    pass


def _d(value: object, name: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as e:
        raise TickerError(f"Invalid decimal for {name}") from e
    if d.is_nan() or d.is_infinite():
        raise TickerError(f"Invalid decimal for {name}")
    return d


def pct(numer: Decimal, denom: Decimal) -> Decimal:
    if denom == 0:
        return Decimal("0")
    return (numer / denom) * Decimal("100")


def quote_volume_24h(stats_24h: dict) -> Decimal:
    return _d(stats_24h.get("quoteVolume") or "0", "quoteVolume")


def spread_pct_from_book(book_ticker: dict) -> tuple[Decimal, Decimal, Decimal]:
    bid = _d(book_ticker.get("bidPrice"), "bidPrice")
    ask = _d(book_ticker.get("askPrice"), "askPrice")
    if bid <= 0 or ask <= 0 or ask < bid:
        raise TickerError("Invalid bid/ask")
    mid = (bid + ask) / Decimal("2")
    return bid, ask, pct(ask - bid, mid)

