from __future__ import annotations

import pandas as pd
from .types import MarketRegime


class RegimeDetector:
    def detect(self, candles: pd.DataFrame) -> MarketRegime:
        returns = candles["close"].pct_change().dropna()
        vol = returns.std()
        slope = candles["close"].tail(20).reset_index(drop=True)
        trend = (slope.iloc[-1] - slope.iloc[0]) / max(slope.iloc[0], 1e-9)
        if vol > 0.03:
            return MarketRegime.HIGH_VOL
        if trend > 0.02:
            return MarketRegime.TREND_UP
        if trend < -0.02:
            return MarketRegime.TREND_DOWN
        if vol > 0.02:
            return MarketRegime.RISK_OFF
        return MarketRegime.RANGE
