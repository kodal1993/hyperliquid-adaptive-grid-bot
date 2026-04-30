from __future__ import annotations

from .grid_manager import GridPlan
from .hyperliquid_client import HyperliquidClient


class ExecutionEngine:
    def __init__(self, client: HyperliquidClient, paper_mode: bool = True) -> None:
        self.client = client
        self.paper_mode = paper_mode

    def cancel_replace_grid(self, symbol: str, plan: GridPlan) -> dict[str, int]:
        # Placeholder for batch cancel + replace orders.
        total_orders = len(plan.long_levels) + len(plan.short_levels)
        return {"canceled": 0, "placed": total_orders, "symbol": symbol}
