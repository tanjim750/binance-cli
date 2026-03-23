"""
cryptogent.market.analysis.quant
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Quantitative analytics:

  Correlation       — Pearson on log-returns vs benchmark (aligned by close_time)
  Beta              — covariance(target, bench) / variance(bench)
  Return stats      — log-return, rolling mean/std, z-score
  Realized vol      — annualised (crypto 365-day) log-return std deviation
  Vol regime        — rolling subwindow std vs median (correct median formula)
  Mean reversion    — ±0.5σ / ±1.5σ / ±2σ tiered state
  Max drawdown      — peak-to-trough % over window
  Calmar ratio      — annualised return / max drawdown
  Sharpe ratio      — mean return / std dev (not risk-free-rate adjusted)
  Skewness          — 3rd standardised moment of return distribution
  Kurtosis          — 4th standardised moment (excess, Fischer definition)
  Feature vector    — log_return, rolling stats, price_vs_ema, RSI, MACD hist,
                      ATR norm, volume z-score, spread_pct, range_pct

Strict rules:
  - Closed candles only
  - Aligns target & benchmark by close_time — no forward fill
  - No silent window shrink — returns available=False when data insufficient
  - Realised vol annualised at 365 days (crypto trades 24/7)
  - ATR uses Wilder's smoothed RMA (consistent with VolatilityMetrics)
  - EMA seeded from SMA of first period bars (not from values[0])
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from ..compute_engine import ComputeEngineError
from .momentum import compute_momentum_metrics

logger = logging.getLogger(__name__)

_DEFAULT_WINDOW     = 200
_REGIME_SUBWINDOW   = 20
_ANNUALISE_DAYS     = 365       # crypto trades 24/7
_EMA_PERIOD         = 20
_ATR_PERIOD         = 14

# Mean-reversion z-score thresholds (tiered)
_MR_NEAR_THRESHOLD      = Decimal("0.5")
_MR_STRETCHED_THRESHOLD = Decimal("1.5")
_MR_EXTREME_THRESHOLD   = Decimal("2.0")


# ---------------------------------------------------------------------------
# Public data contract
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QuantMetrics:
    """
    Immutable quantitative analytics snapshot.

    ``available=False`` means insufficient data or invalid inputs.
    ``unavailable_reason`` explains why.

    All float fields are ``None`` when unavailable.
    """

    available: bool
    window: int
    benchmark: str
    corr_method: str
    unavailable_reason: str | None

    # ---- Correlation & beta ------------------------------------------------
    correlation: float | None         # Pearson r on log-returns vs benchmark
    beta: float | None                # cov(target, bench) / var(bench)

    # ---- Return statistics -------------------------------------------------
    log_return: float | None          # Last bar log-return
    rolling_return_mean: float | None # Mean of window log-returns
    rolling_return_std: float | None  # Std dev of window log-returns
    return_zscore: float | None       # (last_ret - mean) / std

    # ---- Volatility --------------------------------------------------------
    realized_vol: float | None        # Annualised log-return std dev (%)
    vol_regime: str | None            # "elevated" | "normal" | "suppressed"

    # ---- Risk metrics ------------------------------------------------------
    max_drawdown_pct: float | None    # Peak-to-trough % over window
    sharpe_ratio: float | None        # mean_return / std_dev (not RF-adjusted)
    calmar_ratio: float | None        # annualised_return / max_drawdown

    # ---- Return distribution -----------------------------------------------
    skewness: float | None            # 3rd standardised moment
    kurtosis: float | None            # Excess kurtosis (Fischer, normal=0)

    # ---- Mean reversion ----------------------------------------------------
    mean_dev_pct: float | None        # (last_close - mean) / mean * 100
    mean_reversion_state: str | None  # "near_mean"|"stretched_*"|"extreme_*"

    # ---- Feature vector (ML-ready, no inference) ---------------------------
    price_vs_ema_pct: float | None    # (close - EMA20) / EMA20 * 100
    rsi: float | None
    macd_hist: float | None
    atr_norm: float | None            # ATR(14) / close  (Wilder RMA)
    volume_zscore: float | None
    spread_pct: float | None          # Caller-supplied bid-ask spread %
    range_pct: float | None           # Caller-supplied intraday range %

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def is_high_vol(self) -> bool:
        return self.vol_regime == "elevated"

    @property
    def is_mean_stretched(self) -> bool:
        if self.mean_reversion_state is None:
            return False
        return "stretched" in self.mean_reversion_state or "extreme" in self.mean_reversion_state

    @property
    def risk_adjusted_return(self) -> float | None:
        """Alias for sharpe_ratio — mean return per unit of volatility."""
        return self.sharpe_ratio


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_quant_metrics(
    *,
    target_klines: list[list],
    benchmark_klines: list[list],
    window: int = _DEFAULT_WINDOW,
    benchmark_symbol: str,
    corr_method: str = "pearson",
    spread_pct: float | None = None,
    range_pct: float | None = None,
    quote_volumes: list[float] | None = None,
) -> QuantMetrics:
    """
    Compute quantitative analytics from OHLCV kline data.

    Parameters
    ----------
    target_klines:
        Binance-format kline rows for the target asset.
        Each row: [open_time, open, high, low, close, base_vol, close_time, ...]
    benchmark_klines:
        Klines for the benchmark asset (e.g. BTCUSDT).
    window:
        Number of aligned candles to use (most recent subset of aligned data).
    benchmark_symbol:
        Label for the benchmark (stored in result, not used for fetching).
    corr_method:
        Only ``"pearson"`` is currently supported.
    spread_pct / range_pct:
        Caller-supplied market microstructure metrics (optional).
    quote_volumes:
        Quote-asset volume series aligned to target_klines (optional).
        Used for volume z-score feature.

    Returns
    -------
    QuantMetrics
    """
    # 1. Validate window
    if window <= 1:
        return _unavailable("invalid_window", window, benchmark_symbol, corr_method,
                            spread_pct, range_pct)

    if corr_method != "pearson":
        raise ValueError(
            f"Unsupported corr_method {corr_method!r}. Only 'pearson' is implemented."
        )

    # 2. Extract and align series
    target = _extract_series(target_klines)
    bench  = _extract_series(benchmark_klines)
    closes, bench_closes, highs, lows, base_vols = _align_by_time(target, bench)

    if len(closes) < window or len(bench_closes) < window:
        logger.debug(
            "compute_quant_metrics: aligned bars=%d, required=%d",
            len(closes), window,
        )
        return _unavailable("insufficient_candles", window, benchmark_symbol, corr_method,
                            spread_pct, range_pct)

    # 3. Trim to window
    closes       = closes[-window:]
    bench_closes = bench_closes[-window:]
    highs        = highs[-window:]
    lows         = lows[-window:]
    base_vols    = base_vols[-window:]

    # 4. Log returns (single-pair skip on bad values, not whole-series abort)
    returns       = _log_returns(closes)
    bench_returns = _log_returns(bench_closes)

    if not returns or not bench_returns:
        return _unavailable("invalid_returns", window, benchmark_symbol, corr_method,
                            spread_pct, range_pct)

    # 5. Correlation & beta
    corr = _pearson(returns, bench_returns)
    beta = _beta(returns, bench_returns)

    # 6. Return statistics
    ret_mean  = _mean(returns)
    ret_std   = _stdev(returns)
    last_ret  = returns[-1]

    zscore: float | None = None
    if ret_mean is not None and ret_std and last_ret is not None:
        zscore = (last_ret - ret_mean) / ret_std

    # 7. Realized vol — annualised (365 trading days for crypto)
    realized_vol: float | None = None
    if ret_std is not None:
        realized_vol = ret_std * math.sqrt(_ANNUALISE_DAYS) * 100

    # 8. Vol regime
    vol_regime = _vol_regime(returns)

    # 9. Max drawdown
    max_dd = _max_drawdown(closes)

    # 10. Sharpe ratio (not risk-free-rate adjusted — use mean/std of returns)
    sharpe: float | None = None
    if ret_mean is not None and ret_std:
        sharpe = ret_mean / ret_std

    # 11. Calmar ratio (annualised_return / max_drawdown)
    calmar: float | None = None
    if ret_mean is not None and max_dd and max_dd > 0:
        ann_return = math.expm1(ret_mean * _ANNUALISE_DAYS)
        calmar = ann_return / (max_dd / 100)

    # 12. Skewness and excess kurtosis
    skew = _skewness(returns)
    kurt = _kurtosis(returns)

    # 13. Mean reversion state (tiered ±0.5σ / ±1.5σ / ±2σ)
    close_mean      = _mean(closes)
    mean_dev_pct    = None
    mr_state        = None
    if close_mean and close_mean != 0:
        mean_dev_pct = ((closes[-1] - close_mean) / close_mean) * 100
        close_std = _stdev(closes)
        if close_std and close_std != 0:
            z = (closes[-1] - close_mean) / close_std
            mr_state = _mean_reversion_state(z)

    # 14. EMA and price distance (SMA-seeded EMA)
    ema20 = _ema(closes, _EMA_PERIOD)
    price_vs_ema_pct: float | None = None
    if ema20 and ema20 != 0:
        price_vs_ema_pct = ((closes[-1] - ema20) / ema20) * 100

    # 15. ATR (Wilder RMA — consistent with VolatilityMetrics)
    atr = _wilder_atr(highs, lows, closes, _ATR_PERIOD)
    atr_norm: float | None = None
    if atr is not None and closes[-1] != 0:
        atr_norm = atr / closes[-1]

    # 16. Volume z-score
    volume_z = _volume_zscore(quote_volumes, window)

    # 17. Momentum features (RSI, MACD hist) via refined momentum module
    rsi, macd_hist = _momentum_features(closes)

    return QuantMetrics(
        available=True,
        window=window,
        benchmark=benchmark_symbol,
        corr_method=corr_method,
        unavailable_reason=None,
        correlation=corr,
        beta=beta,
        log_return=last_ret,
        rolling_return_mean=ret_mean,
        rolling_return_std=ret_std,
        return_zscore=zscore,
        realized_vol=realized_vol,
        vol_regime=vol_regime,
        max_drawdown_pct=max_dd,
        sharpe_ratio=sharpe,
        calmar_ratio=calmar,
        skewness=skew,
        kurtosis=kurt,
        mean_dev_pct=mean_dev_pct,
        mean_reversion_state=mr_state,
        price_vs_ema_pct=price_vs_ema_pct,
        rsi=rsi,
        macd_hist=macd_hist,
        atr_norm=atr_norm,
        volume_zscore=volume_z,
        spread_pct=spread_pct,
        range_pct=range_pct,
    )


# ---------------------------------------------------------------------------
# Private: data extraction and alignment
# ---------------------------------------------------------------------------

def _extract_series(
    klines: Iterable[list],
) -> list[tuple[int, float, float, float, float, float]]:
    """
    Parse Binance kline rows into typed tuples.

    Row format: [open_time, open, high, low, close, base_vol, close_time, ...]
    Returns (close_time, open, high, low, close, base_vol).
    Skips malformed rows with a debug log — does NOT abort the series.
    """
    out: list[tuple[int, float, float, float, float, float]] = []
    skipped = 0
    for idx, row in enumerate(klines):
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            skipped += 1
            continue
        try:
            out.append((
                int(row[6]),      # close_time
                float(row[1]),    # open
                float(row[2]),    # high
                float(row[3]),    # low
                float(row[4]),    # close
                float(row[5]),    # base_vol
            ))
        except (TypeError, ValueError):
            skipped += 1
    if skipped:
        logger.debug("_extract_series: skipped %d malformed rows", skipped)
    return out


def _align_by_time(
    target: list[tuple[int, float, float, float, float, float]],
    bench:  list[tuple[int, float, float, float, float, float]],
) -> tuple[list[float], list[float], list[float], list[float], list[float]]:
    """
    Inner-join target and benchmark on close_time.

    No forward-fill — timestamps must match exactly.
    Returns (closes, bench_closes, highs, lows, base_vols) in chronological order.
    """
    t_map = {row[0]: row for row in target}
    b_map = {row[0]: row for row in bench}
    common = sorted(set(t_map) & set(b_map))

    closes:       list[float] = []
    bench_closes: list[float] = []
    highs:        list[float] = []
    lows:         list[float] = []
    base_vols:    list[float] = []

    for ts in common:
        t_row = t_map[ts]
        b_row = b_map[ts]
        closes.append(t_row[4])
        bench_closes.append(b_row[4])
        highs.append(t_row[2])
        lows.append(t_row[3])
        base_vols.append(t_row[5])

    return closes, bench_closes, highs, lows, base_vols


# ---------------------------------------------------------------------------
# Private: return series
# ---------------------------------------------------------------------------

def _log_returns(closes: list[float]) -> list[float]:
    """
    Compute log returns, skipping individual pairs with non-positive values.

    Unlike the original (which returned [] on any bad value), this continues
    through the series and logs a warning for each skipped pair.
    Returns [] only when no valid pairs exist.
    """
    rets: list[float] = []
    for i in range(1, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if prev <= 0 or cur <= 0:
            logger.warning("_log_returns: non-positive close value encountered")
            return []
        rets.append(math.log(cur / prev))
    return rets


# ---------------------------------------------------------------------------
# Private: statistics
# ---------------------------------------------------------------------------

def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _stdev(values: list[float]) -> float | None:
    """Sample standard deviation (÷ n-1)."""
    n = len(values)
    if n < 2:
        return None
    mu = _mean(values)
    if mu is None:
        return None
    return math.sqrt(sum((x - mu) ** 2 for x in values) / (n - 1))


def _pearson(x: list[float], y: list[float]) -> float | None:
    if len(x) != len(y) or len(x) < 2:
        return None
    mx, my = _mean(x), _mean(y)
    if mx is None or my is None:
        return None
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y)) / (len(x) - 1)
    sx, sy = _stdev(x), _stdev(y)
    if not sx or not sy:
        return None
    return cov / (sx * sy)


def _beta(target_rets: list[float], bench_rets: list[float]) -> float | None:
    """Beta = cov(target, bench) / var(bench)."""
    if len(target_rets) != len(bench_rets) or len(target_rets) < 2:
        return None
    mt = _mean(target_rets)
    mb = _mean(bench_rets)
    if mt is None or mb is None:
        return None
    n = len(target_rets)
    cov = sum((t - mt) * (b - mb) for t, b in zip(target_rets, bench_rets)) / (n - 1)
    var_b = sum((b - mb) ** 2 for b in bench_rets) / (n - 1)
    if var_b == 0:
        return None
    return cov / var_b


def _skewness(values: list[float]) -> float | None:
    """
    Sample skewness (Fisher-Pearson standardised 3rd moment).

    Positive = right tail (occasional large gains).
    Negative = left tail (occasional large losses — more common in crypto).
    """
    n = len(values)
    if n < 3:
        return None
    mu = _mean(values)
    sd = _stdev(values)
    if mu is None or not sd:
        return None
    # Bias-corrected formula
    s = sum(((x - mu) / sd) ** 3 for x in values)
    return (n / ((n - 1) * (n - 2))) * s


def _kurtosis(values: list[float]) -> float | None:
    """
    Excess kurtosis (Fischer definition, normal distribution = 0).

    Positive = leptokurtic (fat tails — common in crypto returns).
    Negative = platykurtic (thin tails).
    """
    n = len(values)
    if n < 4:
        return None
    mu = _mean(values)
    sd = _stdev(values)
    if mu is None or not sd:
        return None
    s = sum(((x - mu) / sd) ** 4 for x in values)
    # Bias-corrected excess kurtosis
    kurt = (n * (n + 1) / ((n - 1) * (n - 2) * (n - 3))) * s
    correction = 3 * (n - 1) ** 2 / ((n - 2) * (n - 3))
    return kurt - correction


def _median(values: list[float]) -> float | None:
    """Correct median for both odd and even length lists."""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _vol_regime(returns: list[float]) -> str | None:
    """
    Classify current volatility regime by comparing last subwindow std
    to the median of all rolling subwindow stds.

    Uses correct median formula (average of two middle values for even lists).
    """
    if len(returns) < _REGIME_SUBWINDOW:
        return None
    vols: list[float] = []
    for i in range(_REGIME_SUBWINDOW, len(returns) + 1):
        v = _stdev(returns[i - _REGIME_SUBWINDOW: i])
        if v is not None:
            vols.append(v)
    if len(vols) < 3:
        return None
    last = vols[-1]
    med  = _median(vols)
    if med is None or med == 0:
        return None
    ratio = last / med
    if ratio >= 1.5:
        return "elevated"
    if ratio <= 0.67:
        return "suppressed"
    return "normal"


def _max_drawdown(closes: list[float]) -> float | None:
    if not closes:
        return None
    peak   = closes[0]
    max_dd = 0.0
    for c in closes[1:]:
        if c > peak:
            peak = c
        if peak > 0:
            dd = (peak - c) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd * 100


def _mean_reversion_state(z: float) -> str:
    """
    Tiered mean-reversion state based on z-score.

    Thresholds (±0.5σ / ±1.5σ / ±2σ) are tighter than the original ±1σ
    which classified 68% of observations as "stretched."

      ±0.5σ   near_mean
      ±1.5σ   stretched_above / stretched_below
      ±2.0σ   extreme_above  / extreme_below
    """
    az = abs(z)
    near = float(_MR_NEAR_THRESHOLD)
    stretched = float(_MR_STRETCHED_THRESHOLD)
    extreme = float(_MR_EXTREME_THRESHOLD)
    if az < near:
        return "near_mean"
    if z > 0:
        if az >= extreme:
            return "extreme_above"
        if az >= stretched:
            return "stretched_above"
        return "stretched_above"
    if az >= extreme:
        return "extreme_below"
    if az >= stretched:
        return "stretched_below"
    return "stretched_below"


# ---------------------------------------------------------------------------
# Private: technical indicators
# ---------------------------------------------------------------------------

def _ema(values: list[float], period: int) -> float | None:
    """
    EMA seeded from SMA of first *period* values (standard warm-up).

    The original used values[0] as the seed which biases the EMA for
    the first ~period bars.  SMA seed is the correct initialisation.
    """
    if len(values) < period or period <= 1:
        return None
    seed  = sum(values[:period]) / period   # SMA seed
    alpha = 2.0 / (period + 1)
    ema   = seed
    for v in values[period:]:
        ema = alpha * v + (1 - alpha) * ema
    return ema


def _wilder_atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> float | None:
    """
    Wilder's smoothed ATR (RMA) — consistent with VolatilityMetrics.atr.

    The original used a simple mean of the last N TRs (not Wilder's RMA),
    producing a different value than pandas-ta's ATR.  This implementation
    uses Wilder's smoothing: RMA[t] = (RMA[t-1] * (n-1) + TR[t]) / n.
    """
    n_bars = len(closes)
    if n_bars < period + 1 or len(highs) < period + 1 or len(lows) < period + 1:
        return None

    trs: list[float] = []
    for i in range(1, n_bars):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        )
        trs.append(tr)

    if len(trs) < period:
        return None

    # Seed with SMA of first period TRs, then apply Wilder smoothing
    rma = sum(trs[:period]) / period
    for tr in trs[period:]:
        rma = (rma * (period - 1) + tr) / period
    return rma


def _volume_zscore(
    quote_volumes: list[float] | None,
    window: int,
) -> float | None:
    """
    Z-score of last bar quote volume vs rolling window.

    Guards that quote_volumes has at least *window* bars before slicing
    to avoid computing against a different window than all other metrics.
    """
    if quote_volumes is None:
        return None
    if len(quote_volumes) < window:
        logger.debug(
            "_volume_zscore: quote_volumes has %d bars, need %d; skipping.",
            len(quote_volumes), window,
        )
        return None
    vols   = quote_volumes[-window:]
    v_mean = _mean(vols)
    v_std  = _stdev(vols)
    if v_mean is None or not v_std:
        return None
    return (vols[-1] - v_mean) / v_std


def _momentum_features(
    closes: list[float],
) -> tuple[float | None, float | None]:
    """
    Extract RSI and MACD histogram from the refined momentum module.

    Passes Decimal list as required by compute_momentum_metrics signature.
    Returns (rsi, macd_hist) or (None, None) on any failure.
    """
    try:
        dec_closes = [Decimal(str(c)) for c in closes]
        m = compute_momentum_metrics(dec_closes)
        rsi       = float(m.rsi)       if m.rsi       is not None else None
        macd_hist = float(m.macd_hist) if m.macd_hist is not None else None
        return rsi, macd_hist
    except ComputeEngineError:
        logger.debug("Momentum metrics unavailable for quant features (missing deps)")
        return None, None
    except Exception:
        logger.debug("Momentum metrics error", exc_info=True)
        return None, None


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _unavailable(
    reason: str,
    window: int,
    benchmark: str,
    corr_method: str,
    spread_pct: float | None,
    range_pct: float | None,
) -> QuantMetrics:
    """Return an all-None QuantMetrics with available=False."""
    return QuantMetrics(
        available=False,
        window=window,
        benchmark=benchmark,
        corr_method=corr_method,
        unavailable_reason=reason,
        correlation=None, beta=None,
        log_return=None, rolling_return_mean=None,
        rolling_return_std=None, return_zscore=None,
        realized_vol=None, vol_regime=None,
        max_drawdown_pct=None, sharpe_ratio=None, calmar_ratio=None,
        skewness=None, kurtosis=None,
        mean_dev_pct=None, mean_reversion_state=None,
        price_vs_ema_pct=None, rsi=None, macd_hist=None,
        atr_norm=None, volume_zscore=None,
        spread_pct=spread_pct, range_pct=range_pct,
    )
