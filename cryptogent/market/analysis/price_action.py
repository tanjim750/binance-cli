"""
cryptogent.market.analysis.price_action
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Deterministic, non-biased price-action module using OHLC only.

Includes:
  - Support / Resistance (swing clustering with proper centroid)
  - Trend structure (HH/HL, LH/LL with min-distance filter)
  - Breakout / Breakdown (close beyond level + confirmation)
  - Candlestick patterns (pure geometry, window scan)

Design rules (enforced):
  - No strict equality — always tolerance-based comparisons
  - No lookahead — only closed candles used
  - Zero-range candles skipped before any pattern check
  - Detection and scoring are strictly separated
  - All thresholds are named constants — never inline magic numbers
  - Patterns scanned over a configurable window, not just the last bar
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Sequence

from .utils import to_decimal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Named constants — all thresholds in one place
# ---------------------------------------------------------------------------

# Swing detection
_DEFAULT_SWING_LOOKBACK: int = 3      # bars each side — 1 is too noisy

# Clustering
_DEFAULT_TOLERANCE       = Decimal("0.002")   # 0.2% — configurable per asset
_DEFAULT_MIN_SWING_PCT   = Decimal("0.001")   # 0.1% min distance between swings

# Breakout — separate from clustering tolerance
_BREAKOUT_BUFFER         = Decimal("0.001")   # 0.1% beyond level to confirm break
_BREAKOUT_BODY_RATIO     = Decimal("0.6")     # body/range threshold for "confirmed"
_VOL_AVG_WINDOW: int     = 20                 # bars for volume average (match volume module)

# Pattern geometry thresholds
_DOJI_RATIO              = Decimal("0.10")    # body/range < 10% → doji
_WICK_MULT               = Decimal("2.0")     # wick ≥ 2× body for hammer/star
_WICK_MAX                = Decimal("0.3")     # opposite wick ≤ 30% of body
_HAMMER_BODY_POSITION    = Decimal("0.67")    # body_bot must be above 67% of range
_STAR_BODY_POSITION      = Decimal("0.30")    # body_top must be below 30% of range
_MARUBOZU_BODY_MIN       = Decimal("0.90")    # body/range ≥ 90% for marubozu
_MARUBOZU_WICK_MAX       = Decimal("0.02")    # each wick < 2% of range
_STRONG_BODY_RATIO       = Decimal("0.50")    # body/range ≥ 50% → "strong" candle
_SMALL_BODY_RATIO        = Decimal("0.30")    # body/range ≤ 30% → "small" (star midcandle)
_MORNING_STAR_CONFIRM    = Decimal("0.50")    # c3 close > 50% into c1 body

# Context scoring bounds
_CTX_MIN                 = Decimal("0.5")
_CTX_MAX                 = Decimal("1.5")
_NEAR_LEVEL_PCT          = Decimal("0.005")   # 0.5% — "near a S/R level"

# Pattern base reliability scores
_BASE_DOJI               = Decimal("0.8")
_BASE_HAMMER             = Decimal("1.0")
_BASE_MARUBOZU           = Decimal("1.0")
_BASE_ENGULFING          = Decimal("1.2")
_BASE_HARAMI             = Decimal("0.9")
_BASE_TWEEZER            = Decimal("1.0")
_BASE_MORNING_STAR       = Decimal("1.5")

# Structure
_DEFAULT_SWING_WINDOW: int = 6       # last N swing points for structure classification
_MIN_PATTERN_WINDOW: int   = 10      # bars to scan for patterns


# ---------------------------------------------------------------------------
# Public data contracts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CandlePattern:
    pattern_name: str
    direction: str              # "bullish" | "bearish" | "neutral"
    candle_count: int           # 1 | 2 | 3
    bar_index: int              # index within the input series
    bars_ago: int               # how many bars before the last closed bar
    reliability: Decimal | None
    context_valid: bool
    strength_score: Decimal | None


@dataclass(frozen=True)
class SupportResistanceLevel:
    level: Decimal
    touches: int
    level_high: Decimal         # upper edge of zone (level + tolerance band)
    level_low: Decimal          # lower edge of zone
    distance_pct: Decimal | None


@dataclass(frozen=True)
class PriceActionMetrics:
    # ---- Support / Resistance ----------------------------------------------
    support: SupportResistanceLevel | None
    resistance: SupportResistanceLevel | None

    # Convenience flat fields (for backward compat and LLM prompt serialisation)
    support_level: Decimal | None
    support_strength: int | None
    support_distance_pct: Decimal | None
    resistance_level: Decimal | None
    resistance_strength: int | None
    resistance_distance_pct: Decimal | None

    # ---- Trend structure ---------------------------------------------------
    structure_type: str | None          # "bullish" | "bearish" | "neutral"
    last_swing_high: Decimal | None
    last_swing_low: Decimal | None
    swing_high_history: tuple[tuple[int, Decimal], ...]
    swing_low_history: tuple[tuple[int, Decimal], ...]

    # ---- Breakout / Breakdown ----------------------------------------------
    breakout: bool
    breakdown: bool
    breakout_level: Decimal | None
    breakdown_level: Decimal | None
    breakout_strength: str | None       # "weak" | "confirmed"

    # ---- Candlestick patterns ----------------------------------------------
    patterns: tuple[CandlePattern, ...]
    last_pattern: str | None
    dominant_bias: str | None
    signal_count: int
    confluence: bool                    # ≥2 directional patterns agree

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def is_bullish_structure(self) -> bool:
        return self.structure_type == "bullish"

    @property
    def is_bearish_structure(self) -> bool:
        return self.structure_type == "bearish"

    @property
    def has_breakout(self) -> bool:
        return self.breakout

    @property
    def has_breakdown(self) -> bool:
        return self.breakdown

    @property
    def confirmed_breakout(self) -> bool:
        return self.breakout and self.breakout_strength == "confirmed"

    @property
    def confirmed_breakdown(self) -> bool:
        return self.breakdown and self.breakout_strength == "confirmed"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_price_action_metrics(
    *,
    opens:   Sequence[object],
    highs:   Sequence[object],
    lows:    Sequence[object],
    closes:  Sequence[object],
    volumes: Sequence[object] | None = None,
    atr:     Decimal | None = None,         # from VolatilityMetrics — used for min-swing filter
    tolerance:      Decimal = _DEFAULT_TOLERANCE,
    min_swing_pct:  Decimal = _DEFAULT_MIN_SWING_PCT,
    swing_lookback: int     = _DEFAULT_SWING_LOOKBACK,
    swing_window:   int     = _DEFAULT_SWING_WINDOW,
    pattern_window: int     = _MIN_PATTERN_WINDOW,
) -> PriceActionMetrics:
    """
    Compute price-action metrics from OHLC data.

    Parameters
    ----------
    opens, highs, lows, closes:
        Candlestick data oldest → newest.  Minimum 5 bars required.
    volumes:
        Optional — used only for breakout confirmation and pattern scoring.
    atr:
        From VolatilityMetrics.  When provided, ``min_swing_pct`` is
        overridden with ``ATR / current_price`` so the noise filter is
        calibrated to actual market volatility rather than a fixed %.
    tolerance:
        Clustering and breakout-buffer tolerance as a fraction (default 0.2%).
    min_swing_pct:
        Minimum % distance between two swing points (filters noise).
    swing_lookback:
        Bars each side of a candle to confirm a pivot (default 3).
        Lower = more sensitive, higher = fewer but cleaner pivots.
    swing_window:
        Number of recent swing points used for structure classification.
    pattern_window:
        Number of recent bars to scan for candlestick patterns.
    """
    o = _parse(opens)
    h = _parse(highs)
    l = _parse(lows)
    c = _parse(closes)
    v = _parse(volumes) if volumes is not None else None

    n = min(len(o), len(h), len(l), len(c))
    if n < 5:
        return _empty_metrics()

    o, h, l, c = o[-n:], h[-n:], l[-n:], c[-n:]
    v = v[-n:] if v is not None else None

    current   = c[-1]
    mid_price = current

    # Override min_swing_pct with ATR-relative filter when ATR is available
    if atr is not None and current > 0:
        atr_pct      = atr / current          # ATR as fraction of price
        min_swing_pct = max(min_swing_pct, atr_pct * Decimal("0.5"))

    # ------------------------------------------------------------------
    # 1. Swing detection
    # ------------------------------------------------------------------
    swing_highs, swing_lows = _detect_swings(h, l, tolerance, min_swing_pct, swing_lookback)

    # ------------------------------------------------------------------
    # 2. S/R clustering — proper centroid (not running average)
    # ------------------------------------------------------------------
    clusters_hi = _cluster_levels([lvl for _, lvl in swing_highs], mid_price, tolerance)
    clusters_lo = _cluster_levels([lvl for _, lvl in swing_lows],  mid_price, tolerance)

    sup  = _nearest_below(clusters_lo, current, tolerance)
    res  = _nearest_above(clusters_hi, current, tolerance)

    # ------------------------------------------------------------------
    # 3. Trend structure
    # ------------------------------------------------------------------
    structure_type, last_sh, last_sl = _classify_structure(
        swing_highs, swing_lows, swing_window, min_swing_pct
    )

    # ------------------------------------------------------------------
    # 4. Breakout / Breakdown
    # ------------------------------------------------------------------
    breakout, breakdown, bo_strength = _check_breakout(
        c=c[-1], o=o[-1], h=h[-1], l=l[-1],
        support=sup.level if sup else None,
        resistance=res.level if res else None,
        volumes=v,
    )

    # ------------------------------------------------------------------
    # 5. Candlestick pattern scan (window, not just last bar)
    # ------------------------------------------------------------------
    patterns = _scan_patterns(
        o, h, l, c, v,
        tolerance=tolerance,
        structure_type=structure_type,
        support=sup.level if sup else None,
        resistance=res.level if res else None,
        window=pattern_window,
    )

    last_pattern  = patterns[-1].pattern_name if patterns else None
    signal_count  = len(patterns)
    dominant_bias = _dominant_bias(patterns)
    confluence    = _has_confluence(patterns)

    return PriceActionMetrics(
        support=sup,
        resistance=res,
        support_level=sup.level if sup else None,
        support_strength=sup.touches if sup else None,
        support_distance_pct=sup.distance_pct if sup else None,
        resistance_level=res.level if res else None,
        resistance_strength=res.touches if res else None,
        resistance_distance_pct=res.distance_pct if res else None,
        structure_type=structure_type,
        last_swing_high=last_sh,
        last_swing_low=last_sl,
        swing_high_history=tuple(swing_highs),
        swing_low_history=tuple(swing_lows),
        breakout=breakout,
        breakdown=breakdown,
        breakout_level=res.level if breakout and res else None,
        breakdown_level=sup.level if breakdown and sup else None,
        breakout_strength=bo_strength,
        patterns=tuple(patterns),
        last_pattern=last_pattern,
        dominant_bias=dominant_bias,
        signal_count=signal_count,
        confluence=confluence,
    )


# ---------------------------------------------------------------------------
# Private: swing detection
# ---------------------------------------------------------------------------

def _detect_swings(
    highs: list[Decimal],
    lows:  list[Decimal],
    tol:   Decimal,
    min_swing_pct: Decimal,
    lookback: int,
) -> tuple[list[tuple[int, Decimal]], list[tuple[int, Decimal]]]:
    """
    Detect swing highs and lows with configurable lookback.

    Left side uses >= (captures equal highs — liquidity pools).
    Right side uses >  (requires strict reversal after pivot).
    Min-swing filter: adjacent swings must differ by >= min_swing_pct
    measured against the midpoint of the two levels.
    """
    swing_highs: list[tuple[int, Decimal]] = []
    swing_lows:  list[tuple[int, Decimal]] = []
    n = min(len(highs), len(lows))
    if n < 2 * lookback + 1:
        return swing_highs, swing_lows

    last_sh: Decimal | None = None
    last_sl: Decimal | None = None

    for i in range(lookback, n - lookback):
        hi = highs[i]
        lo = lows[i]

        # Swing high: >= on left, > on right
        is_sh = (
            all(hi >= highs[j] for j in range(i - lookback, i))
            and all(hi > highs[j] for j in range(i + 1, i + 1 + lookback))
        )
        if is_sh:
            # Min-distance filter using midpoint as base (symmetric)
            if last_sh is None or _mid_distance_pct(last_sh, hi) >= min_swing_pct * Decimal("100"):
                swing_highs.append((i, hi))
                last_sh = hi

        # Swing low: <= on left, < on right
        is_sl = (
            all(lo <= lows[j] for j in range(i - lookback, i))
            and all(lo < lows[j] for j in range(i + 1, i + 1 + lookback))
        )
        if is_sl:
            if last_sl is None or _mid_distance_pct(last_sl, lo) >= min_swing_pct * Decimal("100"):
                swing_lows.append((i, lo))
                last_sl = lo

    return swing_highs, swing_lows


# ---------------------------------------------------------------------------
# Private: S/R clustering
# ---------------------------------------------------------------------------

def _cluster_levels(
    levels: list[Decimal],
    mid:    Decimal,
    tol:    Decimal,
) -> list[dict[str, Any]]:
    """
    Group swing levels into clusters using midpoint-relative tolerance.

    Centroid is computed as the true mean of all members after assignment
    (not a running average — running averages give early members disproportionate
    influence on the cluster centre).
    """
    # Each cluster: {"members": [Decimal, ...], "touches": int}
    raw: list[dict[str, Any]] = []

    for lvl in levels:
        placed = False
        for cl in raw:
            cl_centre = sum(cl["members"]) / Decimal(len(cl["members"]))
            ref = (lvl + cl_centre) / Decimal("2") if (lvl + cl_centre) != 0 else Decimal("1")
            if ref != 0 and abs(lvl - cl_centre) / ref <= tol:
                cl["members"].append(lvl)
                placed = True
                break
        if not placed:
            raw.append({"members": [lvl], "touches": 0})

    # Convert to final format with true centroid
    clusters: list[dict[str, Any]] = []
    for cl in raw:
        members  = cl["members"]
        centroid = sum(members) / Decimal(len(members))
        clusters.append({"level": centroid, "touches": len(members)})

    return clusters


def _nearest_below(
    clusters: list[dict[str, Any]],
    price: Decimal,
    tol: Decimal,
) -> SupportResistanceLevel | None:
    below = [c for c in clusters if c["level"] < price]
    if not below:
        return None
    best   = max(below, key=lambda c: c["level"])
    level  = best["level"]
    band   = level * tol
    return SupportResistanceLevel(
        level=level,
        touches=int(best["touches"]),
        level_high=level + band,
        level_low=level - band,
        distance_pct=_price_distance_pct(price, level),
    )


def _nearest_above(
    clusters: list[dict[str, Any]],
    price: Decimal,
    tol: Decimal,
) -> SupportResistanceLevel | None:
    above = [c for c in clusters if c["level"] > price]
    if not above:
        return None
    best  = min(above, key=lambda c: c["level"])
    level = best["level"]
    band  = level * tol
    return SupportResistanceLevel(
        level=level,
        touches=int(best["touches"]),
        level_high=level + band,
        level_low=level - band,
        distance_pct=_price_distance_pct(price, level),
    )


# ---------------------------------------------------------------------------
# Private: trend structure
# ---------------------------------------------------------------------------

def _classify_structure(
    swing_highs: list[tuple[int, Decimal]],
    swing_lows:  list[tuple[int, Decimal]],
    window: int,
    min_swing_pct: Decimal,
) -> tuple[str | None, Decimal | None, Decimal | None]:
    """
    Classify structure using last N swing points.

    Requires at least 2 swing points per side.
    Comparison uses the most recent confirmed swing as reference.
    Min-distance filter applied to both directions for symmetry.
    """
    sh = swing_highs[-window:] if swing_highs else []
    sl = swing_lows[-window:]  if swing_lows  else []

    last_sh = sh[-1][1] if sh else None
    last_sl = sl[-1][1] if sl else None

    if len(sh) < 2 or len(sl) < 2:
        return None, last_sh, last_sl

    prev_sh = sh[-2][1]
    prev_sl = sl[-2][1]
    min_pct = min_swing_pct * Decimal("100")

    hh = last_sh > prev_sh and _mid_distance_pct(prev_sh, last_sh) >= min_pct
    hl = last_sl > prev_sl and _mid_distance_pct(prev_sl, last_sl) >= min_pct
    lh = last_sh < prev_sh and _mid_distance_pct(prev_sh, last_sh) >= min_pct
    ll = last_sl < prev_sl and _mid_distance_pct(prev_sl, last_sl) >= min_pct

    if hh and hl:
        return "bullish", last_sh, last_sl
    if lh and ll:
        return "bearish", last_sh, last_sl
    return "neutral", last_sh, last_sl


# ---------------------------------------------------------------------------
# Private: breakout / breakdown
# ---------------------------------------------------------------------------

def _check_breakout(
    *,
    c: Decimal,
    o: Decimal,
    h: Decimal,
    l: Decimal,
    support:    Decimal | None,
    resistance: Decimal | None,
    volumes:    list[Decimal] | None,
) -> tuple[bool, bool, str | None]:
    """
    Detect breakout / breakdown on the last closed candle.

    Breakout : close > resistance + BREAKOUT_BUFFER
    Breakdown: close < support    - BREAKOUT_BUFFER

    Confirmation:
      body_ratio >= 0.6 AND volume > 20-bar average (excl. current bar)
    """
    breakout  = False
    breakdown = False
    strength: str | None = None

    rng        = h - l
    body       = abs(c - o)
    body_ratio = (body / rng) if rng > 0 else Decimal("0")

    # Volume confirmation — exclude current bar from average
    vol_confirm = False
    if volumes is not None and len(volumes) > _VOL_AVG_WINDOW:
        avg = sum(volumes[-_VOL_AVG_WINDOW - 1:-1]) / Decimal(_VOL_AVG_WINDOW)
        vol_confirm = volumes[-1] > avg

    if resistance is not None:
        if c > resistance * (Decimal("1") + _BREAKOUT_BUFFER):
            breakout = True

    if support is not None:
        if c < support * (Decimal("1") - _BREAKOUT_BUFFER):
            breakdown = True

    if breakout or breakdown:
        body_ok = body_ratio >= _BREAKOUT_BODY_RATIO
        vol_ok  = vol_confirm or volumes is None
        strength = "confirmed" if (body_ok and vol_ok) else "weak"

    return breakout, breakdown, strength


# ---------------------------------------------------------------------------
# Private: candlestick pattern scanning
# ---------------------------------------------------------------------------

def _scan_patterns(
    o: list[Decimal],
    h: list[Decimal],
    l: list[Decimal],
    c: list[Decimal],
    v: list[Decimal] | None,
    tolerance: Decimal,
    structure_type: str | None,
    support: Decimal | None,
    resistance: Decimal | None,
    window: int,
) -> list[CandlePattern]:
    """
    Scan the last *window* bars for candlestick patterns.

    Returns all detected patterns ordered oldest → newest.
    Each pattern carries ``bars_ago`` so callers can filter by recency.
    """
    n       = len(c)
    results: list[CandlePattern] = []
    start   = max(0, n - window)

    for i in range(start, n):
        bars_ago = n - 1 - i

        # Skip zero-range candles — geometry undefined
        rng_i = h[i] - l[i]
        if rng_i <= 0:
            continue

        cd   = _candle(o[i], h[i], l[i], c[i])
        ctx  = _context_score(
            direction="bullish",   # will re-call per pattern
            bar_idx=i,
            c=c, v=v,
            support=support,
            resistance=resistance,
            structure_type=structure_type,
        )

        def add(
            name: str,
            direction: str,
            count: int,
            base: Decimal,
        ) -> None:
            ctx_val = _context_score(
                direction=direction,
                bar_idx=i,
                c=c, v=v,
                support=support,
                resistance=resistance,
                structure_type=structure_type,
            )
            score = (base * ctx_val).min(_CTX_MAX)
            results.append(CandlePattern(
                pattern_name=name,
                direction=direction,
                candle_count=count,
                bar_index=i,
                bars_ago=bars_ago,
                reliability=score,
                context_valid=ctx_val >= Decimal("1.0"),
                strength_score=score,
            ))

        # ---- Single-candle patterns ----------------------------------------

        # Doji
        if cd["body"] / rng_i < _DOJI_RATIO:
            add("Doji", "neutral", 1, _BASE_DOJI)

        # Hammer / Hanging Man shape
        if (cd["lower"] >= cd["body"] * _WICK_MULT
                and cd["upper"] <= cd["body"] * _WICK_MAX
                and (cd["body_bot"] - l[i]) / rng_i >= _HAMMER_BODY_POSITION):
            if structure_type == "bearish":
                add("Hammer", "bullish", 1, _BASE_HAMMER)
            elif structure_type == "bullish":
                add("Hanging Man", "bearish", 1, _BASE_HAMMER)
            else:
                add("Hammer Shape", "neutral", 1, _BASE_HAMMER * Decimal("0.8"))

        # Inverted Hammer / Shooting Star shape
        if (cd["upper"] >= cd["body"] * _WICK_MULT
                and cd["lower"] <= cd["body"] * _WICK_MAX
                and (cd["body_bot"] - l[i]) / rng_i <= _STAR_BODY_POSITION):
            if structure_type == "bearish":
                add("Inverted Hammer", "bullish", 1, _BASE_HAMMER)
            elif structure_type == "bullish":
                add("Shooting Star", "bearish", 1, _BASE_HAMMER)
            else:
                add("Inverted Hammer Shape", "neutral", 1, _BASE_HAMMER * Decimal("0.8"))

        # Marubozu — body ≥ 90% of range (wick < 2% each side)
        if (cd["body"] / rng_i >= _MARUBOZU_BODY_MIN
                and cd["upper"] <= rng_i * _MARUBOZU_WICK_MAX
                and cd["lower"] <= rng_i * _MARUBOZU_WICK_MAX):
            direction = "bullish" if c[i] > o[i] else "bearish"
            add("Marubozu", direction, 1, _BASE_MARUBOZU)

        # ---- Two-candle patterns -------------------------------------------
        if i < 1:
            continue
        rng_p = h[i - 1] - l[i - 1]
        if rng_p <= 0:
            continue
        cp = _candle(o[i - 1], h[i - 1], l[i - 1], c[i - 1])

        prev_bear = c[i - 1] < o[i - 1]
        prev_bull = c[i - 1] > o[i - 1]
        curr_bull = c[i]     > o[i]
        curr_bear = c[i]     < o[i]

        # Bullish Engulfing
        if (prev_bear and curr_bull
                and o[i] < c[i - 1]
                and c[i] > o[i - 1]
                and cd["body"] > cp["body"]):
            add("Bullish Engulfing", "bullish", 2, _BASE_ENGULFING)

        # Bearish Engulfing
        if (prev_bull and curr_bear
                and o[i] > c[i - 1]
                and c[i] < o[i - 1]
                and cd["body"] > cp["body"]):
            add("Bearish Engulfing", "bearish", 2, _BASE_ENGULFING)

        # Harami — current candle's body inside prior candle's body
        if cd["body_top"] <= cp["body_top"] and cd["body_bot"] >= cp["body_bot"]:
            # Direction from the current (inner) candle
            if curr_bull and prev_bear:
                add("Bullish Harami", "bullish", 2, _BASE_HARAMI)
            elif curr_bear and prev_bull:
                add("Bearish Harami", "bearish", 2, _BASE_HARAMI)
            else:
                add("Harami", "neutral", 2, _BASE_HARAMI)

        # Tweezer Top — equal highs + first bullish, second bearish
        if prev_bull and curr_bear:
            ref_h = (h[i] + h[i - 1]) / Decimal("2")
            if ref_h > 0 and abs(h[i] - h[i - 1]) / ref_h <= tolerance:
                add("Tweezer Top", "bearish", 2, _BASE_TWEEZER)

        # Tweezer Bottom — equal lows + first bearish, second bullish
        if prev_bear and curr_bull:
            ref_l = (l[i] + l[i - 1]) / Decimal("2")
            if ref_l > 0 and abs(l[i] - l[i - 1]) / ref_l <= tolerance:
                add("Tweezer Bottom", "bullish", 2, _BASE_TWEEZER)

        # ---- Three-candle patterns -----------------------------------------
        if i < 2:
            continue
        rng_pp = h[i - 2] - l[i - 2]
        if rng_pp <= 0:
            continue
        cpp = _candle(o[i - 2], h[i - 2], l[i - 2], c[i - 2])

        # Morning Star
        c1_bear   = c[i - 2] < o[i - 2]
        c1_strong = cpp["body"] / rng_pp >= _STRONG_BODY_RATIO
        c2_small  = cp["body"]  / rng_p  <= _SMALL_BODY_RATIO
        c3_bull   = c[i] > o[i]
        c1_mid    = (o[i - 2] + c[i - 2]) / Decimal("2")
        c3_confirm = c[i] > c1_mid

        if c1_bear and c1_strong and c2_small and c3_bull and c3_confirm:
            add("Morning Star", "bullish", 3, _BASE_MORNING_STAR)

        # Evening Star
        c1_bull_s  = c[i - 2] > o[i - 2]
        c1_strong2 = cpp["body"] / rng_pp >= _STRONG_BODY_RATIO
        c3_bear    = c[i] < o[i]
        c1_mid2    = (o[i - 2] + c[i - 2]) / Decimal("2")
        c3_confirm2 = c[i] < c1_mid2

        if c1_bull_s and c1_strong2 and c2_small and c3_bear and c3_confirm2:
            add("Evening Star", "bearish", 3, _BASE_MORNING_STAR)

    return results


# ---------------------------------------------------------------------------
# Private: pattern scoring and aggregation
# ---------------------------------------------------------------------------

def _candle(o: Decimal, h: Decimal, l: Decimal, c: Decimal) -> dict[str, Decimal]:
    body     = abs(c - o)
    body_top = max(c, o)
    body_bot = min(c, o)
    return {
        "body":     body,
        "body_top": body_top,
        "body_bot": body_bot,
        "upper":    h - body_top,
        "lower":    body_bot - l,
    }


def _context_score(
    *,
    direction: str,
    bar_idx: int,
    c: list[Decimal],
    v: list[Decimal] | None,
    support: Decimal | None,
    resistance: Decimal | None,
    structure_type: str | None,
) -> Decimal:
    """
    Context multiplier for pattern reliability (0.5–1.5).

    Base = 1.0, bonuses added for:
      +0.2  pattern near relevant S/R level
      +0.2  pattern aligns with structure trend
      +0.1  volume spike on pattern bar
    """
    score    = Decimal("1.0")
    price    = c[bar_idx]

    # S/R proximity
    if direction == "bullish" and support is not None:
        if abs(price - support) / price <= _NEAR_LEVEL_PCT:
            score += Decimal("0.2")
    if direction == "bearish" and resistance is not None:
        if abs(price - resistance) / price <= _NEAR_LEVEL_PCT:
            score += Decimal("0.2")

    # Structure alignment
    if structure_type == "bullish" and direction == "bullish":
        score += Decimal("0.2")
    if structure_type == "bearish" and direction == "bearish":
        score += Decimal("0.2")

    # Volume confirmation (exclude current bar from average)
    if v is not None and bar_idx > _VOL_AVG_WINDOW:
        avg = sum(v[bar_idx - _VOL_AVG_WINDOW: bar_idx]) / Decimal(_VOL_AVG_WINDOW)
        if avg > 0 and v[bar_idx] > avg:
            score += Decimal("0.1")

    return max(_CTX_MIN, min(_CTX_MAX, score))


def _dominant_bias(patterns: list[CandlePattern]) -> str | None:
    """
    Dominant bias = direction of highest-scoring pattern.
    Neutral patterns do not count toward directional bias.
    """
    directional = [p for p in patterns if p.direction in ("bullish", "bearish")]
    if not directional:
        return None
    best = max(directional, key=lambda p: (p.reliability or Decimal("0"), p.bar_index))
    return best.direction


def _has_confluence(patterns: list[CandlePattern]) -> bool:
    """
    True when ≥2 bullish OR ≥2 bearish patterns are detected.
    Neutral patterns excluded — two dojis do not constitute confluence.
    """
    bull = sum(1 for p in patterns if p.direction == "bullish")
    bear = sum(1 for p in patterns if p.direction == "bearish")
    return bull >= 2 or bear >= 2


# ---------------------------------------------------------------------------
# Private: geometry utilities
# ---------------------------------------------------------------------------

def _mid_distance_pct(a: Decimal, b: Decimal) -> Decimal:
    """
    Percentage distance between two levels using their midpoint as base.
    Symmetric — gives the same result regardless of argument order.
    """
    mid = (a + b) / Decimal("2")
    if mid == 0:
        return Decimal("0")
    return abs(a - b) / mid * Decimal("100")


def _price_distance_pct(price: Decimal, level: Decimal) -> Decimal | None:
    """Distance from current price to a level, as % of current price."""
    if price == 0:
        return None
    return abs(price - level) / price * Decimal("100")


def _parse(values: Sequence[object] | None) -> list[Decimal]:
    if values is None:
        return []
    out: list[Decimal] = []
    for v in values:
        d = to_decimal(v)
        if d is not None:
            out.append(d)
    return out


# ---------------------------------------------------------------------------
# Private: empty result
# ---------------------------------------------------------------------------

def _empty_metrics() -> PriceActionMetrics:
    return PriceActionMetrics(
        support=None, resistance=None,
        support_level=None, support_strength=None, support_distance_pct=None,
        resistance_level=None, resistance_strength=None, resistance_distance_pct=None,
        structure_type=None,
        last_swing_high=None, last_swing_low=None,
        swing_high_history=(), swing_low_history=(),
        breakout=False, breakdown=False,
        breakout_level=None, breakdown_level=None, breakout_strength=None,
        patterns=(),
        last_pattern=None, dominant_bias=None,
        signal_count=0, confluence=False,
    )