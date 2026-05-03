from __future__ import annotations

import csv
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from .grid_manager import GridPlan
from .hyperliquid_client import HyperliquidClient


@dataclass
class PaperState:
    cash: float
    position_size: float = 0.0
    avg_entry: float = 0.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    daily_start_equity: float = 0.0
    current_day: str = ""


class ExecutionEngine:
    def __init__(self, client: HyperliquidClient, state_file: str, paper_mode: bool = True, enable_live_trading: bool = False, fee_rate: float = 0.0004, start_balance: float = 500.0, fill_model: str = "optimistic") -> None:
        self.client = client
        self.paper_mode = paper_mode
        self.enable_live_trading = enable_live_trading
        self.fee_rate = fee_rate
        self.state_file = Path(state_file)
        self.open_orders: list[dict] = []
        self.fill_model = fill_model
        self.paper = self._load_state(start_balance)

    def _load_state(self, start_balance: float) -> PaperState:
        if self.state_file.exists():
            return PaperState(**json.loads(self.state_file.read_text()))
        return PaperState(cash=start_balance, daily_start_equity=start_balance)

    def save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(asdict(self.paper), indent=2), encoding="utf-8")

    def cancel_replace_grid(self, symbol: str, plan: GridPlan) -> dict[str, int | str]:
        if not plan.should_regrid:
            return {"canceled": 0, "placed": 0, "symbol": symbol}
        self.open_orders = [{"symbol": symbol, "side": o.side, "price": o.price, "size": o.size} for o in (plan.long_levels + plan.short_levels)]
        return {"canceled": 0, "placed": len(self.open_orders), "symbol": symbol}

    def cancel_all_orders(self, symbol: str) -> int:
        before = len(self.open_orders)
        self.open_orders = [o for o in self.open_orders if o.get("symbol") != symbol]
        return before - len(self.open_orders)


    def place_reduce_only_orders(self, symbol: str, position_size: float, mark_price: float, order_size: float, levels: int = 3) -> int:
        if abs(position_size) < 1e-12:
            return 0
        side = "buy" if position_size < 0 else "sell"
        remaining = abs(position_size)
        per_level = max(min(order_size, remaining), 0.0)
        placed = 0
        for i in range(levels):
            if remaining <= 1e-12:
                break
            sz = min(per_level, remaining)
            px = mark_price * (1 - 0.001 * (i + 1)) if side == "buy" else mark_price * (1 + 0.001 * (i + 1))
            self.open_orders.append({"symbol": symbol, "side": side, "price": px, "size": sz, "reduce_only": True})
            remaining -= sz
            placed += 1
        return placed

    def flatten_position(self, symbol: str, mark_price: float) -> bool:
        if abs(self.paper.position_size) < 1e-12:
            return False
        side = "buy" if self.paper.position_size < 0 else "sell"
        self._apply_fill({"symbol": symbol, "side": side, "price": mark_price, "size": abs(self.paper.position_size)})
        return True
    def on_candle(self, candle: dict) -> list[dict]:
        high, low = float(candle["high"]), float(candle["low"])
        fills = self._pick_fills(candle, high, low)
        for fill in fills:
            self._apply_fill(fill)
            self.open_orders.remove(fill)
        self.save_state()
        return fills

    def _pick_fills(self, candle: dict, high: float, low: float) -> list[dict]:
        touched = [o for o in self.open_orders if low <= o["price"] <= high]
        if self.fill_model == "optimistic":
            return touched
        if self.fill_model == "conservative":
            buys = sorted([o for o in touched if o["side"] == "buy"], key=lambda x: x["price"], reverse=True)
            sells = sorted([o for o in touched if o["side"] == "sell"], key=lambda x: x["price"])
            return ([buys[0]] if buys else []) + ([sells[0]] if sells else [])
        # ohlc_path
        open_p = float(candle.get("open", candle["close"]))
        close_p = float(candle["close"])
        path = [open_p, high, low, close_p] if close_p >= open_p else [open_p, low, high, close_p]
        result: list[dict] = []
        for order in sorted(touched, key=lambda x: x["price"]):
            for i in range(1, len(path)):
                lo, hi = min(path[i - 1], path[i]), max(path[i - 1], path[i])
                if lo <= order["price"] <= hi:
                    result.append(order)
                    break
        return result

    def _apply_fill(self, fill: dict) -> None:
        price, size = fill["price"], fill["size"]
        signed = size if fill["side"] == "buy" else -size
        fee = abs(price * size) * self.fee_rate
        self.paper.fees_paid += fee
        self.paper.cash -= fee
        new_pos = self.paper.position_size + signed
        if self.paper.position_size == 0 or (self.paper.position_size > 0 and signed > 0) or (self.paper.position_size < 0 and signed < 0):
            self.paper.avg_entry = (abs(self.paper.avg_entry * self.paper.position_size) + abs(price * signed)) / max(abs(new_pos), 1e-9)
        else:
            closed = min(abs(self.paper.position_size), abs(signed))
            pnl = (price - self.paper.avg_entry) * closed * (1 if self.paper.position_size > 0 else -1)
            self.paper.realized_pnl += pnl
            self.paper.cash += pnl
            if abs(signed) > abs(self.paper.position_size):
                self.paper.avg_entry = price
            elif abs(new_pos) < 1e-12:
                self.paper.avg_entry = 0.0
        self.paper.position_size = new_pos
        if abs(self.paper.position_size) < 1e-12:
            self.paper.position_size = 0.0
            self.paper.avg_entry = 0.0

    def unrealized_pnl(self, mark_price: float) -> float:
        if self.paper.position_size == 0:
            return 0.0
        return (mark_price - self.paper.avg_entry) * self.paper.position_size

    def equity(self, mark_price: float) -> float:
        return self.paper.cash + self.unrealized_pnl(mark_price)
