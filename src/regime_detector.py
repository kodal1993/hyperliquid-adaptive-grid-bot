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
    ) -> MarketRegime:
        signal = self.detect_signal(
            candles,
            trend_lookback_candles=trend_lookback_candles,
            trend_move_threshold_pct=trend_move_threshold_pct,
            ema_slope_threshold_pct=ema_slope_threshold_pct,
        )
        return signal.regime

    def detect_signal(
        self,
        candles: pd.DataFrame,
        trend_lookback_candles: int = 90,
        trend_move_threshold_pct: float = 0.008,
        ema_slope_threshold_pct: float = 0.004,
    ) -> RegimeSignal:
        raw_signal = self._detect_raw_signal(
            candles,
            trend_lookback_candles=trend_lookback_candles,
            trend_move_threshold_pct=trend_move_threshold_pct,
            ema_slope_threshold_pct=ema_slope_threshold_pct,
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
        )
        return self.last_signal

    def _detect_raw_signal(
        self,
        candles: pd.DataFrame,
        trend_lookback_candles: int,
        trend_move_threshold_pct: float,
        ema_slope_threshold_pct: float,
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
        breakout_up = closes.iloc[-1] > closes.tail(lb).max()
        breakout_down = closes.iloc[-1] < closes.tail(lb).min()

        if vol > 0.03:
            return RegimeSignal(MarketRegime.RISK_OFF, 1.0, atr_pct, vol, move_pct, ema_slope_fast, ema_slope_slow)
        if vol > 0.02 and atr_pct > 0.015:
            return RegimeSignal(MarketRegime.HIGH_VOL, 0.9, atr_pct, vol, move_pct, ema_slope_fast, ema_slope_slow)

        move_score = abs(move_pct) / max(trend_move_threshold_pct, 1e-9)
        fast_score = abs(ema_slope_fast) / max(ema_slope_threshold_pct, 1e-9)
        slow_score = abs(ema_slope_slow) / max(ema_slope_threshold_pct * 0.7, 1e-9)
        confidence = max(0.0, min(1.0, (0.45 * move_score + 0.35 * fast_score + 0.20 * slow_score) / 2.0))
        if breakout_up or breakout_down:
            confidence = min(1.0, confidence + 0.15)

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
        if trend_up and not trend_down:
            return RegimeSignal(MarketRegime.TREND_UP, confidence, atr_pct, vol, move_pct, ema_slope_fast, ema_slope_slow)
        if trend_down and not trend_up:
            return RegimeSignal(MarketRegime.TREND_DOWN, confidence, atr_pct, vol, move_pct, ema_slope_fast, ema_slope_slow)
        return RegimeSignal(MarketRegime.RANGE, min(confidence, 0.3), atr_pct, vol, move_pct, ema_slope_fast, ema_slope_slow)

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
