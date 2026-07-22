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
    spacing_pct: float = 0.0
    spacing_source: str = "fixed_pct"
    atr_pct: float = 0.0
    grid_levels: int = 0
    trend_bias: float = 0.5
    regime_confidence: float = 0.0

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
        spacing_source: str = "fixed_plus_vol_multiplier",
        atr_pct: float = 0.0,
        trend_bias: float = 0.70,
        regime_confidence: float = 0.0,
    ) -> GridPlan:
        # 1. Alap távolság kiszámítása a volatilitás bevonásával
        base_spacing = base_spacing_pct * (1 + volatility)
        spacing_long = base_spacing
        spacing_short = base_spacing

        # 2. Aszimmetrikus rácstávolság: a trend elleni irányba szélesebb a rács, hogy elkerüljük a zajt
        if mode == GridMode.LONG_BIASED:
            spacing_short = base_spacing * (1 + (trend_bias - 0.5)) 
        elif mode == GridMode.SHORT_BIASED:
            spacing_long = base_spacing * (1 + (trend_bias - 0.5))

        # 3. Dinamikus újrakalibrálási küszöb az ATR alapján (Anti-Chop védelem)
        # Ha a piac nagyon volatil (magas ATR), a bot nem regridel feleslegesen kis mozgásokra.
        dynamic_regrid_threshold = max(regrid_threshold_pct, atr_pct * 0.5) if atr_pct > 0 else regrid_threshold_pct

        should_regrid = (
            force_recenter
            or self.last_mid is None
            or self.last_regime != regime
            or abs(mid_price - self.last_mid) / self.last_mid >= dynamic_regrid_threshold
        )
        
        # Ha még a maker fee-ket sem fedezné a távolság, letiltjuk a regridet
        if min(spacing_long, spacing_short) <= min_grid_profit_over_fees_pct:
            should_regrid = False

        long_levels: list[GridLevel] = []
        short_levels: list[GridLevel] = []
        buy_level_count, sell_level_count = self._level_counts(levels, mode, trend_bias=trend_bias)

        for i in range(1, buy_level_count + 1):
            if allow_buys:
                long_levels.append(
                    GridLevel(price=mid_price * (1 - spacing_long * i), side="buy", size=order_size)
                )

        for i in range(1, sell_level_count + 1):
            if allow_sells:
                short_levels.append(
                    GridLevel(price=mid_price * (1 + spacing_short * i), side="sell", size=order_size)
                )

        if should_regrid:
            self.last_mid = mid_price
            self.last_regime = regime

        return GridPlan(
            regime=regime,
            mode=mode,
            long_levels=long_levels,
            short_levels=short_levels,
            should_regrid=should_regrid,
            spacing_pct=base_spacing, # Exportáljuk a bázist
            spacing_source=spacing_source,
            atr_pct=atr_pct,
            grid_levels=max(int(levels), 1),
            trend_bias=trend_bias,
            regime_confidence=regime_confidence,
        )

    def _level_counts(self, levels: int, mode: GridMode, *, trend_bias: float = 0.70) -> tuple[int, int]:
        levels = max(int(levels), 1)
        bias = max(0.5, min(float(trend_bias), 0.9))
        if mode == GridMode.LONG_BIASED:
            trend_levels = max(1, round(levels * bias))
            counter_levels = max(1, levels - trend_levels)
            return trend_levels, counter_levels
        if mode == GridMode.SHORT_BIASED:
            trend_levels = max(1, round(levels * bias))
            counter_levels = max(1, levels - trend_levels)
            return counter_levels, trend_levels
        return levels, levels
