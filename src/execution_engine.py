from __future__ import annotations

import json
from dataclasses import asdict, dataclass
import logging
from datetime import datetime, timezone
import time
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


logger = logging.getLogger(__name__)

def would_increase_exposure(position_size: float, side: str) -> bool:
    side = side.lower()
    if position_size < 0 and side == "sell":
        return True
    if position_size < 0 and side == "buy":
        return False
    if position_size > 0 and side == "buy":
        return True
    if position_size > 0 and side == "sell":
        return False
    return True

class ExecutionEngine:
    def __init__(self, client: HyperliquidClient, state_file: str, paper_mode: bool = True, enable_live_trading: bool = False, fee_rate: float = 0.0004, start_balance: float = 500.0, fill_model: str = "optimistic", max_position_notional_usd: float = 500.0, soft_exposure_cap_pct: float = 0.6, hard_exposure_cap_pct: float = 0.8, absolute_exposure_cap_pct: float = 1.0) -> None:
        self.client = client
        self.paper_mode = paper_mode
        self.enable_live_trading = enable_live_trading
        self.fee_rate = fee_rate
        self.state_file = Path(state_file)
        self.open_orders: list[dict] = []
        self.fill_model = fill_model
        self.paper = self._load_state(start_balance)
        self.trade_log: list[dict] = []
        self.trade_ledger_csv = Path("logs/trades.csv")
        self.trade_ledger_jsonl = Path("logs/trades.jsonl")
        self.risk_decisions_csv = Path("logs/risk_decisions.csv")
        self.max_position_notional_usd = max_position_notional_usd
        self.soft_exposure_cap_pct = soft_exposure_cap_pct
        self.hard_exposure_cap_pct = hard_exposure_cap_pct
        self.absolute_exposure_cap_pct = absolute_exposure_cap_pct

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

    def on_candle(self, candle: dict, *, regime: str = "unknown", mode: str = "unknown", risk_state: str = "OK", pause_reason: str = "") -> list[dict]:
        high, low = float(candle["high"]), float(candle["low"])
        mark_price = float(candle.get("close", candle.get("open", high)))
        candidate_fills = self._pick_fills(candle, high, low)
        executed_fills: list[dict] = []
        for fill in candidate_fills:
            decision, block_reason = self._risk_decision(fill, mark_price, risk_state, pause_reason, regime, mode)
            if decision == "block":
                continue
            self._apply_fill(fill, regime=regime, mode=mode, risk_state=risk_state, pause_reason=pause_reason)
            executed_fills.append(fill)
            self.open_orders.remove(fill)
        self.save_state()
        return executed_fills

    def _pick_fills(self, candle: dict, high: float, low: float) -> list[dict]:
        touched = [o for o in self.open_orders if low <= o["price"] <= high]
        if self.fill_model == "optimistic":
            return touched
        if self.fill_model == "conservative":
            buys = sorted([o for o in touched if o["side"] == "buy"], key=lambda x: x["price"], reverse=True)
            sells = sorted([o for o in touched if o["side"] == "sell"], key=lambda x: x["price"])
            return ([buys[0]] if buys else []) + ([sells[0]] if sells else [])
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

    def _apply_fill(self, fill: dict, reason: str = "grid_fill", regime: str = "unknown", mode: str = "unknown", risk_state: str = "OK", pause_reason: str = "") -> None:
        price, size = float(fill["price"]), float(fill["size"])
        position_before = self.paper.position_size
        realized_before = self.paper.realized_pnl
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

        realized_delta = self.paper.realized_pnl - realized_before
        position_notional = abs(self.paper.position_size * price)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": fill.get("symbol", ""),
            "side": fill["side"],
            "price": price,
            "qty": size,
            "notional": abs(price * size),
            "fee": fee,
            "realized_pnl_delta": realized_delta,
            "realized_pnl_total": self.paper.realized_pnl,
            "cash": self.paper.cash,
            "equity": self.equity(price),
            "position_before": position_before,
            "position_after": self.paper.position_size,
            "position_size": self.paper.position_size,
            "position_notional": position_notional,
            "regime": regime,
            "mode": mode,
            "risk_state": risk_state,
            "pause_reason": pause_reason,
            "reason": reason,
        }
        self.trade_log.append(entry)
        self._append_trade_ledger(entry)

    def _risk_decision(self, fill: dict, mark_price: float, risk_state: str, pause_reason: str, regime: str, mode: str) -> tuple[str, str]:
        side = str(fill["side"]).lower()
        price, qty = float(fill["price"]), float(fill["size"])
        order_notional = abs(price * qty)
        position_size_before = self.paper.position_size
        position_notional_before = abs(position_size_before * mark_price)
        would_inc = would_increase_exposure(position_size_before, side)
        max_n = max(self.max_position_notional_usd, 1e-9)
        exposure_ratio = position_notional_before / max_n

        decision = "allow"
        block_reason = ""
        if would_inc:
            if exposure_ratio >= self.absolute_exposure_cap_pct:
                decision, block_reason = "block", "absolute_exposure_cap"
            elif exposure_ratio >= self.hard_exposure_cap_pct:
                decision, block_reason = "block", "hard_exposure_cap"
            elif exposure_ratio >= self.soft_exposure_cap_pct:
                decision, block_reason = "block", "soft_exposure_cap"
            elif (position_notional_before + order_notional) > self.max_position_notional_usd:
                decision, block_reason = "block", "max_position_notional"

        if would_inc and (risk_state == "BLOCKED" or pause_reason in {"one_direction_exposure", "max_position_notional"}):
            decision, block_reason = "block", pause_reason or "risk_state_blocked"

        payload = {"symbol": fill.get("symbol", ""), "side": side, "price": price, "qty": qty, "order_notional": order_notional, "position_size_before": position_size_before, "position_notional_before": position_notional_before, "max_position_notional_usd": self.max_position_notional_usd, "exposure_ratio": exposure_ratio, "would_increase_exposure": would_inc, "risk_state_before": risk_state, "pause_reason_before": pause_reason, "decision": decision, "block_reason": block_reason, "regime": regime, "mode": mode}
        self._append_risk_decision(payload)
        logger.info("risk_decision symbol=%s side=%s price=%s qty=%s order_notional=%s position_notional_before=%s max_position_notional_usd=%s exposure_ratio=%.3f would_increase_exposure=%s risk_state_before=%s pause_reason_before=%s decision=%s block_reason=%s", payload["symbol"], side, price, qty, order_notional, position_notional_before, self.max_position_notional_usd, exposure_ratio, would_inc, risk_state, pause_reason or "none", decision, block_reason or "none")
        return decision, block_reason

    def _append_risk_decision(self, entry: dict) -> None:
        self.risk_decisions_csv.parent.mkdir(parents=True, exist_ok=True)
        fields = ["timestamp", "symbol", "side", "price", "qty", "order_notional", "position_size_before", "position_notional_before", "max_position_notional_usd", "exposure_ratio", "would_increase_exposure", "risk_state_before", "pause_reason_before", "decision", "block_reason", "regime", "mode"]
        write_header = not self.risk_decisions_csv.exists()
        row = {"timestamp": datetime.now(timezone.utc).isoformat(), **entry}
        with self.risk_decisions_csv.open("a", encoding="utf-8") as f:
            if write_header:
                f.write(",".join(fields) + "\n")
            f.write(",".join(str(row.get(k, "")) for k in fields) + "\n")

    def unrealized_pnl(self, mark_price: float) -> float:
        if self.paper.position_size == 0:
            return 0.0
        return (mark_price - self.paper.avg_entry) * self.paper.position_size

    def equity(self, mark_price: float) -> float:
        return self.paper.cash + self.unrealized_pnl(mark_price)

    def _append_trade_ledger(self, entry: dict) -> None:
        self.trade_ledger_csv.parent.mkdir(parents=True, exist_ok=True)
        fields = ["timestamp", "symbol", "side", "price", "qty", "notional", "fee", "realized_pnl_delta", "realized_pnl_total", "cash", "equity", "position_size", "position_notional", "regime", "mode", "risk_state", "pause_reason", "reason"]
        write_header = not self.trade_ledger_csv.exists()
        with self.trade_ledger_csv.open("a", encoding="utf-8") as f:
            if write_header:
                f.write(",".join(fields) + "\n")
            f.write(",".join(str(entry.get(k, "")) for k in fields) + "\n")
        with self.trade_ledger_jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def consume_trade_log(self) -> list[dict]:
        out = list(self.trade_log)
        self.trade_log.clear()
        return out
