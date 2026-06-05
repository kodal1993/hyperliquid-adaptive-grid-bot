from __future__ import annotations

import json
from dataclasses import asdict, dataclass
import logging
from decimal import Decimal
from datetime import datetime, timezone
import time
from pathlib import Path

from .grid_manager import GridPlan
from .hyperliquid_client import HyperliquidClient, normalize_price, normalize_size


@dataclass
class PaperState:
    cash: float
    position_size: float = 0.0
    avg_entry: float = 0.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    daily_start_equity: float = 0.0
    daily_start_realized_pnl: float = 0.0
    daily_start_fees_paid: float = 0.0
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

class PaperExecutionEngine:
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

    def _require_paper_execution(self, action: str) -> None:
        if not self.paper_mode:
            raise RuntimeError(
                f"live_execution_not_implemented: {action} cannot use paper-simulated orders/fills when PAPER_MODE=false"
            )

    def _load_state(self, start_balance: float) -> PaperState:
        if self.state_file.exists():
            return PaperState(**json.loads(self.state_file.read_text()))
        return PaperState(cash=start_balance, daily_start_equity=start_balance)

    def save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(asdict(self.paper), indent=2), encoding="utf-8")

    def cancel_replace_grid(self, symbol: str, plan: GridPlan) -> dict[str, int | str]:
        self._require_paper_execution("cancel_replace_grid")
        if not plan.should_regrid:
            return {"canceled": 0, "placed": 0, "symbol": symbol}
        now_ts = time.time()
        self.open_orders = [{"symbol": symbol, "side": o.side, "price": o.price, "size": o.size, "created_ts": now_ts} for o in (plan.long_levels + plan.short_levels)]
        return {"canceled": 0, "placed": len(self.open_orders), "symbol": symbol}

    def cancel_all_orders(self, symbol: str) -> int:
        self._require_paper_execution("cancel_all_orders")
        before = len(self.open_orders)
        self.open_orders = [o for o in self.open_orders if o.get("symbol") != symbol]
        return before - len(self.open_orders)

    def place_reduce_only_orders(self, symbol: str, position_size: float, mark_price: float, order_size: float, levels: int = 3) -> int:
        self._require_paper_execution("place_reduce_only_orders")
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
            self.open_orders.append({"symbol": symbol, "side": side, "price": px, "size": sz, "reduce_only": True, "created_ts": time.time()})
            remaining -= sz
            placed += 1
        return placed

    def flatten_position(self, symbol: str, mark_price: float) -> bool:
        self._require_paper_execution("flatten_position")
        if abs(self.paper.position_size) < 1e-12:
            return False
        side = "buy" if self.paper.position_size < 0 else "sell"
        self._apply_fill({"symbol": symbol, "side": side, "price": mark_price, "size": abs(self.paper.position_size), "created_ts": time.time()})
        return True

    def on_candle(self, candle: dict, *, regime: str = "unknown", mode: str = "unknown", risk_state: str = "OK", pause_reason: str = "", strategy_status: str = "running") -> list[dict]:
        self._require_paper_execution("on_candle")
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
        self._require_paper_execution("_apply_fill")
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


