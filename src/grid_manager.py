from __future__ import annotations

from dataclasses import dataclass
from .types import GridLevel, MarketRegime


@dataclass(slots=True)
class GridPlan:
    regime: MarketRegime
    long_levels: list[GridLevel]
    short_levels: list[GridLevel]


class GridManager:
    def build_grid(self, mid_price: float, levels: int, base_spacing_pct: float, volatility: float, regime: MarketRegime, order_size: float) -> GridPlan:
        spacing = base_spacing_pct * (1 + volatility)
        if regime in (MarketRegime.HIGH_VOL, MarketRegime.RISK_OFF):
            spacing *= 2.5
        long_levels = []
        short_levels = []
        for i in range(1, levels + 1):
            long_levels.append(GridLevel(price=mid_price * (1 - spacing * i), side="buy", size=order_size))
            short_levels.append(GridLevel(price=mid_price * (1 + spacing * i), side="sell", size=order_size))
        return GridPlan(regime=regime, long_levels=long_levels, short_levels=short_levels)
