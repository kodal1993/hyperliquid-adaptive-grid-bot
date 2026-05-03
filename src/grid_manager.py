from __future__ import annotations

from dataclasses import dataclass
from .types import GridLevel, GridMode, MarketRegime


@dataclass(slots=True)
class GridPlan:
    regime: MarketRegime
    mode: GridMode
    long_levels: list[GridLevel]
    short_levels: list[GridLevel]
    should_regrid: bool


class GridManager:
    def __init__(self) -> None:
        self.last_mid: float | None = None
        self.last_regime: MarketRegime | None = None

    def build_grid(self, mid_price: float, levels: int, base_spacing_pct: float, volatility: float, regime: MarketRegime, order_size: float, regrid_threshold_pct: float, min_grid_profit_over_fees_pct: float, mode: GridMode, allow_buys: bool = True, allow_sells: bool = True) -> GridPlan:
        spacing = base_spacing_pct * (1 + volatility)
        should_regrid = self.last_mid is None or self.last_regime != regime or abs(mid_price - self.last_mid) / self.last_mid >= regrid_threshold_pct
        if spacing <= min_grid_profit_over_fees_pct:
            should_regrid = False

        long_levels: list[GridLevel] = []
        short_levels: list[GridLevel] = []
        for i in range(1, levels + 1):
            if allow_buys and mode in (GridMode.NEUTRAL, GridMode.LONG_BIASED):
                long_levels.append(GridLevel(price=mid_price * (1 - spacing * i), side="buy", size=order_size))
            if allow_sells and mode in (GridMode.NEUTRAL, GridMode.SHORT_BIASED):
                short_levels.append(GridLevel(price=mid_price * (1 + spacing * i), side="sell", size=order_size))

        self.last_mid = mid_price
        self.last_regime = regime
        return GridPlan(regime=regime, mode=mode, long_levels=long_levels, short_levels=short_levels, should_regrid=should_regrid)
