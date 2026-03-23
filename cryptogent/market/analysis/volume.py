"""
cryptogent.market.volume
~~~~~~~~~~~~~~~~~~~~~~~~
Volume & liquidity analytics:

  Volume stats     — last bar, 20/50-bar avg, sample std, z-score
  RVOL             — Relative Volume (last bar / 20-bar avg)
  Volume momentum  — short/long avg ratio (5-bar vs 20-bar acceleration)
  Volume trend     — OLS linear slope label over rolling window
  Spike detection  — ratio-based AND z-score-based, OR-gated
  Taker pressure   — single-bar + 20-bar rolling avg buy ratio
  OBV              — On-Balance Volume + trend
  VWAP             — Rolling VWAP (close-only approx; HLC/3 when H/L provided)
  Vol-price conf   — per-bar confirmation label + consecutive streak counter
  Order book       — bid/ask qty, imbalance, wall count + largest wall
  Liquidity zone   — bid_dominant / ask_dominant / balanced
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from .utils import to_decimal
from cryptogent.market.compute_engine import ComputeEngineError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------
_WINDOW_FAST: int = 20
_WINDOW_SLOW: int = 50
_WINDOW_SHORT: int = 5               # short window for vol momentum
_SPIKE_RATIO          = Decimal("2")
_Z_THRESHOLD          = Decimal("2")
_BUY_RATIO_HI         = Decimal("0.55")
_BUY_RATIO_LO         = Decimal("0.45")
_WALL_RATIO           = Decimal("3")    # level qty ≥ N× median → wall
_IMBALANCE_THRESHOLD  = Decimal("0.2")
_VOL_CONFIRM_RATIO    = Decimal("1.1")  # vol ≥ 110 % avg → "confirmed"

# Public string constants
PRESSURE_BUY     = "buy"
PRESSURE_SELL    = "sell"
PRESSURE_NEUTRAL = "neutral"
TREND_UP   = "up"
TREND_DOWN = "down"
TREND_FLAT = "flat"
ZONE_BID_DOMINANT = "bid_dominant"
ZONE_ASK_DOMINANT = "ask_dominant"
ZONE_BALANCED     = "balanced"
CONF_CONFIRMED  = "confirmed"
CONF_DIVERGING  = "diverging"
CONF_NEUTRAL    = "neutral"


# ---------------------------------------------------------------------------
# Public data contract
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VolumeMetrics:
    """
    Immutable volume and liquidity snapshot for a single asset.

    All fields are ``None`` when input data is absent or a computation
    failed.  Partial results are intentional — ``None`` means "unavailable",
    not zero.

    KPI summary
    -----------
    base_last / quote_last      Raw last-bar volumes
    quote_avg_20 / _50          Rolling averages
    quote_std_20                Sample std deviation (÷ n-1)
    quote_zscore_20             Z-score of last bar vs 20-bar window
    rvol                        Relative volume: last / avg_20
    vol_momentum_pct            (avg_5 / avg_20 - 1) × 100 — acceleration
    quote_trend                 OLS slope direction
    spike                       True when ratio ≥ 2× OR z ≥ 2
    taker_buy_ratio             Single-bar taker buy / total quote
    taker_buy_ratio_avg20       20-bar rolling avg taker buy ratio
    buy_pressure                Driven by rolling avg
    obv / obv_trend             On-Balance Volume + slope direction
    vwap_20                     Rolling VWAP (HLC/3 when H/L provided, else close)
    price_vs_vwap_pct           % distance of last close from VWAP
    vol_price_confirmation      Per-bar: confirmed / diverging / neutral
    vol_conf_streak             Consecutive bars of current confirmation state
    bid_qty / ask_qty           Order book totals
    book_imbalance              (bid - ask) / (bid + ask) ∈ [-1, 1]
    liquidity_zones             bid_dominant / ask_dominant / balanced
    buy_wall_price / _qty       Largest qualifying bid wall (structured)
    buy_wall_count              Number of bid levels qualifying as walls
    sell_wall_price / _qty      Largest qualifying ask wall (structured)
    sell_wall_count             Number of ask levels qualifying as walls
    """

    # ---- Raw volume --------------------------------------------------------
    base_last: Decimal | None
    quote_last: Decimal | None

    # ---- Rolling stats -----------------------------------------------------
    quote_avg_20: Decimal | None
    quote_avg_50: Decimal | None
    quote_std_20: Decimal | None        # sample std (÷ n-1)
    quote_zscore_20: Decimal | None

    # ---- RVOL & momentum ---------------------------------------------------
    rvol: Decimal | None               # last / avg_20
    vol_momentum_pct: Decimal | None   # (avg_5 / avg_20 - 1) * 100

    # ---- Trend & spike -----------------------------------------------------
    quote_trend: str | None            # "up" | "down" | "flat"
    spike: bool | None

    # ---- Taker pressure ----------------------------------------------------
    taker_buy_ratio: Decimal | None
    taker_buy_ratio_avg20: Decimal | None
    buy_pressure: str | None           # "buy" | "sell" | "neutral"

    # ---- OBV ---------------------------------------------------------------
    obv: Decimal | None
    obv_trend: str | None              # "up" | "down" | "flat"

    # ---- VWAP --------------------------------------------------------------
    vwap_20: Decimal | None
    price_vs_vwap_pct: Decimal | None  # (price - vwap) / vwap * 100

    # ---- Vol-price confirmation --------------------------------------------
    vol_price_confirmation: str | None  # "confirmed" | "diverging" | "neutral"
    vol_conf_streak: int | None         # consecutive bars in current state

    # ---- Order book --------------------------------------------------------
    bid_qty: Decimal | None
    ask_qty: Decimal | None
    book_imbalance: Decimal | None     # (bid - ask) / (bid + ask)
    liquidity_zones: str | None        # "bid_dominant" | "ask_dominant" | "balanced"

    # ---- Walls (structured) ------------------------------------------------
    buy_wall_price: Decimal | None
    buy_wall_qty: Decimal | None
    buy_wall_count: int | None         # # bid levels qualifying as walls
    sell_wall_price: Decimal | None
    sell_wall_qty: Decimal | None
    sell_wall_count: int | None        # # ask levels qualifying as walls

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        return self.quote_last is None and self.base_last is None

    @property
    def is_spike(self) -> bool:
        return self.spike is True

    @property
    def is_high_rvol(self) -> bool | None:
        """``True`` when RVOL ≥ 2 (last bar is at least 2× average volume)."""
        if self.rvol is None:
            return None
        return self.rvol >= Decimal("2")

    @property
    def vol_accelerating(self) -> bool | None:
        """``True`` when short-term avg is outpacing long-term avg (momentum > 0)."""
        if self.vol_momentum_pct is None:
            return None
        return self.vol_momentum_pct > 0

    @property
    def sustained_buy_pressure(self) -> bool | None:
        """Both single-bar AND rolling avg must show buy pressure."""
        if self.taker_buy_ratio is None or self.taker_buy_ratio_avg20 is None:
            return None
        return (
            self.taker_buy_ratio > _BUY_RATIO_HI
            and self.taker_buy_ratio_avg20 > _BUY_RATIO_HI
        )

    @property
    def sustained_sell_pressure(self) -> bool | None:
        """Both single-bar AND rolling avg must show sell pressure."""
        if self.taker_buy_ratio is None or self.taker_buy_ratio_avg20 is None:
            return None
        return (
            self.taker_buy_ratio < _BUY_RATIO_LO
            and self.taker_buy_ratio_avg20 < _BUY_RATIO_LO
        )

    @property
    def price_above_vwap(self) -> bool | None:
        if self.price_vs_vwap_pct is None:
            return None
        return self.price_vs_vwap_pct > 0

    @property
    def has_buy_wall(self) -> bool:
        return self.buy_wall_price is not None

    @property
    def has_sell_wall(self) -> bool:
        return self.sell_wall_price is not None

    @property
    def obv_price_divergence(self) -> str | None:
        """
        Detects OBV / price trend divergence.

        Returns ``'bearish_divergence'`` when price trend is up but OBV is down,
        ``'bullish_divergence'`` when price is down but OBV is up,
        ``'confirmed'`` when both agree, or ``None`` when data is unavailable.
        """
        if self.quote_trend is None or self.obv_trend is None:
            return None
        p_up   = self.quote_trend == TREND_UP
        p_down = self.quote_trend == TREND_DOWN
        o_up   = self.obv_trend   == TREND_UP
        o_down = self.obv_trend   == TREND_DOWN
        if p_up and o_down:
            return "bearish_divergence"
        if p_down and o_up:
            return "bullish_divergence"
        if (p_up and o_up) or (p_down and o_down):
            return "confirmed"
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_volume_metrics(
    *,
    base_volumes: Sequence[Decimal],
    quote_volumes: Sequence[Decimal],
    closes: Sequence[Decimal] | None = None,
    highs: Sequence[Decimal] | None = None,
    lows: Sequence[Decimal] | None = None,
    taker_buy_quote_volumes: Sequence[Decimal] | None = None,
    bid_qty: Decimal | None = None,
    ask_qty: Decimal | None = None,
    depth_bids: list[tuple[Decimal, Decimal]] | None = None,
    depth_asks: list[tuple[Decimal, Decimal]] | None = None,
    window_fast: int = _WINDOW_FAST,
    window_slow: int = _WINDOW_SLOW,
    spike_ratio: Decimal = _SPIKE_RATIO,
    z_threshold: Decimal = _Z_THRESHOLD,
    buy_ratio_hi: Decimal = _BUY_RATIO_HI,
    buy_ratio_lo: Decimal = _BUY_RATIO_LO,
    wall_ratio: Decimal = _WALL_RATIO,
    imbalance_threshold: Decimal = _IMBALANCE_THRESHOLD,
) -> VolumeMetrics:
    """
    Compute volume and liquidity metrics.

    Parameters
    ----------
    base_volumes, quote_volumes:
        Per-bar volumes (oldest → newest).  Lengths must match or the
        shorter is used with a warning.
    closes:
        Closing prices.  Required for OBV, VWAP, vol-price confirmation.
    highs, lows:
        Optional.  When provided, VWAP uses typical price (H+L+C)/3 instead
        of close-only approximation.
    taker_buy_quote_volumes:
        Per-bar taker buy side quote volume (from exchange kline data).
    bid_qty / ask_qty:
        Top-of-book scalar quantities.  Ignored when depth_bids/asks provided.
    depth_bids / depth_asks:
        Full depth as ``[(price, qty), ...]``.  Takes priority over scalars.
    window_fast / window_slow:
        Rolling window sizes (default 20 / 50).

    Returns
    -------
    VolumeMetrics

    Raises
    ------
    ComputeEngineError
        On unconvertible volume series data.
    """
    # 1. Parse and validate core series
    try:
        base_list  = [_to_dec(v, "base_volumes")  for v in base_volumes]
        quote_list = [_to_dec(v, "quote_volumes") for v in quote_volumes]
    except (TypeError, ValueError) as exc:
        raise ComputeEngineError(f"Invalid volume series: {exc}") from exc

    if not base_list or not quote_list:
        return _empty_metrics()

    # 2. Align all series to the shortest
    n = min(len(base_list), len(quote_list))
    if len(base_list) != len(quote_list):
        logger.warning(
            "compute_volume_metrics: base/quote volume lengths differ "
            "(%d vs %d); truncating to %d.",
            len(base_list), len(quote_list), n,
        )
    base_list  = base_list[:n]
    quote_list = quote_list[:n]

    # Parse and align optional series — log and disable on any failure
    close_list = _parse_optional_series(closes,  n, "closes")
    high_list  = _parse_optional_series(highs,   n, "highs")
    low_list   = _parse_optional_series(lows,    n, "lows")

    base_last  = base_list[-1]
    quote_last = quote_list[-1]

    # 3. Rolling stats (sample std ÷ n-1)
    quote_avg_20 = _mean_tail(quote_list, window_fast)
    quote_avg_50 = _mean_tail(quote_list, window_slow)
    quote_std_20 = _std_tail(quote_list, window_fast)

    quote_zscore_20: Decimal | None = None
    if quote_avg_20 is not None and quote_std_20 not in (None, Decimal("0")):
        quote_zscore_20 = (quote_last - quote_avg_20) / quote_std_20  # type: ignore[operator]

    # 4. RVOL and volume momentum
    rvol = _compute_rvol(quote_last, quote_avg_20)
    vol_momentum_pct = _compute_vol_momentum(quote_list, window_fast)

    # 5. Spike detection (ratio OR z-score)
    spike: bool | None = None
    if quote_avg_20 is not None and quote_avg_20 != 0:
        spike = (quote_last / quote_avg_20) >= spike_ratio or (
            quote_zscore_20 is not None and quote_zscore_20 >= z_threshold
        )

    # 6. Volume trend (OLS slope)
    quote_trend = _trend_label(quote_list, window_fast)

    # 7. Taker buy pressure
    taker_buy_ratio, taker_buy_ratio_avg20, buy_pressure = _compute_taker_pressure(
        quote_list, taker_buy_quote_volumes, n,
        buy_ratio_hi, buy_ratio_lo, window_fast,
    )

    # 8. OBV (requires closes, aligned to same n)
    obv, obv_trend = _compute_obv(quote_list, close_list, window_fast)

    # 9. VWAP — uses HLC/3 when H/L available, else close
    vwap_20, price_vs_vwap_pct = _compute_vwap(
        quote_list, close_list, high_list, low_list, window_fast
    )

    # 10. Vol-price confirmation + streak
    vol_price_confirmation, vol_conf_streak = _compute_vol_price_confirmation(
        quote_list, close_list, quote_avg_20
    )

    # 11. Order book
    computed_bid_qty, computed_ask_qty = _compute_book_totals(
        bid_qty, ask_qty, depth_bids, depth_asks
    )
    book_imbalance, liquidity_zones = _compute_book_imbalance(
        computed_bid_qty, computed_ask_qty, imbalance_threshold
    )
    buy_wall_price,  buy_wall_qty,  buy_wall_count  = _find_walls(depth_bids, wall_ratio)
    sell_wall_price, sell_wall_qty, sell_wall_count = _find_walls(depth_asks, wall_ratio)

    return VolumeMetrics(
        base_last=base_last,
        quote_last=quote_last,
        quote_avg_20=quote_avg_20,
        quote_avg_50=quote_avg_50,
        quote_std_20=quote_std_20,
        quote_zscore_20=quote_zscore_20,
        rvol=rvol,
        vol_momentum_pct=vol_momentum_pct,
        quote_trend=quote_trend,
        spike=spike,
        taker_buy_ratio=taker_buy_ratio,
        taker_buy_ratio_avg20=taker_buy_ratio_avg20,
        buy_pressure=buy_pressure,
        obv=obv,
        obv_trend=obv_trend,
        vwap_20=vwap_20,
        price_vs_vwap_pct=price_vs_vwap_pct,
        vol_price_confirmation=vol_price_confirmation,
        vol_conf_streak=vol_conf_streak,
        bid_qty=computed_bid_qty,
        ask_qty=computed_ask_qty,
        book_imbalance=book_imbalance,
        liquidity_zones=liquidity_zones,
        buy_wall_price=buy_wall_price,
        buy_wall_qty=buy_wall_qty,
        buy_wall_count=buy_wall_count,
        sell_wall_price=sell_wall_price,
        sell_wall_qty=sell_wall_qty,
        sell_wall_count=sell_wall_count,
    )


# ---------------------------------------------------------------------------
# Private indicator functions
# ---------------------------------------------------------------------------

def _compute_rvol(
    quote_last: Decimal,
    quote_avg_20: Decimal | None,
) -> Decimal | None:
    """Relative Volume = last bar / 20-bar average. The most direct spike measure."""
    if quote_avg_20 is None or quote_avg_20 == 0:
        return None
    return quote_last / quote_avg_20


def _compute_vol_momentum(
    quote_list: list[Decimal],
    window_fast: int,
) -> Decimal | None:
    """
    Volume acceleration: (avg_short / avg_long - 1) * 100.

    Positive = short-term volume rising faster than long-term average
    (accelerating).  Negative = decelerating / fading participation.
    """
    if len(quote_list) < window_fast:
        return None
    avg_short = _mean_tail(quote_list, _WINDOW_SHORT)
    avg_long  = _mean_tail(quote_list, window_fast)
    if avg_short is None or avg_long is None or avg_long == 0:
        return None
    return (avg_short / avg_long - Decimal("1")) * Decimal("100")


def _compute_taker_pressure(
    quote_list: list[Decimal],
    taker_buy_quote_volumes: Sequence[Decimal] | None,
    n: int,
    buy_ratio_hi: Decimal,
    buy_ratio_lo: Decimal,
    window_fast: int,
) -> tuple[Decimal | None, Decimal | None, str | None]:
    if taker_buy_quote_volumes is None:
        return None, None, None
    try:
        tbq = [_to_dec(v, "taker_buy_quote_volumes") for v in taker_buy_quote_volumes][:n]
    except (TypeError, ValueError) as exc:
        logger.warning("Taker buy volumes invalid: %s", exc)
        return None, None, None

    if not tbq:
        return None, None, None

    # Single-bar ratio
    taker_buy_ratio: Decimal | None = None
    quote_last = quote_list[-1]
    if quote_last != 0:
        taker_buy_ratio = tbq[-1] / quote_last

    # Rolling avg: per-bar ratios then mean
    tail_len = min(window_fast, len(tbq), len(quote_list))
    rolling_ratios: list[Decimal] = [
        t / q
        for t, q in zip(tbq[-tail_len:], quote_list[-tail_len:])
        if q != 0
    ]
    avg_ratio: Decimal | None = (
        sum(rolling_ratios) / Decimal(str(len(rolling_ratios)))
        if rolling_ratios else None
    )

    ref = avg_ratio if avg_ratio is not None else taker_buy_ratio
    pressure: str | None = None
    if ref is not None:
        if ref > buy_ratio_hi:
            pressure = PRESSURE_BUY
        elif ref < buy_ratio_lo:
            pressure = PRESSURE_SELL
        else:
            pressure = PRESSURE_NEUTRAL

    return taker_buy_ratio, avg_ratio, pressure


def _compute_obv(
    quote_list: list[Decimal],
    close_list: list[Decimal] | None,
    window_fast: int,
) -> tuple[Decimal | None, str | None]:
    """
    On-Balance Volume.

    OBV[t] = OBV[t-1] + volume  if close[t] > close[t-1]
           = OBV[t-1] - volume  if close[t] < close[t-1]
           = OBV[t-1]           otherwise

    Explicitly checks that close_list and quote_list are equal length
    after any prior truncation to avoid misaligned zip.
    """
    if close_list is None:
        return None, None
    # Both must have same length — enforce here
    length = min(len(close_list), len(quote_list))
    if length < 2:
        return None, None
    closes  = close_list[:length]
    volumes = quote_list[:length]

    try:
        obv = Decimal("0")
        obv_series: list[Decimal] = [Decimal("0")]
        for i in range(1, length):
            if closes[i] > closes[i - 1]:
                obv += volumes[i]
            elif closes[i] < closes[i - 1]:
                obv -= volumes[i]
            obv_series.append(obv)

        return obv, _trend_label(obv_series, window_fast)
    except Exception as exc:  # noqa: BLE001
        logger.warning("OBV computation failed: %s", exc)
        return None, None


def _compute_vwap(
    quote_list: list[Decimal],
    close_list: list[Decimal] | None,
    high_list: list[Decimal] | None,
    low_list: list[Decimal] | None,
    window: int,
) -> tuple[Decimal | None, Decimal | None]:
    """
    Rolling VWAP over the last *window* bars.

    Typical price = (H + L + C) / 3  when highs/lows are available.
    Falls back to close-only when H/L are absent.

    VWAP = Σ(typical_price × volume) / Σ(volume)
    """
    if close_list is None or len(close_list) < window or len(quote_list) < window:
        return None, None
    try:
        c_tail = close_list[-window:]
        v_tail = quote_list[-window:]

        # Use HLC/3 if available and aligned
        use_hlc = (
            high_list is not None
            and low_list is not None
            and len(high_list) >= window
            and len(low_list)  >= window
        )
        if use_hlc:
            h_tail = high_list[-window:]   # type: ignore[index]
            l_tail = low_list[-window:]    # type: ignore[index]
            typical = [
                (h + l + c) / Decimal("3")
                for h, l, c in zip(h_tail, l_tail, c_tail)
            ]
        else:
            typical = c_tail

        total_vol = sum(v_tail)
        if total_vol == 0:
            return None, None

        vwap = sum(tp * v for tp, v in zip(typical, v_tail)) / total_vol
        last_close = close_list[-1]
        price_vs_vwap_pct = (last_close - vwap) / vwap * Decimal("100")
        return vwap, price_vs_vwap_pct
    except Exception as exc:  # noqa: BLE001
        logger.warning("VWAP computation failed: %s", exc)
        return None, None


def _compute_vol_price_confirmation(
    quote_list: list[Decimal],
    close_list: list[Decimal] | None,
    quote_avg_20: Decimal | None,
) -> tuple[str | None, int | None]:
    """
    Classify per-bar vol-price confirmation and count consecutive streak.

    Returns (label, streak_count).

    Logic per bar:
      price moved + vol ≥ 110 % avg  → "confirmed"
      price moved + vol <  avg       → "diverging"
      price flat  or  avg unavail    → "neutral"

    Streak = how many consecutive bars have the same label as the last bar.
    This filters single-bar noise — a streak of 3+ is meaningful.
    """
    if close_list is None or len(close_list) < 2 or quote_avg_20 is None:
        return None, None
    try:
        labels: list[str] = []
        length = min(len(close_list), len(quote_list))
        for i in range(1, length):
            c_now  = close_list[i]
            c_prev = close_list[i - 1]
            vol    = quote_list[i]
            moved  = c_now != c_prev
            strong = vol >= quote_avg_20 * _VOL_CONFIRM_RATIO
            weak   = vol < quote_avg_20
            if moved and strong:
                labels.append(CONF_CONFIRMED)
            elif moved and weak:
                labels.append(CONF_DIVERGING)
            else:
                labels.append(CONF_NEUTRAL)

        if not labels:
            return None, None

        last_label = labels[-1]
        streak = 0
        for lbl in reversed(labels):
            if lbl == last_label:
                streak += 1
            else:
                break

        return last_label, streak
    except Exception as exc:  # noqa: BLE001
        logger.warning("Vol-price confirmation failed: %s", exc)
        return None, None


def _compute_book_totals(
    bid_qty_arg: Decimal | None,
    ask_qty_arg: Decimal | None,
    depth_bids: list[tuple[Decimal, Decimal]] | None,
    depth_asks: list[tuple[Decimal, Decimal]] | None,
) -> tuple[Decimal | None, Decimal | None]:
    """Depth totals take priority; scalar args used only when depth absent."""
    if depth_bids and depth_asks:
        try:
            return (
                sum(q for _, q in depth_bids),
                sum(q for _, q in depth_asks),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Depth book total computation failed: %s", exc)
    return bid_qty_arg, ask_qty_arg


def _compute_book_imbalance(
    bid_qty: Decimal | None,
    ask_qty: Decimal | None,
    threshold: Decimal,
) -> tuple[Decimal | None, str | None]:
    if bid_qty is None or ask_qty is None:
        return None, None
    denom = bid_qty + ask_qty
    if denom == 0:
        return Decimal("0"), ZONE_BALANCED
    imbalance = (bid_qty - ask_qty) / denom
    if imbalance > threshold:
        zone = ZONE_BID_DOMINANT
    elif imbalance < -threshold:
        zone = ZONE_ASK_DOMINANT
    else:
        zone = ZONE_BALANCED
    return imbalance, zone


def _find_walls(
    depth: list[tuple[Decimal, Decimal]] | None,
    wall_ratio: Decimal,
) -> tuple[Decimal | None, Decimal | None, int | None]:
    """
    Find all order walls on one side of the book.

    A wall = any level whose qty ≥ wall_ratio × median qty.
    Returns (largest_wall_price, largest_wall_qty, wall_count).
    wall_count enables callers to distinguish a single large wall from
    a cluster of walls (more significant support/resistance).
    """
    if not depth:
        return None, None, None
    try:
        median = _median_qty(depth)
        if median is None or median == 0:
            return None, None, None
        threshold = wall_ratio * median
        walls = [(p, q) for p, q in depth if q >= threshold]
        if not walls:
            return None, None, 0
        largest = max(walls, key=lambda x: x[1])
        return largest[0], largest[1], len(walls)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Wall detection failed: %s", exc)
        return None, None, None


# ---------------------------------------------------------------------------
# Private stats helpers
# ---------------------------------------------------------------------------

def _mean_tail(values: list[Decimal], window: int) -> Decimal | None:
    if len(values) < window:
        return None
    tail = values[-window:]
    return sum(tail) / Decimal(str(window))


def _std_tail(values: list[Decimal], window: int) -> Decimal | None:
    """Sample std deviation (÷ n-1). Population std inflates z-scores."""
    if len(values) < window:
        return None
    tail = values[-window:]
    mean = sum(tail) / Decimal(str(window))
    var  = sum((v - mean) ** 2 for v in tail) / Decimal(str(window - 1))
    return var.sqrt()


def _trend_label(values: list[Decimal], window: int) -> str | None:
    if len(values) < window:
        return None
    slope = _linear_slope(values[-window:])
    if slope > 0:
        return TREND_UP
    if slope < 0:
        return TREND_DOWN
    return TREND_FLAT


def _linear_slope(values: list[Decimal]) -> Decimal:
    """OLS slope over evenly-spaced index 0..n-1."""
    n = len(values)
    if n < 2:
        return Decimal("0")
    x_sum  = Decimal(str(n * (n - 1) // 2))
    y_sum  = sum(values)
    xx_sum = Decimal(str(n * (n - 1) * (2 * n - 1) // 6))
    xy_sum = sum(Decimal(str(i)) * v for i, v in enumerate(values))
    denom  = Decimal(str(n)) * xx_sum - x_sum ** 2
    if denom == 0:
        return Decimal("0")
    return (Decimal(str(n)) * xy_sum - x_sum * y_sum) / denom


def _median_qty(levels: list[tuple[Decimal, Decimal]]) -> Decimal | None:
    if not levels:
        return None
    qtys = sorted(q for _, q in levels)
    mid  = len(qtys) // 2
    if len(qtys) % 2 == 1:
        return qtys[mid]
    return (qtys[mid - 1] + qtys[mid]) / Decimal("2")


def _parse_optional_series(
    seq: Sequence[Decimal] | None,
    n: int,
    name: str,
) -> list[Decimal] | None:
    """
    Parse and truncate an optional price/volume series to exactly *n* bars.

    Returns ``None`` and logs a warning on any conversion failure or if the
    parsed list is shorter than *n* after truncation.
    """
    if seq is None:
        return None
    try:
        parsed = [_to_dec(v, name) for v in seq]
    except (TypeError, ValueError) as exc:
        logger.warning(
            "compute_volume_metrics: invalid %s — dependent KPIs disabled: %s",
            name, exc,
        )
        return None
    result = parsed[:n]
    if len(result) < n:
        logger.warning(
            "compute_volume_metrics: %s has %d bars, expected %d — "
            "dependent KPIs may be partial.",
            name, len(result), n,
        )
    return result if result else None


def _to_dec(value: object, field: str) -> Decimal:
    result = to_decimal(value)
    if result is None:
        raise ValueError(f"Non-numeric value in {field}: {value!r}")
    return result


def _empty_metrics() -> VolumeMetrics:
    return VolumeMetrics(
        base_last=None, quote_last=None,
        quote_avg_20=None, quote_avg_50=None,
        quote_std_20=None, quote_zscore_20=None,
        rvol=None, vol_momentum_pct=None,
        quote_trend=None, spike=None,
        taker_buy_ratio=None, taker_buy_ratio_avg20=None, buy_pressure=None,
        obv=None, obv_trend=None,
        vwap_20=None, price_vs_vwap_pct=None,
        vol_price_confirmation=None, vol_conf_streak=None,
        bid_qty=None, ask_qty=None,
        book_imbalance=None, liquidity_zones=None,
        buy_wall_price=None, buy_wall_qty=None, buy_wall_count=None,
        sell_wall_price=None, sell_wall_qty=None, sell_wall_count=None,
    )