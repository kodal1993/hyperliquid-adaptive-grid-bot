from __future__ import annotations

from dataclasses import dataclass
import pandas as pd
from .types import MarketRegime


@dataclass(slots=True)
class RegimeSignal:
    regime: MarketRegime
    confidence: float
    atr_pct: float
    return_vol_pct: float
    move_pct: float
    ema_slope_fast_pct: float
    ema_slope_slow_pct: float
    trend_structure_score: float = 0.0
    momentum_score: float = 0.0
    trend_strength_score: float = 0.0


class RegimeDetector:
    def __init__(self, trend_hold_ticks: int = 4) -> None:
        self.trend_hold_ticks = max(int(trend_hold_ticks), 0)
        self._last_regime: MarketRegime = MarketRegime.RANGE
        self._trend_hold_remaining = 0
        self.regime_confidence = 0.0
        self.last_signal = RegimeSignal(MarketRegime.RANGE, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    def detect(
        self,
        candles: pd.DataFrame,
        trend_lookback_candles: int = 90,
        trend_move_threshold_pct: float = 0.008,
        ema_slope_threshold_pct: float = 0.004,
        trend_strength_threshold: float = 0.65,
    ) -> MarketRegime:
        signal = self.detect_signal(
            candles,
            trend_lookback_candles=trend_lookback_candles,
            trend_move_threshold_pct=trend_move_threshold_pct,
            ema_slope_threshold_pct=ema_slope_threshold_pct,
            trend_strength_threshold=trend_strength_threshold,
        )
        return signal.regime

    def detect_signal(
        self,
        candles: pd.DataFrame,
        trend_lookback_candles: int = 90,
        trend_move_threshold_pct: float = 0.008,
        ema_slope_threshold_pct: float = 0.004,
        trend_strength_threshold: float = 0.65,
    ) -> RegimeSignal:
        raw_signal = self._detect_raw_signal(
            candles,
            trend_lookback_candles=trend_lookback_candles,
            trend_move_threshold_pct=trend_move_threshold_pct,
            ema_slope_threshold_pct=ema_slope_threshold_pct,
            trend_strength_threshold=trend_strength_threshold,
        )
        regime = self._apply_hysteresis(raw_signal.regime)
        confidence = raw_signal.confidence
        if regime != raw_signal.regime and regime in {MarketRegime.TREND_UP, MarketRegime.TREND_DOWN}:
            confidence = min(confidence, 0.35)
        self.regime_confidence = max(0.0, min(1.0, confidence))
        self.last_signal = RegimeSignal(
            regime=regime,
            confidence=self.regime_confidence,
            atr_pct=raw_signal.atr_pct,
            return_vol_pct=raw_signal.return_vol_pct,
            move_pct=raw_signal.move_pct,
            ema_slope_fast_pct=raw_signal.ema_slope_fast_pct,
            ema_slope_slow_pct=raw_signal.ema_slope_slow_pct,
            trend_structure_score=raw_signal.trend_structure_score,
            momentum_score=raw_signal.momentum_score,
            trend_strength_score=raw_signal.trend_strength_score,
        )
        return self.last_signal

    def _detect_raw_signal(
        self,
        candles: pd.DataFrame,
        trend_lookback_candles: int,
        trend_move_threshold_pct: float,
        ema_slope_threshold_pct: float,
        trend_strength_threshold: float,
    ) -> RegimeSignal:
        closes = candles["close"].astype(float)
        returns = closes.pct_change().dropna()
        if len(closes) < 30 or returns.empty:
            return RegimeSignal(MarketRegime.RANGE, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

        vol = float(returns.rolling(20).std().iloc[-1] or 0.0)
        atr_pct = calculate_atr_pct(candles, period=14)

        ema_fast = closes.ewm(span=10).mean()
        ema_slow = closes.ewm(span=30).mean()
        fast_base = max(float(ema_fast.iloc[-5]), 1e-9)
        slow_base = max(float(ema_slow.iloc[-10]), 1e-9)
        ema_slope_fast = (float(ema_fast.iloc[-1]) - float(ema_fast.iloc[-5])) / fast_base
        ema_slope_slow = (float(ema_slow.iloc[-1]) - float(ema_slow.iloc[-10])) / slow_base

        lb = min(trend_lookback_candles, len(closes) - 1)
        move_pct = (float(closes.iloc[-1]) - float(closes.iloc[-1 - lb])) / max(float(closes.iloc[-1 - lb]), 1e-9)
        prior_closes = closes.iloc[-1 - lb:-1]
        breakout_up = not prior_closes.empty and closes.iloc[-1] > prior_closes.max()
        breakout_down = not prior_closes.empty and closes.iloc[-1] < prior_closes.min()
        trend_structure_score = calculate_trend_structure_score(candles, lookback=lb)

        move_score = abs(move_pct) / max(trend_move_threshold_pct, 1e-9)
        fast_score = abs(ema_slope_fast) / max(ema_slope_threshold_pct, 1e-9)
        slow_score = abs(ema_slope_slow) / max(ema_slope_threshold_pct * 0.7, 1e-9)
        signed_move_score = _signed_score(move_pct, trend_move_threshold_pct)
        signed_fast_score = _signed_score(ema_slope_fast, ema_slope_threshold_pct)
        signed_slow_score = _signed_score(ema_slope_slow, ema_slope_threshold_pct * 0.7)
        momentum_score = _clamp(0.45 * signed_move_score + 0.35 * signed_fast_score + 0.20 * signed_slow_score, -1.0, 1.0)
        confidence = max(0.0, min(1.0, (0.45 * move_score + 0.35 * fast_score + 0.20 * slow_score) / 2.0))
        if breakout_up or breakout_down:
            confidence = min(1.0, confidence + 0.15)
        candidate_direction = 1.0 if trend_structure_score + momentum_score >= 0 else -1.0
        trend_direction_score = _clamp(
            0.40 * trend_structure_score + 0.35 * momentum_score + 0.25 * candidate_direction * confidence,
            -1.0,
            1.0,
        )
        trend_strength_score = abs(trend_direction_score)

        if vol > 0.03:
            return RegimeSignal(MarketRegime.RISK_OFF, 1.0, atr_pct, vol, move_pct, ema_slope_fast, ema_slope_slow, trend_structure_score, momentum_score, trend_strength_score)
        if vol > 0.02 and atr_pct > 0.015:
            return RegimeSignal(MarketRegime.HIGH_VOL, 0.9, atr_pct, vol, move_pct, ema_slope_fast, ema_slope_slow, trend_structure_score, momentum_score, trend_strength_score)

        trend_up = (
            move_pct >= trend_move_threshold_pct
            or (ema_slope_fast >= ema_slope_threshold_pct and ema_slope_slow >= ema_slope_threshold_pct * 0.7)
            or breakout_up
        )
        trend_down = (
            move_pct <= -trend_move_threshold_pct
            or (ema_slope_fast <= -ema_slope_threshold_pct and ema_slope_slow <= -ema_slope_threshold_pct * 0.7)
            or breakout_down
        )
        early_trend_up = trend_strength_score > trend_strength_threshold and trend_direction_score > 0
        early_trend_down = trend_strength_score > trend_strength_threshold and trend_direction_score < 0
        if (trend_up or early_trend_up) and not trend_down:
            return RegimeSignal(MarketRegime.TREND_UP, confidence, atr_pct, vol, move_pct, ema_slope_fast, ema_slope_slow, trend_structure_score, momentum_score, trend_strength_score)
        if (trend_down or early_trend_down) and not trend_up:
            return RegimeSignal(MarketRegime.TREND_DOWN, confidence, atr_pct, vol, move_pct, ema_slope_fast, ema_slope_slow, trend_structure_score, momentum_score, trend_strength_score)
        return RegimeSignal(MarketRegime.RANGE, min(confidence, 0.3), atr_pct, vol, move_pct, ema_slope_fast, ema_slope_slow, trend_structure_score, momentum_score, trend_strength_score)

    def _apply_hysteresis(self, raw_regime: MarketRegime) -> MarketRegime:
        if raw_regime in {MarketRegime.RISK_OFF, MarketRegime.HIGH_VOL}:
            self._last_regime = raw_regime
            self._trend_hold_remaining = 0
            return raw_regime

        if raw_regime in {MarketRegime.TREND_UP, MarketRegime.TREND_DOWN}:
            self._last_regime = raw_regime
            self._trend_hold_remaining = self.trend_hold_ticks
            return raw_regime

        if (
            raw_regime == MarketRegime.RANGE
            and self._last_regime in {MarketRegime.TREND_UP, MarketRegime.TREND_DOWN}
            and self._trend_hold_remaining > 0
        ):
            self._trend_hold_remaining -= 1
            return self._last_regime

        self._last_regime = raw_regime
        self._trend_hold_remaining = 0
        return raw_regime


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(float(value), high))


def _signed_score(value: float, threshold: float) -> float:
    return _clamp(float(value) / max(float(threshold), 1e-9), -1.0, 1.0)


def calculate_trend_structure_score(candles: pd.DataFrame, lookback: int = 60) -> float:
    """Score recent HH/HL vs LH/LL structure from -1.0 (bearish) to +1.0 (bullish)."""
    if candles.empty or not {"high", "low"}.issubset(candles.columns):
        return 0.0
    if len(candles) < 10:
        return 0.0
    window = max(10, min(int(lookback), len(candles)))
    highs = candles["high"].astype(float).tail(window)
    lows = candles["low"].astype(float).tail(window)
    split = len(highs) // 2
    if split < 2 or len(highs) - split < 2:
        return 0.0

    previous_high = float(highs.iloc[:split].max())
    recent_high = float(highs.iloc[split:].max())
    previous_low = float(lows.iloc[:split].min())
    recent_low = float(lows.iloc[split:].min())
    reference_price = max(abs(float(candles["close"].astype(float).iloc[-1])) if "close" in candles else max(abs(previous_high), 1.0), 1e-9)
    tolerance = reference_price * 1e-6

    higher_high = recent_high > previous_high + tolerance
    higher_low = recent_low > previous_low + tolerance
    lower_high = recent_high < previous_high - tolerance
    lower_low = recent_low < previous_low - tolerance

    bullish_points = int(higher_high) + int(higher_low)
    bearish_points = int(lower_high) + int(lower_low)
    return (bullish_points - bearish_points) / 2.0


def calculate_atr_pct(candles: pd.DataFrame, period: int = 14) -> float:
    if candles.empty or len(candles) < 2:
        return 0.0
    highs = candles["high"].astype(float)
    lows = candles["low"].astype(float)
    closes = candles["close"].astype(float)
    prev_close = closes.shift(1)
    true_range = pd.concat([(highs - lows).abs(), (highs - prev_close).abs(), (lows - prev_close).abs()], axis=1).max(axis=1)
    atr = float(true_range.rolling(period).mean().iloc[-1] or 0.0)
    return atr / max(float(closes.iloc[-1]), 1e-9)
