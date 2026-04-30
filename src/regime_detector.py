from __future__ import annotations

import pandas as pd
from .types import MarketRegime


class RegimeDetector:
    def detect(self, candles: pd.DataFrame) -> MarketRegime:
        closes = candles["close"].astype(float)
        returns = closes.pct_change().dropna()
        if len(closes) < 30 or returns.empty:
            return MarketRegime.RANGE

        vol = returns.rolling(20).std().iloc[-1]
        ema_fast = closes.ewm(span=10).mean()
        ema_slope = (ema_fast.iloc[-1] - ema_fast.iloc[-5]) / max(ema_fast.iloc[-5], 1e-9)
        atr_like = (candles["high"] - candles["low"]).rolling(14).mean().iloc[-1] / max(closes.iloc[-1], 1e-9)

        if vol > 0.02 and atr_like > 0.015:
            return MarketRegime.HIGH_VOL
        if vol > 0.03:
            return MarketRegime.RISK_OFF
        if ema_slope > 0.01:
            return MarketRegime.TREND_UP
        if ema_slope < -0.01:
            return MarketRegime.TREND_DOWN
        return MarketRegime.RANGE
