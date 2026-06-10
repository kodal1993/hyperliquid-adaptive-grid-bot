from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class PositionState:
    net_size: float
    unrealized_pnl: float


class PositionManager:
    def summarize(self, positions: list[dict]) -> PositionState:
        net_size = sum(float(p.get("size", 0.0)) for p in positions)
        pnl = sum(float(p.get("unrealizedPnl", 0.0)) for p in positions)
        return PositionState(net_size=net_size, unrealized_pnl=pnl)

    def classify_side(self, position_size: float) -> str:
        if position_size > 0:
            return "LONG"
        if position_size < 0:
            return "SHORT"
        return "FLAT"

    def is_dust(self, position_notional: float, threshold_usd: float) -> bool:
        return 0.0 < abs(position_notional) < threshold_usd

    def effective_state(
        self, position_size: float, position_notional: float, dust_threshold_usd: float
    ) -> tuple[float, float, str]:
        if self.is_dust(position_notional, dust_threshold_usd):
            return 0.0, 0.0, "FLAT"
        effective_size = float(position_size)
        effective_notional = abs(float(position_notional))
        return effective_size, effective_notional, self.classify_side(effective_size)

    def unrealized_pnl_pct(self, position_size: float, avg_entry: float, mark_price: float) -> float:
        if abs(position_size) <= 1e-12 or avg_entry <= 0 or mark_price <= 0:
            return 0.0
        if position_size > 0:
            return (mark_price - avg_entry) / avg_entry
        return (avg_entry - mark_price) / avg_entry

    def exposure_pct(self, position_notional: float, equity: float) -> float:
        if equity <= 1e-9:
            return 0.0
        return abs(position_notional) / equity
