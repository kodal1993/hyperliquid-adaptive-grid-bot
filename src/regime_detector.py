from __future__ import annotations

import pandas as pd
from .types import MarketRegime


class RegimeDetector:
    def __init__(self, trend_hold_ticks: int = 4) -> None:
        self.trend_hold_ticks = max(int(trend_hold_ticks), 0)
        self._last_regime: MarketRegime = MarketRegime.RANGE
        self._trend_hold_remaining = 0

    def detect(
        self,
        candles: pd.DataFrame,
        trend_lookback_candles: int = 90,
        trend_move_threshold_pct: float = 0.008,
        ema_slope_threshold_pct: float = 0.004,
    ) -> MarketRegime:
        raw_regime = self._detect_raw(
            candles,
            trend_lookback_candles=trend_lookback_candles,
            trend_move_threshold_pct=trend_move_threshold_pct,
            ema_slope_threshold_pct=ema_slope_threshold_pct,
        )
        return self._apply_hysteresis(raw_regime)

    def _detect_raw(
        self,
        candles: pd.DataFrame,
        trend_lookback_candles: int,
        trend_move_threshold_pct: float,
        ema_slope_threshold_pct: float,
    ) -> MarketRegime:
        closes = candles["close"].astype(float)
        returns = closes.pct_change().dropna()
        if len(closes) < 30 or returns.empty:
            return MarketRegime.RANGE

        vol = returns.rolling(20).std().iloc[-1]
        atr_like = (candles["high"] - candles["low"]).rolling(14).mean().iloc[-1] / max(closes.iloc[-1], 1e-9)

        ema_fast = closes.ewm(span=10).mean()
        ema_slow = closes.ewm(span=30).mean()
        fast_base = max(ema_fast.iloc[-5], 1e-9)
        slow_base = max(ema_slow.iloc[-10], 1e-9)
        ema_slope_fast = (ema_fast.iloc[-1] - ema_fast.iloc[-5]) / fast_base
        ema_slope_slow = (ema_slow.iloc[-1] - ema_slow.iloc[-10]) / slow_base

        lb = min(trend_lookback_candles, len(closes) - 1)
        move_pct = (closes.iloc[-1] - closes.iloc[-1 - lb]) / max(closes.iloc[-1 - lb], 1e-9)
        breakout_up = closes.iloc[-1] > closes.tail(lb).max()
        breakout_down = closes.iloc[-1] < closes.tail(lb).min()

        if vol > 0.03:
            return MarketRegime.RISK_OFF
        if vol > 0.02 and atr_like > 0.015:
            return MarketRegime.HIGH_VOL

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
            return MarketRegime.TREND_UP
        if trend_down and not trend_up:
            return MarketRegime.TREND_DOWN
        return MarketRegime.RANGE

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
