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

def _signed_order_size(side: str, qty: float) -> float:
    return qty if side.lower() == "buy" else -qty


def classify_exposure_action(position_size: float, side: str, qty: float) -> str:
    side = side.lower()
    qty = abs(qty)
    if qty <= 0:
        return "flat"
    signed_order = _signed_order_size(side, qty)
    pos_after = position_size + signed_order

    if abs(position_size) < 1e-12:
        return "increasing"
    if abs(pos_after) < 1e-12:
        return "flat"
    if position_size * pos_after < 0:
        return "flipping"
    if abs(pos_after) > abs(position_size):
        return "increasing"
    return "reducing"


def would_increase_exposure(position_size: float, side: str, qty: float) -> bool:
    return classify_exposure_action(position_size, side, qty) == "increasing"

class ExecutionEngine:
    def __init__(self, client: HyperliquidClient, state_file: str, paper_mode: bool = True, enable_live_trading: bool = False, fee_rate: float = 0.0004, start_balance: float = 500.0, fill_model: str = "optimistic", max_position_notional_usd: float = 500.0, soft_exposure_cap_pct: float = 0.6, hard_exposure_cap_pct: float = 0.8, absolute_exposure_cap_pct: float = 1.0, allow_position_flip: bool = False, trade_ledger_csv: str | None = None, trade_ledger_jsonl: str | None = None, risk_decisions_csv: str | None = None) -> None:
        self.client = client
        self.paper_mode = paper_mode
        self.enable_live_trading = enable_live_trading
        self.fee_rate = fee_rate
        self.state_file = Path(state_file)
        self.open_orders: list[dict] = []
        self.fill_model = fill_model
        self.paper = self._load_state(start_balance)
        self.trade_log: list[dict] = []
        self.trade_ledger_csv = Path(trade_ledger_csv) if trade_ledger_csv else Path("logs/trades.csv")
        self.trade_ledger_jsonl = Path(trade_ledger_jsonl) if trade_ledger_jsonl else Path("logs/trades.jsonl")
        self.risk_decisions_csv = Path(risk_decisions_csv) if risk_decisions_csv else Path("logs/risk_decisions.csv")
        self.max_position_notional_usd = max_position_notional_usd
        self.soft_exposure_cap_pct = soft_exposure_cap_pct
        self.hard_exposure_cap_pct = hard_exposure_cap_pct
        self.absolute_exposure_cap_pct = absolute_exposure_cap_pct
        self.allow_position_flip = allow_position_flip
        self.no_fill_cycles = 0
        self.last_real_trade_ts: datetime | None = None

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

    def on_candle(self, candle: dict, *, regime: str = "unknown", mode: str = "unknown", risk_state: str = "OK", pause_reason: str = "", strategy_status: str = "running") -> list[dict]:
        high, low = float(candle["high"]), float(candle["low"])
        mark_price = float(candle.get("close", candle.get("open", high)))
        candidate_fills, fill_meta = self._pick_fills(candle, high, low, mark_price)
        if not candidate_fills:
            self.no_fill_cycles += 1
            logger.info(
                "fill_model_result model=%s touched=%s selected=%s reason=%s position_before=%s position_notional_before=%s",
                self.fill_model,
                fill_meta["touched_count"],
                fill_meta["selected_count"],
                fill_meta["reason"],
                self.paper.position_size,
                abs(self.paper.position_size * mark_price),
            )
        executed_fills: list[dict] = []
        for original_fill in candidate_fills:
            fill = self._resize_close_only_fill(original_fill, mark_price)
            if fill is None:
                continue
            position_before = self.paper.position_size
            position_notional_before = abs(position_before * mark_price)
            logger.info(
                "candidate_fill side=%s price=%s qty=%s position_before=%s position_notional_before=%s",
                fill["side"],
                fill["price"],
                fill["size"],
                position_before,
                position_notional_before,
            )
            decision, block_reason = self._risk_decision(fill, mark_price, risk_state, pause_reason, regime, mode, strategy_status)
            logger.info(
                "risk_decision_result decision=%s block_reason=%s side=%s price=%s qty=%s",
                decision,
                block_reason or "none",
                fill["side"],
                fill["price"],
                fill["size"],
            )
            if decision == "block":
                logger.info("order_skipped reason=risk_block side=%s price=%s qty=%s", fill["side"], fill["price"], fill["size"])
                continue
            logger.info("order_submitted mode=paper side=%s price=%s qty=%s", fill["side"], fill["price"], fill["size"])
            self._apply_fill(fill, regime=regime, mode=mode, risk_state=risk_state, pause_reason=pause_reason)
            logger.info(
                "fill_executed side=%s price=%s qty=%s position_before=%s position_after=%s",
                fill["side"],
                fill["price"],
                fill["size"],
                position_before,
                self.paper.position_size,
            )
            executed_fills.append(fill)
            self.open_orders.remove(original_fill)
        if executed_fills:
            self.no_fill_cycles = 0
            self.last_real_trade_ts = datetime.now(timezone.utc)
        self.save_state()
        return executed_fills

    def _resize_close_only_fill(self, fill: dict, mark_price: float) -> dict | None:
        side = str(fill["side"]).lower()
        qty = float(fill["size"])
        action = classify_exposure_action(self.paper.position_size, side, qty)
        if action != "flipping" or self.allow_position_flip:
            return fill
        close_qty = abs(self.paper.position_size)
        if close_qty <= 1e-12:
            return None
        resized = dict(fill)
        resized["size"] = min(qty, close_qty)
        logger.info(
            "close_only_resize_applied side=%s original_qty=%s resized_qty=%s position_before=%s position_notional_before=%s reason=flip_disabled_close_only",
            side,
            qty,
            resized["size"],
            self.paper.position_size,
            abs(self.paper.position_size * mark_price),
        )
        return resized

    def _pick_fills(self, candle: dict, high: float, low: float, mark_price: float) -> tuple[list[dict], dict]:
        touched = [o for o in self.open_orders if low <= o["price"] <= high]
        meta = {"touched_count": len(touched), "selected_count": 0, "reason": "none"}
        if not touched:
            meta["reason"] = "no_orders_touched_in_candle_range"
            return [], meta
        if self.fill_model == "optimistic":
            meta["selected_count"] = len(touched)
            meta["reason"] = "optimistic_fill_all_touched"
            return touched, meta
        if self.fill_model == "conservative":
            buys = sorted([o for o in touched if o["side"] == "buy"], key=lambda x: x["price"], reverse=True)
            sells = sorted([o for o in touched if o["side"] == "sell"], key=lambda x: x["price"])
            selected = ([buys[0]] if buys else []) + ([sells[0]] if sells else [])
            selected = self._maybe_apply_emergency_reduce_fill(selected, touched, mark_price)
            meta["selected_count"] = len(selected)
            meta["reason"] = "conservative_best_per_side_with_emergency_reduce_override"
            return selected, meta
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
        result = self._maybe_apply_emergency_reduce_fill(result, touched, mark_price)
        meta["selected_count"] = len(result)
        meta["reason"] = "path_fill_with_emergency_reduce_override"
        return result, meta

    def _maybe_apply_emergency_reduce_fill(self, selected: list[dict], touched: list[dict], mark_price: float) -> list[dict]:
        if not self.paper_mode:
            return selected
        max_n = max(self.max_position_notional_usd, 1e-9)
        position_notional = abs(self.paper.position_size * mark_price)
        if position_notional <= 0.8 * max_n:
            return selected
        emergency_selected = list(selected)
        for order in touched:
            if order in emergency_selected:
                continue
            if classify_exposure_action(self.paper.position_size, str(order["side"]).lower(), float(order.get("size", 0.0))) in {"reducing", "flat"}:
                emergency_selected.append(order)
        if len(emergency_selected) > len(selected):
            logger.info(
                "emergency_reduce_override enabled=true paper_mode=%s position_notional=%s threshold=%s added_exposure_reducing_orders=%s",
                self.paper_mode,
                position_notional,
                0.8 * max_n,
                len(emergency_selected) - len(selected),
            )
        return emergency_selected

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

    def _risk_decision(self, fill: dict, mark_price: float, risk_state: str, pause_reason: str, regime: str, mode: str, strategy_status: str = "running") -> tuple[str, str]:
        side = str(fill["side"]).lower()
        price, qty = float(fill["price"]), float(fill["size"])
        order_notional = abs(price * qty)
        position_size_before = self.paper.position_size
        position_notional_before = abs(position_size_before * mark_price)
        exposure_action = classify_exposure_action(position_size_before, side, qty)
        would_inc = exposure_action == "increasing"
        exposure_reducing_override = exposure_action in {"reducing", "flat"}
        max_n = max(self.max_position_notional_usd, 1e-9)
        exposure_ratio = position_notional_before / max_n

        decision = "allow"
        block_reason = ""
        estimated_position_notional_after = abs((position_size_before + _signed_order_size(side, qty)) * mark_price)

        allow_reason = ""
        risk_state_before = risk_state
        if exposure_action in {"reducing", "flat"}:
            decision, block_reason = "allow", ""
            if risk_state == "BLOCKED":
                allow_reason = "exposure_reducing_override"
        elif exposure_action == "flipping":
            decision, block_reason = ("allow", "") if self.allow_position_flip else ("block", "position_flip_blocked")
        else:
            if exposure_ratio >= self.absolute_exposure_cap_pct:
                decision, block_reason = "block", "absolute_exposure_cap"
            elif exposure_ratio >= self.hard_exposure_cap_pct:
                decision, block_reason = "block", "hard_exposure_cap"
            elif exposure_ratio >= self.soft_exposure_cap_pct:
                decision, block_reason = "block", "soft_exposure_cap"
            elif estimated_position_notional_after > self.max_position_notional_usd:
                decision, block_reason = "block", "max_position_notional"

        if exposure_action == "increasing" and (risk_state == "BLOCKED" or pause_reason in {"one_direction_exposure", "max_position_notional", "emergency_stop", "max_drawdown", "daily_loss_limit", "daily_loss", "liquidation_risk", "liq_distance"}):
            decision, block_reason = "block", pause_reason or "risk_state_blocked"

        payload = {"symbol": fill.get("symbol", ""), "side": side, "price": price, "qty": qty, "order_notional": order_notional, "position_size_before": position_size_before, "position_notional_before": position_notional_before, "estimated_position_notional_after": estimated_position_notional_after, "max_position_notional_usd": self.max_position_notional_usd, "exposure_ratio": exposure_ratio, "would_increase_exposure": would_inc, "exposure_action": exposure_action, "exposure_reducing_override": exposure_reducing_override, "risk_state": risk_state, "risk_state_before": risk_state_before, "strategy_status": strategy_status, "pause_reason": pause_reason, "decision": decision, "allow_reason": allow_reason, "block_reason": block_reason, "regime": regime, "mode": mode}
        self._append_risk_decision(payload)
        logger.info("risk_decision symbol=%s side=%s price=%s qty=%s order_notional=%s position_notional_before=%s max_position_notional_usd=%s exposure_ratio=%.3f would_increase_exposure=%s exposure_reducing_override=%s risk_state_before=%s risk_state=%s strategy_status=%s pause_reason=%s decision=%s allow_reason=%s block_reason=%s", payload["symbol"], side, price, qty, order_notional, position_notional_before, self.max_position_notional_usd, exposure_ratio, would_inc, exposure_reducing_override, risk_state_before, risk_state, payload["strategy_status"], pause_reason or "none", decision, allow_reason or "none", block_reason or "none")
        return decision, block_reason

    def _append_risk_decision(self, entry: dict) -> None:
        self.risk_decisions_csv.parent.mkdir(parents=True, exist_ok=True)
        fields = ["timestamp", "symbol", "side", "price", "qty", "order_notional", "position_size_before", "position_notional_before", "estimated_position_notional_after", "max_position_notional_usd", "exposure_ratio", "would_increase_exposure", "exposure_action", "exposure_reducing_override", "risk_state_before", "risk_state", "strategy_status", "pause_reason", "decision", "allow_reason", "block_reason", "regime", "mode"]
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
