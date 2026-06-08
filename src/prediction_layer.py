from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

import pandas as pd

from .types import MarketRegime


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


@dataclass(slots=True)
class PredictionResult:
    available: bool
    directional_score: float
    bullish_probability: float
    bearish_probability: float
    neutral_probability: float
    confidence_score: float
    prediction_bias: str
    contributing_signals: dict[str, float] = field(default_factory=dict)
    fallback_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "prediction_layer": self.available,
            "prediction_available": self.available,
            "directional_score": self.directional_score,
            "bullish_probability": self.bullish_probability,
            "bearish_probability": self.bearish_probability,
            "neutral_probability": self.neutral_probability,
            "confidence_score": self.confidence_score,
            "prediction_bias": self.prediction_bias,
            "contributing_signals": self.contributing_signals,
            "prediction_fallback_reason": self.fallback_reason,
        }


class PredictionLayer:
    DEFAULT_WEIGHTS = {
        "orderbook_pressure": 0.25,
        "momentum_score": 0.20,
        "ema_slope": 0.15,
        "ema_slope_acceleration": 0.10,
        "vwap_distance": 0.10,
        "volume_zscore": 0.10,
        "regime_confidence": 0.10,
    }

    def __init__(self, weights: dict[str, float] | None = None) -> None:
        self.weights = dict(weights or self.DEFAULT_WEIGHTS)

    def unavailable(self, reason: str = "unavailable") -> PredictionResult:
        return PredictionResult(
            available=False,
            directional_score=0.0,
            bullish_probability=1 / 3,
            bearish_probability=1 / 3,
            neutral_probability=1 / 3,
            confidence_score=0.0,
            prediction_bias="NEUTRAL",
            contributing_signals={},
            fallback_reason=reason,
        )

    def predict(
        self,
        *,
        candles: pd.DataFrame,
        regime: MarketRegime,
        regime_confidence: float,
        regime_signal: Any,
        orderbook: dict[str, Any] | None = None,
        recent_taker_pressure: float | None = None,
        ema_slope_threshold_pct: float = 0.004,
    ) -> PredictionResult:
        if candles is None or candles.empty or "close" not in candles:
            return self.unavailable("missing_candles")

        orderbook = orderbook or {}
        close = candles["close"].astype(float)
        if len(close) < 3:
            return self.unavailable("insufficient_candles")

        signals: dict[str, float] = {}
        threshold = max(float(ema_slope_threshold_pct or 0.004), 1e-9)
        ema_fast = float(getattr(regime_signal, "ema_slope_fast_pct", 0.0) or 0.0)
        ema_slow = float(getattr(regime_signal, "ema_slope_slow_pct", 0.0) or 0.0)
        signals["ema_slope"] = _clamp(((ema_fast + ema_slow) / 2.0) / max(threshold * 2.0, 1e-9))
        signals["ema_slope_acceleration"] = _clamp(float(getattr(regime_signal, "ema_slope_acceleration_score", 0.0) or 0.0))
        signals["momentum_score"] = _clamp(float(getattr(regime_signal, "momentum_score", 0.0) or 0.0))

        regime_direction = 0.0
        if regime in {MarketRegime.TREND_UP, MarketRegime.TREND_UP_PULLBACK}:
            regime_direction = 1.0
        elif regime in {MarketRegime.TREND_DOWN, MarketRegime.TREND_DOWN_PULLBACK}:
            regime_direction = -1.0
        else:
            transition_direction = str(getattr(regime_signal, "transition_direction", "NONE") or "NONE")
            if transition_direction == "UP":
                regime_direction = 1.0
            elif transition_direction == "DOWN":
                regime_direction = -1.0
        signals["regime_confidence"] = _clamp(regime_direction * max(float(regime_confidence or 0.0), 0.0))

        if bool(orderbook.get("available")) and not bool(orderbook.get("stale")):
            signals["orderbook_pressure"] = _clamp(float(orderbook.get("pressure_score", 0.0) or 0.0))
        if recent_taker_pressure is not None:
            signals["recent_taker_pressure"] = _clamp(float(recent_taker_pressure))

        vwap_signal = self._vwap_distance_signal(candles)
        if vwap_signal is not None:
            signals["vwap_distance"] = vwap_signal

        volume_signal = self._volume_zscore_signal(candles)
        if volume_signal is not None:
            signals["volume_zscore"] = volume_signal

        weighted_sum = 0.0
        used_weight = 0.0
        for name, value in signals.items():
            weight = float(self.weights.get(name, 0.05 if name == "recent_taker_pressure" else 0.0) or 0.0)
            if weight <= 0:
                continue
            weighted_sum += weight * value
            used_weight += weight
        if used_weight <= 0:
            return self.unavailable("no_weighted_signals")

        directional_score = _clamp(weighted_sum / used_weight)
        bullish, bearish, neutral = self._probabilities(directional_score)
        confidence = self._confidence(directional_score, signals)
        bias = self._bias(directional_score, confidence)
        return PredictionResult(
            available=True,
            directional_score=directional_score,
            bullish_probability=bullish,
            bearish_probability=bearish,
            neutral_probability=neutral,
            confidence_score=confidence,
            prediction_bias=bias,
            contributing_signals={k: round(v, 6) for k, v in signals.items()},
        )

    def _vwap_distance_signal(self, candles: pd.DataFrame) -> float | None:
        if "volume" not in candles:
            return None
        window = candles.tail(min(len(candles), 50)).copy()
        volume = window["volume"].astype(float)
        total_volume = float(volume.sum())
        if total_volume <= 0:
            return None
        typical = (window["high"].astype(float) + window["low"].astype(float) + window["close"].astype(float)) / 3.0 if {"high", "low"}.issubset(window.columns) else window["close"].astype(float)
        vwap = float((typical * volume).sum() / total_volume)
        last = float(window["close"].iloc[-1])
        return _clamp((last - vwap) / max(vwap * 0.01, 1e-9))

    def _volume_zscore_signal(self, candles: pd.DataFrame) -> float | None:
        if "volume" not in candles or len(candles) < 10:
            return None
        volume = candles["volume"].astype(float).tail(min(len(candles), 50))
        std = float(volume.std() or 0.0)
        if std <= 0:
            return None
        zscore = (float(volume.iloc[-1]) - float(volume.mean())) / std
        close = candles["close"].astype(float)
        last_return = (float(close.iloc[-1]) - float(close.iloc[-2])) / max(float(close.iloc[-2]), 1e-9)
        direction = 1.0 if last_return > 0 else (-1.0 if last_return < 0 else 0.0)
        return _clamp(direction * zscore / 3.0)

    def _probabilities(self, score: float) -> tuple[float, float, float]:
        bullish_raw = math.exp(2.2 * score)
        bearish_raw = math.exp(-2.2 * score)
        neutral_raw = math.exp(2.0 * (1.0 - abs(score)))
        total = bullish_raw + bearish_raw + neutral_raw
        return bullish_raw / total, bearish_raw / total, neutral_raw / total

    def _confidence(self, score: float, signals: dict[str, float]) -> float:
        directional = [v for v in signals.values() if abs(v) > 1e-9]
        if not directional:
            return 0.0
        sign = 1.0 if score >= 0 else -1.0
        agreement = sum(1 for v in directional if v * sign > 0) / len(directional)
        strength = sum(abs(v) for v in directional) / len(directional)
        return max(0.0, min(1.0, 0.55 * agreement + 0.45 * strength))

    def _bias(self, score: float, confidence: float) -> str:
        if score >= 0.55 and confidence >= 0.65:
            return "STRONG_LONG"
        if score >= 0.15:
            return "LONG"
        if score <= -0.55 and confidence >= 0.65:
            return "STRONG_SHORT"
        if score <= -0.15:
            return "SHORT"
        return "NEUTRAL"
