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

    def build_grid(
        self,
        mid_price: float,
        levels: int,
        base_spacing_pct: float,
        volatility: float,
        regime: MarketRegime,
        order_size: float,
        regrid_threshold_pct: float,
        min_grid_profit_over_fees_pct: float,
        mode: GridMode,
        allow_buys: bool = True,
        allow_sells: bool = True,
        force_recenter: bool = False,
    ) -> GridPlan:
        spacing = base_spacing_pct * (1 + volatility)
        should_regrid = (
            force_recenter
            or self.last_mid is None
            or self.last_regime != regime
            or abs(mid_price - self.last_mid) / self.last_mid >= regrid_threshold_pct
        )
        if spacing <= min_grid_profit_over_fees_pct:
            should_regrid = False

        long_levels: list[GridLevel] = []
        short_levels: list[GridLevel] = []
        buy_level_count, sell_level_count = self._level_counts(levels, mode)

        for i in range(1, buy_level_count + 1):
            if allow_buys:
                long_levels.append(
                    GridLevel(price=mid_price * (1 - spacing * i), side="buy", size=order_size)
                )

        for i in range(1, sell_level_count + 1):
            if allow_sells:
                short_levels.append(
                    GridLevel(price=mid_price * (1 + spacing * i), side="sell", size=order_size)
                )

        if should_regrid:
            self.last_mid = mid_price
            self.last_regime = regime
        return GridPlan(regime=regime, mode=mode, long_levels=long_levels, short_levels=short_levels, should_regrid=should_regrid)

    def _level_counts(self, levels: int, mode: GridMode) -> tuple[int, int]:
        """Return buy/sell level counts for the current strategy mode.

        RANGE/neutral stays symmetric. Trend-biased modes are deliberately
        asymmetric so the bot spends most of its order capacity in the trend
        direction while still allowing a small amount of passive exit/rebalance
        structure when the orchestrator permits it.
        """
        levels = max(int(levels), 1)
        if mode == GridMode.LONG_BIASED:
            trend_levels = max(1, round(levels * 0.8))
            counter_levels = max(1, levels - trend_levels)
            return trend_levels, counter_levels
        if mode == GridMode.SHORT_BIASED:
            trend_levels = max(1, round(levels * 0.8))
            counter_levels = max(1, levels - trend_levels)
            return counter_levels, trend_levels
        return levels, levels
