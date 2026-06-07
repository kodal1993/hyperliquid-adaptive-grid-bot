from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MarketRegime(str, Enum):
    RANGE = "RANGE"
    TREND_UP = "TREND_UP"
    TREND_UP_PULLBACK = "TREND_UP_PULLBACK"
    TREND_DOWN = "TREND_DOWN"
    TREND_DOWN_PULLBACK = "TREND_DOWN_PULLBACK"
    RISK_OFF = "RISK_OFF"
    HIGH_VOL = "HIGH_VOL"


class GridMode(str, Enum):
    NEUTRAL = "neutral"
    LONG_BIASED = "long_biased"
    SHORT_BIASED = "short_biased"
    REDUCE_ONLY = "reduce_only"


@dataclass(slots=True)
class GridLevel:
    price: float
    side: str
    size: float
