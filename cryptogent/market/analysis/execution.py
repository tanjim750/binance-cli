"""
cryptogent.market.analysis.execution
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Execution-quality analytics based on live order book depth.

  Spread            — absolute, %, quality tier (tight / normal / wide)
  Effective spread  — 2 × slippage (standard microstructure definition)
  Slippage          — simulated walk of the book at actual level prices
                      (notional-based, not qty-based at mid)
  Fill ratio        — % of requested notional fillable within depth_n levels
  Depth metrics     — top-N notional per side, imbalance, depth-weighted spread
  Market impact     — estimated mid-price shift after filling the order
  Best bid/ask      — validated against book top (warns on mismatch)

Conventions
-----------
  - All prices and quantities are ``Decimal`` for precision.
  - Slippage is always expressed as a cost (positive = worse execution).
  - ``available=False`` with ``unavailable_reason`` on any hard failure.
  - Silent shrinks are replaced with logged warnings.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Spread quality thresholds
#   Calibrated for liquid crypto majors (BTC, ETH).
#   Low-cap alts typically have spreads of 0.1–0.5%+ — callers should
#   pass custom thresholds via the spread_tight / spread_normal parameters.
# ---------------------------------------------------------------------------
_SPREAD_TIGHT_DEFAULT  = Decimal("0.0002")   # ≤ 0.02 % → tight
_SPREAD_NORMAL_DEFAULT = Decimal("0.001")    # ≤ 0.10 % → normal  (else → wide)

# Mismatch tolerance between passed best_bid/ask and book top
_PRICE_MISMATCH_TOLERANCE = Decimal("0.001")   # 0.1 %

# Public string constants
SPREAD_TIGHT  = "tight"
SPREAD_NORMAL = "normal"
SPREAD_WIDE   = "wide"
SIDE_BUY  = "buy"
SIDE_SELL = "sell"


# ---------------------------------------------------------------------------
# Public data contract
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExecutionMetrics:
    """
    Immutable execution-quality snapshot for a single order simulation.

    All fields are ``None`` when unavailable.

    New KPIs vs previous version
    ----------------------------
    effective_spread_pct   2 × slippage_pct (standard microstructure metric)
    fill_ratio_pct         % of notional fillable within depth_n levels
    notional_available     total notional on the relevant side within depth_n
    market_impact_pct      estimated mid-price shift after this order fills
    """

    available: bool
    unavailable_reason: str | None

    # ---- Best bid/ask ------------------------------------------------------
    mid_price: Decimal | None
    best_bid: Decimal | None
    best_ask: Decimal | None

    # ---- Spread ------------------------------------------------------------
    spread_abs: Decimal | None
    spread_pct: Decimal | None
    spread_quality: str | None          # "tight" | "normal" | "wide"

    # ---- Slippage ----------------------------------------------------------
    slippage_pct: Decimal | None        # cost as positive % of mid
    effective_spread_pct: Decimal | None  # 2 × slippage_pct
    avg_fill_price: Decimal | None
    notional_used: Decimal | None
    fill_ratio_pct: Decimal | None      # filled_notional / requested_notional * 100
    levels_used: int | None
    side: str | None

    # ---- Market impact -----------------------------------------------------
    market_impact_pct: Decimal | None   # (last_fill_price - mid) / mid

    # ---- Depth metrics -----------------------------------------------------
    depth_levels: int | None            # actual levels used (may be < requested)
    bid_depth_notional: Decimal | None
    ask_depth_notional: Decimal | None
    notional_available: Decimal | None  # relevant side total notional
    depth_imbalance: Decimal | None     # (bid - ask) / (bid + ask) ∈ [-1, 1]
    depth_spread_pct: Decimal | None    # (weighted_ask - weighted_bid) / mid

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def is_tight(self) -> bool:
        return self.spread_quality == SPREAD_TIGHT

    @property
    def is_wide(self) -> bool:
        return self.spread_quality == SPREAD_WIDE

    @property
    def total_execution_cost_pct(self) -> Decimal | None:
        """
        Spread cost + slippage = total round-trip cost estimate.

        spread_pct represents the half-spread cost of entry.
        slippage_pct is the additional market-impact cost.
        """
        if self.spread_pct is None or self.slippage_pct is None:
            return None
        return (self.spread_pct / Decimal("2")) + self.slippage_pct

    @property
    def is_fully_filled(self) -> bool | None:
        """``True`` when the simulated order was 100% filled within available depth."""
        if self.fill_ratio_pct is None:
            return None
        return self.fill_ratio_pct >= Decimal("100")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_execution_metrics(
    *,
    bids: list[tuple[Decimal, Decimal]],
    asks: list[tuple[Decimal, Decimal]],
    best_bid: Decimal | None = None,
    best_ask: Decimal | None = None,
    depth_levels: int,
    notional: Decimal,
    side: str,
    spread_tight: Decimal = _SPREAD_TIGHT_DEFAULT,
    spread_normal: Decimal = _SPREAD_NORMAL_DEFAULT,
) -> ExecutionMetrics:
    """
    Compute execution-quality metrics from order book depth.

    Parameters
    ----------
    bids, asks:
        Order book levels as ``[(price, qty), ...]``.
        Need not be pre-sorted — this function sorts them.
    best_bid / best_ask:
        Optional explicit top-of-book prices.  When omitted, derived from
        the sorted book.  When provided, validated against the book top —
        a mismatch beyond 0.1% logs a warning.
    depth_levels:
        Number of price levels to include in depth metrics and slippage
        simulation.  If fewer levels exist, the actual count is used and
        a warning is logged.
    notional:
        Order size in quote currency (e.g. USDT).
    side:
        ``"buy"`` or ``"sell"``.
    spread_tight / spread_normal:
        Configurable spread quality thresholds.  Default values are
        calibrated for liquid majors; pass wider values for altcoins.

    Returns
    -------
    ExecutionMetrics
    """
    # 1. Input validation
    if not bids or not asks:
        return _unavailable("empty_order_book")
    if depth_levels <= 0:
        return _unavailable("invalid_depth_levels")
    if notional <= 0:
        return _unavailable("invalid_notional")

    side_norm = side.lower().strip()
    if side_norm not in (SIDE_BUY, SIDE_SELL):
        return _unavailable("invalid_side")

    # 2. Sort book — bids descending (best bid first), asks ascending
    try:
        bids_sorted = sorted(bids, key=lambda x: x[0], reverse=True)
        asks_sorted = sorted(asks, key=lambda x: x[0])
    except Exception as exc:
        logger.warning("execution: failed to sort order book: %s", exc)
        return _unavailable("book_sort_error")

    # 3. Resolve and validate best bid/ask
    book_best_bid = bids_sorted[0][0]
    book_best_ask = asks_sorted[0][0]

    best_bid, best_ask = _resolve_best_prices(
        best_bid, best_ask, book_best_bid, book_best_ask
    )

    if best_bid <= 0 or best_ask <= 0:
        return _unavailable("invalid_best_bid_ask")
    if best_bid >= best_ask:
        return _unavailable("crossed_book")

    # 4. Mid price and spread
    mid        = (best_bid + best_ask) / Decimal("2")
    spread_abs = best_ask - best_bid
    spread_pct = spread_abs / mid if mid != 0 else None
    spread_quality = _classify_spread(spread_pct, spread_tight, spread_normal)

    # 5. Actual depth (warn if book shallower than requested)
    actual_depth = min(depth_levels, len(bids_sorted), len(asks_sorted))
    if actual_depth < depth_levels:
        logger.warning(
            "execution: requested depth_levels=%d but book only has "
            "bids=%d asks=%d; using depth=%d.",
            depth_levels, len(bids_sorted), len(asks_sorted), actual_depth,
        )

    bid_slice = bids_sorted[:actual_depth]
    ask_slice = asks_sorted[:actual_depth]

    # 6. Depth notionals
    bid_depth_notional = _sum_notional(bid_slice)
    ask_depth_notional = _sum_notional(ask_slice)

    # 7. Depth imbalance
    depth_imbalance: Decimal | None = None
    if bid_depth_notional is not None and ask_depth_notional is not None:
        denom = bid_depth_notional + ask_depth_notional
        if denom != 0:
            depth_imbalance = (bid_depth_notional - ask_depth_notional) / denom

    # 8. Depth-weighted spread
    depth_spread_pct: Decimal | None = None
    weighted_bid = _weighted_price(bid_slice)
    weighted_ask = _weighted_price(ask_slice)
    if weighted_bid is not None and weighted_ask is not None and mid != 0:
        depth_spread_pct = (weighted_ask - weighted_bid) / mid

    # 9. Slippage simulation (notional-based walk, not qty-at-mid conversion)
    relevant_book = ask_slice if side_norm == SIDE_BUY else bid_slice
    notional_available = ask_depth_notional if side_norm == SIDE_BUY else bid_depth_notional

    (avg_fill, filled_notional,
     levels_used, last_fill_price) = _simulate_fill(
        book=relevant_book,
        notional=notional,
    )

    if avg_fill is None or filled_notional is None:
        return _unavailable("insufficient_depth")

    # 10. Slippage, effective spread, fill ratio, market impact
    if side_norm == SIDE_BUY:
        slippage_pct     = (avg_fill - mid) / mid
        market_impact_pct = (last_fill_price - mid) / mid if last_fill_price else None
    else:
        slippage_pct     = (mid - avg_fill) / mid
        market_impact_pct = (mid - last_fill_price) / mid if last_fill_price else None

    effective_spread_pct = slippage_pct * Decimal("2")

    fill_ratio_pct = (filled_notional / notional * Decimal("100")) if notional != 0 else None

    return ExecutionMetrics(
        available=True,
        unavailable_reason=None,
        mid_price=mid,
        best_bid=best_bid,
        best_ask=best_ask,
        spread_abs=spread_abs,
        spread_pct=spread_pct,
        spread_quality=spread_quality,
        slippage_pct=slippage_pct,
        effective_spread_pct=effective_spread_pct,
        avg_fill_price=avg_fill,
        notional_used=filled_notional,
        fill_ratio_pct=fill_ratio_pct,
        levels_used=levels_used,
        side=side_norm,
        market_impact_pct=market_impact_pct,
        depth_levels=actual_depth,
        bid_depth_notional=bid_depth_notional,
        ask_depth_notional=ask_depth_notional,
        notional_available=notional_available,
        depth_imbalance=depth_imbalance,
        depth_spread_pct=depth_spread_pct,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _resolve_best_prices(
    passed_bid: Decimal | None,
    passed_ask: Decimal | None,
    book_bid: Decimal,
    book_ask: Decimal,
) -> tuple[Decimal, Decimal]:
    """
    Resolve best bid/ask from passed args or book top.

    When explicit values are passed, validate them against the book top.
    A mismatch > _PRICE_MISMATCH_TOLERANCE logs a warning — could mean
    stale data was passed — and the book top is used instead.
    """
    def _validate_and_resolve(passed: Decimal | None, book: Decimal, label: str) -> Decimal:
        if passed is None:
            return book
        if book != 0:
            pct_diff = abs(passed - book) / book
            if pct_diff > _PRICE_MISMATCH_TOLERANCE:
                logger.warning(
                    "execution: passed %s=%s differs from book top %s by %.4f%% "
                    "— using book top (possible stale data).",
                    label, passed, book, float(pct_diff * 100),
                )
                return book
        return passed

    resolved_bid = _validate_and_resolve(passed_bid, book_bid, "best_bid")
    resolved_ask = _validate_and_resolve(passed_ask, book_ask, "best_ask")
    return resolved_bid, resolved_ask


def _classify_spread(
    spread_pct: Decimal | None,
    tight: Decimal,
    normal: Decimal,
) -> str | None:
    if spread_pct is None:
        return None
    if spread_pct <= tight:
        return SPREAD_TIGHT
    if spread_pct <= normal:
        return SPREAD_NORMAL
    return SPREAD_WIDE


def _sum_notional(levels: list[tuple[Decimal, Decimal]]) -> Decimal | None:
    """Sum price * qty across levels. Returns None on empty input (not zero)."""
    if not levels:
        return None
    total = Decimal("0")
    for price, qty in levels:
        total += price * qty
    return total


def _weighted_price(levels: list[tuple[Decimal, Decimal]]) -> Decimal | None:
    """Volume-weighted average price across levels."""
    if not levels:
        return None
    total_notional = Decimal("0")
    total_qty      = Decimal("0")
    for price, qty in levels:
        total_notional += price * qty
        total_qty      += qty
    if total_qty == 0:
        return None
    return total_notional / total_qty


def _simulate_fill(
    *,
    book: list[tuple[Decimal, Decimal]],
    notional: Decimal,
) -> tuple[Decimal | None, Decimal | None, int | None, Decimal | None]:
    """
    Walk the order book and simulate filling a notional-sized order.

    Operates in quote (notional) space directly — does NOT convert to base
    qty at mid first.  This correctly captures slippage as prices worsen
    through the book.

    Parameters
    ----------
    book:
        Price levels sorted best-first: asks ascending (buy), bids descending (sell).
    notional:
        Order size in quote currency.

    Returns
    -------
    (avg_fill_price, filled_notional, levels_used, last_fill_price)
    All None when the order cannot be partially or fully filled.
    """
    if not book or notional <= 0:
        return None, None, None, None

    remaining_notional = notional
    total_qty          = Decimal("0")
    total_cost         = Decimal("0")
    levels_used        = 0
    last_fill_price: Decimal | None = None

    for price, qty in book:
        if remaining_notional <= 0:
            break
        if price <= 0:
            continue

        # Notional available at this level
        level_notional = price * qty

        if level_notional <= remaining_notional:
            # Consume entire level
            take_notional = level_notional
            take_qty      = qty
        else:
            # Partial fill at this level
            take_notional = remaining_notional
            take_qty      = take_notional / price

        total_cost         += take_notional
        total_qty          += take_qty
        remaining_notional -= take_notional
        levels_used        += 1
        last_fill_price     = price

    filled_notional = total_cost
    if total_qty == 0 or filled_notional == 0:
        return None, None, None, None

    avg_fill = total_cost / total_qty
    return avg_fill, filled_notional, levels_used, last_fill_price


def _unavailable(reason: str) -> ExecutionMetrics:
    return ExecutionMetrics(
        available=False,
        unavailable_reason=reason,
        mid_price=None, best_bid=None, best_ask=None,
        spread_abs=None, spread_pct=None, spread_quality=None,
        slippage_pct=None, effective_spread_pct=None,
        avg_fill_price=None, notional_used=None,
        fill_ratio_pct=None, levels_used=None, side=None,
        market_impact_pct=None,
        depth_levels=None,
        bid_depth_notional=None, ask_depth_notional=None,
        notional_available=None, depth_imbalance=None,
        depth_spread_pct=None,
    )