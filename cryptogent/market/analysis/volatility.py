"""
cryptogent.market.volatility
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Volatility indicator computation:

  ATR          — Average True Range (price units + % of close)
  Bollinger    — Upper / mid / lower bands, width %, %B, position
  Keltner      — Keltner Channel upper / lower (EMA ± k*ATR)
  Squeeze      — True squeeze: BB inside KC (TTM-style), not a fixed threshold
  Hist. Vol    — 20-bar log-return standard deviation, annualised
  Chandelier   — Volatility-adjusted trailing stop levels (long + short)
  Vol regime   — low / normal / high / extreme classification
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from .utils import series_last, series_last_valid, to_decimal
from cryptogent.market.compute_engine import ComputeEngineError

if TYPE_CHECKING:
    import pandas as pd
    import pandas_ta as pta

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bar floors
# ---------------------------------------------------------------------------
_MIN_BARS: int = 22          # BBands(20) + 2 buffer
_ATR_LENGTH: int = 14
_BB_LENGTH: int = 20
_BB_STD: float = 2.0
_KC_EMA_LENGTH: int = 20     # Keltner Channel EMA period
_KC_ATR_MULT: float = 1.5    # Keltner ATR multiplier (standard)
_HV_LENGTH: int = 20         # Historical volatility window
_HV_ANNUALISE: int = 365     # Crypto trades 24/7 — use 365 not 252
_CHANDELIER_LENGTH: int = 22 # Bars for Chandelier Exit high/low lookback
_CHANDELIER_MULT: Decimal = Decimal("3")  # ATR multiplier for Chandelier

# ---------------------------------------------------------------------------
# Vol-regime ATR% thresholds (crypto defaults)
#   low      ATR% <  1 %    tight consolidation
#   normal   ATR% 1–3 %     typical trending
#   high     ATR% 3–6 %     elevated (news / breakout)
#   extreme  ATR% >  6 %    panic / blow-off
# ---------------------------------------------------------------------------
_VOL_LOW_THRESH     = Decimal("1")
_VOL_NORMAL_THRESH  = Decimal("3")
_VOL_HIGH_THRESH    = Decimal("6")

# Bollinger Band column prefixes (pandas-ta naming)
_BB_UPPER_PREFIX = "BBU_"
_BB_MID_PREFIX   = "BBM_"
_BB_LOWER_PREFIX = "BBL_"
_BB_PCT_PREFIX   = "BBP_"   # %B column

# Public string constants
BB_ABOVE_UPPER = "above_upper"
BB_ABOVE_MID   = "above_mid"
BB_BELOW_MID   = "below_mid"
BB_BELOW_LOWER = "below_lower"

VOL_LOW     = "low"
VOL_NORMAL  = "normal"
VOL_HIGH    = "high"
VOL_EXTREME = "extreme"


# ---------------------------------------------------------------------------
# Public data contract
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VolatilityMetrics:
    """
    Immutable volatility snapshot for a single asset.

    All fields are ``None`` when fewer than ``_MIN_BARS`` bars are available
    or when that specific indicator failed.

    New KPIs vs previous version
    ----------------------------
    bb_pct_b          : Bollinger %B  — price position within bands [0, 1]
    kc_upper          : Keltner Channel upper band
    kc_lower          : Keltner Channel lower band
    squeeze           : True when BB is *inside* KC (TTM Squeeze logic)
    hist_vol_pct      : 20-bar annualised historical volatility (log returns)
    chandelier_long   : Chandelier Exit long stop (highest_high - mult*ATR)
    chandelier_short  : Chandelier Exit short stop (lowest_low  + mult*ATR)
    vol_regime        : "low" | "normal" | "high" | "extreme"
    """

    # ---- ATR ---------------------------------------------------------------
    atr: Decimal | None             # ATR(14) in price units
    atr_pct: Decimal | None         # ATR as % of last close

    # ---- Bollinger Bands ---------------------------------------------------
    bb_upper: Decimal | None
    bb_mid: Decimal | None
    bb_lower: Decimal | None
    bb_width_pct: Decimal | None    # (upper - lower) / mid * 100
    bb_pct_b: Decimal | None        # %B: (price - lower) / (upper - lower)
    bb_position: str | None         # "above_upper"|"above_mid"|"below_mid"|"below_lower"

    # ---- Keltner Channel ---------------------------------------------------
    kc_upper: Decimal | None        # EMA(20) + 1.5 * ATR(14)
    kc_lower: Decimal | None        # EMA(20) - 1.5 * ATR(14)

    # ---- Squeeze -----------------------------------------------------------
    squeeze: bool | None            # True when BB is inside KC (breakout loading)

    # ---- Historical Volatility ---------------------------------------------
    hist_vol_pct: Decimal | None    # Annualised 20-bar log-return std deviation (%)

    # ---- Chandelier Exit ---------------------------------------------------
    chandelier_long: Decimal | None   # Trailing stop for longs
    chandelier_short: Decimal | None  # Trailing stop for shorts

    # ---- Composite --------------------------------------------------------
    vol_regime: str | None          # "low" | "normal" | "high" | "extreme"

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        """``True`` when every core field is ``None``."""
        return all(
            v is None
            for v in (self.atr, self.bb_upper, self.bb_mid, self.bb_lower)
        )

    @property
    def is_squeeze(self) -> bool | None:
        """
        ``True`` when price action is in a TTM-style squeeze
        (Bollinger Bands inside Keltner Channel).
        Returns ``None`` when KC or BB data is unavailable.
        """
        return self.squeeze

    @property
    def price_in_upper_half(self) -> bool | None:
        """``True`` when price is above the BB mid band."""
        if self.bb_position is None:
            return None
        return self.bb_position in (BB_ABOVE_UPPER, BB_ABOVE_MID)

    @property
    def chandelier_breach_long(self) -> bool | None:
        """
        ``True`` when last close is below the long Chandelier Exit —
        a potential long exit / trend reversal signal.
        Requires both ``chandelier_long`` and ``bb_mid`` (used as price proxy).
        """
        # We don't store last_close in the dataclass; callers should compare
        # chandelier_long directly against their price.  This property exists
        # as a reminder / documentation of usage intent.
        return None  # Intentionally None — compare chandelier_long externally


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_volatility_metrics(
    highs: list[Decimal],
    lows: list[Decimal],
    closes: list[Decimal],
) -> VolatilityMetrics:
    """
    Compute full volatility metrics from OHLC data.

    Parameters
    ----------
    highs, lows, closes:
        Ordered price lists (oldest → newest).  If lengths differ, the
        shortest is used and a warning is logged.

    Returns
    -------
    VolatilityMetrics

    Raises
    ------
    ComputeEngineError
        On import failure.
    ValueError
        On empty or None input lists.
    """
    # 1. Import guard
    try:
        import pandas as _pd
        import pandas_ta as _pta
    except ImportError as exc:
        raise ComputeEngineError(
            "pandas-ta is required for volatility indicators. "
            "Install with `pip install -e '.[market]'` on Python <=3.13."
        ) from exc

    # 2. Input validation
    highs, lows, closes = _validate_inputs(highs, lows, closes)

    # 3. Length alignment
    n = min(len(highs), len(lows), len(closes))
    if not (len(highs) == len(lows) == len(closes)):
        logger.warning(
            "compute_volatility_metrics: input lengths differ "
            "(highs=%d, lows=%d, closes=%d); truncating to %d bars.",
            len(highs), len(lows), len(closes), n,
        )
    highs, lows, closes = highs[:n], lows[:n], closes[:n]

    # 4. Minimum bar guard
    if n < _MIN_BARS:
        logger.debug(
            "compute_volatility_metrics: %d bars < minimum %d; returning empty.",
            n, _MIN_BARS,
        )
        return _empty_metrics()

    # 5. Build Series
    close_s = _pd.Series([float(c) for c in closes], dtype="float64")
    high_s  = _pd.Series([float(h) for h in highs],  dtype="float64")
    low_s   = _pd.Series([float(l) for l in lows],   dtype="float64")

    last_close = series_last(close_s)
    if last_close is None:
        logger.warning("compute_volatility_metrics: last close is NaN; returning empty.")
        return _empty_metrics()

    # 6. Compute indicators independently
    atr, atr_pct = _compute_atr(high_s, low_s, close_s, last_close, _pta)

    (bb_upper, bb_mid, bb_lower,
     bb_width_pct, bb_pct_b, bb_position) = _compute_bbands(close_s, last_close, _pta)

    kc_upper, kc_lower = _compute_keltner(close_s, high_s, low_s, atr, _pta)

    squeeze = _compute_squeeze(bb_upper, bb_lower, kc_upper, kc_lower)

    hist_vol_pct = _compute_hist_vol(close_s)

    chandelier_long, chandelier_short = _compute_chandelier(high_s, low_s, atr)

    vol_regime = _classify_vol_regime(atr_pct)

    return VolatilityMetrics(
        atr=atr,
        atr_pct=atr_pct,
        bb_upper=bb_upper,
        bb_mid=bb_mid,
        bb_lower=bb_lower,
        bb_width_pct=bb_width_pct,
        bb_pct_b=bb_pct_b,
        bb_position=bb_position,
        kc_upper=kc_upper,
        kc_lower=kc_lower,
        squeeze=squeeze,
        hist_vol_pct=hist_vol_pct,
        chandelier_long=chandelier_long,
        chandelier_short=chandelier_short,
        vol_regime=vol_regime,
    )


# ---------------------------------------------------------------------------
# Private indicator functions
# ---------------------------------------------------------------------------

def _compute_atr(
    high_s: "pd.Series",
    low_s: "pd.Series",
    close_s: "pd.Series",
    last_close: Decimal,
    pta: "pta",
) -> tuple[Decimal | None, Decimal | None]:
    if len(close_s) < _ATR_LENGTH:
        return None, None
    try:
        atr_series = pta.atr(high_s, low_s, close_s, length=_ATR_LENGTH)
        atr = series_last(atr_series) or series_last_valid(atr_series)
        if atr is None:
            return None, None
        atr_pct = (atr / last_close * Decimal("100")) if last_close != 0 else None
        return atr, atr_pct
    except Exception as exc:  # noqa: BLE001
        logger.warning("ATR computation failed: %s", exc)
        return None, None


def _compute_bbands(
    close_s: "pd.Series",
    last_close: Decimal,
    pta: "pta",
) -> tuple[
    Decimal | None, Decimal | None, Decimal | None,
    Decimal | None, Decimal | None, str | None,
]:
    """Return (bb_upper, bb_mid, bb_lower, bb_width_pct, bb_pct_b, bb_position)."""
    if len(close_s) < _BB_LENGTH:
        return None, None, None, None, None, None
    try:
        bb_df = pta.bbands(close_s, length=_BB_LENGTH, std=_BB_STD)
        if bb_df is None or bb_df.empty:
            return None, None, None, None, None, None

        col_upper = _find_col(bb_df, _BB_UPPER_PREFIX)
        col_mid   = _find_col(bb_df, _BB_MID_PREFIX)
        col_lower = _find_col(bb_df, _BB_LOWER_PREFIX)
        col_pct_b = _find_col(bb_df, _BB_PCT_PREFIX)

        if not all((col_upper, col_mid, col_lower)):
            logger.warning("BBands columns missing. Got: %s", list(bb_df.columns))
            return None, None, None, None, None, None

        bb_upper = to_decimal(bb_df[col_upper].iloc[-1])
        bb_mid   = to_decimal(bb_df[col_mid].iloc[-1])
        bb_lower = to_decimal(bb_df[col_lower].iloc[-1])

        if any(v is None for v in (bb_upper, bb_mid, bb_lower)):
            return None, None, None, None, None, None

        bb_width_pct: Decimal | None = None
        if bb_mid != 0:
            bb_width_pct = (bb_upper - bb_lower) / bb_mid * Decimal("100")  # type: ignore[operator]

        # %B — prefer pandas-ta's own column; fall back to manual calculation
        bb_pct_b: Decimal | None = None
        if col_pct_b:
            bb_pct_b = to_decimal(bb_df[col_pct_b].iloc[-1])
        if bb_pct_b is None and bb_upper != bb_lower:  # type: ignore[operator]
            band_range = bb_upper - bb_lower  # type: ignore[operator]
            if band_range != 0:
                bb_pct_b = (last_close - bb_lower) / band_range  # type: ignore[operator]

        bb_position = _classify_bb_position(last_close, bb_upper, bb_mid, bb_lower)  # type: ignore[arg-type]

        return bb_upper, bb_mid, bb_lower, bb_width_pct, bb_pct_b, bb_position

    except Exception as exc:  # noqa: BLE001
        logger.warning("Bollinger Bands computation failed: %s", exc)
        return None, None, None, None, None, None


def _compute_keltner(
    close_s: "pd.Series",
    high_s: "pd.Series",
    low_s: "pd.Series",
    atr: Decimal | None,
    pta: "pta",
) -> tuple[Decimal | None, Decimal | None]:
    """
    Compute Keltner Channel upper and lower bands.

    KC = EMA(close, 20) ± KC_MULT * ATR(14)

    We compute EMA independently so KC can be returned even when the
    pandas-ta ``kc`` function is unavailable in older versions.
    """
    if atr is None or len(close_s) < _KC_EMA_LENGTH:
        return None, None
    try:
        ema_series = pta.ema(close_s, length=_KC_EMA_LENGTH)
        ema = series_last(ema_series) or series_last_valid(ema_series)
        if ema is None:
            return None, None
        offset = Decimal(str(_KC_ATR_MULT)) * atr
        return ema + offset, ema - offset
    except Exception as exc:  # noqa: BLE001
        logger.warning("Keltner Channel computation failed: %s", exc)
        return None, None


def _compute_squeeze(
    bb_upper: Decimal | None,
    bb_lower: Decimal | None,
    kc_upper: Decimal | None,
    kc_lower: Decimal | None,
) -> bool | None:
    """
    TTM Squeeze: True when Bollinger Bands are fully inside Keltner Channel.

    This is the canonical squeeze definition from John Carter / Lazy Bear.
    A squeeze indicates compressed volatility with a high-probability breakout
    loading.  Direction of breakout is not determined here.
    """
    if any(v is None for v in (bb_upper, bb_lower, kc_upper, kc_lower)):
        return None
    return bb_upper <= kc_upper and bb_lower >= kc_lower  # type: ignore[operator]


def _compute_hist_vol(close_s: "pd.Series") -> Decimal | None:
    """
    Compute 20-bar annualised historical volatility from log returns.

    HV = std(log(close[t] / close[t-1]), window=20) * sqrt(365) * 100

    Uses 365 for annualisation since crypto markets run 24/7/365.
    Returns as a percentage (e.g. 80.5 means 80.5 % annualised vol).
    """
    if len(close_s) < _HV_LENGTH + 1:
        return None
    try:
        log_returns = close_s.pct_change().apply(
            lambda r: math.log(1 + r) if r > -1 else float("nan")
        )
        hv_std = log_returns.rolling(_HV_LENGTH).std().iloc[-1]
        if hv_std is None or math.isnan(hv_std):
            return None
        annualised = hv_std * math.sqrt(_HV_ANNUALISE) * 100
        return to_decimal(annualised)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Historical volatility computation failed: %s", exc)
        return None


def _compute_chandelier(
    high_s: "pd.Series",
    low_s: "pd.Series",
    atr: Decimal | None,
) -> tuple[Decimal | None, Decimal | None]:
    """
    Compute Chandelier Exit levels.

    chandelier_long  = highest_high(n) - mult * ATR
    chandelier_short = lowest_low(n)  + mult * ATR

    These are volatility-adjusted trailing stop levels.  Price closing below
    chandelier_long signals a long exit; closing above chandelier_short signals
    a short exit.
    """
    if atr is None or len(high_s) < _CHANDELIER_LENGTH:
        return None, None
    try:
        highest_high = to_decimal(high_s.rolling(_CHANDELIER_LENGTH).max().iloc[-1])
        lowest_low   = to_decimal(low_s.rolling(_CHANDELIER_LENGTH).min().iloc[-1])
        if highest_high is None or lowest_low is None:
            return None, None
        offset = _CHANDELIER_MULT * atr
        return highest_high - offset, lowest_low + offset
    except Exception as exc:  # noqa: BLE001
        logger.warning("Chandelier Exit computation failed: %s", exc)
        return None, None


def _classify_bb_position(
    price: Decimal,
    upper: Decimal,
    mid: Decimal,
    lower: Decimal,
) -> str:
    if price > upper:
        return BB_ABOVE_UPPER
    if price < lower:
        return BB_BELOW_LOWER
    if price >= mid:
        return BB_ABOVE_MID
    return BB_BELOW_MID


def _classify_vol_regime(atr_pct: Decimal | None) -> str | None:
    """
    Four-tier vol regime from ATR%.

    low      < 1 %    consolidation
    normal   1–3 %    trending
    high     3–6 %    elevated / news
    extreme  > 6 %    panic / blow-off top
    """
    if atr_pct is None:
        return None
    if atr_pct < _VOL_LOW_THRESH:
        return VOL_LOW
    if atr_pct <= _VOL_NORMAL_THRESH:
        return VOL_NORMAL
    if atr_pct <= _VOL_HIGH_THRESH:
        return VOL_HIGH
    return VOL_EXTREME


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _find_col(df: "pd.DataFrame", prefix: str) -> str | None:
    for col in df.columns:
        if str(col).startswith(prefix):
            return str(col)
    return None


def _validate_inputs(
    highs: object,
    lows: object,
    closes: object,
) -> tuple[list, list, list]:
    for name, seq in (("highs", highs), ("lows", lows), ("closes", closes)):
        if not seq:
            raise ValueError(
                f"compute_volatility_metrics: '{name}' must be a non-empty list, "
                f"got {type(seq).__name__}."
            )
    return list(highs), list(lows), list(closes)  # type: ignore[arg-type]


def _empty_metrics() -> VolatilityMetrics:
    return VolatilityMetrics(
        atr=None, atr_pct=None,
        bb_upper=None, bb_mid=None, bb_lower=None,
        bb_width_pct=None, bb_pct_b=None, bb_position=None,
        kc_upper=None, kc_lower=None,
        squeeze=None,
        hist_vol_pct=None,
        chandelier_long=None, chandelier_short=None,
        vol_regime=None,
    )