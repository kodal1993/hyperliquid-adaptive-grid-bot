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
