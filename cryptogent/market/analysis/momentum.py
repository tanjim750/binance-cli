"""
cryptogent.market.momentum
~~~~~~~~~~~~~~~~~~~~~~~~~~
Momentum indicator computation:

  RSI          — Relative Strength Index (14)
  MACD         — Moving Average Convergence Divergence (12/26/9)
  StochRSI     — Stochastic RSI %K and %D (14/14/3/3)
  Williams %R  — Overbought/oversold oscillator [-100, 0]
  CCI          — Commodity Channel Index, deviation from statistical mean
  ROC          — Rate of Change, raw price momentum acceleration
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from .utils import df_last, series_last, series_last_valid, validate_closes
from cryptogent.market.compute_engine import ComputeEngineError

if TYPE_CHECKING:
    import pandas as pd
    import pandas_ta as pta

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bar floors
#   RSI(14)                  → 14 bars
#   MACD(12, 26, 9)          → 35 bars (26 slow + 9 signal)
#   StochRSI(14, 14, 3, 3)   → ~31 bars
#   Williams %R(14)          → 14 bars
#   CCI(20)                  → 20 bars
#   ROC(10)                  → 10 bars
#   Safe combined floor      → 60 bars
# ---------------------------------------------------------------------------
_MIN_BARS: int = 60

# Indicator parameters
_RSI_LENGTH: int = 14
_MACD_FAST: int = 12
_MACD_SLOW: int = 26
_MACD_SIGNAL: int = 9
_STOCH_LENGTH: int = 14
_STOCH_RSI_LENGTH: int = 14
_STOCH_K: int = 3
_STOCH_D: int = 3
_WILLR_LENGTH: int = 14
_CCI_LENGTH: int = 20
_ROC_LENGTH: int = 10

# Derived pandas-ta column names
_MACD_COL     = f"MACD_{_MACD_FAST}_{_MACD_SLOW}_{_MACD_SIGNAL}"
_MACDS_COL    = f"MACDs_{_MACD_FAST}_{_MACD_SLOW}_{_MACD_SIGNAL}"
_MACDH_COL    = f"MACDh_{_MACD_FAST}_{_MACD_SLOW}_{_MACD_SIGNAL}"
_STOCH_K_COL  = f"STOCHRSIk_{_STOCH_LENGTH}_{_STOCH_RSI_LENGTH}_{_STOCH_K}_{_STOCH_D}"
_STOCH_D_COL  = f"STOCHRSId_{_STOCH_LENGTH}_{_STOCH_RSI_LENGTH}_{_STOCH_K}_{_STOCH_D}"

# RSI / Williams %R zone thresholds
_RSI_OVERBOUGHT  = Decimal("70")
_RSI_OVERSOLD    = Decimal("30")
_WILLR_OVERBOUGHT = Decimal("-20")   # closer to 0 = overbought in [-100, 0] scale
_WILLR_OVERSOLD   = Decimal("-80")
_CCI_OVERBOUGHT   = Decimal("100")
_CCI_OVERSOLD     = Decimal("-100")


# ---------------------------------------------------------------------------
# Public data contract
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MomentumMetrics:
    """
    Immutable momentum snapshot for a single asset.

    All fields are ``None`` when fewer than ``_MIN_BARS`` closes are provided
    or when that indicator failed.  Partial results (some ``None``) are
    intentional — callers treat ``None`` as "unavailable."

    New KPIs vs previous version
    ----------------------------
    rsi_prev       : Previous bar's RSI — needed for divergence detection
    williams_r     : Williams %R oscillator [-100, 0]
    cci            : Commodity Channel Index
    roc            : Rate of Change (10-bar price momentum %)
    composite_signal: Voting signal across all available indicators
    """

    # ---- RSI ---------------------------------------------------------------
    rsi: Decimal | None
    rsi_prev: Decimal | None        # Previous bar — enables divergence checks

    # ---- MACD --------------------------------------------------------------
    macd: Decimal | None
    macd_signal: Decimal | None
    macd_hist: Decimal | None

    # ---- StochRSI ----------------------------------------------------------
    stoch_rsi_k: Decimal | None     # %K fast line
    stoch_rsi_d: Decimal | None     # %D signal line

    # ---- Williams %R -------------------------------------------------------
    williams_r: Decimal | None      # Range: [-100, 0]; near 0 = overbought

    # ---- CCI ---------------------------------------------------------------
    cci: Decimal | None             # >+100 overbought; <-100 oversold

    # ---- ROC ---------------------------------------------------------------
    roc: Decimal | None             # 10-bar % price change (momentum acceleration)

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        return all(
            v is None
            for v in (self.rsi, self.macd, self.stoch_rsi_k,
                      self.williams_r, self.cci, self.roc)
        )

    @property
    def rsi_zone(self) -> str:
        if self.rsi is None:
            return "unavailable"
        if self.rsi >= _RSI_OVERBOUGHT:
            return "overbought"
        if self.rsi <= _RSI_OVERSOLD:
            return "oversold"
        return "neutral"

    @property
    def williams_r_zone(self) -> str:
        if self.williams_r is None:
            return "unavailable"
        if self.williams_r >= _WILLR_OVERBOUGHT:
            return "overbought"
        if self.williams_r <= _WILLR_OVERSOLD:
            return "oversold"
        return "neutral"

    @property
    def cci_zone(self) -> str:
        if self.cci is None:
            return "unavailable"
        if self.cci >= _CCI_OVERBOUGHT:
            return "overbought"
        if self.cci <= _CCI_OVERSOLD:
            return "oversold"
        return "neutral"

    @property
    def macd_bias(self) -> str:
        if self.macd is None or self.macd_signal is None:
            return "unavailable"
        diff = self.macd - self.macd_signal
        if diff > 0:
            return "bullish"
        if diff < 0:
            return "bearish"
        return "flat"

    @property
    def stoch_rsi_bias(self) -> str:
        if self.stoch_rsi_k is None or self.stoch_rsi_d is None:
            return "unavailable"
        diff = self.stoch_rsi_k - self.stoch_rsi_d
        if diff > 0:
            return "bullish"
        if diff < 0:
            return "bearish"
        return "flat"

    @property
    def roc_bias(self) -> str:
        """Positive ROC = bullish acceleration; negative = bearish."""
        if self.roc is None:
            return "unavailable"
        if self.roc > 0:
            return "bullish"
        if self.roc < 0:
            return "bearish"
        return "flat"

    @property
    def composite_signal(self) -> str:
        """
        Six-indicator voting signal.

        Each available indicator casts a directional vote:
          bullish = +1 | bearish = -1 | neutral/flat = 0

        RSI, Williams %R, and CCI vote as bearish when overbought,
        bullish when oversold — because overbought means mean-reversion risk.

        Score → label:
          ≥ 3   strong_bull
          1–2   bull
          0     neutral
          -1–-2 bear
          ≤ -3  strong_bear
        """
        _ob_map = {"overbought": "bearish", "oversold": "bullish",
                   "neutral": "neutral", "unavailable": None}

        votes = [
            self.macd_bias       if self.macd_bias       != "unavailable" else None,
            self.stoch_rsi_bias  if self.stoch_rsi_bias  != "unavailable" else None,
            self.roc_bias        if self.roc_bias         != "unavailable" else None,
            _ob_map.get(self.rsi_zone),
            _ob_map.get(self.williams_r_zone),
            _ob_map.get(self.cci_zone),
        ]
        active = [v for v in votes if v is not None]
        if not active:
            return "unavailable"

        score = sum(1 if v == "bullish" else -1 if v == "bearish" else 0 for v in active)
        if score >= 3:
            return "strong_bull"
        if score >= 1:
            return "bull"
        if score <= -3:
            return "strong_bear"
        if score <= -1:
            return "bear"
        return "neutral"

    @property
    def rsi_bearish_divergence(self) -> bool | None:
        """
        Detects a single-bar RSI bearish divergence signal.

        ``True`` when RSI fell bar-over-bar while price is in the overbought
        zone.  This is a shallow check — callers should confirm against price
        action (higher high on price + lower high on RSI for full divergence).

        Returns ``None`` when either RSI bar is unavailable.
        """
        if self.rsi is None or self.rsi_prev is None:
            return None
        return self.rsi < self.rsi_prev and self.rsi_zone == "overbought"

    @property
    def rsi_bullish_divergence(self) -> bool | None:
        """
        Single-bar RSI bullish divergence hint.

        ``True`` when RSI rose bar-over-bar while in the oversold zone.
        """
        if self.rsi is None or self.rsi_prev is None:
            return None
        return self.rsi > self.rsi_prev and self.rsi_zone == "oversold"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_momentum_metrics(closes: list[Decimal]) -> MomentumMetrics:
    """
    Compute full momentum metrics from closing prices.

    Parameters
    ----------
    closes:
        Ordered closing prices (oldest → newest).  Requires >= 60 bars.

    Returns
    -------
    MomentumMetrics

    Raises
    ------
    ComputeEngineError
        On import failure.
    ValueError
        On malformed input.
    """
    try:
        import pandas as _pd
        import pandas_ta as _pta
    except ImportError as exc:
        raise ComputeEngineError(
            "pandas-ta is required for momentum indicators. "
            "Install with `pip install -e '.[market]'` on Python <=3.13."
        ) from exc

    float_closes = validate_closes(closes, "compute_momentum_metrics")

    if len(float_closes) < _MIN_BARS:
        logger.debug(
            "compute_momentum_metrics: %d bars < minimum %d; returning empty.",
            len(float_closes), _MIN_BARS,
        )
        return _empty_metrics()

    series = _pd.Series(float_closes, dtype="float64")

    rsi, rsi_prev           = _compute_rsi(series, _pta)
    macd, macd_signal, macd_hist = _compute_macd(series, _pta)
    stoch_k, stoch_d        = _compute_stoch_rsi(series, _pta)
    williams_r              = _compute_williams_r(series, _pta)
    cci                     = _compute_cci(series, _pta)
    roc                     = _compute_roc(series, _pta)

    return MomentumMetrics(
        rsi=rsi,
        rsi_prev=rsi_prev,
        macd=macd,
        macd_signal=macd_signal,
        macd_hist=macd_hist,
        stoch_rsi_k=stoch_k,
        stoch_rsi_d=stoch_d,
        williams_r=williams_r,
        cci=cci,
        roc=roc,
    )


# ---------------------------------------------------------------------------
# Private indicator functions
# ---------------------------------------------------------------------------

def _compute_rsi(
    series: "pd.Series",
    pta: "pta",
) -> tuple[Decimal | None, Decimal | None]:
    """Return (rsi_current, rsi_prev)."""
    try:
        result = pta.rsi(series, length=_RSI_LENGTH)
        curr = series_last(result) or series_last_valid(result)
        # Previous bar: positional [-2] on the original series
        prev = None
        if result is not None and len(result) >= 2:
            from .utils import to_decimal
            prev = to_decimal(result.iloc[-2])
        return curr, prev
    except Exception as exc:  # noqa: BLE001
        logger.warning("RSI computation failed: %s", exc)
        return None, None


def _compute_macd(
    series: "pd.Series",
    pta: "pta",
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    try:
        df = pta.macd(series, fast=_MACD_FAST, slow=_MACD_SLOW, signal=_MACD_SIGNAL)
        return df_last(df, _MACD_COL), df_last(df, _MACDS_COL), df_last(df, _MACDH_COL)
    except Exception as exc:  # noqa: BLE001
        logger.warning("MACD computation failed: %s", exc)
        return None, None, None


def _compute_stoch_rsi(
    series: "pd.Series",
    pta: "pta",
) -> tuple[Decimal | None, Decimal | None]:
    try:
        df = pta.stochrsi(
            series,
            length=_STOCH_LENGTH,
            rsi_length=_STOCH_RSI_LENGTH,
            k=_STOCH_K,
            d=_STOCH_D,
        )
        return df_last(df, _STOCH_K_COL), df_last(df, _STOCH_D_COL)
    except Exception as exc:  # noqa: BLE001
        logger.warning("StochRSI computation failed: %s", exc)
        return None, None


def _compute_williams_r(series: "pd.Series", pta: "pta") -> Decimal | None:
    """
    Williams %R oscillator.

    pandas-ta computes this from close only (no H/L passed here).
    For OHLC-accurate %R pass highs/lows — acceptable simplification
    for a close-only momentum module.  Callers needing OHLC accuracy
    should use the volatility module's high/low series directly.
    """
    try:
        result = pta.willr(series, series, series, length=_WILLR_LENGTH)
        return series_last(result) or series_last_valid(result)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Williams %%R computation failed: %s", exc)
        return None


def _compute_cci(series: "pd.Series", pta: "pta") -> Decimal | None:
    """
    Commodity Channel Index.

    CCI = (typical_price - SMA(typical_price)) / (0.015 * mean_deviation)
    For a closes-only series, typical_price ≈ close.
    """
    try:
        result = pta.cci(series, series, series, length=_CCI_LENGTH)
        return series_last(result) or series_last_valid(result)
    except Exception as exc:  # noqa: BLE001
        logger.warning("CCI computation failed: %s", exc)
        return None


def _compute_roc(series: "pd.Series", pta: "pta") -> Decimal | None:
    """
    Rate of Change: (close - close[n]) / close[n] * 100.

    Measures raw price momentum without any smoothing — catches acceleration
    that RSI / MACD may lag on.
    """
    try:
        result = pta.roc(series, length=_ROC_LENGTH)
        return series_last(result) or series_last_valid(result)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ROC computation failed: %s", exc)
        return None


def _empty_metrics() -> MomentumMetrics:
    return MomentumMetrics(
        rsi=None, rsi_prev=None,
        macd=None, macd_signal=None, macd_hist=None,
        stoch_rsi_k=None, stoch_rsi_d=None,
        williams_r=None, cci=None, roc=None,
    )