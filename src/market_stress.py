from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
from typing import Any

import pandas as pd

from .types import MarketRegime

logger = logging.getLogger(__name__)


class VolatilityMode(str, Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    EXTREME = "EXTREME"


class OpportunityMode(str, Enum):
    NORMAL_GRID = "NORMAL_GRID"
    TREND_FOLLOW_SHORT = "TREND_FOLLOW_SHORT"
    TREND_FOLLOW_LONG = "TREND_FOLLOW_LONG"
    NO_NEW_EXPOSURE = "NO_NEW_EXPOSURE"
    REDUCE_ONLY = "REDUCE_ONLY"
    EMERGENCY_FLAT = "EMERGENCY_FLAT"


@dataclass(slots=True)
class MarketStressDecision:
    enabled: bool
    volatility_mode: VolatilityMode
    market_stress_score: float
    trend_strength_score: float
    opportunity_mode: OpportunityMode
    btc_5m_return_pct: float
    btc_15m_return_pct: float
    btc_1h_return_pct: float
    candle_range_pct: float
    allow_buys: bool
    allow_sells: bool
    grid_levels: int
    spacing_multiplier: float
    reason: str
    reduce_only: bool = False
    emergency_flat: bool = False

    def telemetry(self) -> dict[str, Any]:
        return {
            "market_stress_enabled": self.enabled,
            "volatility_mode": self.volatility_mode.value,
            "market_stress_score": self.market_stress_score,
            "trend_strength_score": self.trend_strength_score,
            "opportunity_mode": self.opportunity_mode.value,
            "btc_5m_return_pct": self.btc_5m_return_pct,
            "btc_15m_return_pct": self.btc_15m_return_pct,
            "btc_1h_return_pct": self.btc_1h_return_pct,
            "candle_range_pct": self.candle_range_pct,
            "market_stress_reason": self.reason,
        }


def _cfg_float(config: Any, name: str, default: float) -> float:
    return float(getattr(config, name, default))


def _cfg_bool(config: Any, name: str, default: bool) -> bool:
    return bool(getattr(config, name, default))


def _regime_value(regime: MarketRegime | str) -> str:
    return regime.value if isinstance(regime, MarketRegime) else str(regime)


def _pct_return(closes: pd.Series, bars: int) -> float:
    if len(closes) <= bars:
        return 0.0
    current = float(closes.iloc[-1])
    previous = float(closes.iloc[-1 - bars])
    if previous <= 0:
        return 0.0
    return (current - previous) / previous


def _reduce_side(position_side: str) -> tuple[bool, bool]:
    side = position_side.upper()
    if side == "LONG":
        return False, True
    if side == "SHORT":
        return True, False
    return False, False


def _normal_decision(*, enabled: bool, allow_buys: bool, allow_sells: bool, current_levels: int, reason: str, btc_5m_return_pct: float = 0.0, btc_15m_return_pct: float = 0.0, btc_1h_return_pct: float = 0.0, candle_range_pct: float = 0.0) -> MarketStressDecision:
    return MarketStressDecision(
        enabled=enabled,
        volatility_mode=VolatilityMode.NORMAL,
        market_stress_score=0.0,
        trend_strength_score=0.0,
        opportunity_mode=OpportunityMode.NORMAL_GRID,
        btc_5m_return_pct=btc_5m_return_pct,
        btc_15m_return_pct=btc_15m_return_pct,
        btc_1h_return_pct=btc_1h_return_pct,
        candle_range_pct=candle_range_pct,
        allow_buys=allow_buys,
        allow_sells=allow_sells,
        grid_levels=current_levels,
        spacing_multiplier=1.0,
        reason=reason,
    )


def decide_market_stress(
    *,
    candles: pd.DataFrame,
    raw_regime: MarketRegime | str,
    confirmed_regime: MarketRegime | str,
    regime_confidence: float,
    atr_pct: float,
    position_side: str,
    position_notional: float,
    config: Any,
    allow_buys: bool,
    allow_sells: bool,
    current_levels: int,
    regime_trend_strength_score: float | None = None,
) -> MarketStressDecision:
    """BTC-only Phase 1 market stress layer using local candle data only.

    The decision is intentionally conservative: it can reduce exposure-building
    directions, lower grid levels, and widen spacing, but it never blocks the
    side needed to reduce an existing LONG/SHORT position.
    """
    enabled = _cfg_bool(config, "enable_market_stress_filter", True)
    current_levels = max(int(current_levels), 0)
    if not enabled:
        return _normal_decision(enabled=False, allow_buys=allow_buys, allow_sells=allow_sells, current_levels=current_levels, reason="market_stress_disabled")

    if candles is None or candles.empty or "close" not in candles:
        return _normal_decision(enabled=True, allow_buys=allow_buys, allow_sells=allow_sells, current_levels=current_levels, reason="insufficient_candle_data")

    try:
        closes = pd.to_numeric(candles["close"], errors="coerce").dropna()
        if closes.empty:
            return _normal_decision(enabled=True, allow_buys=allow_buys, allow_sells=allow_sells, current_levels=current_levels, reason="insufficient_candle_data")
        btc_5m_return_pct = _pct_return(closes, 1)
        btc_15m_return_pct = _pct_return(closes, 3)
        btc_1h_return_pct = _pct_return(closes, 12)
        if {"high", "low", "close"}.issubset(candles.columns):
            last = candles.iloc[-1]
            last_close = float(last["close"])
            candle_range_pct = 0.0 if last_close <= 0 else (float(last["high"]) - float(last["low"])) / last_close
        else:
            candle_range_pct = abs(btc_5m_return_pct)

        high_vol_atr_pct = _cfg_float(config, "high_vol_atr_pct", 0.006)
        extreme_vol_atr_pct = _cfg_float(config, "extreme_vol_atr_pct", 0.010)
        btc_5m_shock_pct = _cfg_float(config, "btc_5m_shock_pct", 0.008)
        btc_1h_shock_pct = _cfg_float(config, "btc_1h_shock_pct", 0.025)
        trend_strong_confidence = _cfg_float(config, "trend_strong_confidence", 0.75)
        trend_weak_confidence = _cfg_float(config, "trend_weak_confidence", 0.45)
        panic_score_threshold = _cfg_float(config, "panic_score_threshold", 0.80)
        extreme_flat_enabled = _cfg_bool(config, "market_stress_extreme_flat", False)

        shock_score = max(
            min(abs(btc_5m_return_pct) / max(btc_5m_shock_pct, 1e-12), 1.0),
            min(abs(btc_1h_return_pct) / max(btc_1h_shock_pct, 1e-12), 1.0),
        )
        atr_score = min(max(float(atr_pct), 0.0) / max(extreme_vol_atr_pct, 1e-12), 1.0)
        market_stress_score = max(0.0, min(max(atr_score, shock_score), 1.0))
        close_return_vol = float(closes.pct_change().dropna().std() or 0.0)
        quiet_close_tape = max(abs(btc_5m_return_pct), abs(btc_15m_return_pct), abs(btc_1h_return_pct), close_return_vol) < btc_5m_shock_pct * 0.25

        if quiet_close_tape:
            volatility_mode = VolatilityMode.LOW if atr_pct < high_vol_atr_pct * 0.5 else VolatilityMode.NORMAL
            market_stress_score = min(market_stress_score, 0.34)
        elif atr_pct >= extreme_vol_atr_pct or abs(btc_1h_return_pct) >= btc_1h_shock_pct * 1.5:
            volatility_mode = VolatilityMode.EXTREME
        elif atr_pct >= high_vol_atr_pct or abs(btc_5m_return_pct) >= btc_5m_shock_pct or abs(btc_1h_return_pct) >= btc_1h_shock_pct:
            volatility_mode = VolatilityMode.HIGH
        elif atr_pct < high_vol_atr_pct * 0.5 and market_stress_score < 0.35:
            volatility_mode = VolatilityMode.LOW
        else:
            volatility_mode = VolatilityMode.NORMAL

        raw = _regime_value(raw_regime)
        confirmed = _regime_value(confirmed_regime)
        trend_strength_input = regime_confidence if regime_trend_strength_score is None else regime_trend_strength_score
        trend_strength_score = max(0.0, min(float(trend_strength_input), 1.0)) if confirmed in {"TREND_UP", "TREND_DOWN", "TREND_UP_PULLBACK", "TREND_DOWN_PULLBACK"} else 0.0
        strong_trend = confirmed in {"TREND_UP", "TREND_DOWN", "TREND_UP_PULLBACK", "TREND_DOWN_PULLBACK"} and trend_strength_score >= trend_strong_confidence
        weak_trend = confirmed == "RANGE" or trend_strength_score <= trend_weak_confidence
        spacing_multiplier = 1.0
        new_allow_buys = bool(allow_buys)
        new_allow_sells = bool(allow_sells)
        new_levels = current_levels
        opportunity_mode = OpportunityMode.NORMAL_GRID
        reduce_only = False
        emergency_flat = False
        reason = f"normal_grid raw_regime={raw} confirmed_regime={confirmed}"

        if volatility_mode in {VolatilityMode.HIGH, VolatilityMode.EXTREME} and strong_trend and confirmed in {"TREND_DOWN", "TREND_DOWN_PULLBACK"}:
            opportunity_mode = OpportunityMode.TREND_FOLLOW_SHORT
            new_allow_sells = position_side.upper() != "SHORT" and bool(allow_sells)
            new_allow_buys = position_side.upper() == "SHORT"
            new_levels = max(1, min(current_levels, 2))
            spacing_multiplier = 1.5 if volatility_mode == VolatilityMode.EXTREME else 1.2
            reason = "high_or_extreme_vol_strong_trend_down"
        elif volatility_mode in {VolatilityMode.HIGH, VolatilityMode.EXTREME} and strong_trend and confirmed in {"TREND_UP", "TREND_UP_PULLBACK"}:
            opportunity_mode = OpportunityMode.TREND_FOLLOW_LONG
            new_allow_buys = position_side.upper() != "LONG" and bool(allow_buys)
            new_allow_sells = position_side.upper() == "LONG"
            new_levels = max(1, min(current_levels, 2))
            spacing_multiplier = 1.5 if volatility_mode == VolatilityMode.EXTREME else 1.2
            reason = "high_or_extreme_vol_strong_trend_up"
        elif volatility_mode == VolatilityMode.EXTREME and weak_trend:
            if market_stress_score >= panic_score_threshold and extreme_flat_enabled:
                opportunity_mode = OpportunityMode.EMERGENCY_FLAT
                emergency_flat = True
                new_allow_buys, new_allow_sells = _reduce_side(position_side)
                new_levels = 0
                spacing_multiplier = 1.5
                reason = "extreme_vol_panic_emergency_flat_enabled"
            else:
                opportunity_mode = OpportunityMode.REDUCE_ONLY
                reduce_only = True
                new_allow_buys, new_allow_sells = _reduce_side(position_side)
                new_levels = max(1, min(current_levels, 1)) if position_side.upper() in {"LONG", "SHORT"} else 0
                spacing_multiplier = 1.5
                reason = "extreme_vol_weak_trend_reduce_only"
        elif volatility_mode == VolatilityMode.HIGH and weak_trend:
            opportunity_mode = OpportunityMode.NO_NEW_EXPOSURE
            new_allow_buys, new_allow_sells = _reduce_side(position_side)
            new_levels = max(1, min(current_levels, 2)) if position_side.upper() in {"LONG", "SHORT"} else 0
            spacing_multiplier = 1.3
            reason = "high_vol_weak_trend_no_new_exposure"
        elif confirmed == "RANGE" and volatility_mode == VolatilityMode.HIGH:
            new_levels = max(1, min(current_levels, 2))
            spacing_multiplier = 1.3
            reason = "range_high_vol_reduced_grid_widened_spacing"
            if market_stress_score >= panic_score_threshold:
                opportunity_mode = OpportunityMode.NO_NEW_EXPOSURE
                new_allow_buys, new_allow_sells = _reduce_side(position_side)
                new_levels = max(1, min(current_levels, 1)) if position_side.upper() in {"LONG", "SHORT"} else 0
                reason = "range_high_vol_high_stress_no_new_exposure"

        return MarketStressDecision(
            enabled=True,
            volatility_mode=volatility_mode,
            market_stress_score=market_stress_score,
            trend_strength_score=trend_strength_score,
            opportunity_mode=opportunity_mode,
            btc_5m_return_pct=btc_5m_return_pct,
            btc_15m_return_pct=btc_15m_return_pct,
            btc_1h_return_pct=btc_1h_return_pct,
            candle_range_pct=candle_range_pct,
            allow_buys=new_allow_buys,
            allow_sells=new_allow_sells,
            grid_levels=new_levels,
            spacing_multiplier=spacing_multiplier,
            reason=reason,
            reduce_only=reduce_only,
            emergency_flat=emergency_flat,
        )
    except Exception:
        logger.exception("market_stress_error_fallback_current_strategy")
        return _normal_decision(enabled=True, allow_buys=allow_buys, allow_sells=allow_sells, current_levels=current_levels, reason="market_stress_error_fallback_current_strategy")
