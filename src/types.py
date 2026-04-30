from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MarketRegime(str, Enum):
    RANGE = "RANGE"
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RISK_OFF = "RISK_OFF"
    HIGH_VOL = "HIGH_VOL"


@dataclass(slots=True)
class GridLevel:
    price: float
    side: str
    size: float
