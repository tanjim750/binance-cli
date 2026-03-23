"""
cryptogent.market.trend
~~~~~~~~~~~~~~~~~~~~~~~
Trend indicator computation:

  EMA/SMA      — 20 / 50 / 200 moving averages + crossovers + strength
  ADX          — Average Directional Index (trend strength, directionless)
  Ichimoku     — Tenkan / Kijun / Senkou A & B / Chikou spans
  Price vs MA  — Normalised distance of price from each EMA (%)
  Trend bias   — Composite regime: strong_bull / bull / neutral / bear / strong_bear
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from .utils import (
    series_last,
    series_last_valid,
    series_prev,
    to_decimal,
    validate_closes,
)
from cryptogent.market.compute_engine import ComputeEngineError

if TYPE_CHECKING:
    import pandas as pd
    import pandas_ta as pta

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bar floors
#   EMA/SMA(200)    → 202 bars (200 + 2 for prev-bar crossover)
#   ADX(14)         → 28 bars (2 * period, standard warm-up)
#   Ichimoku        → 52 bars (Senkou B span = 52 by default)
#   Safe floor      → 202 bars (EMA-200 dominates)
# ---------------------------------------------------------------------------
_MIN_BARS: int = 202
_ADX_LENGTH: int = 14
_SLOPE_LOOKBACK: int = 3      # Multi-bar EMA-50 slope window

# Ichimoku parameters (standard crypto settings = same as equities)
_ICHI_TENKAN: int = 9
_ICHI_KIJUN: int  = 26
_ICHI_SENKOU: int = 52

# Trend-bias distance thresholds (EMA-50 vs EMA-200, % of EMA-200)
_STRONG_BULL_DIST_PCT = Decimal("1")
_STRONG_BEAR_DIST_PCT = Decimal("-1")

# ADX strength thresholds
_ADX_WEAK_TREND    = Decimal("20")   # < 20: no meaningful trend
_ADX_STRONG_TREND  = Decimal("40")   # > 40: strong trend

# Public string constants
BULLISH      = "bullish"
BEARISH      = "bearish"
FLAT         = "flat"
GOLDEN_CROSS = "golden_cross"
DEATH_CROSS  = "death_cross"
BULLISH_CROSS = "bullish_cross"
BEARISH_CROSS = "bearish_cross"


# ---------------------------------------------------------------------------
# Public data contract
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrendMetrics:
    """
    Immutable trend snapshot for a single asset.

    All fields are ``None`` when fewer than ``_MIN_BARS`` closes are available
    or when that indicator failed.

    New KPIs vs previous version
    ----------------------------
    adx                 : ADX value — trend strength regardless of direction
    adx_pos             : +DI directional indicator (bullish pressure)
    adx_neg             : -DI directional indicator (bearish pressure)
    adx_trend_strength  : "no_trend" | "weak" | "strong" | "very_strong"
    ichi_tenkan         : Ichimoku Tenkan-sen (9-bar conversion line)
    ichi_kijun          : Ichimoku Kijun-sen  (26-bar base line)
    ichi_senkou_a       : Ichimoku Senkou Span A (leading span, cloud top/bottom)
    ichi_senkou_b       : Ichimoku Senkou Span B (leading span, cloud top/bottom)
    ichi_cloud_bias     : "bullish" | "bearish" | "flat" (price vs cloud)
    price_vs_ema20_pct  : % distance of price from EMA-20
    price_vs_ema50_pct  : % distance of price from EMA-50
    price_vs_ema200_pct : % distance of price from EMA-200
    ema_50_200_strength_pct: Gap % between EMA-50 and EMA-200 (now on both pairs)
    """

    # ---- MA values (current) -----------------------------------------------
    ema_20: Decimal | None
    ema_50: Decimal | None
    ema_200: Decimal | None
    sma_20: Decimal | None
    sma_50: Decimal | None
    sma_200: Decimal | None

    # ---- MA values (previous bar) ------------------------------------------
    ema_20_prev: Decimal | None
    ema_50_prev: Decimal | None
    ema_200_prev: Decimal | None
    sma_20_prev: Decimal | None
    sma_50_prev: Decimal | None
    sma_200_prev: Decimal | None

    # ---- EMA 20/50 crossover -----------------------------------------------
    crossover: str | None
    crossover_event: str | None
    crossover_strength_pct: Decimal | None

    # ---- EMA 50/200 (Golden / Death cross) ---------------------------------
    ema_50_200_crossover: str | None
    ema_50_200_event: str | None
    ema_50_200_strength_pct: Decimal | None

    # ---- SMA crossovers ----------------------------------------------------
    sma_20_50_crossover: str | None
    sma_20_50_event: str | None
    sma_50_200_crossover: str | None
    sma_50_200_event: str | None

    # ---- ADX ---------------------------------------------------------------
    adx: Decimal | None
    adx_pos: Decimal | None                # +DI
    adx_neg: Decimal | None                # -DI
    adx_trend_strength: str | None         # "no_trend"|"weak"|"strong"|"very_strong"

    # ---- Ichimoku ----------------------------------------------------------
    ichi_tenkan: Decimal | None
    ichi_kijun: Decimal | None
    ichi_senkou_a: Decimal | None
    ichi_senkou_b: Decimal | None
    ichi_cloud_bias: str | None            # "bullish"|"bearish"|"flat"

    # ---- Price vs MA distance (%) ------------------------------------------
    price_vs_ema20_pct: Decimal | None
    price_vs_ema50_pct: Decimal | None
    price_vs_ema200_pct: Decimal | None

    # ---- Composite ---------------------------------------------------------
    trend_bias: str | None

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        return all(
            v is None
            for v in (self.ema_20, self.ema_50, self.ema_200,
                      self.sma_20, self.sma_50, self.sma_200)
        )

    @property
    def has_golden_cross(self) -> bool:
        return self.ema_50_200_event == GOLDEN_CROSS

    @property
    def has_death_cross(self) -> bool:
        return self.ema_50_200_event == DEATH_CROSS

    @property
    def trend_confirmed_by_adx(self) -> bool | None:
        """
        ``True`` when ADX >= 20, confirming a directional trend is in play.
        Returns ``None`` when ADX is unavailable.
        """
        if self.adx is None:
            return None
        return self.adx >= _ADX_WEAK_TREND

    @property
    def ichimoku_tk_cross_bullish(self) -> bool | None:
        """``True`` when Tenkan > Kijun (bullish TK cross)."""
        if self.ichi_tenkan is None or self.ichi_kijun is None:
            return None
        return self.ichi_tenkan > self.ichi_kijun

    @property
    def composite_trend_signal(self) -> str:
        """
        Multi-source trend signal combining EMA bias, ADX confirmation,
        Ichimoku cloud, and trend_bias into a single label.

        Returns the strongest signal where all available sources agree,
        or 'mixed' when sources conflict.
        """
        signals = []

        # EMA 50/200 bias
        if self.ema_50_200_crossover is not None:
            signals.append(self.ema_50_200_crossover)

        # Ichimoku cloud
        if self.ichi_cloud_bias is not None and self.ichi_cloud_bias != FLAT:
            signals.append(self.ichi_cloud_bias)

        # Trend bias
        if self.trend_bias is not None:
            if "bull" in self.trend_bias:
                signals.append(BULLISH)
            elif "bear" in self.trend_bias:
                signals.append(BEARISH)

        if not signals:
            return "unavailable"

        bulls = signals.count(BULLISH)
        bears = signals.count(BEARISH)

        if bulls > 0 and bears > 0:
            return "mixed"
        if bulls == len(signals):
            return "strong_bull" if bulls >= 2 else BULLISH
        if bears == len(signals):
            return "strong_bear" if bears >= 2 else BEARISH
        return "neutral"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_trend_metrics(closes: list[Decimal]) -> TrendMetrics:
    """
    Compute full trend metrics from closing prices.

    Parameters
    ----------
    closes:
        Ordered closing prices (oldest → newest). Requires >= 202 bars.

    Returns
    -------
    TrendMetrics

    Raises
    ------
    ComputeEngineError
        On import failure.
    ValueError
        On malformed input.

    Note
    ----
    ADX and Ichimoku are most accurate when computed from OHLC data.
    This module accepts closes only for API consistency.  For full
    OHLC accuracy on ADX/Ichimoku, extend the signature with highs/lows.
    """
    try:
        import pandas as _pd
        import pandas_ta as _pta
    except ImportError as exc:
        raise ComputeEngineError(
            "pandas-ta is required for trend indicators. "
            "Install with `pip install -e '.[market]'` on Python <=3.13."
        ) from exc

    float_closes = validate_closes(closes, "compute_trend_metrics")

    if len(float_closes) < _MIN_BARS:
        logger.debug(
            "compute_trend_metrics: %d bars < minimum %d; returning empty.",
            len(float_closes), _MIN_BARS,
        )
        return _empty_metrics()

    series = _pd.Series(float_closes, dtype="float64")

    # Last close — used for price-vs-MA distance calculations
    last_close = series_last(series)
    if last_close is None:
        return _empty_metrics()

    # ---- MAs ---------------------------------------------------------------
    ema20  = _safe_ma(lambda: _pta.ema(series, length=20),  "EMA-20")
    ema50  = _safe_ma(lambda: _pta.ema(series, length=50),  "EMA-50")
    ema200 = _safe_ma(lambda: _pta.ema(series, length=200), "EMA-200")
    sma20  = _safe_ma(lambda: _pta.sma(series, length=20),  "SMA-20")
    sma50  = _safe_ma(lambda: _pta.sma(series, length=50),  "SMA-50")
    sma200 = _safe_ma(lambda: _pta.sma(series, length=200), "SMA-200")

    ema_20  = series_last(ema20)  or series_last_valid(ema20)
    ema_50  = series_last(ema50)  or series_last_valid(ema50)
    ema_200 = series_last(ema200) or series_last_valid(ema200)
    sma_20  = series_last(sma20)  or series_last_valid(sma20)
    sma_50  = series_last(sma50)  or series_last_valid(sma50)
    sma_200 = series_last(sma200) or series_last_valid(sma200)

    ema_20_prev  = series_prev(ema20)
    ema_50_prev  = series_prev(ema50)
    ema_200_prev = series_prev(ema200)
    sma_20_prev  = series_prev(sma20)
    sma_50_prev  = series_prev(sma50)
    sma_200_prev = series_prev(sma200)

    # ---- Crossovers --------------------------------------------------------
    crossover              = _direction(ema_20, ema_50)
    crossover_event        = _crossover_event(ema_20, ema_50, ema_20_prev, ema_50_prev)
    crossover_strength_pct = _strength_pct(ema_20, ema_50)

    ema_50_200_crossover     = _direction(ema_50, ema_200)
    ema_50_200_event         = _crossover_event(
        ema_50, ema_200, ema_50_prev, ema_200_prev,
        bullish_label=GOLDEN_CROSS, bearish_label=DEATH_CROSS,
    )
    ema_50_200_strength_pct  = _strength_pct(ema_50, ema_200)

    sma_20_50_crossover = _direction(sma_20, sma_50)
    sma_20_50_event     = _crossover_event(sma_20, sma_50, sma_20_prev, sma_50_prev)
    sma_50_200_crossover = _direction(sma_50, sma_200)
    sma_50_200_event    = _crossover_event(
        sma_50, sma_200, sma_50_prev, sma_200_prev,
        bullish_label=GOLDEN_CROSS, bearish_label=DEATH_CROSS,
    )

    # ---- ADX ---------------------------------------------------------------
    adx, adx_pos, adx_neg, adx_strength = _compute_adx(series, _pta)

    # ---- Ichimoku ----------------------------------------------------------
    ichi_tenkan, ichi_kijun, ichi_a, ichi_b, ichi_bias = _compute_ichimoku(
        series, last_close, _pta
    )

    # ---- Price vs MA distance ----------------------------------------------
    price_vs_ema20_pct  = _price_vs_ma_pct(last_close, ema_20)
    price_vs_ema50_pct  = _price_vs_ma_pct(last_close, ema_50)
    price_vs_ema200_pct = _price_vs_ma_pct(last_close, ema_200)

    # ---- Trend bias (multi-bar slope) --------------------------------------
    ema50_lookback = _nth_prev(ema50, _SLOPE_LOOKBACK)
    trend_bias     = _compute_trend_bias(ema_50, ema_200, ema50_lookback)

    return TrendMetrics(
        ema_20=ema_20, ema_50=ema_50, ema_200=ema_200,
        sma_20=sma_20, sma_50=sma_50, sma_200=sma_200,
        ema_20_prev=ema_20_prev, ema_50_prev=ema_50_prev, ema_200_prev=ema_200_prev,
        sma_20_prev=sma_20_prev, sma_50_prev=sma_50_prev, sma_200_prev=sma_200_prev,
        crossover=crossover,
        crossover_event=crossover_event,
        crossover_strength_pct=crossover_strength_pct,
        ema_50_200_crossover=ema_50_200_crossover,
        ema_50_200_event=ema_50_200_event,
        ema_50_200_strength_pct=ema_50_200_strength_pct,
        sma_20_50_crossover=sma_20_50_crossover,
        sma_20_50_event=sma_20_50_event,
        sma_50_200_crossover=sma_50_200_crossover,
        sma_50_200_event=sma_50_200_event,
        adx=adx, adx_pos=adx_pos, adx_neg=adx_neg,
        adx_trend_strength=adx_strength,
        ichi_tenkan=ichi_tenkan, ichi_kijun=ichi_kijun,
        ichi_senkou_a=ichi_a, ichi_senkou_b=ichi_b,
        ichi_cloud_bias=ichi_bias,
        price_vs_ema20_pct=price_vs_ema20_pct,
        price_vs_ema50_pct=price_vs_ema50_pct,
        price_vs_ema200_pct=price_vs_ema200_pct,
        trend_bias=trend_bias,
    )


# ---------------------------------------------------------------------------
# Private indicator functions
# ---------------------------------------------------------------------------

def _compute_adx(
    series: "pd.Series",
    pta: "pta",
) -> tuple[Decimal | None, Decimal | None, Decimal | None, str | None]:
    """
    Compute ADX, +DI, -DI and classify trend strength.

    ADX < 20  → no meaningful trend (ranging market)
    20–40     → weak / developing trend
    40–60     → strong trend
    > 60      → very strong (often near exhaustion)
    """
    try:
        # pandas-ta adx() accepts a single series (uses it as H/L/C proxy)
        adx_df = pta.adx(series, series, series, length=_ADX_LENGTH)
        if adx_df is None or adx_df.empty:
            return None, None, None, None

        adx_col  = _find_col(adx_df, f"ADX_{_ADX_LENGTH}")
        dmp_col  = _find_col(adx_df, f"DMP_{_ADX_LENGTH}")
        dmn_col  = _find_col(adx_df, f"DMN_{_ADX_LENGTH}")

        if not adx_col:
            logger.warning("ADX column not found. Available: %s", list(adx_df.columns))
            return None, None, None, None

        adx     = to_decimal(adx_df[adx_col].iloc[-1])
        adx_pos = to_decimal(adx_df[dmp_col].iloc[-1]) if dmp_col else None
        adx_neg = to_decimal(adx_df[dmn_col].iloc[-1]) if dmn_col else None
        strength = _classify_adx_strength(adx)

        return adx, adx_pos, adx_neg, strength
    except Exception as exc:  # noqa: BLE001
        logger.warning("ADX computation failed: %s", exc)
        return None, None, None, None


def _classify_adx_strength(adx: Decimal | None) -> str | None:
    if adx is None:
        return None
    if adx < _ADX_WEAK_TREND:
        return "no_trend"
    if adx < Decimal("40"):
        return "weak"
    if adx < Decimal("60"):
        return "strong"
    return "very_strong"


def _compute_ichimoku(
    series: "pd.Series",
    last_close: Decimal,
    pta: "pta",
) -> tuple[
    Decimal | None, Decimal | None,
    Decimal | None, Decimal | None,
    str | None,
]:
    """
    Compute Ichimoku Cloud components and classify price vs cloud.

    Returns (tenkan, kijun, senkou_a, senkou_b, cloud_bias).

    cloud_bias:
      "bullish" — price above both Senkou spans (above cloud)
      "bearish" — price below both Senkou spans (below cloud)
      "flat"    — price inside the cloud (indecision)
    """
    try:
        ichi_df = pta.ichimoku(
            series, series, series,
            tenkan=_ICHI_TENKAN,
            kijun=_ICHI_KIJUN,
            senkou=_ICHI_SENKOU,
        )
        # pandas-ta returns a tuple: (span_df, leading_df) or just a df
        if isinstance(ichi_df, tuple):
            span_df, lead_df = ichi_df[0], ichi_df[1] if len(ichi_df) > 1 else None
        else:
            span_df, lead_df = ichi_df, None

        if span_df is None or span_df.empty:
            return None, None, None, None, None

        tenkan_col  = _find_col(span_df, f"ITS_{_ICHI_TENKAN}")
        kijun_col   = _find_col(span_df, f"IKS_{_ICHI_KIJUN}")
        senkou_a_col = _find_col(span_df, "ISA_")
        senkou_b_col = _find_col(span_df, "ISB_")

        tenkan   = to_decimal(span_df[tenkan_col].iloc[-1])  if tenkan_col   else None
        kijun    = to_decimal(span_df[kijun_col].iloc[-1])   if kijun_col    else None
        senkou_a = to_decimal(span_df[senkou_a_col].iloc[-1]) if senkou_a_col else None
        senkou_b = to_decimal(span_df[senkou_b_col].iloc[-1]) if senkou_b_col else None

        cloud_bias = _classify_ichimoku_bias(last_close, senkou_a, senkou_b)

        return tenkan, kijun, senkou_a, senkou_b, cloud_bias

    except Exception as exc:  # noqa: BLE001
        logger.warning("Ichimoku computation failed: %s", exc)
        return None, None, None, None, None


def _classify_ichimoku_bias(
    price: Decimal,
    senkou_a: Decimal | None,
    senkou_b: Decimal | None,
) -> str | None:
    if senkou_a is None or senkou_b is None:
        return None
    cloud_top    = max(senkou_a, senkou_b)
    cloud_bottom = min(senkou_a, senkou_b)
    if price > cloud_top:
        return BULLISH
    if price < cloud_bottom:
        return BEARISH
    return FLAT


def _price_vs_ma_pct(price: Decimal, ma: Decimal | None) -> Decimal | None:
    """
    Normalised distance of price from a moving average.

    (price - ma) / ma * 100

    Positive = price above MA (bullish extension).
    Negative = price below MA (bearish extension or support).
    """
    if ma is None or ma == 0:
        return None
    return (price - ma) / ma * Decimal("100")


# ---------------------------------------------------------------------------
# Shared crossover / direction helpers
# ---------------------------------------------------------------------------

def _safe_ma(fn, label: str) -> "pd.Series | None":
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s computation failed: %s", label, exc)
        return None


def _direction(fast: Decimal | None, slow: Decimal | None) -> str | None:
    if fast is None or slow is None:
        return None
    if fast > slow:
        return BULLISH
    if fast < slow:
        return BEARISH
    return FLAT


def _crossover_event(
    curr_fast: Decimal | None,
    curr_slow: Decimal | None,
    prev_fast: Decimal | None,
    prev_slow: Decimal | None,
    bullish_label: str = BULLISH_CROSS,
    bearish_label: str = BEARISH_CROSS,
) -> str | None:
    """Strict bar-over-bar crossover — excludes the prev==slow touching edge case."""
    if any(v is None for v in (curr_fast, curr_slow, prev_fast, prev_slow)):
        return None
    if prev_fast < prev_slow and curr_fast > curr_slow:  # type: ignore[operator]
        return bullish_label
    if prev_fast > prev_slow and curr_fast < curr_slow:  # type: ignore[operator]
        return bearish_label
    return None


def _strength_pct(fast: Decimal | None, slow: Decimal | None) -> Decimal | None:
    if fast is None or slow is None or slow == 0:
        return None
    return abs(fast - slow) / slow * Decimal("100")


def _nth_prev(series: "pd.Series | None", n: int) -> Decimal | None:
    if series is None or len(series) < n + 1:
        return None
    return to_decimal(series.iloc[-(n + 1)])


def _find_col(df: "pd.DataFrame", prefix: str) -> str | None:
    for col in df.columns:
        if str(col).startswith(prefix):
            return str(col)
    return None


def _compute_trend_bias(
    ema_50: Decimal | None,
    ema_200: Decimal | None,
    ema_50_lookback: Decimal | None,
) -> str | None:
    """
    Regime classification from EMA-50 position vs EMA-200 + multi-bar slope.
    Returns ``None`` without EMA-200 (misleading to classify without long anchor).
    """
    if ema_50 is None or ema_200 is None:
        return None

    slope_pct = Decimal("0")
    if ema_50_lookback is not None and ema_50_lookback != 0:
        slope_pct = (ema_50 - ema_50_lookback) / ema_50_lookback * Decimal("100")

    dist_pct = Decimal("0")
    if ema_200 != 0:
        dist_pct = (ema_50 - ema_200) / ema_200 * Decimal("100")

    if ema_50 > ema_200:
        if slope_pct > 0 and dist_pct >= _STRONG_BULL_DIST_PCT:
            return "strong_bull"
        if slope_pct > 0:
            return "bull"
    elif ema_50 < ema_200:
        if slope_pct < 0 and dist_pct <= _STRONG_BEAR_DIST_PCT:
            return "strong_bear"
        if slope_pct < 0:
            return "bear"

    return "neutral"


def _empty_metrics() -> TrendMetrics:
    return TrendMetrics(
        ema_20=None, ema_50=None, ema_200=None,
        sma_20=None, sma_50=None, sma_200=None,
        ema_20_prev=None, ema_50_prev=None, ema_200_prev=None,
        sma_20_prev=None, sma_50_prev=None, sma_200_prev=None,
        crossover=None, crossover_event=None, crossover_strength_pct=None,
        ema_50_200_crossover=None, ema_50_200_event=None, ema_50_200_strength_pct=None,
        sma_20_50_crossover=None, sma_20_50_event=None,
        sma_50_200_crossover=None, sma_50_200_event=None,
        adx=None, adx_pos=None, adx_neg=None, adx_trend_strength=None,
        ichi_tenkan=None, ichi_kijun=None,
        ichi_senkou_a=None, ichi_senkou_b=None, ichi_cloud_bias=None,
        price_vs_ema20_pct=None, price_vs_ema50_pct=None, price_vs_ema200_pct=None,
        trend_bias=None,
    )