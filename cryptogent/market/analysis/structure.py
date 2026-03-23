"""
cryptogent.market.structure
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Market structure analytics following SMC / ICT methodology:

  Pivot detection     — swing highs / lows with configurable lookback
                        (≥ on lookback side to capture equal highs/lows /
                        liquidity pools)
  Structure trend     — requires 2 consecutive HH+HL or LH+LL to confirm
  BOS                 — Break of Structure: close breaks the *prior* swing
                        level in the direction of trend
  CHoCH               — Change of Character: close breaks the *opposing*
                        swing level, signalling a potential reversal
  Range analysis      — high / low / width% over rolling window
  Range state         — trending / range-bound / transitional (ATR-relative)
  Premium / Discount  — price position within the overall range (SMC zones)
  FVG                 — Fair Value Gap detection (3-candle imbalance)
  Accumulation        — heuristic from price position + buy pressure + duration
  BOS streak          — consecutive BOS count (trend strength proxy)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from .utils import to_decimal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------
_DEFAULT_LOOKBACK: int = 3       # bars each side of pivot
_DEFAULT_RANGE_WINDOW: int = 20  # bars for range high/low
_MIN_STRUCTURE_PIVOTS: int = 2   # consecutive pivot pairs needed to confirm trend
_MIN_ACCUM_DURATION: int = 5     # min bars in range before accumulation fires
_PREMIUM_THRESHOLD   = Decimal("0.618")  # above 61.8% = premium
_DISCOUNT_THRESHOLD  = Decimal("0.382")  # below 38.2% = discount
_ACCUM_ZONE          = Decimal("0.33")   # bottom 33% of range
_DIST_ZONE           = Decimal("0.67")   # top 33% of range (1 - 0.33)

# Public string constants
BULLISH  = "bullish"
BEARISH  = "bearish"
NEUTRAL  = "neutral"
TRENDING     = "trending"
RANGE_BOUND  = "range-bound"
TRANSITIONAL = "transitional"
PREMIUM  = "premium"
DISCOUNT = "discount"
AT_EQUILIBRIUM = "equilibrium"
ACCUMULATION  = "accumulation"
DISTRIBUTION  = "distribution"
UNCLEAR       = "unclear"


# ---------------------------------------------------------------------------
# Supporting types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FairValueGap:
    """
    A 3-candle Fair Value Gap (imbalance).

    Bullish FVG: candle[i-2].high < candle[i].low  (gap above c[i-2])
    Bearish FVG: candle[i-2].low  > candle[i].high (gap below c[i-2])

    The gap zone is [gap_low, gap_high].
    """
    direction: str          # "bullish" | "bearish"
    gap_high: Decimal
    gap_low: Decimal
    bar_index: int          # index of the third candle (candle[i])
    mitigated: bool         # True when price re-enters the gap zone

    @property
    def gap_size(self) -> Decimal:
        return self.gap_high - self.gap_low


# ---------------------------------------------------------------------------
# Public data contract
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StructureMetrics:
    """
    Immutable market structure snapshot.

    All fields are ``None`` when insufficient data was provided or the
    computation could not produce a meaningful result.

    SMC / ICT field guide
    ---------------------
    structure_trend     HH+HL = bullish | LH+LL = bearish | mixed = neutral
                        Requires 2 consecutive confirming pivot pairs.
    bos                 True on the bar price closes beyond the *prior*
                        swing high (bull BOS) or prior swing low (bear BOS).
    bos_direction       Direction of the confirmed BOS.
    bos_streak          Consecutive BOS count in the same direction —
                        higher = stronger trend conviction.
    choch               True when price closes through the *opposing* swing
                        level, signalling a potential structural reversal.
    choch_direction     Direction of the CHoCH (new potential trend direction).
    range_state         trending / range-bound / transitional, calibrated to ATR.
    range_high/low      Rolling window extremes.
    range_width_pct     (high - low) / mid * 100.
    price_zone          premium / discount / equilibrium (SMC concept).
    fvg_list            All unmitigated Fair Value Gaps detected in the window.
    last_fvg            Most recent FVG (convenience shortcut).
    accumulation        accumulation / distribution / unclear — only fires
                        when range-bound for >= _MIN_ACCUM_DURATION bars.
    last_swing_high     Most recent confirmed pivot high.
    last_swing_low      Most recent confirmed pivot low.
    prev_swing_high     Second-to-last pivot high (used for BOS reference).
    prev_swing_low      Second-to-last pivot low  (used for BOS reference).
    swing_high_history  All detected pivot highs [(bar_index, price), ...].
    swing_low_history   All detected pivot lows  [(bar_index, price), ...].
    """

    # ---- Trend -------------------------------------------------------------
    structure_trend: str | None

    # ---- BOS ---------------------------------------------------------------
    bos: bool | None
    bos_direction: str | None
    bos_streak: int | None          # consecutive BOS events in same direction

    # ---- CHoCH -------------------------------------------------------------
    choch: bool | None
    choch_direction: str | None

    # ---- Range -------------------------------------------------------------
    range_state: str | None
    range_high: Decimal | None
    range_low: Decimal | None
    range_width_pct: Decimal | None

    # ---- SMC zones ---------------------------------------------------------
    price_zone: str | None          # "premium" | "discount" | "equilibrium"

    # ---- Fair Value Gaps ---------------------------------------------------
    fvg_list: tuple[FairValueGap, ...]  # all FVGs in window (oldest → newest)
    last_fvg: FairValueGap | None       # most recent FVG

    # ---- Accumulation / Distribution ---------------------------------------
    accumulation: str | None

    # ---- Swing levels ------------------------------------------------------
    last_swing_high: Decimal | None
    last_swing_low: Decimal | None
    prev_swing_high: Decimal | None
    prev_swing_low: Decimal | None
    swing_high_history: tuple[tuple[int, Decimal], ...]
    swing_low_history: tuple[tuple[int, Decimal], ...]

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        return (
            self.structure_trend is None
            and self.last_swing_high is None
            and self.last_swing_low is None
        )

    @property
    def is_trending(self) -> bool:
        return self.range_state == TRENDING

    @property
    def is_range_bound(self) -> bool:
        return self.range_state == RANGE_BOUND

    @property
    def has_fvg(self) -> bool:
        return len(self.fvg_list) > 0

    @property
    def in_premium(self) -> bool | None:
        """Price is in the upper half of the range — SMC sell zone."""
        if self.price_zone is None:
            return None
        return self.price_zone == PREMIUM

    @property
    def in_discount(self) -> bool | None:
        """Price is in the lower half of the range — SMC buy zone."""
        if self.price_zone is None:
            return None
        return self.price_zone == DISCOUNT

    @property
    def bullish_confluence(self) -> bool:
        """
        Strong bullish setup: bullish structure + discount zone + no CHoCH.

        A classic SMC long entry condition.
        """
        return (
            self.structure_trend == BULLISH
            and self.price_zone == DISCOUNT
            and self.choch is False
        )

    @property
    def bearish_confluence(self) -> bool:
        """Strong bearish setup: bearish structure + premium zone + no CHoCH."""
        return (
            self.structure_trend == BEARISH
            and self.price_zone == PREMIUM
            and self.choch is False
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_structure_metrics(
    *,
    highs: Sequence[Decimal],
    lows: Sequence[Decimal],
    closes: Sequence[Decimal],
    atr_pct: Decimal | None = None,
    volume_trend: str | None = None,
    buy_pressure: str | None = None,
    lookback: int = _DEFAULT_LOOKBACK,
    range_window: int = _DEFAULT_RANGE_WINDOW,
) -> StructureMetrics:
    """
    Compute SMC / ICT market structure metrics.

    Parameters
    ----------
    highs, lows, closes:
        OHLC bar data (oldest → newest).  Minimum useful length is
        ``2 * lookback + 1`` for pivot detection; more bars give better
        structure analysis.
    atr_pct:
        ATR as % of close (from VolatilityMetrics.atr_pct).  Used to
        calibrate range_state thresholds.  Without it, range_state returns
        ``None`` rather than using an unreliable hardcoded threshold.
    volume_trend:
        From VolumeMetrics.quote_trend — used in accumulation heuristic.
    buy_pressure:
        From VolumeMetrics.buy_pressure — used in accumulation heuristic.
    lookback:
        Number of bars each side of a candle required to confirm a pivot.
        Higher = fewer but higher-quality pivots.
    range_window:
        Number of bars for range high/low computation.

    Returns
    -------
    StructureMetrics
    """
    # 1. Validate inputs
    if not highs or not lows or not closes:
        return _empty_metrics()

    n = min(len(highs), len(lows), len(closes))
    if n < (2 * lookback + 1):
        logger.warning(
            "compute_structure_metrics: insufficient bars (%d) for lookback=%d",
            n,
            lookback,
        )
        return _empty_metrics()
    if n != len(highs) or n != len(lows) or n != len(closes):
        logger.warning(
            "compute_structure_metrics: input lengths differ "
            "(highs=%d lows=%d closes=%d); truncating to %d.",
            len(highs), len(lows), len(closes), n,
        )

    h: list[Decimal] = [_to_dec(v, "highs")  for v in highs[:n]]
    l: list[Decimal] = [_to_dec(v, "lows")   for v in lows[:n]]
    c: list[Decimal] = [_to_dec(v, "closes") for v in closes[:n]]
    last_close = c[-1]

    # 2. Pivot detection
    #    ≥ on the lookback (left) side captures equal highs/lows (liquidity pools)
    #    > on the forward (right) side avoids false early confirmation
    pivot_highs = _pivot_highs(h, lookback)
    pivot_lows  = _pivot_lows(l, lookback)

    # 3. Structure trend — requires 2 consecutive confirming pivot pairs
    structure_trend = _compute_structure_trend(pivot_highs, pivot_lows)

    # 4. Extract swing reference levels
    last_sh  = pivot_highs[-1][1] if len(pivot_highs) >= 1 else None
    prev_sh  = pivot_highs[-2][1] if len(pivot_highs) >= 2 else None
    last_sl  = pivot_lows[-1][1]  if len(pivot_lows)  >= 1 else None
    prev_sl  = pivot_lows[-2][1]  if len(pivot_lows)  >= 2 else None

    # 5. BOS and CHoCH
    #    BOS  — price closes through the *prior* swing level (trend continuation)
    #    CHoCH — price closes through the *opposing* last swing level (reversal signal)
    bos, bos_dir, choch, choch_dir = _compute_bos_choch(
        last_close, structure_trend, last_sh, last_sl, prev_sh, prev_sl
    )

    # 6. BOS streak (consecutive BOS in same direction)
    bos_streak = _compute_bos_streak(pivot_highs, pivot_lows, structure_trend)

    # 7. Range analysis
    range_high, range_low, range_width_pct = _compute_range(h, l, range_window)

    # 8. Range state (requires ATR for meaningful calibration)
    range_state = _compute_range_state(range_width_pct, atr_pct)

    # 9. Price zone (SMC premium / discount)
    price_zone = _compute_price_zone(last_close, range_high, range_low)

    # 10. Fair Value Gaps
    fvg_all = _detect_fvgs(h, l, range_window)
    fvg_list = [f for f in fvg_all if not f.mitigated]
    last_fvg = fvg_list[-1] if fvg_list else None

    # 11. Accumulation / distribution
    accumulation = _compute_accumulation(
        last_close, range_state, range_high, range_low,
        buy_pressure, volume_trend, n, range_window,
    )

    return StructureMetrics(
        structure_trend=structure_trend,
        bos=bos,
        bos_direction=bos_dir,
        bos_streak=bos_streak,
        choch=choch,
        choch_direction=choch_dir,
        range_state=range_state,
        range_high=range_high,
        range_low=range_low,
        range_width_pct=range_width_pct,
        price_zone=price_zone,
        fvg_list=tuple(fvg_list),
        last_fvg=last_fvg,
        accumulation=accumulation,
        last_swing_high=last_sh,
        last_swing_low=last_sl,
        prev_swing_high=prev_sh,
        prev_swing_low=prev_sl,
        swing_high_history=tuple(pivot_highs),
        swing_low_history=tuple(pivot_lows),
    )


# ---------------------------------------------------------------------------
# Private: pivot detection
# ---------------------------------------------------------------------------

def _pivot_highs(
    highs: list[Decimal],
    lookback: int,
) -> list[tuple[int, Decimal]]:
    """
    Detect pivot highs.

    Left side uses >=  (captures equal highs / liquidity pools).
    Right side uses >  (requires a strict lower high after the pivot).
    """
    pivots: list[tuple[int, Decimal]] = []
    n = len(highs)
    for i in range(lookback, n - lookback):
        h = highs[i]
        left_ok  = all(h >= highs[j] for j in range(i - lookback, i))
        right_ok = all(h >  highs[j] for j in range(i + 1, i + 1 + lookback))
        if left_ok and right_ok:
            pivots.append((i, h))
    return pivots


def _pivot_lows(
    lows: list[Decimal],
    lookback: int,
) -> list[tuple[int, Decimal]]:
    """
    Detect pivot lows.

    Left side uses <=  (captures equal lows / liquidity pools).
    Right side uses <  (requires a strict higher low after the pivot).
    """
    pivots: list[tuple[int, Decimal]] = []
    n = len(lows)
    for i in range(lookback, n - lookback):
        lo = lows[i]
        left_ok  = all(lo <= lows[j] for j in range(i - lookback, i))
        right_ok = all(lo <  lows[j] for j in range(i + 1, i + 1 + lookback))
        if left_ok and right_ok:
            pivots.append((i, lo))
    return pivots


# ---------------------------------------------------------------------------
# Private: structure trend
# ---------------------------------------------------------------------------

def _compute_structure_trend(
    pivot_highs: list[tuple[int, Decimal]],
    pivot_lows: list[tuple[int, Decimal]],
) -> str | None:
    """
    Determine structural trend from swing pivot sequence.

    Requires at least 2 consecutive HH + HL for bullish confirmation,
    or 2 consecutive LH + LL for bearish confirmation.
    A single pivot pair is insufficient — markets produce false swings.

    Logic:
      Bullish: last_sh > prev_sh (HH) AND last_sl > prev_sl (HL)
               AND prev_sh > prev_prev_sh AND prev_sl > prev_prev_sl
               (two consecutive confirming pairs)
      Bearish: inverse
      Neutral: mixed signals
    """
    if len(pivot_highs) < 3 or len(pivot_lows) < 3:
        # Not enough consecutive pivots to confirm trend by standard rules.
        return None

    # Two-pair confirmation (higher confidence)
    sh = pivot_highs
    sl = pivot_lows

    pair1_bull = sh[-1][1] > sh[-2][1] and sl[-1][1] > sl[-2][1]  # HH + HL (latest)
    pair2_bull = sh[-2][1] > sh[-3][1] and sl[-2][1] > sl[-3][1]  # HH + HL (prior)

    pair1_bear = sh[-1][1] < sh[-2][1] and sl[-1][1] < sl[-2][1]  # LH + LL (latest)
    pair2_bear = sh[-2][1] < sh[-3][1] and sl[-2][1] < sl[-3][1]  # LH + LL (prior)

    if pair1_bull and pair2_bull:
        return BULLISH
    if pair1_bear and pair2_bear:
        return BEARISH
    return NEUTRAL


# ---------------------------------------------------------------------------
# Private: BOS / CHoCH
# ---------------------------------------------------------------------------

def _compute_bos_choch(
    last_close: Decimal,
    structure_trend: str | None,
    last_sh: Decimal | None,
    last_sl: Decimal | None,
    prev_sh: Decimal | None,
    prev_sl: Decimal | None,
) -> tuple[bool, str | None, bool, str | None]:
    """
    Compute BOS and CHoCH signals.

    BOS (Break of Structure) — trend continuation:
      Bullish structure: close > prev_swing_high  (breaks prior high = new HH confirmed)
      Bearish structure: close < prev_swing_low   (breaks prior low  = new LL confirmed)

    CHoCH (Change of Character) — reversal signal:
      Bullish structure: close < last_swing_low   (breaks the last HL = structure threatened)
      Bearish structure: close > last_swing_high  (breaks the last LH = structure threatened)

    Note: BOS uses *prev* swing level (the one before current), not the latest.
    CHoCH uses the *last* swing level on the opposing side.
    """
    bos     = False
    bos_dir: str | None = None
    choch     = False
    choch_dir: str | None = None

    if structure_trend == BULLISH:
        # BOS: close breaks prior swing high (confirms new HH)
        if prev_sh is not None and last_close > prev_sh:
            bos     = True
            bos_dir = BULLISH
        # CHoCH: close breaks below the last swing low (HL violated)
        if last_sl is not None and last_close < last_sl:
            choch     = True
            choch_dir = BEARISH   # CHoCH direction = new potential trend

    elif structure_trend == BEARISH:
        # BOS: close breaks prior swing low (confirms new LL)
        if prev_sl is not None and last_close < prev_sl:
            bos     = True
            bos_dir = BEARISH
        # CHoCH: close breaks above the last swing high (LH violated)
        if last_sh is not None and last_close > last_sh:
            choch     = True
            choch_dir = BULLISH

    else:
        # Neutral / unknown structure — neither BOS nor CHoCH meaningful
        pass

    return bos, bos_dir, choch, choch_dir


def _compute_bos_streak(
    pivot_highs: list[tuple[int, Decimal]],
    pivot_lows: list[tuple[int, Decimal]],
    structure_trend: str | None,
) -> int | None:
    """
    Count consecutive BOS events in the current trend direction.

    Iterates backward through pivot pairs — each time a prior swing level
    is broken in the trend direction, the streak increments.  Stops on the
    first failure.  A streak ≥ 3 indicates strong trending conviction.
    """
    if structure_trend is None:
        return None
    if structure_trend == BULLISH and len(pivot_highs) < 2:
        return None
    if structure_trend == BEARISH and len(pivot_lows) < 2:
        return None

    streak = 0
    try:
        if structure_trend == BULLISH:
            # Each consecutive pair: did last high break prior high?
            for i in range(len(pivot_highs) - 1, 0, -1):
                if pivot_highs[i][1] > pivot_highs[i - 1][1]:
                    streak += 1
                else:
                    break
        else:  # BEARISH
            for i in range(len(pivot_lows) - 1, 0, -1):
                if pivot_lows[i][1] < pivot_lows[i - 1][1]:
                    streak += 1
                else:
                    break
    except Exception as exc:  # noqa: BLE001
        logger.warning("BOS streak computation failed: %s", exc)
        return None

    return streak


# ---------------------------------------------------------------------------
# Private: range analysis
# ---------------------------------------------------------------------------

def _compute_range(
    highs: list[Decimal],
    lows: list[Decimal],
    window: int,
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    if len(highs) < window:
        return None, None, None
    rh = max(highs[-window:])
    rl = min(lows[-window:])
    mid = (rh + rl) / Decimal("2")
    width_pct = (rh - rl) / mid * Decimal("100") if mid != 0 else None
    return rh, rl, width_pct


def _compute_range_state(
    range_width_pct: Decimal | None,
    atr_pct: Decimal | None,
) -> str | None:
    """
    Classify range state relative to ATR.

    Without ATR, returns ``None`` — a hardcoded % threshold is meaningless
    across different assets and timeframes.

    With ATR:
      range_width ≤ 2× ATR  → range-bound  (tight, low-momentum)
      range_width ≥ 3× ATR  → trending     (strong directional move)
      between               → transitional
    """
    if range_width_pct is None:
        return None
    if atr_pct is None:
        # Cannot classify without ATR context
        return None
    lower = max(Decimal("2"), atr_pct * Decimal("2"))
    upper = atr_pct * Decimal("3")
    if range_width_pct <= lower:
        return RANGE_BOUND
    if range_width_pct >= upper:
        return TRENDING
    return TRANSITIONAL


def _compute_price_zone(
    last_close: Decimal,
    range_high: Decimal | None,
    range_low: Decimal | None,
) -> str | None:
    """
    SMC premium / discount / equilibrium classification.

    Premium  — price above 50% of range (overvalued vs range mean, sell zone)
    Discount — price below 50% of range (undervalued vs range mean, buy zone)
    Equilibrium — price at 50% (fair value)
    """
    if range_high is None or range_low is None:
        return None
    band = range_high - range_low
    if band == 0:
        return AT_EQUILIBRIUM
    pos = (last_close - range_low) / band
    if pos > _PREMIUM_THRESHOLD:
        return PREMIUM
    if pos < _DISCOUNT_THRESHOLD:
        return DISCOUNT
    return AT_EQUILIBRIUM


# ---------------------------------------------------------------------------
# Private: Fair Value Gap detection
# ---------------------------------------------------------------------------

def _detect_fvgs(
    highs: list[Decimal],
    lows: list[Decimal],
    window: int,
) -> list[FairValueGap]:
    """
    Detect Fair Value Gaps (imbalances) in the last *window* bars.

    Bullish FVG: lows[i] > highs[i-2]  — gap between candle i-2 high and candle i low
    Bearish FVG: highs[i] < lows[i-2]  — gap between candle i-2 low and candle i high

    The FVG represents an area where price moved so fast that no orders
    were filled — price tends to return to fill these gaps (mitigation).
    Only the most recent *window* bars are scanned.
    """
    fvgs: list[FairValueGap] = []
    start = max(2, len(highs) - window)
    for i in range(start, len(highs)):
        # Bullish FVG
        if lows[i] > highs[i - 2]:
            gap_high = lows[i]
            gap_low = highs[i - 2]
            mitigated = _is_fvg_mitigated(highs, lows, gap_low, gap_high, i)
            fvgs.append(
                FairValueGap(
                    direction=BULLISH,
                    gap_high=gap_high,
                    gap_low=gap_low,
                    bar_index=i,
                    mitigated=mitigated,
                )
            )
        # Bearish FVG
        elif highs[i] < lows[i - 2]:
            gap_high = lows[i - 2]
            gap_low = highs[i]
            mitigated = _is_fvg_mitigated(highs, lows, gap_low, gap_high, i)
            fvgs.append(
                FairValueGap(
                    direction=BEARISH,
                    gap_high=gap_high,
                    gap_low=gap_low,
                    bar_index=i,
                    mitigated=mitigated,
                )
            )
    return fvgs


def _is_fvg_mitigated(
    highs: list[Decimal],
    lows: list[Decimal],
    gap_low: Decimal,
    gap_high: Decimal,
    start_index: int,
) -> bool:
    """
    A FVG is mitigated when price trades back into the gap zone.
    """
    for i in range(start_index + 1, len(highs)):
        if lows[i] <= gap_high and highs[i] >= gap_low:
            return True
    return False


# ---------------------------------------------------------------------------
# Private: accumulation / distribution
# ---------------------------------------------------------------------------

def _compute_accumulation(
    last_close: Decimal,
    range_state: str | None,
    range_high: Decimal | None,
    range_low: Decimal | None,
    buy_pressure: str | None,
    volume_trend: str | None,
    n: int,
    range_window: int,
) -> str | None:
    """
    Heuristic accumulation / distribution classification.

    Conditions for accumulation (Wyckoff-inspired):
      1. Range-bound for at least _MIN_ACCUM_DURATION bars
      2. Price in the lower third of range (discount zone)
      3. Buy pressure present AND/OR volume trend rising

    Conditions for distribution:
      1. Range-bound for at least _MIN_ACCUM_DURATION bars
      2. Price in the upper third of range (premium zone)
      3. Sell pressure present AND/OR volume trend rising

    Returns None when range_state is not range-bound (accumulation /
    distribution only meaningful in consolidation phases).
    """
    if range_state != RANGE_BOUND:
        return None
    if range_high is None or range_low is None:
        return None
    # Require the asset to have been in range for a minimum duration
    if n < _MIN_ACCUM_DURATION:
        return None

    band = range_high - range_low
    if band == 0:
        return None

    pos = (last_close - range_low) / band

    in_accum_zone = pos <= _ACCUM_ZONE
    in_dist_zone  = pos >= _DIST_ZONE

    vol_rising = volume_trend == "up"
    has_buy    = buy_pressure == "buy"
    has_sell   = buy_pressure == "sell"

    if in_accum_zone and (has_buy or vol_rising):
        return ACCUMULATION
    if in_dist_zone and (has_sell or vol_rising):
        return DISTRIBUTION
    return UNCLEAR


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _to_dec(value: object, field: str) -> Decimal:
    result = to_decimal(value)
    if result is None:
        raise ValueError(
            f"compute_structure_metrics: non-numeric value in '{field}': {value!r}"
        )
    return result


def _empty_metrics() -> StructureMetrics:
    return StructureMetrics(
        structure_trend=None,
        bos=None,
        bos_direction=None,
        bos_streak=None,
        choch=None,
        choch_direction=None,
        range_state=None,
        range_high=None,
        range_low=None,
        range_width_pct=None,
        price_zone=None,
        fvg_list=(),
        last_fvg=None,
        accumulation=None,
        last_swing_high=None,
        last_swing_low=None,
        prev_swing_high=None,
        prev_swing_low=None,
        swing_high_history=(),
        swing_low_history=(),
    )
