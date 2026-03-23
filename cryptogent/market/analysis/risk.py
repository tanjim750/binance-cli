"""
cryptogent.market.analysis.risk
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Risk module:
  - Stop-loss suggestion (ATR / Swing / Chandelier)
  - Take-profit targets (R:R multiples + structure context)
  - Position sizing (fixed fractional + 3 caps + slippage adjustment)
  - Leverage suggestion + liquidation proximity warning
  - Risk score (1–10) with per-component breakdown

Vol regime strings (from VolatilityMetrics):
  "low" | "normal" | "high" | "extreme"

Structure trend strings (from StructureMetrics):
  "bullish" | "bearish" | "neutral" | None

Trend bias strings (from TrendMetrics):
  "strong_bull" | "bull" | "neutral" | "bear" | "strong_bear" | None
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ATR multiplier by vol regime
#   Uses VolatilityMetrics.vol_regime strings: "low"|"normal"|"high"|"extreme"
# ---------------------------------------------------------------------------
_ATR_MULT: dict[str | None, Decimal] = {
    "low":     Decimal("1.5"),
    "normal":  Decimal("2.0"),
    "high":    Decimal("3.0"),
    "extreme": Decimal("3.5"),
    None:      Decimal("2.0"),
}

# Leverage hard caps by vol regime
_LEVERAGE_CAP: dict[str | None, int] = {
    "low":     7,
    "normal":  5,
    "high":    3,
    "extreme": 2,
    None:      5,
}

# Risk score vol regime component scores
_VOL_SCORE: dict[str | None, Decimal] = {
    "low":     Decimal("7"),
    "normal":  Decimal("10"),
    "high":    Decimal("3"),
    "extreme": Decimal("1"),
    None:      Decimal("5"),
}


# ---------------------------------------------------------------------------
# Public data contract
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskMetrics:
    """
    Immutable risk snapshot for a single trade setup.

    ``viable=False`` means the setup should not be traded.
    ``rejection_reason`` explains the primary blocker.
    All Decimal fields are ``None`` when not computable.
    """

    viable: bool
    rejection_reason: str | None
    risk_score: Decimal | None
    risk_score_breakdown: dict[str, Decimal] | None

    entry_price: Decimal | None
    side: str | None
    effective_entry: Decimal | None

    stop_price: Decimal | None
    stop_method: str | None
    stop_distance_pct: Decimal | None
    stop_atr_multiple: Decimal | None
    stop_candidates: dict[str, dict[str, Any]] | None

    tp1: Decimal | None
    tp2: Decimal | None
    tp3: Decimal | None
    tp_fvg: Decimal | None
    tp_structure: Decimal | None
    tp_cloud: Decimal | None
    reward_risk_ratio: Decimal | None

    position_size_base: Decimal | None
    position_size_quote: Decimal | None
    position_size_pct: Decimal | None
    max_loss_quote: Decimal | None
    risk_pct_used: Decimal | None
    caps_applied: list[str] | None

    suggested_leverage: int | None
    liquidation_price: Decimal | None
    liquidation_distance_pct: Decimal | None

    wide_stop: bool
    concentration_cap: bool
    liquidity_cap: bool
    liquidation_warning: bool
    low_adx_warning: bool

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def is_high_quality(self) -> bool:
        """True when risk_score >= 7 and viable."""
        return self.viable and self.risk_score is not None and self.risk_score >= Decimal("7")

    @property
    def has_structure_tp(self) -> bool:
        return self.tp_structure is not None

    @property
    def has_fvg_tp(self) -> bool:
        return self.tp_fvg is not None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_risk_metrics(
    *,
    # Required
    entry_price: Decimal,
    side: str,
    account_balance: Decimal,
    risk_pct: Decimal = Decimal("1"),
    max_position_pct: Decimal = Decimal("20"),

    # From VolatilityMetrics
    atr: Decimal | None = None,
    vol_regime: str | None = None,
    chandelier_long: Decimal | None = None,
    chandelier_short: Decimal | None = None,

    # From StructureMetrics
    structure_trend: str | None = None,     # "bullish"|"bearish"|"neutral"|None
    last_swing_low: Decimal | None = None,
    last_swing_high: Decimal | None = None,
    prev_swing_low: Decimal | None = None,
    prev_swing_high: Decimal | None = None,
    price_zone: str | None = None,
    bos_streak: int | None = None,
    choch: bool | None = None,
    last_fvg_direction: str | None = None,
    last_fvg_low: Decimal | None = None,
    last_fvg_high: Decimal | None = None,
    ichi_senkou_a: Decimal | None = None,
    ichi_senkou_b: Decimal | None = None,

    # From TrendMetrics
    trend_bias: str | None = None,          # "strong_bull"|"bull"|etc — score only
    adx: Decimal | None = None,
    adx_trend_strength: str | None = None,
    ema_50_200_crossover: str | None = None,

    # From MomentumMetrics
    composite_signal: str | None = None,
    rsi_zone: str | None = None,
    macd_bias: str | None = None,

    # From ExecutionMetrics
    slippage_pct: Decimal | None = None,
    spread_pct: Decimal | None = None,
    notional_available: Decimal | None = None,
    fill_ratio_pct: Decimal | None = None,

    # From VolumeMetrics
    buy_pressure: str | None = None,
    sustained_buy_pressure: bool | None = None,
    vol_price_confirmation: str | None = None,

    # Last candle OHLC (for stop_inside_candle guard)
    last_candle_low: Decimal | None = None,
    last_candle_high: Decimal | None = None,
) -> RiskMetrics:
    """
    Compute risk metrics for a trade setup.

    All module inputs are optional — the function degrades gracefully
    when upstream modules are unavailable.

    Parameters
    ----------
    entry_price:
        Intended entry price (mid or limit price).
    side:
        ``"long"`` or ``"short"``.
    account_balance:
        Total account equity in quote currency.
    risk_pct:
        Maximum % of account to risk per trade (default 1%).
    max_position_pct:
        Maximum position size as % of account (default 20%).
    structure_trend:
        From ``StructureMetrics.structure_trend`` — used for swing stop
        validation.  NOT ``trend_bias`` from TrendMetrics.
    """
    # ------------------------------------------------------------------
    # 1. Basic validation
    # ------------------------------------------------------------------
    if entry_price <= 0:
        return _unavailable("invalid_entry")
    if account_balance <= 0 or risk_pct <= 0:
        return _unavailable("invalid_account_balance")
    side_norm = side.lower().strip()
    if side_norm not in ("long", "short"):
        return _unavailable("invalid_side")

    # ------------------------------------------------------------------
    # 2. Stop loss candidates
    # ------------------------------------------------------------------
    stop_candidates: dict[str, dict[str, Any]] = {}

    # Method A — ATR-based
    if atr is not None and atr > 0:
        mult = _ATR_MULT.get(vol_regime, Decimal("2.0"))
        raw_stop = (
            entry_price - (mult * atr)
            if side_norm == "long"
            else entry_price + (mult * atr)
        )
        stop_candidates["atr"] = _candidate_dict(
            stop=raw_stop, entry=entry_price, atr=atr,
            method="atr", side=side_norm,
            last_low=last_candle_low, last_high=last_candle_high,
        )
    else:
        stop_candidates["atr"] = {"valid": False, "reason": "missing_atr"}

    # Method B — Swing level
    # Requires: structure_trend confirmed, choch=False, swing within 4×ATR
    # Uses structure_trend (StructureMetrics) NOT trend_bias (TrendMetrics)
    swing_confirmed = (
        atr is not None
        and atr > 0
        and choch is False
        and _structure_confirmed(structure_trend, side_norm)
    )
    if swing_confirmed:
        swing_level = (
            last_swing_low  if side_norm == "long"  else last_swing_high
        )
        if swing_level is not None:
            buffer  = Decimal("0.2") * atr
            raw_stop = (
                swing_level - buffer if side_norm == "long"
                else swing_level + buffer
            )
            dist = abs(entry_price - raw_stop)
            if dist <= Decimal("4") * atr:
                stop_candidates["swing"] = _candidate_dict(
                    stop=raw_stop, entry=entry_price, atr=atr,
                    method="swing", side=side_norm,
                    last_low=last_candle_low, last_high=last_candle_high,
                )
            else:
                stop_candidates["swing"] = {
                    "valid": False, "reason": "swing_beyond_4atr"
                }
        else:
            stop_candidates["swing"] = {
                "valid": False, "reason": "missing_swing_level"
            }
    else:
        stop_candidates["swing"] = {
            "valid": False, "reason": "structure_not_confirmed"
        }

    # Method C — Chandelier Exit
    if atr is not None and atr > 0:
        chandelier = chandelier_long if side_norm == "long" else chandelier_short
        if chandelier is not None:
            stop_candidates["chandelier"] = _candidate_dict(
                stop=chandelier, entry=entry_price, atr=atr,
                method="chandelier", side=side_norm,
                last_low=last_candle_low, last_high=last_candle_high,
            )
        else:
            stop_candidates["chandelier"] = {
                "valid": False, "reason": "missing_chandelier"
            }
    else:
        stop_candidates["chandelier"] = {"valid": False, "reason": "missing_atr"}

    # ------------------------------------------------------------------
    # 3. Select best stop
    #    Long  → highest valid stop (closest to entry, most capital efficient)
    #    Short → lowest valid stop
    #    Must be outside current candle and >= 0.5% from entry
    # ------------------------------------------------------------------
    valid_candidates = [
        (name, data) for name, data in stop_candidates.items()
        if data.get("valid") and data.get("stop_price") is not None
    ]
    valid_candidates.sort(
        key=lambda x: x[1]["stop_price"],
        reverse=(side_norm == "long"),
    )

    stop_price: Decimal | None = None
    stop_method: str | None = None
    stop_distance_pct: Decimal | None = None
    stop_atr_multiple: Decimal | None = None
    stop_too_tight = False

    for name, data in valid_candidates:
        dist_pct = data.get("stop_distance_pct")
        if dist_pct is None:
            continue
        if dist_pct < Decimal("0.5"):
            stop_too_tight = True
            logger.debug("Stop candidate %s too tight (%.3f%%) — skipping", name, dist_pct)
            continue
        stop_price       = data["stop_price"]
        stop_method      = name
        stop_distance_pct = dist_pct
        stop_atr_multiple = data.get("stop_atr_multiple")
        break

    if stop_price is None or stop_distance_pct is None:
        reason = "stop_too_tight" if stop_too_tight else "no_valid_stop"
        return _unavailable(reason, stop_candidates=stop_candidates)

    wide_stop = (
        stop_distance_pct > Decimal("5")
        and vol_regime not in ("high", "extreme")
    )

    # ------------------------------------------------------------------
    # 4. Take profit levels
    # ------------------------------------------------------------------
    stop_distance = abs(entry_price - stop_price)
    sign = Decimal("1") if side_norm == "long" else Decimal("-1")
    tp1 = entry_price + sign * stop_distance
    tp2 = entry_price + sign * stop_distance * Decimal("2")
    tp3 = entry_price + sign * stop_distance * Decimal("3")

    reward_risk_ratio = _reward_risk(entry_price, stop_price, tp2, side_norm)

    tp_fvg       = _fvg_tp(side_norm, last_fvg_direction, last_fvg_low, last_fvg_high, entry_price)
    tp_structure = _structure_tp(side_norm, entry_price, prev_swing_high, prev_swing_low)
    tp_cloud     = _cloud_tp(side_norm, entry_price, ichi_senkou_a, ichi_senkou_b)

    # ------------------------------------------------------------------
    # 5. Position sizing — fixed fractional with 3 ordered caps
    # ------------------------------------------------------------------
    caps: list[str] = []

    max_risk_quote      = (account_balance * risk_pct) / Decimal("100")
    stop_distance_quote = (entry_price * stop_distance_pct) / Decimal("100")

    if stop_distance_quote <= 0:
        return _unavailable("invalid_stop_distance", stop_candidates=stop_candidates)

    pos_base  = max_risk_quote / stop_distance_quote
    pos_quote = pos_base * entry_price
    pos_pct   = (pos_quote / account_balance) * Decimal("100")

    # Cap 1 — account concentration
    concentration_cap = False
    if pos_pct > max_position_pct:
        scale     = max_position_pct / pos_pct
        pos_base  *= scale
        pos_quote *= scale
        pos_pct    = max_position_pct
        concentration_cap = True
        caps.append("concentration_cap_applied")

    # Cap 2 — liquidity (order should not exceed 50% of visible depth)
    liquidity_cap = False
    if notional_available is not None and notional_available > 0:
        max_liq = notional_available * Decimal("0.5")
        if pos_quote > max_liq:
            scale     = max_liq / pos_quote
            pos_base  *= scale
            pos_quote *= scale
            pos_pct    = (pos_quote / account_balance) * Decimal("100")
            liquidity_cap = True
            caps.append("liquidity_cap_applied")

    # Cap 3 — slippage adjustment
    # Recalculate stop distance from effective entry to account for fill cost
    effective_entry = _effective_entry(entry_price, slippage_pct, side_norm)
    eff_stop_dist   = abs(effective_entry - stop_price)

    if eff_stop_dist > 0:
        size_after_slip = max_risk_quote / eff_stop_dist
        if size_after_slip < pos_base:
            pos_base  = size_after_slip
            pos_quote = pos_base * effective_entry
            pos_pct   = (pos_quote / account_balance) * Decimal("100")
            caps.append("slippage_adjustment_applied")

    # Wide stop proportional scale — preserve max_loss by reducing size
    if wide_stop and stop_distance_pct > 0:
        scale = Decimal("5") / stop_distance_pct
        if scale < Decimal("1"):
            pos_base  *= scale
            pos_quote *= scale
            pos_pct    = (pos_quote / account_balance) * Decimal("100")
            caps.append("wide_stop_scale_applied")

    # Recalculate max_loss from final position size
    max_loss_quote = pos_base * eff_stop_dist
    risk_pct_used  = (max_loss_quote / account_balance) * Decimal("100")

    # ------------------------------------------------------------------
    # 6. Leverage
    # ------------------------------------------------------------------
    suggested_leverage   = _calc_leverage(stop_distance_pct, vol_regime)
    liquidation_price    = _liquidation_price(entry_price, suggested_leverage, side_norm)
    liquidation_dist_pct = _distance_pct(entry_price, liquidation_price) if liquidation_price else None

    liquidation_warning = False
    if (
        atr is not None
        and liquidation_price is not None
        and abs(entry_price - liquidation_price) <= Decimal("2") * atr
    ):
        liquidation_warning  = True
        suggested_leverage   = max(1, suggested_leverage - 1)
        liquidation_price    = _liquidation_price(entry_price, suggested_leverage, side_norm)
        liquidation_dist_pct = _distance_pct(entry_price, liquidation_price) if liquidation_price else None
        logger.warning(
            "Liquidation price within 2×ATR of entry — "
            "leverage reduced to %d×", suggested_leverage,
        )

    # ------------------------------------------------------------------
    # 7. Risk score
    # ------------------------------------------------------------------
    risk_score, breakdown = _risk_score(
        side=side_norm,
        rr=reward_risk_ratio,
        structure_trend=structure_trend,
        price_zone=price_zone,
        adx=adx,
        vol_regime=vol_regime,
        bos_streak=bos_streak,
        composite_signal=composite_signal,
    )

    low_adx_warning = adx is not None and adx < Decimal("20")

    # ------------------------------------------------------------------
    # 8. Viability gates — first rejection wins
    # ------------------------------------------------------------------
    viable    = True
    rejection: str | None = None

    def _reject(reason: str) -> None:
        nonlocal viable, rejection
        if viable:          # first reason wins
            viable    = False
            rejection = reason

    if choch is True:
        _reject("structure_invalidated")
    if stop_too_tight:
        _reject("invalid_stop")
    if reward_risk_ratio is not None and reward_risk_ratio < Decimal("1.5"):
        _reject("insufficient_reward_risk")
    if risk_score is not None and risk_score < Decimal("4.0"):
        _reject("low_risk_score")

    return RiskMetrics(
        viable=viable,
        rejection_reason=rejection,
        risk_score=risk_score,
        risk_score_breakdown=breakdown,
        entry_price=entry_price,
        side=side_norm,
        effective_entry=effective_entry,
        stop_price=stop_price,
        stop_method=stop_method,
        stop_distance_pct=stop_distance_pct,
        stop_atr_multiple=stop_atr_multiple,
        stop_candidates=stop_candidates,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        tp_fvg=tp_fvg,
        tp_structure=tp_structure,
        tp_cloud=tp_cloud,
        reward_risk_ratio=reward_risk_ratio,
        position_size_base=pos_base,
        position_size_quote=pos_quote,
        position_size_pct=pos_pct,
        max_loss_quote=max_loss_quote,
        risk_pct_used=risk_pct_used,
        caps_applied=caps,
        suggested_leverage=suggested_leverage,
        liquidation_price=liquidation_price,
        liquidation_distance_pct=liquidation_dist_pct,
        wide_stop=wide_stop,
        concentration_cap=concentration_cap,
        liquidity_cap=liquidity_cap,
        liquidation_warning=liquidation_warning,
        low_adx_warning=low_adx_warning,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _candidate_dict(
    *,
    stop: Decimal,
    entry: Decimal,
    atr: Decimal,
    method: str,
    side: str,
    last_low: Decimal | None,
    last_high: Decimal | None,
) -> dict[str, Any]:
    """
    Build a stop candidate dict with validity check.

    stop_inside_candle: stop is strictly inside the candle body
      Long:  stop > last_candle_low  (stop is above the candle low — inside)
      Short: stop < last_candle_high (stop is below the candle high — inside)

    The candle low/high itself is a valid stop level — only reject when
    strictly inside (would be triggered by normal price action).
    """
    valid  = True
    reason = None

    if side == "long" and stop >= entry:
        valid, reason = False, "stop_not_below_entry"
    elif side == "short" and stop <= entry:
        valid, reason = False, "stop_not_above_entry"
    elif last_low is not None and side == "long" and stop > last_low:
        # Stop is inside the candle body — would be triggered by current bar
        valid, reason = False, "stop_inside_candle"
    elif last_high is not None and side == "short" and stop < last_high:
        valid, reason = False, "stop_inside_candle"

    dist_pct = _distance_pct(entry, stop)
    atr_mult = (abs(entry - stop) / atr) if atr > 0 else None

    return {
        "method":            method,
        "stop_price":        stop,
        "stop_distance_pct": dist_pct,
        "stop_atr_multiple": atr_mult,
        "valid":             valid,
        "reason":            reason,
    }


def _distance_pct(a: Decimal, b: Decimal) -> Decimal:
    if a == 0:
        return Decimal("0")
    return (abs(a - b) / a) * Decimal("100")


def _structure_confirmed(structure_trend: str | None, side: str) -> bool:
    """
    Validate structure_trend (from StructureMetrics, "bullish"|"bearish"|"neutral")
    against trade direction.

    NOTE: Do NOT pass trend_bias (TrendMetrics) here — different value space.
    """
    if structure_trend is None:
        return False
    if side == "long":
        return structure_trend == "bullish"
    return structure_trend == "bearish"


def _reward_risk(
    entry: Decimal,
    stop: Decimal,
    tp2: Decimal,
    side: str,
) -> Decimal | None:
    risk   = entry - stop  if side == "long" else stop - entry
    reward = tp2 - entry   if side == "long" else entry - tp2
    if risk <= 0:
        return None
    return reward / risk


def _fvg_tp(
    side: str,
    direction: str | None,
    gap_low: Decimal | None,
    gap_high: Decimal | None,
    entry: Decimal,
) -> Decimal | None:
    """
    FVG midpoint as a TP target — only when the gap is in the trade direction
    AND its midpoint is beyond entry (i.e. it's a target, not a support).

    Bullish FVG for longs: gap_low = highs[i-2], gap_high = lows[i]
    The midpoint should be above entry to be a valid TP.
    """
    if direction is None or gap_low is None or gap_high is None:
        return None
    if side == "long" and direction != "bullish":
        return None
    if side == "short" and direction != "bearish":
        return None
    mid = (gap_low + gap_high) / Decimal("2")
    # TP must be beyond entry in the trade direction
    if side == "long" and mid <= entry:
        return None
    if side == "short" and mid >= entry:
        return None
    return mid


def _structure_tp(
    side: str,
    entry: Decimal,
    prev_high: Decimal | None,
    prev_low: Decimal | None,
) -> Decimal | None:
    if side == "long" and prev_high is not None and prev_high > entry:
        return prev_high
    if side == "short" and prev_low is not None and prev_low < entry:
        return prev_low
    return None


def _cloud_tp(
    side: str,
    entry: Decimal,
    senkou_a: Decimal | None,
    senkou_b: Decimal | None,
) -> Decimal | None:
    """Return the nearest Senkou span beyond entry in the trade direction."""
    vals = [v for v in (senkou_a, senkou_b) if v is not None]
    if not vals:
        return None
    if side == "long":
        above = [v for v in vals if v > entry]
        return min(above) if above else None
    below = [v for v in vals if v < entry]
    return max(below) if below else None


def _effective_entry(
    entry: Decimal,
    slippage_pct: Decimal | None,
    side: str,
) -> Decimal:
    """Slippage-adjusted entry price (worsens the entry for sizing conservatism)."""
    if slippage_pct is None:
        return entry
    if side == "long":
        return entry * (Decimal("1") + slippage_pct)
    return entry * (Decimal("1") - slippage_pct)


def _calc_leverage(stop_distance_pct: Decimal, vol_regime: str | None) -> int:
    """
    Suggest leverage based on stop distance and vol regime.

    Natural max = floor(100 / stop_distance_pct)
    Hard cap from _LEVERAGE_CAP by vol regime.
    Result clamped to [1, hard_cap].
    """
    if stop_distance_pct <= 0:
        return 1
    natural  = int((Decimal("100") / stop_distance_pct).to_integral_value(rounding=ROUND_FLOOR))
    hard_cap = _LEVERAGE_CAP.get(vol_regime, 5)
    return max(1, min(natural, hard_cap))


def _liquidation_price(
    entry: Decimal,
    leverage: int,
    side: str,
) -> Decimal | None:
    if leverage <= 0:
        return None
    factor = Decimal("1") / Decimal(leverage)
    if side == "long":
        return entry * (Decimal("1") - factor)
    return entry * (Decimal("1") + factor)


def _risk_score(
    *,
    side: str,
    rr: Decimal | None,
    structure_trend: str | None,
    price_zone: str | None,
    adx: Decimal | None,
    vol_regime: str | None,
    bos_streak: int | None,
    composite_signal: str | None,
) -> tuple[Decimal | None, dict[str, Decimal] | None]:
    """
    Weighted risk score 1–10.

    Component weights:
      R:R ratio       25%
      Structure       20%
      ADX             15%
      Vol regime      15%
      Price zone      10%
      BOS streak      10%
      Momentum         5%
    """
    if rr is None:
        return None, None

    # R:R
    if rr >= Decimal("3.0"):
        rr_s = Decimal("10")
    elif rr >= Decimal("2.0"):
        rr_s = Decimal("7")
    elif rr >= Decimal("1.5"):
        rr_s = Decimal("5")
    else:
        rr_s = Decimal("0")

    # Structure — uses structure_trend ("bullish"|"bearish"|"neutral")
    if structure_trend is None:
        struct_s = Decimal("0")
    elif (side == "long" and structure_trend == "bullish") or \
         (side == "short" and structure_trend == "bearish"):
        struct_s = Decimal("10")
    elif structure_trend == "neutral":
        struct_s = Decimal("5")
    else:
        struct_s = Decimal("0")

    # ADX
    if adx is None:
        adx_s = Decimal("3")
    elif adx > Decimal("40"):
        adx_s = Decimal("10")
    elif adx >= Decimal("20"):
        adx_s = Decimal("6")
    else:
        adx_s = Decimal("2")

    # Vol regime — uses VolatilityMetrics strings: "low"|"normal"|"high"|"extreme"
    vol_s = _VOL_SCORE.get(vol_regime, Decimal("5"))

    # Price zone
    if price_zone is None:
        zone_s = Decimal("0")
    elif (side == "long"  and price_zone == "discount") or \
         (side == "short" and price_zone == "premium"):
        zone_s = Decimal("10")
    elif price_zone == "equilibrium":
        zone_s = Decimal("5")
    else:
        zone_s = Decimal("0")

    # BOS streak
    if bos_streak is None:
        bos_s = Decimal("1")
    elif bos_streak >= 3:
        bos_s = Decimal("10")
    elif bos_streak == 2:
        bos_s = Decimal("7")
    elif bos_streak == 1:
        bos_s = Decimal("4")
    else:
        bos_s = Decimal("1")

    # Momentum composite signal
    if composite_signal in (None, "unavailable"):
        mom_s = Decimal("3")
    elif side == "long":
        mom_s = {
            "strong_bull": Decimal("10"),
            "bull":        Decimal("6"),
            "neutral":     Decimal("3"),
            "bear":        Decimal("1"),
            "strong_bear": Decimal("0"),
        }.get(composite_signal, Decimal("3"))
    else:
        mom_s = {
            "strong_bear": Decimal("10"),
            "bear":        Decimal("6"),
            "neutral":     Decimal("3"),
            "bull":        Decimal("1"),
            "strong_bull": Decimal("0"),
        }.get(composite_signal, Decimal("3"))

    breakdown = {
        "rr":         rr_s,
        "structure":  struct_s,
        "adx":        adx_s,
        "vol_regime": vol_s,
        "price_zone": zone_s,
        "bos_streak": bos_s,
        "momentum":   mom_s,
    }

    weighted = (
        rr_s     * Decimal("0.25")
        + struct_s * Decimal("0.20")
        + adx_s    * Decimal("0.15")
        + vol_s    * Decimal("0.15")
        + zone_s   * Decimal("0.10")
        + bos_s    * Decimal("0.10")
        + mom_s    * Decimal("0.05")
    )
    return weighted.quantize(Decimal("0.1")), breakdown


def _unavailable(
    reason: str,
    stop_candidates: dict[str, dict[str, Any]] | None = None,
) -> RiskMetrics:
    return RiskMetrics(
        viable=False,
        rejection_reason=reason,
        risk_score=None,
        risk_score_breakdown=None,
        entry_price=None,
        side=None,
        effective_entry=None,
        stop_price=None,
        stop_method=None,
        stop_distance_pct=None,
        stop_atr_multiple=None,
        stop_candidates=stop_candidates,
        tp1=None, tp2=None, tp3=None,
        tp_fvg=None, tp_structure=None, tp_cloud=None,
        reward_risk_ratio=None,
        position_size_base=None,
        position_size_quote=None,
        position_size_pct=None,
        max_loss_quote=None,
        risk_pct_used=None,
        caps_applied=None,
        suggested_leverage=None,
        liquidation_price=None,
        liquidation_distance_pct=None,
        wide_stop=False,
        concentration_cap=False,
        liquidity_cap=False,
        liquidation_warning=False,
        low_adx_warning=False,
    )