class LiveExecutionEngine(PaperExecutionEngine):
    """Authenticated Hyperliquid execution engine.

    Live mode never derives fills from candles. Orders are submitted to the
    exchange and trade ledger entries are emitted only from Hyperliquid user
    fills returned by the authenticated account API.
    """

    def _load_state(self, start_balance: float) -> PaperState:
        # Live state stores exchange-fill de-duplication metadata, not simulated cash/PnL.
        return PaperState(cash=start_balance, daily_start_equity=start_balance)

    def __init__(
        self,
        client: HyperliquidClient,
        state_file: str,
        *,
        max_notional_per_trade_usd: float = 10.0,
        min_notional_usd: float = 10.0,
        leverage: int = 1,
        **kwargs,
    ) -> None:
        super().__init__(client=client, state_file=state_file, paper_mode=False, enable_live_trading=True, **kwargs)
        self.max_notional_per_trade_usd = max_notional_per_trade_usd
        self.min_notional_usd = min_notional_usd
        self.leverage = leverage
        self._seen_fill_ids: set[str] = set()
        self._load_live_seen_fills()
        self.client.require_live_execution_support()

    def _require_paper_execution(self, action: str) -> None:
        raise RuntimeError(f"live_execution_forbids_paper_simulation: {action} cannot run in LIVE mode")

    def _load_live_seen_fills(self) -> None:
        if not self.state_file.exists():
            return
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if isinstance(payload, dict):
            self._seen_fill_ids = {str(x) for x in payload.get("seen_fill_ids", [])}
            self.paper.daily_start_equity = float(payload.get("daily_start_equity", self.paper.daily_start_equity) or 0.0)
            self.paper.daily_start_realized_pnl = float(payload.get("daily_start_realized_pnl", self.paper.daily_start_realized_pnl) or 0.0)
            self.paper.daily_start_fees_paid = float(payload.get("daily_start_fees_paid", self.paper.daily_start_fees_paid) or 0.0)
            self.paper.current_day = str(payload.get("current_day", self.paper.current_day) or "")

    def save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "mode": "live",
            "seen_fill_ids": sorted(self._seen_fill_ids)[-1000:],
            "last_real_trade_ts": self.last_real_trade_ts.isoformat() if self.last_real_trade_ts else "",
            "daily_start_equity": self.paper.daily_start_equity,
            "daily_start_realized_pnl": self.paper.daily_start_realized_pnl,
            "daily_start_fees_paid": self.paper.daily_start_fees_paid,
            "current_day": self.paper.current_day,
        }
        self.state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def sync_account(self, symbol: str, mark_price: float | None = None) -> dict:
        balance = self.client.get_balance()
        position = self.client.get_position(symbol)
        size = float(position.get("szi", 0.0) or 0.0)
        entry_px = float(position.get("entryPx", 0.0) or 0.0)
        self.paper.position_size = size
        self.paper.avg_entry = entry_px
        self.paper.cash = float(balance.get("equity", 0.0) or 0.0)
        if mark_price is not None:
            self.paper.daily_start_equity = self.paper.daily_start_equity or self.paper.cash
        self.sync_open_orders(symbol)
        return {"equity": self.paper.cash, "position_size": size, "avg_entry": entry_px, "open_orders": len(self.open_orders)}

    def sync_open_orders(self, symbol: str) -> list[dict]:
        orders: list[dict] = []
        for raw in self.client.get_open_orders(symbol):
            side_raw = str(raw.get("side", "")).lower()
            side = "buy" if side_raw in {"b", "buy"} else "sell"
            orders.append(
                {
                    "symbol": raw.get("coin", symbol),
                    "side": side,
                    "price": float(raw.get("limitPx", raw.get("px", 0.0)) or 0.0),
                    "size": float(raw.get("sz", 0.0) or 0.0),
                    "oid": raw.get("oid"),
                    "reduce_only": bool(raw.get("reduceOnly", False)),
                    "created_ts": self._order_created_ts(raw),
                    "raw": raw,
                }
            )
        self.open_orders = orders
        return self.open_orders


    def _order_created_ts(self, raw: dict) -> float | None:
        for key in ("timestamp", "time", "createdAt", "created_at", "orderTime", "order_time"):
            value = raw.get(key)
            if value is None:
                continue
            try:
                ts = float(value)
            except (TypeError, ValueError):
                continue
            if ts > 1_000_000_000_000:
                return ts / 1000.0
            return ts
        return None

    def cancel_replace_grid(self, symbol: str, plan: GridPlan) -> dict[str, int | str]:
        if not plan.should_regrid:
            self.sync_open_orders(symbol)
            return {"canceled": 0, "placed": 0, "symbol": symbol}
        canceled = self.cancel_all_orders(symbol)
        remaining_after_cancel = self.sync_open_orders(symbol)
        if remaining_after_cancel:
            logger.warning("live_grid_rebuild_skipped reason=cancel_confirmation_pending remaining_open_orders=%s", len(remaining_after_cancel))
            return {"canceled": canceled, "placed": 0, "symbol": symbol, "error": "cancel_confirmation_pending"}
        placed = 0
        for order in plan.long_levels + plan.short_levels:
            if self._submit_live_limit(symbol, order.side, order.size, order.price, reduce_only=False):
                placed += 1
        self.sync_open_orders(symbol)
        return {"canceled": canceled, "placed": placed, "symbol": symbol}

    def cancel_all_orders(self, symbol: str) -> int:
        canceled = self.client.cancel_all_orders(symbol)
        self.sync_open_orders(symbol)
        return canceled

    def place_reduce_only_orders(self, symbol: str, position_size: float, mark_price: float, order_size: float, levels: int = 3) -> int:
        self.sync_account(symbol, mark_price)
        live_position_size = self.paper.position_size
        if abs(live_position_size) < 1e-12:
            return 0
        side = "buy" if live_position_size < 0 else "sell"
        remaining = abs(live_position_size)
        per_level = max(min(order_size, remaining), 0.0)
        placed = 0
        for i in range(levels):
            if remaining <= 1e-12:
                break
            size = min(per_level, remaining)
            price = mark_price * (1 - 0.001 * (i + 1)) if side == "buy" else mark_price * (1 + 0.001 * (i + 1))
            if self._submit_live_limit(symbol, side, size, price, reduce_only=True):
                placed += 1
            remaining -= size
        self.sync_open_orders(symbol)
        return placed

    def flatten_position(self, symbol: str, mark_price: float) -> bool:
        self.sync_account(symbol, mark_price)
        if abs(self.paper.position_size) < 1e-12:
            return False
        side = "buy" if self.paper.position_size < 0 else "sell"
        limit_price = mark_price * 1.002 if side == "buy" else mark_price * 0.998
        return self._submit_live_limit(symbol, side, abs(self.paper.position_size), limit_price, reduce_only=True)

    def on_candle(self, candle: dict, *, regime: str = "unknown", mode: str = "unknown", risk_state: str = "OK", pause_reason: str = "", strategy_status: str = "running") -> list[dict]:
        symbol = str(candle.get("symbol") or "")
        if not symbol and self.open_orders:
            symbol = str(self.open_orders[0].get("symbol", ""))
        # No candle-touch fill simulation in live mode. This only polls exchange fills.
        return self.sync_user_fills(symbol or None, regime=regime, mode=mode, risk_state=risk_state, pause_reason=pause_reason)

    def sync_user_fills(self, symbol: str | None = None, *, regime: str = "unknown", mode: str = "unknown", risk_state: str = "OK", pause_reason: str = "") -> list[dict]:
        fills = self.client.get_user_fills(symbol)
        new_entries: list[dict] = []
        for raw in fills:
            fill_id = self._fill_id(raw)
            if fill_id in self._seen_fill_ids:
                continue
            entry = self._ledger_entry_from_exchange_fill(raw, regime=regime, mode=mode, risk_state=risk_state, pause_reason=pause_reason)
            self._seen_fill_ids.add(fill_id)
            self.paper.realized_pnl += float(entry.get("realized_pnl_delta", 0.0) or 0.0)
            self.paper.fees_paid += float(entry.get("fee", 0.0) or 0.0)
            entry["realized_pnl_total"] = self.paper.realized_pnl
            entry["fees_paid"] = self.paper.fees_paid
            self.trade_log.append(entry)
            self._append_trade_ledger(entry)
            new_entries.append(entry)
        if new_entries:
            self.last_real_trade_ts = datetime.now(timezone.utc)
            if symbol:
                self.sync_account(symbol, None)
            self.save_state()
        return new_entries

    def _submit_live_limit(self, symbol: str, side: str, size: float, price: float, *, reduce_only: bool) -> bool:
        normalized_price = normalize_price(symbol, price)
        normalized_size = normalize_size(symbol, size)
        notional_decimal = abs(Decimal(str(normalized_size)) * Decimal(str(normalized_price)))
        min_notional_decimal = Decimal(str(self.min_notional_usd))
        max_notional_decimal = Decimal(str(self.max_notional_per_trade_usd))

        if normalized_price <= 0 or normalized_size <= 0:
            logger.warning(
                "live_order_skipped reason=non_positive_normalized_order side=%s raw_price=%s raw_size=%s normalized_price=%s normalized_size=%s",
                side,
                price,
                size,
                normalized_price,
                normalized_size,
            )
            return False

        if notional_decimal > max_notional_decimal:
            capped_size_decimal = max_notional_decimal / Decimal(str(normalized_price))
            normalized_size = normalize_size(symbol, capped_size_decimal)
            notional_decimal = abs(Decimal(str(normalized_size)) * Decimal(str(normalized_price)))

        if normalized_size <= 0 or (not reduce_only and notional_decimal < min_notional_decimal):
            logger.warning(
                "live_order_skipped reason=min_notional normalized_price=%s normalized_size=%s notional=%s min_notional_usd=%s reduce_only=%s",
                normalized_price,
                normalized_size,
                notional_decimal,
                self.min_notional_usd,
                reduce_only,
            )
            return False

        if notional_decimal > max_notional_decimal:
            logger.warning(
                "live_order_skipped reason=max_notional normalized_price=%s normalized_size=%s notional=%s max_notional_per_trade_usd=%s",
                normalized_price,
                normalized_size,
                notional_decimal,
                self.max_notional_per_trade_usd,
            )
            return False

        notional = float(notional_decimal)
        if not reduce_only and abs((self.paper.position_size + _signed_order_size(side, normalized_size)) * normalized_price) > self.max_position_notional_usd + 1e-9:
            logger.warning("live_order_blocked reason=max_position_notional side=%s price=%s size=%s", side, normalized_price, normalized_size)
            return False
        logger.info("order_submitted mode=live side=%s price=%s qty=%s notional=%s reduce_only=%s", side, normalized_price, normalized_size, notional, reduce_only)
        try:
            self.client.place_limit_order(symbol, side, normalized_size, normalized_price, reduce_only=reduce_only)
        except ValueError as exc:
            logger.warning("live_order_skipped reason=sdk_wire_rounding_error side=%s price=%s size=%s error=%s", side, normalized_price, normalized_size, exc)
            return False
        return True

    def _fill_id(self, raw: dict) -> str:
        for key in ("tid", "hash", "oid"):
            if raw.get(key) is not None:
                return f"{key}:{raw.get(key)}:{raw.get('time', '')}"
        return json.dumps(raw, sort_keys=True)

    def _ledger_entry_from_exchange_fill(self, raw: dict, *, regime: str, mode: str, risk_state: str, pause_reason: str) -> dict:
        price = float(raw.get("px", raw.get("price", 0.0)) or 0.0)
        size = float(raw.get("sz", raw.get("size", 0.0)) or 0.0)
        side_raw = str(raw.get("side", "")).lower()
        side = "buy" if side_raw in {"b", "buy"} else "sell"
        fee = abs(float(raw.get("fee", 0.0) or 0.0))
        realized_delta = float(raw.get("closedPnl", raw.get("closed_pnl", 0.0)) or 0.0)
        ts_raw = raw.get("time")
        if ts_raw is not None:
            timestamp = datetime.fromtimestamp(float(ts_raw) / 1000, tz=timezone.utc).isoformat()
        else:
            timestamp = datetime.now(timezone.utc).isoformat()
        position_notional = abs(self.paper.position_size * price)
        return {
            "timestamp": timestamp,
            "symbol": raw.get("coin", ""),
            "side": side,
            "price": price,
            "qty": size,
            "notional": abs(price * size),
            "fee": fee,
            "realized_pnl_delta": realized_delta,
            "realized_pnl_total": self.paper.realized_pnl + realized_delta,
            "cash": self.paper.cash,
            "equity": self.paper.cash,
            "position_size": self.paper.position_size,
            "position_notional": position_notional,
            "regime": regime,
            "mode": mode,
            "risk_state": risk_state,
            "pause_reason": pause_reason,
            "reason": "exchange_fill",
        }


# Backwards-compatible public name used by existing tests and integrations.
ExecutionEngine = PaperExecutionEngine
