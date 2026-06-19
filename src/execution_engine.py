from __future__ import annotations

import json
from dataclasses import asdict, dataclass
import logging
import re
from decimal import Decimal
from datetime import datetime, timezone
import time
from pathlib import Path

from .grid_manager import GridPlan
from .hyperliquid_client import HyperliquidClient, normalize_price, normalize_size


@dataclass
class AccountState:
    state_source: str
    equity: float
    position_size: float
    avg_entry: float
    position_notional: float
    unrealized_pnl: float
    realized_pnl: float
    fees_paid: float
    daily_start_equity: float
    daily_start_realized_pnl: float
    daily_start_fees_paid: float
    current_day: str


@dataclass
class ExecutionState:
    equity: float
    position_size: float = 0.0
    avg_entry: float = 0.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    daily_start_equity: float = 0.0
    daily_start_realized_pnl: float = 0.0
    daily_start_fees_paid: float = 0.0
    bot_position_size: float = 0.0
    bot_realized_pnl: float = 0.0
    bot_fees_paid: float = 0.0
    bot_daily_realized_pnl: float = 0.0
    bot_daily_fees_paid: float = 0.0
    current_day: str = ""

    @property
    def cash(self) -> float:
        return self.equity

    @cash.setter
    def cash(self, value: float) -> None:
        self.equity = value


logger = logging.getLogger(__name__)

def _signed_order_size(side: str, qty: float) -> float:
    return qty if side.lower() == "buy" else -qty


def _classify_fill_liquidity(raw: dict | None = None, *, default: str = "maker") -> str:
    """Classify a fill as maker or taker from exchange metadata when available."""
    raw = raw or {}
    for key in ("liquidity", "liquidityType", "liquidity_type", "fillType", "fill_type"):
        value = str(raw.get(key, "")).lower()
        if "maker" in value or value in {"m", "add", "added"}:
            return "maker"
        if "taker" in value or value in {"t", "remove", "removed"}:
            return "taker"
    for key in ("maker", "isMaker", "is_maker"):
        if key in raw:
            return "maker" if bool(raw.get(key)) else "taker"
    return default if default in {"maker", "taker"} else "unknown"


def _is_dust_notional(notional: float, threshold_usd: float = 1.0) -> bool:
    return 0.0 < abs(notional) < threshold_usd


def _diff_grid_orders(
    active_orders: list[dict],
    desired_levels: list[dict],
    price_tolerance_pct: float,
    size_tolerance_pct: float = 0.05,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Match desired grid levels against active orders to minimize churn.

    Returns (keep, cancel, place): active orders close enough to a desired
    level are kept, unmatched active orders are canceled, and unmatched
    desired levels are placed. Greedy nearest-price matching per side.
    """
    unmatched_active = list(active_orders)
    keep: list[dict] = []
    place: list[dict] = []
    for level in desired_levels:
        target_price = float(level.get("price", 0.0) or 0.0)
        target_size = float(level.get("size", 0.0) or 0.0)
        best = None
        best_delta = None
        for order in unmatched_active:
            if str(order.get("side", "")).lower() != str(level.get("side", "")).lower():
                continue
            order_price = float(order.get("price", 0.0) or 0.0)
            order_size = float(order.get("size", 0.0) or 0.0)
            price_delta = abs(order_price - target_price) / max(target_price, 1e-9)
            if price_delta > price_tolerance_pct:
                continue
            if abs(order_size - target_size) / max(target_size, 1e-9) > size_tolerance_pct:
                continue
            if best is None or price_delta < best_delta:
                best, best_delta = order, price_delta
        if best is not None:
            unmatched_active.remove(best)
            keep.append(best)
        else:
            place.append(level)
    return keep, unmatched_active, place


def _grid_plan_center(plan: GridPlan) -> float:
    """Approximate the mid the grid was built around, for fill-proximity sorting."""
    buys = [float(level.price) for level in plan.long_levels]
    sells = [float(level.price) for level in plan.short_levels]
    if buys and sells:
        return (max(buys) + min(sells)) / 2.0
    if buys:
        return max(buys)
    if sells:
        return min(sells)
    return 0.0


def _order_submit_error(response: object) -> str:
    """Extract an error message from a Hyperliquid order response, if any.

    Returns "" when the response indicates success or carries no status info
    (e.g. test doubles returning None).
    """
    if not isinstance(response, dict):
        return ""
    if str(response.get("status", "ok")).lower() != "ok":
        return str(response.get("response") or response.get("status"))
    inner = response.get("response")
    if isinstance(inner, dict):
        data = inner.get("data")
        statuses = data.get("statuses") if isinstance(data, dict) else None
        if isinstance(statuses, list):
            for status in statuses:
                if isinstance(status, dict) and status.get("error"):
                    return str(status["error"])
    return ""



def _extract_order_oid(response: object) -> int | None:
    """Best-effort extraction of the exchange order id from an SDK response."""
    if isinstance(response, dict):
        for key in ("oid", "orderId", "order_id"):
            if response.get(key) is not None:
                try:
                    return int(response[key])
                except (TypeError, ValueError):
                    pass
        for value in response.values():
            found = _extract_order_oid(value)
            if found is not None:
                return found
    elif isinstance(response, list):
        for item in response:
            found = _extract_order_oid(item)
            if found is not None:
                return found
    return None


def _is_post_only_reject(error: str) -> bool:
    lowered = error.lower()
    return "post only" in lowered or "alo" in lowered or "immediately match" in lowered


def _is_cumulative_rate_limit_error(error: str) -> bool:
    lowered = error.lower()
    request_budget_markers = (
        "too many cumulative requests",
        "cumulative volume traded",
        "cumulative request",
        "request payload",
        "free up",
        "request per usdc",
    )
    return any(marker in lowered for marker in request_budget_markers) and ("request" in lowered or "cumulative" in lowered)


def _extract_request_deficit(error: str) -> float:
    """Return a best-effort Hyperliquid cumulative request deficit from an error."""
    match = re.search(r"\((\d+(?:\.\d+)?)\s*>\s*(\d+(?:\.\d+)?)\)", error)
    if match:
        used = float(match.group(1))
        allowed = float(match.group(2))
        return max(used - allowed, 0.0)
    match = re.search(r"deficit[^0-9]*(\d+(?:\.\d+)?)", error, flags=re.IGNORECASE)
    if match:
        return max(float(match.group(1)), 0.0)
    return 0.0


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


def trade_category_for_action(exposure_action: str) -> str:
    return {
        "increasing": "entry",
        "reducing": "reduce",
        "flat": "close",
        "flipping": "flip",
    }.get(exposure_action, exposure_action or "unknown")

class PaperExecutionEngine:
    def __init__(self, client: HyperliquidClient, state_file: str, paper_mode: bool = True, enable_live_trading: bool = False, fee_rate: float = 0.0004, start_balance: float = 500.0, fill_model: str = "optimistic", max_position_notional_usd: float = 500.0, soft_exposure_cap_pct: float = 0.6, hard_exposure_cap_pct: float = 0.8, absolute_exposure_cap_pct: float = 1.0, allow_position_flip: bool = False, min_order_notional_usd: float | None = None, dust_position_notional_usd: float = 2.0, trade_ledger_csv: str | None = None, trade_ledger_jsonl: str | None = None, risk_decisions_csv: str | None = None, min_order_lifetime_seconds: int = 90, min_reprice_distance_pct: float = 0.0015, maker_fee_rate: float | None = None, taker_fee_rate: float | None = None, paired_take_profit_enabled: bool = True, paired_tp_spacing_multiplier: float = 1.0) -> None:
        self.client = client
        self.paper_mode = paper_mode
        self.enable_live_trading = enable_live_trading
        self.fee_rate = fee_rate
        self.maker_fee_rate = float(maker_fee_rate if maker_fee_rate is not None else fee_rate)
        self.taker_fee_rate = float(taker_fee_rate if taker_fee_rate is not None else fee_rate)
        self.state_file = Path(state_file)
        self.open_orders: list[dict] = []
        self.fill_model = fill_model
        self.state = self._load_state(start_balance)
        # Compatibility for unit tests and offline simulation utilities only.
        self.paper = self.state
        self.trade_log: list[dict] = []
        self.trade_ledger_csv = Path(trade_ledger_csv) if trade_ledger_csv else Path("logs/trades.csv")
        self.trade_ledger_jsonl = Path(trade_ledger_jsonl) if trade_ledger_jsonl else Path("logs/trades.jsonl")
        self.risk_decisions_csv = Path(risk_decisions_csv) if risk_decisions_csv else Path("logs/risk_decisions.csv")
        self.max_position_notional_usd = max_position_notional_usd
        self.soft_exposure_cap_pct = soft_exposure_cap_pct
        self.hard_exposure_cap_pct = hard_exposure_cap_pct
        self.absolute_exposure_cap_pct = absolute_exposure_cap_pct
        self.allow_position_flip = allow_position_flip
        self.min_order_notional_usd = float(min_order_notional_usd if min_order_notional_usd is not None else 0.0)
        self.dust_position_notional_usd = float(dust_position_notional_usd)
        self.min_notional_blocked_count = 0
        self.no_fill_cycles = 0
        self.last_real_trade_ts: datetime | None = None
        self.flip_trade_count = 0
        self.last_flip_trade_ts: datetime | None = None
        self.min_order_lifetime_seconds = max(int(min_order_lifetime_seconds), 0)
        self.min_reprice_distance_pct = max(float(min_reprice_distance_pct), 0.0)
        self.paired_take_profit_enabled = bool(paired_take_profit_enabled)
        self.paired_tp_spacing_multiplier = max(float(paired_tp_spacing_multiplier), 0.0)
        self.paired_tp_placed_count = 0
        self.last_grid_spacing_pct = 0.003
        self.order_events: list[dict] = []

    def _record_order_event(self, event_type: str, *, symbol: str = "", side: str = "", price: float | None = None, size: float | None = None, lifetime_seconds: float | None = None) -> None:
        self.order_events.append({"ts": time.time(), "event": event_type, "symbol": symbol, "side": side, "price": price, "size": size, "lifetime_seconds": lifetime_seconds})
        cutoff = time.time() - 24 * 3600
        self.order_events = [e for e in self.order_events if float(e.get("ts", 0.0) or 0.0) >= cutoff]

    def fill_rate_metrics(self, mark_price: float, *, symbol: str = "") -> dict:
        now = time.time()
        out: dict[str, object] = {}
        for label, seconds in (("1h", 3600), ("6h", 21600), ("24h", 86400)):
            events = [e for e in self.order_events if now - float(e.get("ts", 0.0) or 0.0) <= seconds and (not symbol or str(e.get("symbol", "")).upper() == symbol.upper())]
            submitted = sum(1 for e in events if e.get("event") == "submitted")
            cancelled = sum(1 for e in events if e.get("event") == "cancelled")
            filled = sum(1 for e in events if e.get("event") == "filled")
            lifetimes = [float(e["lifetime_seconds"]) for e in events if e.get("event") in {"cancelled", "filled"} and e.get("lifetime_seconds") is not None]
            avg_lifetime = (sum(lifetimes) / len(lifetimes)) if lifetimes else 0.0
            out[label] = {"orders_submitted": submitted, "orders_cancelled": cancelled, "orders_filled": filled, "fill_rate": (filled / submitted) if submitted else 0.0, "cancel_to_fill_ratio": (cancelled / filled) if filled else float(cancelled), "avg_order_lifetime_seconds": avg_lifetime, "average_order_lifetime_seconds": avg_lifetime}
        relevant_orders = [o for o in self.open_orders if not symbol or str(o.get("symbol", "")).upper() == symbol.upper()]
        distances = [abs(float(o.get("price", 0.0) or 0.0) - mark_price) / max(mark_price, 1e-9) for o in relevant_orders]
        buys = [o for o in relevant_orders if str(o.get("side", "")).lower() == "buy"]
        sells = [o for o in relevant_orders if str(o.get("side", "")).lower() == "sell"]
        nearest_buy = max((float(o.get("price", 0.0) or 0.0) for o in buys), default=None)
        nearest_sell = min((float(o.get("price", 0.0) or 0.0) for o in sells), default=None)
        last_fill_age = None
        if self.last_real_trade_ts is not None:
            last_fill_age = (datetime.now(timezone.utc) - self.last_real_trade_ts).total_seconds() / 60.0
        out.update({"average_distance_to_mid_pct": (sum(distances) / len(distances)) if distances else None, "nearest_buy_distance_pct": ((mark_price - nearest_buy) / mark_price) if nearest_buy else None, "nearest_sell_distance_pct": ((nearest_sell - mark_price) / mark_price) if nearest_sell else None, "time_since_last_fill_minutes": last_fill_age})
        return out

    def _can_replace_grid_now(self, symbol: str, plan: GridPlan) -> tuple[bool, str]:
        active = [o for o in self.open_orders if str(o.get("symbol", symbol)).upper() == symbol.upper() and not bool(o.get("reduce_only", False))]
        if not active:
            return True, "no_existing_orders"
        now = time.time()
        ages = [now - float(o.get("created_ts") or now) for o in active]
        min_age = min(ages) if ages else 0.0
        if min_age < self.min_order_lifetime_seconds:
            return False, "min_order_lifetime"
        new_by_side = {"buy": sorted([x.price for x in plan.long_levels], reverse=True), "sell": sorted([x.price for x in plan.short_levels])}
        old_by_side = {"buy": sorted([float(o.get("price", 0.0) or 0.0) for o in active if str(o.get("side", "")).lower() == "buy"], reverse=True), "sell": sorted([float(o.get("price", 0.0) or 0.0) for o in active if str(o.get("side", "")).lower() == "sell"])}
        deltas: list[float] = []
        for side in ("buy", "sell"):
            for old_px, new_px in zip(old_by_side[side], new_by_side[side]):
                deltas.append(abs(float(new_px) - old_px) / max(old_px, 1e-9))
            if len(old_by_side[side]) != len(new_by_side[side]):
                deltas.append(self.min_reprice_distance_pct)
        max_delta = max(deltas) if deltas else self.min_reprice_distance_pct
        if max_delta < self.min_reprice_distance_pct:
            return False, "min_reprice_distance"
        return True, "reprice_threshold_met"

    def _require_paper_execution(self, action: str) -> None:
        if not self.paper_mode:
            raise RuntimeError(
                f"live_execution_not_implemented: {action} cannot use paper-simulated orders/fills when PAPER_MODE=false"
            )

    def _load_state(self, start_balance: float) -> ExecutionState:
        if self.state_file.exists():
            payload = json.loads(self.state_file.read_text())
            valid = set(ExecutionState.__dataclass_fields__)
            return ExecutionState(**{k: v for k, v in payload.items() if k in valid})
        return ExecutionState(equity=start_balance, daily_start_equity=start_balance)

    def save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(asdict(self.paper), indent=2), encoding="utf-8")

    def account_state(self, symbol: str, mark_price: float) -> AccountState:
        return AccountState(
            state_source="paper",
            equity=self.equity(mark_price),
            position_size=self.paper.position_size,
            avg_entry=self.paper.avg_entry,
            position_notional=abs(self.paper.position_size * mark_price),
            unrealized_pnl=self.unrealized_pnl(mark_price),
            realized_pnl=self.paper.realized_pnl,
            fees_paid=self.paper.fees_paid,
            daily_start_equity=self.paper.daily_start_equity,
            daily_start_realized_pnl=self.paper.daily_start_realized_pnl,
            daily_start_fees_paid=self.paper.daily_start_fees_paid,
            current_day=self.paper.current_day,
        )

    def cancel_replace_grid(self, symbol: str, plan: GridPlan) -> dict[str, int | str]:
        self._require_paper_execution("cancel_replace_grid")
        if plan.spacing_pct and plan.spacing_pct > 0:
            self.last_grid_spacing_pct = float(plan.spacing_pct)
        if not plan.should_regrid:
            return {"canceled": 0, "placed": 0, "symbol": symbol, "reprice_decision": "skip", "reprice_reason": "regrid_not_required"}
        can_replace, reprice_reason = self._can_replace_grid_now(symbol, plan)
        if not can_replace:
            logger.info("reprice_decision decision=skip reprice_reason=%s min_order_lifetime_seconds=%s min_reprice_distance_pct=%s", reprice_reason, self.min_order_lifetime_seconds, self.min_reprice_distance_pct)
            return {"canceled": 0, "placed": 0, "symbol": symbol, "reprice_decision": "skip", "reprice_reason": reprice_reason}
        now_ts = time.time()
        retained = [o for o in self.open_orders if str(o.get("symbol", symbol)).upper() != symbol.upper() or bool(o.get("reduce_only", False))]
        active = [o for o in self.open_orders if str(o.get("symbol", symbol)).upper() == symbol.upper() and not bool(o.get("reduce_only", False))]
        desired: list[dict] = []
        for o in (plan.long_levels + plan.short_levels):
            notional = abs(o.price * o.size)
            if self.min_order_notional_usd > 0 and notional + 1e-9 < self.min_order_notional_usd:
                self.min_notional_blocked_count += 1
                logger.warning(
                    "min_notional_blocked side=%s price=%s qty=%s notional=%s min_order_notional_usd=%s",
                    o.side,
                    o.price,
                    o.size,
                    notional,
                    self.min_order_notional_usd,
                )
                continue
            desired.append({"side": o.side, "price": o.price, "size": o.size})
        kept, to_cancel, to_place = _diff_grid_orders(active, desired, self.min_reprice_distance_pct)
        canceled = 0
        for old in to_cancel:
            canceled += 1
            created = old.get("created_ts")
            lifetime = (now_ts - float(created)) if created else None
            self._record_order_event("cancelled", symbol=symbol, side=str(old.get("side", "")), price=float(old.get("price", 0.0) or 0.0), size=float(old.get("size", 0.0) or 0.0), lifetime_seconds=lifetime)
        orders = []
        for level in to_place:
            order = {"symbol": symbol, "side": level["side"], "price": level["price"], "size": level["size"], "created_ts": now_ts, "order_source": "normal_entry"}
            orders.append(order)
            self._record_order_event("submitted", symbol=symbol, side=level["side"], price=level["price"], size=level["size"])
        self.open_orders = retained + kept + orders
        logger.info("reprice_decision decision=replace reprice_reason=%s canceled=%s placed=%s kept=%s", reprice_reason, canceled, len(orders), len(kept))
        return {"canceled": canceled, "placed": len(orders), "kept": len(kept), "symbol": symbol, "reprice_decision": "replace", "reprice_reason": reprice_reason}

    def cancel_all_orders(self, symbol: str, *, force: bool = False) -> int:
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
            self.open_orders.append({"symbol": symbol, "side": side, "price": px, "size": sz, "reduce_only": True, "created_ts": time.time(), "order_source": "reduce_only"})
            remaining -= sz
            placed += 1
        return placed

    def ensure_reduce_only_close_order(self, symbol: str, position_size: float, mark_price: float, replace_threshold_pct: float = 0.0015) -> dict[str, object]:
        self._require_paper_execution("ensure_reduce_only_close_order")
        side = "buy" if position_size < 0 else "sell"
        remaining_qty = abs(position_size)
        if remaining_qty < 1e-12:
            return {"submitted": False, "active_cleanup_orders": 0, "canceled": 0, "reason": "flat"}
        target_price = mark_price * 1.002 if side == "buy" else mark_price * 0.998
        before = len(self.open_orders)
        self.open_orders = [
            o for o in self.open_orders
            if o.get("symbol") != symbol or (bool(o.get("reduce_only", False)) and o.get("side") == side)
        ]
        canceled = before - len(self.open_orders)
        active = [o for o in self.open_orders if o.get("symbol") == symbol and bool(o.get("reduce_only", False)) and o.get("side") == side]
        keep = active[:1]
        extra_ids = {id(o) for o in active[1:]}
        if extra_ids:
            self.open_orders = [o for o in self.open_orders if id(o) not in extra_ids]
            canceled += len(extra_ids)
        existing = keep[0] if keep else None
        price_moved = False
        size_changed = False
        if existing is not None:
            existing_price = float(existing.get("price", 0.0) or 0.0)
            existing_size = float(existing.get("size", 0.0) or 0.0)
            price_moved = abs(target_price - existing_price) / max(existing_price, 1e-9) > replace_threshold_pct
            size_changed = abs(existing_size - remaining_qty) > 1e-12
            if price_moved or size_changed:
                self.open_orders = [o for o in self.open_orders if o is not existing]
                canceled += 1
            else:
                return {"submitted": False, "active_cleanup_orders": 1, "canceled": canceled, "price": existing_price, "size": existing_size, "reason": "existing_order_kept", "price_moved": False}
        self.open_orders.append({"symbol": symbol, "side": side, "price": target_price, "size": remaining_qty, "reduce_only": True, "dust_cleanup": True, "created_ts": time.time(), "order_source": "dust_cleanup"})
        return {"submitted": True, "active_cleanup_orders": 1, "canceled": canceled, "price": target_price, "size": remaining_qty, "reason": "submitted", "price_moved": price_moved, "size_changed": size_changed}

    def flatten_position(self, symbol: str, mark_price: float) -> bool:
        self._require_paper_execution("flatten_position")
        if abs(self.paper.position_size) < 1e-12:
            return False
        side = "buy" if self.paper.position_size < 0 else "sell"
        self._apply_fill({"symbol": symbol, "side": side, "price": mark_price, "size": abs(self.paper.position_size), "created_ts": time.time(), "order_source": "reduce_only"})
        return True

    def on_candle(self, candle: dict, *, regime: str = "unknown", mode: str = "unknown", risk_state: str = "OK", pause_reason: str = "", strategy_status: str = "running", orderbook_classification: str = "", orderbook_imbalance_ratio: float | None = None, orderbook_pressure_score: float | None = None, prediction_bias: str = "") -> list[dict]:
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
            self._apply_fill(fill, regime=regime, mode=mode, risk_state=risk_state, pause_reason=pause_reason, orderbook_classification=orderbook_classification, orderbook_imbalance_ratio=orderbook_imbalance_ratio, orderbook_pressure_score=orderbook_pressure_score, prediction_bias=prediction_bias)
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
            self._maybe_place_paper_paired_take_profit(fill, position_before)
        if executed_fills:
            self.no_fill_cycles = 0
            self.last_real_trade_ts = datetime.now(timezone.utc)
        self.save_state()
        return executed_fills

    def _maybe_place_paper_paired_take_profit(self, fill: dict, position_before: float) -> bool:
        """Pair an executed opening grid fill with a reduce-only TP one spacing away."""
        if not self.paired_take_profit_enabled or bool(fill.get("reduce_only")):
            return False
        if abs(self.paper.position_size) <= abs(position_before) + 1e-12:
            return False
        spacing = max(float(self.last_grid_spacing_pct), 0.0) * self.paired_tp_spacing_multiplier
        price = float(fill.get("price", 0.0) or 0.0)
        size = float(fill.get("size", 0.0) or 0.0)
        if spacing <= 0 or price <= 0 or size <= 0:
            return False
        side = str(fill.get("side", "")).lower()
        tp_side = "sell" if side == "buy" else "buy"
        tp_price = price * (1.0 + spacing) if side == "buy" else price * (1.0 - spacing)
        self.open_orders.append({"symbol": fill.get("symbol", ""), "side": tp_side, "price": tp_price, "size": size, "reduce_only": True, "created_ts": time.time(), "order_source": "paired_tp"})
        self.paired_tp_placed_count += 1
        logger.info("paired_tp_submitted mode=paper entry_side=%s entry_price=%s qty=%s tp_side=%s tp_price=%s spacing_pct=%s paired_tp_placed_count=%s", side, price, size, tp_side, tp_price, spacing, self.paired_tp_placed_count)
        return True

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

    def _apply_fill(self, fill: dict, reason: str = "grid_fill", regime: str = "unknown", mode: str = "unknown", risk_state: str = "OK", pause_reason: str = "", orderbook_classification: str = "", orderbook_imbalance_ratio: float | None = None, orderbook_pressure_score: float | None = None, prediction_bias: str = "") -> None:
        self._require_paper_execution("_apply_fill")
        price, size = float(fill["price"]), float(fill["size"])
        position_before = self.paper.position_size
        realized_before = self.paper.realized_pnl
        exposure_action = classify_exposure_action(position_before, fill["side"], size)
        trade_category = trade_category_for_action(exposure_action)
        is_flip_trade = exposure_action == "flipping"
        signed = size if fill["side"] == "buy" else -size
        notional = abs(price * size)
        fill_liquidity = _classify_fill_liquidity(fill, default="maker")
        fee = notional * (self.maker_fee_rate if fill_liquidity == "maker" else self.taker_fee_rate)
        is_dust_fill = _is_dust_notional(notional, self.dust_position_notional_usd)
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
        self.paper.bot_position_size = self.paper.position_size
        self.paper.bot_realized_pnl += realized_delta
        self.paper.bot_fees_paid += fee
        self.paper.bot_daily_realized_pnl += realized_delta
        self.paper.bot_daily_fees_paid += fee
        position_notional = abs(self.paper.position_size * price)
        if is_flip_trade:
            self.flip_trade_count += 1
            self.last_flip_trade_ts = datetime.now(timezone.utc)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": fill.get("symbol", ""),
            "side": fill["side"],
            "price": price,
            "qty": size,
            "notional": notional,
            "fee": fee,
            "fill_liquidity": fill_liquidity,
            "is_maker_fill": fill_liquidity == "maker",
            "is_taker_fill": fill_liquidity == "taker",
            "dust_fill": is_dust_fill,
            "realized_pnl_delta": realized_delta,
            "realized_pnl_total": self.paper.realized_pnl,
            "cash": self.paper.cash,
            "equity": self.equity(price),
            "position_before": position_before,
            "position_after": self.paper.position_size,
            "position_size": self.paper.position_size,
            "exposure_action": exposure_action,
            "trade_category": trade_category,
            "is_flip_trade": is_flip_trade,
            "flip_trade_count": self.flip_trade_count,
            "position_notional": position_notional,
            "regime": regime,
            "mode": mode,
            "risk_state": risk_state,
            "pause_reason": pause_reason,
            "reason": reason,
            "order_source": fill.get("order_source") or ("dust_cleanup" if fill.get("dust_cleanup") else ("reduce_only" if fill.get("reduce_only") else "normal_entry")),
            "is_bot_fill": True,
            "sub_10_usd_fill": 0.0 < notional < 10.0,
            "sub_5_usd_fill": 0.0 < notional < 5.0,
            "orderbook_classification": orderbook_classification,
            "orderbook_imbalance_ratio": orderbook_imbalance_ratio,
            "orderbook_pressure_score": orderbook_pressure_score,
            "prediction_bias": prediction_bias,
        }
        created = fill.get("created_ts")
        lifetime = (time.time() - float(created)) if created else None
        self._record_order_event("filled", symbol=str(fill.get("symbol", "")), side=str(fill.get("side", "")), price=price, size=size, lifetime_seconds=lifetime)
        self.trade_log.append(entry)
        self._append_trade_ledger(entry)

    def _risk_decision(self, fill: dict, mark_price: float, risk_state: str, pause_reason: str, regime: str, mode: str, strategy_status: str = "running") -> tuple[str, str]:
        side = str(fill["side"]).lower()
        price, qty = float(fill["price"]), float(fill["size"])
        order_notional = abs(price * qty)
        position_size_before = self.paper.position_size
        position_notional_before = abs(position_size_before * mark_price)
        exposure_action = classify_exposure_action(position_size_before, side, qty)
        trade_category = trade_category_for_action(exposure_action)
        is_flip_trade = exposure_action == "flipping"
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

        payload = {"symbol": fill.get("symbol", ""), "side": side, "price": price, "qty": qty, "order_notional": order_notional, "position_size_before": position_size_before, "position_notional_before": position_notional_before, "estimated_position_notional_after": estimated_position_notional_after, "max_position_notional_usd": self.max_position_notional_usd, "exposure_ratio": exposure_ratio, "would_increase_exposure": would_inc, "exposure_action": exposure_action, "trade_category": trade_category, "is_flip_trade": is_flip_trade, "exposure_reducing_override": exposure_reducing_override, "risk_state": risk_state, "risk_state_before": risk_state_before, "strategy_status": strategy_status, "pause_reason": pause_reason, "decision": decision, "allow_reason": allow_reason, "block_reason": block_reason, "regime": regime, "mode": mode}
        self._append_risk_decision(payload)
        logger.info("risk_decision symbol=%s side=%s price=%s qty=%s order_notional=%s position_notional_before=%s max_position_notional_usd=%s exposure_ratio=%.3f would_increase_exposure=%s exposure_reducing_override=%s risk_state_before=%s risk_state=%s strategy_status=%s pause_reason=%s decision=%s allow_reason=%s block_reason=%s", payload["symbol"], side, price, qty, order_notional, position_notional_before, self.max_position_notional_usd, exposure_ratio, would_inc, exposure_reducing_override, risk_state_before, risk_state, payload["strategy_status"], pause_reason or "none", decision, allow_reason or "none", block_reason or "none")
        return decision, block_reason

    def _append_risk_decision(self, entry: dict) -> None:
        self.risk_decisions_csv.parent.mkdir(parents=True, exist_ok=True)
        fields = ["timestamp", "symbol", "side", "price", "qty", "order_notional", "position_size_before", "position_notional_before", "estimated_position_notional_after", "max_position_notional_usd", "exposure_ratio", "would_increase_exposure", "exposure_action", "trade_category", "is_flip_trade", "exposure_reducing_override", "risk_state_before", "risk_state", "strategy_status", "pause_reason", "decision", "allow_reason", "block_reason", "regime", "mode"]
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
        fields = ["timestamp", "symbol", "side", "price", "qty", "notional", "fee", "fill_liquidity", "is_maker_fill", "is_taker_fill", "dust_fill", "realized_pnl_delta", "realized_pnl_total", "cash", "equity", "position_before", "position_after", "position_size", "position_notional", "exposure_action", "trade_category", "is_flip_trade", "flip_trade_count", "regime", "mode", "risk_state", "pause_reason", "reason", "order_source", "is_bot_fill", "sub_10_usd_fill", "sub_5_usd_fill", "orderbook_classification", "orderbook_imbalance_ratio", "orderbook_pressure_score", "prediction_bias"]
        write_header = not self.trade_ledger_csv.exists()
        with self.trade_ledger_csv.open("a", encoding="utf-8") as f:
            if write_header:
                f.write(",".join(fields) + "\n")
            f.write(",".join(str(entry.get(k, "")) for k in fields) + "\n")
        with self.trade_ledger_jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def last_closed_trade_net_pnls(self, limit: int) -> list[float]:
        if limit <= 0 or not self.trade_ledger_csv.exists():
            return []
        import csv
        pnls: list[float] = []
        with self.trade_ledger_csv.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    realized = float(row.get("realized_pnl_delta", 0.0) or 0.0)
                    fee = abs(float(row.get("fee", 0.0) or 0.0))
                except (TypeError, ValueError):
                    continue
                if abs(realized) > 1e-12:
                    pnls.append(realized - fee)
        return pnls[-limit:]

    def consume_trade_log(self) -> list[dict]:
        out = list(self.trade_log)
        self.trade_log.clear()
        return out


class LiveExecutionEngine:
    """Authenticated Hyperliquid execution engine.

    Live mode never derives fills from candles. Orders are submitted to the
    exchange and trade ledger entries are emitted only from Hyperliquid user
    fills returned by the authenticated account API.
    """

    def __init__(
        self,
        client: HyperliquidClient,
        state_file: str,
        *,
        max_notional_per_trade_usd: float = 10.0,
        min_notional_usd: float = 10.0,
        min_order_notional_usd: float | None = None,
        dust_position_notional_usd: float = 2.0,
        leverage: int = 1,
        fee_rate: float = 0.0004,
        maker_fee_rate: float | None = None,
        taker_fee_rate: float | None = None,
        use_alo_orders: bool = True,
        paired_take_profit_enabled: bool = True,
        paired_tp_spacing_multiplier: float = 1.0,
        **kwargs,
    ) -> None:
        self.client = client
        self.execution_mode = "live_only"
        self.state_file = Path(state_file)
        self.open_orders: list[dict] = []
        self.state = ExecutionState(equity=float(kwargs.pop("start_balance", 0.0) or 0.0))
        self.state.daily_start_equity = self.state.equity
        self.trade_log: list[dict] = []
        self.trade_ledger_csv = Path(kwargs.pop("trade_ledger_csv", None) or "logs/trades.csv")
        self.trade_ledger_jsonl = Path(kwargs.pop("trade_ledger_jsonl", None) or "logs/trades.jsonl")
        self.risk_decisions_csv = Path(kwargs.pop("risk_decisions_csv", None) or "logs/risk_decisions.csv")
        self.max_position_notional_usd = float(kwargs.pop("max_position_notional_usd", 500.0))
        self.allow_position_flip = bool(kwargs.pop("allow_position_flip", False))
        self.no_fill_cycles = 0
        self.last_real_trade_ts: datetime | None = None
        self.flip_trade_count = 0
        self.last_flip_trade_ts: datetime | None = None
        self.max_notional_per_trade_usd = max_notional_per_trade_usd
        self.min_notional_usd = min_notional_usd
        self.min_order_notional_usd = float(min_order_notional_usd if min_order_notional_usd is not None else min_notional_usd)
        self.dust_position_notional_usd = float(dust_position_notional_usd)
        self.min_notional_blocked_count = 0
        self.leverage = leverage
        self.fee_rate = fee_rate
        self.maker_fee_rate = float(maker_fee_rate if maker_fee_rate is not None else fee_rate)
        self.taker_fee_rate = float(taker_fee_rate if taker_fee_rate is not None else fee_rate)
        self.use_alo_orders = bool(use_alo_orders)
        self.alo_rejected_count = 0
        self.paired_take_profit_enabled = bool(paired_take_profit_enabled)
        self.paired_tp_spacing_multiplier = max(float(paired_tp_spacing_multiplier), 0.0)
        self.paired_tp_placed_count = 0
        self.last_grid_spacing_pct = 0.003
        self.rate_limit_cooldown_seconds = max(float(kwargs.pop("rate_limit_cooldown_seconds", 120.0)), 0.0)
        self.rate_limit_deficit_cooldown_seconds_per_request = max(float(kwargs.pop("rate_limit_deficit_cooldown_seconds_per_request", 0.05)), 0.0)
        self.rate_limit_max_cooldown_seconds = max(float(kwargs.pop("rate_limit_max_cooldown_seconds", 900.0)), self.rate_limit_cooldown_seconds)
        self.rate_limit_orphan_grace_seconds = max(float(kwargs.pop("rate_limit_orphan_grace_seconds", 600.0)), 0.0)
        self.rate_limit_recovery_window_seconds = max(float(kwargs.pop("rate_limit_recovery_window_seconds", 1800.0)), 0.0)
        self.rate_limit_recovery_min_order_lifetime_seconds = max(float(kwargs.pop("rate_limit_recovery_min_order_lifetime_seconds", 600.0)), 0.0)
        self.rate_limit_recovery_min_reprice_distance_pct = max(float(kwargs.pop("rate_limit_recovery_min_reprice_distance_pct", 0.005)), 0.0)
        self.rate_limit_recovery_max_ops_per_hour = max(int(kwargs.pop("rate_limit_recovery_max_ops_per_hour", 6)), 1)
        self.rate_limit_budget_poll_seconds = max(float(kwargs.pop("rate_limit_budget_poll_seconds", 600.0)), 0.0)
        self.rate_limit_headroom_alert = max(float(kwargs.pop("rate_limit_headroom_alert", 1000.0)), 0.0)
        self.rate_limit_headroom_frugal = max(float(kwargs.pop("rate_limit_headroom_frugal", 500.0)), 0.0)
        self.rate_limit_min_action_interval_seconds = max(float(kwargs.pop("rate_limit_min_action_interval_seconds", 12.0)), 0.0)
        self.paired_tp_post_only = bool(kwargs.pop("paired_tp_post_only", True))
        self.flatten_maker_first = bool(kwargs.pop("flatten_maker_first", True))
        self.flatten_maker_wait_seconds = max(float(kwargs.pop("flatten_maker_wait_seconds", 8.0)), 0.0)
        self._flatten_pending: dict | None = None
        self.max_order_ops_per_cycle = max(int(kwargs.pop("max_order_ops_per_cycle", 2)), 1)
        self.rate_limited_until = 0.0
        self.last_rate_limit_ts = 0.0
        self.last_request_deficit = 0.0
        self.rate_limit_trigger_count = 0
        self.rate_limited_skip_count = 0
        self.rate_limit_budget: dict = {}
        self.rate_limit_budget_frugal = False
        self._last_budget_poll_ts = 0.0
        self._last_exchange_action_ts = 0.0
        self._exchange_action_times: list[float] = []
        self.last_submit_error_kind: str | None = None
        self.fill_health_events: list[dict] = []
        self._seen_fill_ids: set[str] = set()
        self._bot_order_oids: set[int] = set()
        self._load_live_seen_fills()
        self.min_order_lifetime_seconds = max(int(kwargs.pop("min_order_lifetime_seconds", 90)), 0)
        self.min_reprice_distance_pct = max(float(kwargs.pop("min_reprice_distance_pct", 0.0015)), 0.0)
        self.order_events: list[dict] = []
        self.client.require_live_execution_support()

    def _record_order_event(self, event_type: str, *, symbol: str = "", side: str = "", price: float | None = None, size: float | None = None, lifetime_seconds: float | None = None) -> None:
        self.order_events.append({"ts": time.time(), "event": event_type, "symbol": symbol, "side": side, "price": price, "size": size, "lifetime_seconds": lifetime_seconds})
        cutoff = time.time() - 24 * 3600
        self.order_events = [e for e in self.order_events if float(e.get("ts", 0.0) or 0.0) >= cutoff]

    def fill_rate_metrics(self, mark_price: float, *, symbol: str = "") -> dict:
        now = time.time()
        out: dict[str, object] = {}
        for label, seconds in (("1h", 3600), ("6h", 21600), ("24h", 86400)):
            events = [e for e in self.order_events if now - float(e.get("ts", 0.0) or 0.0) <= seconds and (not symbol or str(e.get("symbol", "")).upper() == symbol.upper())]
            submitted = sum(1 for e in events if e.get("event") == "submitted")
            cancelled = sum(1 for e in events if e.get("event") == "cancelled")
            filled = sum(1 for e in events if e.get("event") == "filled")
            lifetimes = [float(e["lifetime_seconds"]) for e in events if e.get("event") in {"cancelled", "filled"} and e.get("lifetime_seconds") is not None]
            avg_lifetime = (sum(lifetimes) / len(lifetimes)) if lifetimes else 0.0
            out[label] = {"orders_submitted": submitted, "orders_cancelled": cancelled, "orders_filled": filled, "fill_rate": (filled / submitted) if submitted else 0.0, "cancel_to_fill_ratio": (cancelled / filled) if filled else float(cancelled), "avg_order_lifetime_seconds": avg_lifetime, "average_order_lifetime_seconds": avg_lifetime}
        relevant_orders = [o for o in self.open_orders if not symbol or str(o.get("symbol", "")).upper() == symbol.upper()]
        distances = [abs(float(o.get("price", 0.0) or 0.0) - mark_price) / max(mark_price, 1e-9) for o in relevant_orders]
        buys = [o for o in relevant_orders if str(o.get("side", "")).lower() == "buy"]
        sells = [o for o in relevant_orders if str(o.get("side", "")).lower() == "sell"]
        nearest_buy = max((float(o.get("price", 0.0) or 0.0) for o in buys), default=None)
        nearest_sell = min((float(o.get("price", 0.0) or 0.0) for o in sells), default=None)
        last_fill_age = None
        if self.last_real_trade_ts is not None:
            last_fill_age = (datetime.now(timezone.utc) - self.last_real_trade_ts).total_seconds() / 60.0
        out.update({"average_distance_to_mid_pct": (sum(distances) / len(distances)) if distances else None, "nearest_buy_distance_pct": ((mark_price - nearest_buy) / mark_price) if nearest_buy else None, "nearest_sell_distance_pct": ((nearest_sell - mark_price) / mark_price) if nearest_sell else None, "time_since_last_fill_minutes": last_fill_age})
        return out

    def _can_replace_grid_now(self, symbol: str, plan: GridPlan) -> tuple[bool, str]:
        active = [o for o in self.open_orders if str(o.get("symbol", symbol)).upper() == symbol.upper() and not bool(o.get("reduce_only", False))]
        if not active:
            return True, "no_existing_orders"
        now = time.time()
        min_lifetime = float(self.min_order_lifetime_seconds)
        min_distance = self.min_reprice_distance_pct
        if self._in_rate_limit_recovery(now):
            # While the request deficit is being repaid, chasing the price is
            # pure budget burn: every cancel+replace costs 2 requests and earns
            # nothing (observed live as a slow bleed with zero fills). Hold the
            # resting orders much longer and only reprice on a move bigger than
            # a full grid spacing.
            min_lifetime = max(min_lifetime, self.rate_limit_recovery_min_order_lifetime_seconds)
            min_distance = max(min_distance, self.rate_limit_recovery_min_reprice_distance_pct, float(plan.spacing_pct or 0.0))
        ages = [now - float(o.get("created_ts") or now) for o in active]
        if ages and min(ages) < min_lifetime:
            return False, "min_order_lifetime"
        new_by_side = {"buy": sorted([x.price for x in plan.long_levels], reverse=True), "sell": sorted([x.price for x in plan.short_levels])}
        old_by_side = {"buy": sorted([float(o.get("price", 0.0) or 0.0) for o in active if str(o.get("side", "")).lower() == "buy"], reverse=True), "sell": sorted([float(o.get("price", 0.0) or 0.0) for o in active if str(o.get("side", "")).lower() == "sell"])}
        deltas: list[float] = []
        for side in ("buy", "sell"):
            for old_px, new_px in zip(old_by_side[side], new_by_side[side]):
                deltas.append(abs(float(new_px) - old_px) / max(old_px, 1e-9))
            if len(old_by_side[side]) != len(new_by_side[side]):
                deltas.append(min_distance)
        if (max(deltas) if deltas else min_distance) < min_distance:
            return False, "min_reprice_distance"
        return True, "reprice_threshold_met"

    def _in_rate_limit_recovery(self, now_ts: float | None = None) -> bool:
        """True while the address is presumed to still carry a cumulative-request deficit.

        The deficit only shrinks with traded volume, so it outlives the
        rejection cooldown by a wide margin; the recovery window keeps order
        operations frugal (one per cycle, placements first) after the last
        observed rate-limit rejection. The polled budget can also pre-arm
        frugal mode before the exchange ever rejects anything.
        """
        if self.rate_limit_budget_frugal:
            return True
        if self.last_rate_limit_ts <= 0 or self.rate_limit_recovery_window_seconds <= 0:
            return False
        now = time.time() if now_ts is None else now_ts
        return (now - self.last_rate_limit_ts) <= self.rate_limit_recovery_window_seconds

    def poll_rate_limit_budget(self, now_ts: float | None = None) -> dict | None:
        """Periodically fetch the address request budget and pre-arm frugal mode.

        Returns the refreshed budget dict on a successful poll, None when the
        poll is throttled or fails. Never raises: budget telemetry must not
        break the trading tick.
        """
        now = time.time() if now_ts is None else now_ts
        if self.rate_limit_budget_poll_seconds <= 0:
            return None
        if now - self._last_budget_poll_ts < self.rate_limit_budget_poll_seconds:
            return None
        self._last_budget_poll_ts = now
        try:
            raw = self.client.get_user_rate_limit()
        except Exception as exc:
            logger.warning("rate_limit_budget_poll_failed error=%s", exc)
            return None
        if not isinstance(raw, dict) or not raw:
            return None
        cum_volume = float(raw.get("cumVlm", 0.0) or 0.0)
        used = float(raw.get("nRequestsUsed", 0.0) or 0.0)
        cap = float(raw.get("nRequestsCap", 0.0) or 0.0)
        headroom = cap - used
        was_frugal = self.rate_limit_budget_frugal
        self.rate_limit_budget_frugal = headroom < self.rate_limit_headroom_frugal
        self.rate_limit_budget = {
            "cum_volume_usd": cum_volume,
            "requests_used": used,
            "requests_cap": cap,
            "headroom": headroom,
            "requests_per_usdc": (used / cum_volume) if cum_volume > 0 else None,
            "headroom_alert": headroom < self.rate_limit_headroom_alert,
            "budget_frugal": self.rate_limit_budget_frugal,
            "checked_ts": now,
        }
        logger.info(
            "rate_limit_budget headroom=%.0f requests_used=%.0f requests_cap=%.0f cum_volume_usd=%.2f requests_per_usdc=%s budget_frugal=%s",
            headroom, used, cap, cum_volume,
            f"{used / cum_volume:.2f}" if cum_volume > 0 else "n/a",
            self.rate_limit_budget_frugal,
        )
        if self.rate_limit_budget_frugal and not was_frugal:
            logger.warning("rate_limit_budget_frugal_mode_entered headroom=%.0f threshold=%.0f", headroom, self.rate_limit_headroom_frugal)
        elif was_frugal and not self.rate_limit_budget_frugal:
            logger.info("rate_limit_budget_frugal_mode_exited headroom=%.0f threshold=%.0f", headroom, self.rate_limit_headroom_frugal)
        return self.rate_limit_budget

    def _record_exchange_action(self) -> None:
        now = time.time()
        self._last_exchange_action_ts = now
        self._exchange_action_times.append(now)
        cutoff = now - 3600
        self._exchange_action_times = [t for t in self._exchange_action_times if t >= cutoff]

    def _exchange_actions_last_hour(self, now_ts: float | None = None) -> int:
        now = time.time() if now_ts is None else now_ts
        cutoff = now - 3600
        return sum(1 for t in self._exchange_action_times if t >= cutoff)

    def _record_fill_health(self, entry: dict) -> None:
        self.fill_health_events.append({
            "ts": time.time(),
            "is_maker": bool(entry.get("is_maker_fill", False)),
            "realized_pnl_delta": float(entry.get("realized_pnl_delta", 0.0) or 0.0),
            "fee": float(entry.get("fee", 0.0) or 0.0),
            "notional": float(entry.get("notional", 0.0) or 0.0),
            "closing": str(entry.get("exposure_action", "")) in {"decreasing", "flat", "flipping"},
        })
        cutoff = time.time() - 24 * 3600
        self.fill_health_events = [e for e in self.fill_health_events if float(e.get("ts", 0.0) or 0.0) >= cutoff]

    def trade_health_metrics(self) -> dict:
        """Rolling 24h health snapshot: edge per roundtrip, maker share, op frugality."""
        now = time.time()
        cutoff = now - 24 * 3600
        fills = [e for e in self.fill_health_events if float(e.get("ts", 0.0) or 0.0) >= cutoff]
        events = [e for e in self.order_events if now - float(e.get("ts", 0.0) or 0.0) <= 86400]
        ops = sum(1 for e in events if e.get("event") in {"submitted", "cancelled"})
        fills_n = len(fills)
        closing = [e for e in fills if e.get("closing")]
        volume = sum(float(e.get("notional", 0.0) or 0.0) for e in fills)
        return {
            "fills_24h": fills_n,
            "maker_share_24h": (sum(1 for e in fills if e.get("is_maker")) / fills_n) if fills_n else None,
            "net_realized_24h": sum(float(e.get("realized_pnl_delta", 0.0) or 0.0) for e in fills),
            "fees_24h": sum(float(e.get("fee", 0.0) or 0.0) for e in fills),
            "volume_24h": volume,
            "roundtrips_24h": len(closing),
            "avg_roundtrip_net_24h": (sum(float(e.get("realized_pnl_delta", 0.0) or 0.0) for e in closing) / len(closing)) if closing else None,
            "order_ops_24h": ops,
            "ops_per_fill_24h": (ops / fills_n) if fills_n else None,
            "ops_per_usdc_24h": (ops / volume) if volume > 0 else None,
        }

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
            self._bot_order_oids = {int(x) for x in payload.get("bot_order_oids", []) if str(x).lstrip("-").isdigit()}
            self.state.daily_start_equity = float(payload.get("daily_start_equity", self.state.daily_start_equity) or 0.0)
            self.state.daily_start_realized_pnl = float(payload.get("daily_start_realized_pnl", self.state.daily_start_realized_pnl) or 0.0)
            self.state.daily_start_fees_paid = float(payload.get("daily_start_fees_paid", self.state.daily_start_fees_paid) or 0.0)
            self.state.bot_position_size = float(payload.get("bot_position_size", self.state.bot_position_size) or 0.0)
            self.state.bot_realized_pnl = float(payload.get("bot_realized_pnl", self.state.bot_realized_pnl) or 0.0)
            self.state.bot_fees_paid = float(payload.get("bot_fees_paid", self.state.bot_fees_paid) or 0.0)
            self.state.bot_daily_realized_pnl = float(payload.get("bot_daily_realized_pnl", self.state.bot_daily_realized_pnl) or 0.0)
            self.state.bot_daily_fees_paid = float(payload.get("bot_daily_fees_paid", self.state.bot_daily_fees_paid) or 0.0)
            self.state.current_day = str(payload.get("current_day", self.state.current_day) or "")
            self.last_rate_limit_ts = float(payload.get("last_rate_limit_ts", 0.0) or 0.0)

    def save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "mode": "live",
            "seen_fill_ids": sorted(self._seen_fill_ids)[-1000:],
            "bot_order_oids": sorted(self._bot_order_oids)[-1000:],
            "last_real_trade_ts": self.last_real_trade_ts.isoformat() if self.last_real_trade_ts else "",
            "daily_start_equity": self.state.daily_start_equity,
            "daily_start_realized_pnl": self.state.daily_start_realized_pnl,
            "daily_start_fees_paid": self.state.daily_start_fees_paid,
            "bot_position_size": self.state.bot_position_size,
            "bot_realized_pnl": self.state.bot_realized_pnl,
            "bot_fees_paid": self.state.bot_fees_paid,
            "bot_daily_realized_pnl": self.state.bot_daily_realized_pnl,
            "bot_daily_fees_paid": self.state.bot_daily_fees_paid,
            "current_day": self.state.current_day,
            "last_rate_limit_ts": self.last_rate_limit_ts,
        }
        self.state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def sync_account(self, symbol: str, mark_price: float | None = None) -> dict:
        balance = self.client.get_balance()
        position = self.client.get_position(symbol)
        size = float(position.get("szi", 0.0) or 0.0)
        entry_px = float(position.get("entryPx", 0.0) or 0.0)
        self.state.position_size = size
        self.state.avg_entry = entry_px
        self.state.cash = float(balance.get("equity", 0.0) or 0.0)
        if mark_price is not None:
            self.state.daily_start_equity = self.state.daily_start_equity or self.state.cash
        self.sync_open_orders(symbol)
        return {"equity": self.state.cash, "position_size": size, "avg_entry": entry_px, "open_orders": len(self.open_orders)}


    def account_state(self, symbol: str, mark_price: float) -> AccountState:
        self.sync_account(symbol, mark_price)
        unrealized = 0.0
        try:
            position = self.client.get_position(symbol)
            unrealized = float(position.get("unrealizedPnl", position.get("unrealized_pnl", 0.0)) or 0.0)
        except Exception:
            unrealized = 0.0
        return AccountState(
            state_source="live",
            equity=self.state.cash,
            position_size=self.state.position_size,
            avg_entry=self.state.avg_entry,
            position_notional=abs(self.state.position_size * mark_price),
            unrealized_pnl=unrealized,
            realized_pnl=self.state.realized_pnl,
            fees_paid=self.state.fees_paid,
            daily_start_equity=self.state.daily_start_equity,
            daily_start_realized_pnl=self.state.daily_start_realized_pnl,
            daily_start_fees_paid=self.state.daily_start_fees_paid,
            current_day=self.state.current_day,
        )

    def sync_open_orders(self, symbol: str) -> list[dict]:
        orders: list[dict] = []
        for raw in self.client.get_open_orders(symbol):
            side_raw = str(raw.get("side", "")).lower()
            side = "buy" if side_raw in {"b", "buy"} else "sell"
            oid_raw = raw.get("oid")
            try:
                oid_int = int(oid_raw) if oid_raw is not None else None
            except (TypeError, ValueError):
                oid_int = None
            is_bot_order = oid_int is not None and oid_int in self._bot_order_oids
            orders.append(
                {
                    "symbol": raw.get("coin", symbol),
                    "side": side,
                    "price": float(raw.get("limitPx", raw.get("px", 0.0)) or 0.0),
                    "size": float(raw.get("sz", 0.0) or 0.0),
                    "oid": oid_raw,
                    "reduce_only": bool(raw.get("reduceOnly", False)),
                    "is_bot_order": is_bot_order,
                    "order_source": "bot" if is_bot_order else "external",
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
        self.sync_open_orders(symbol)
        if plan.spacing_pct and plan.spacing_pct > 0:
            self.last_grid_spacing_pct = float(plan.spacing_pct)
        if not plan.should_regrid:
            return {"canceled": 0, "placed": 0, "symbol": symbol, "reprice_decision": "skip", "reprice_reason": "regrid_not_required"}
        if time.time() < self.rate_limited_until:
            # Canceling here would be one-way: the replacement placement is
            # going to be rejected anyway, so the regrid would only shrink the
            # surviving grid. Freeze it until the cooldown ends.
            logger.warning(
                "reprice_decision decision=skip reprice_reason=rate_limit_cooldown seconds_remaining=%.0f open_orders_kept=%s",
                self.rate_limited_until - time.time(),
                len(self.open_orders),
            )
            return {"canceled": 0, "placed": 0, "kept": len(self.open_orders), "symbol": symbol, "reprice_decision": "skip", "reprice_reason": "rate_limit_cooldown"}
        active = [o for o in self.open_orders if str(o.get("symbol", symbol)).upper() == symbol.upper() and not bool(o.get("reduce_only", False))]
        desired = [{"side": o.side, "price": o.price, "size": o.size} for o in plan.long_levels + plan.short_levels]
        kept, to_cancel, to_place = _diff_grid_orders(active, desired, self.min_reprice_distance_pct)
        can_replace, reprice_reason = self._can_replace_grid_now(symbol, plan)
        if not can_replace:
            if to_cancel:
                logger.info("reprice_decision decision=skip reprice_reason=%s min_order_lifetime_seconds=%s min_reprice_distance_pct=%s", reprice_reason, self.min_order_lifetime_seconds, self.min_reprice_distance_pct)
                return {"canceled": 0, "placed": 0, "kept": len(kept), "symbol": symbol, "reprice_decision": "skip", "reprice_reason": reprice_reason}
            # The anti-churn gates only guard resting orders from being
            # canceled; filling in missing levels is always allowed.
            reprice_reason = "missing_levels_fill_in"
        now_ts = time.time()
        # Place the levels most likely to fill first; when the op budget cuts
        # the list short, the surviving orders should be the closest to mid.
        plan_center = _grid_plan_center(plan)
        if plan_center > 0:
            to_place = sorted(to_place, key=lambda lvl: abs(float(lvl.get("price", 0.0) or 0.0) - plan_center))
        in_recovery = self._in_rate_limit_recovery(now_ts)
        if in_recovery and (now_ts - self._last_exchange_action_ts) < self.rate_limit_min_action_interval_seconds:
            # The over-limit trickle accepts ~1 action per 10s; keep a safety
            # margin so a borderline-timed action does not get rejected and
            # re-arm the cooldown.
            logger.info(
                "reprice_decision decision=skip reprice_reason=rate_limit_trickle_spacing seconds_since_last_action=%.1f min_action_interval=%.1f",
                now_ts - self._last_exchange_action_ts,
                self.rate_limit_min_action_interval_seconds,
            )
            return {"canceled": 0, "placed": 0, "kept": len(kept), "symbol": symbol, "reprice_decision": "skip", "reprice_reason": "rate_limit_trickle_spacing", "rate_limit_recovery_mode": True}
        if in_recovery and active and self._exchange_actions_last_hour(now_ts) >= self.rate_limit_recovery_max_ops_per_hour:
            # Volatile sessions can pass the reprice gates often enough that
            # legitimate reshuffles still outspend the fill income (observed
            # live: +16 requests in 30 min with zero volume). While in deficit,
            # grid maintenance gets a hard hourly action budget; an empty book
            # may always be re-seeded, and reduce-only TPs are exempt because
            # they only follow fills that just paid for them.
            logger.info(
                "reprice_decision decision=skip reprice_reason=rate_limit_hourly_op_budget ops_last_hour=%s max_ops_per_hour=%s open_orders_kept=%s",
                self._exchange_actions_last_hour(now_ts),
                self.rate_limit_recovery_max_ops_per_hour,
                len(active),
            )
            return {"canceled": 0, "placed": 0, "kept": len(kept), "symbol": symbol, "reprice_decision": "skip", "reprice_reason": "rate_limit_hourly_op_budget", "rate_limit_recovery_mode": True}
        if in_recovery:
            # The address is repaying a cumulative-request deficit, so the
            # exchange accepts roughly one action per 10s. Only traded volume
            # repays the deficit and only resting orders can trade, so the
            # single slot goes to the placement closest to mid; cancels would
            # just burn it (observed live as a grid bleeding to zero resting
            # orders: cancel accepted, replacement rejected, cooldown).
            # Existing orders are kept as fill chances, and cancels only run
            # when there is nothing to place or the book is already full.
            op_budget = 1
            if to_place and len(active) < max(len(desired), 1):
                cancel_budget = 0
            else:
                to_place = []
                cancel_budget = op_budget
        else:
            op_budget = self.max_order_ops_per_cycle
            # Reserve at least half the budget for placements so a long cancel
            # backlog cannot starve the grid of resting orders.
            place_budget = min(len(to_place), max(op_budget - len(to_cancel), (op_budget + 1) // 2))
            cancel_budget = op_budget - place_budget
        canceled = 0
        cancel_oids: set[int] = set()
        for old in to_cancel[:cancel_budget]:
            oid = old.get("oid")
            if oid is None:
                continue
            self._record_exchange_action()
            try:
                self.client.cancel_order(symbol, int(oid))
            except Exception as exc:
                # The order may have just filled or been canceled; the post-cancel
                # sync below decides whether state is safe to keep building on.
                logger.warning("live_cancel_failed oid=%s side=%s price=%s error=%s", oid, old.get("side"), old.get("price"), exc)
                continue
            canceled += 1
            cancel_oids.add(int(oid))
            created = old.get("created_ts")
            lifetime = (now_ts - float(created)) if created else None
            self._record_order_event("cancelled", symbol=symbol, side=str(old.get("side", "")), price=float(old.get("price", 0.0) or 0.0), size=float(old.get("size", 0.0) or 0.0), lifetime_seconds=lifetime)
        skipped_cancels = max(len(to_cancel) - canceled, 0)
        if canceled:
            remaining_after_cancel = self.sync_open_orders(symbol)
            still_present = [o for o in remaining_after_cancel if o.get("oid") is not None and int(o["oid"]) in cancel_oids]
            if still_present or canceled < len(to_cancel[:cancel_budget]):
                logger.warning(
                    "live_grid_rebuild_skipped reason=cancel_confirmation_pending remaining_canceled_orders=%s cancel_failures=%s kept=%s",
                    len(still_present),
                    max(len(to_cancel[:cancel_budget]) - canceled, 0),
                    len(kept),
                )
                return {"canceled": canceled, "placed": 0, "kept": len(kept), "symbol": symbol, "error": "cancel_confirmation_pending", "reprice_decision": "replace", "reprice_reason": reprice_reason}
        remaining_budget = max(op_budget - canceled, 0)
        placed = 0
        for level in to_place[:remaining_budget]:
            if self._submit_live_limit(symbol, level["side"], level["size"], level["price"], reduce_only=False):
                placed += 1
        self.sync_open_orders(symbol)
        skipped_places = max(len(to_place) - placed, 0)
        budget_exhausted = skipped_cancels > 0 or skipped_places > 0
        logger.info("reprice_decision decision=replace reprice_reason=%s canceled=%s placed=%s kept=%s skipped_cancels=%s skipped_places=%s max_order_ops_per_cycle=%s rate_limit_recovery_mode=%s", reprice_reason, canceled, placed, len(kept), skipped_cancels, skipped_places, self.max_order_ops_per_cycle, in_recovery)
        return {"canceled": canceled, "placed": placed, "kept": len(kept), "symbol": symbol, "reprice_decision": "replace", "reprice_reason": reprice_reason, "skipped_cancels": skipped_cancels, "skipped_places": skipped_places, "order_op_budget_exhausted": budget_exhausted, "rate_limit_recovery_mode": in_recovery}

    def cancel_all_orders(self, symbol: str, *, force: bool = False) -> int:
        # Discretionary cancels (one-siding, recenter, neutral-block) must not
        # run while repaying the request deficit: they empty the book, the
        # re-seed re-fills one side, and the loop burns the budget with zero
        # fills (observed live: ~45 req/h, frozen volume). Genuine risk cancels
        # (emergency flat, daily loss, max position, hard risk) pass force=True
        # and always go through.
        if not force and self._in_rate_limit_recovery() and self._exchange_actions_last_hour() >= self.rate_limit_recovery_max_ops_per_hour:
            logger.info(
                "cancel_all_skipped reason=rate_limit_recovery_discretionary ops_last_hour=%s max_ops_per_hour=%s open_orders_kept=%s",
                self._exchange_actions_last_hour(),
                self.rate_limit_recovery_max_ops_per_hour,
                len(self.open_orders),
            )
            return 0
        self._record_exchange_action()
        canceled = self.client.cancel_all_orders(symbol)
        self.sync_open_orders(symbol)
        return canceled

    def place_reduce_only_orders(self, symbol: str, position_size: float, mark_price: float, order_size: float, levels: int = 3) -> int:
        self.sync_account(symbol, mark_price)
        live_position_size = self.state.position_size
        if abs(live_position_size) < 1e-12:
            return 0
        side = "buy" if live_position_size < 0 else "sell"
        remaining = abs(live_position_size)
        per_level = max(min(order_size, remaining), 0.0)
        placed = 0
        for i in range(min(levels, self.max_order_ops_per_cycle)):
            if remaining <= 1e-12:
                break
            size = min(per_level, remaining)
            price = mark_price * (1 - 0.001 * (i + 1)) if side == "buy" else mark_price * (1 + 0.001 * (i + 1))
            if self._submit_live_limit(symbol, side, size, price, reduce_only=True):
                placed += 1
            remaining -= size
        self.sync_open_orders(symbol)
        return placed

    def ensure_reduce_only_close_order(self, symbol: str, position_size: float, mark_price: float, replace_threshold_pct: float = 0.0015) -> dict[str, object]:
        self.sync_account(symbol, mark_price)
        live_position_size = self.state.position_size
        side = "buy" if live_position_size < 0 else "sell"
        remaining_qty = abs(live_position_size)
        if remaining_qty < 1e-12:
            return {"submitted": False, "active_cleanup_orders": 0, "canceled": 0, "reason": "flat"}
        target_price = mark_price * 1.002 if side == "buy" else mark_price * 0.998
        canceled = 0
        active_cleanup: list[dict] = []
        for order in list(self.open_orders):
            if order.get("symbol") != symbol:
                continue
            is_close_side_reduce_only = bool(order.get("reduce_only", False)) and order.get("side") == side
            oid = order.get("oid")
            if not is_close_side_reduce_only:
                if oid is not None:
                    self.client.cancel_order(symbol, int(oid))
                    canceled += 1
            else:
                active_cleanup.append(order)
        active_cleanup.sort(key=lambda o: float(o.get("created_ts") or 0.0))
        keep = active_cleanup[:1]
        for order in active_cleanup[1:]:
            oid = order.get("oid")
            if oid is not None:
                self.client.cancel_order(symbol, int(oid))
                canceled += 1
        existing = keep[0] if keep else None
        price_moved = False
        size_changed = False
        if existing is not None:
            existing_price = float(existing.get("price", 0.0) or 0.0)
            existing_size = float(existing.get("size", 0.0) or 0.0)
            price_moved = abs(target_price - existing_price) / max(existing_price, 1e-9) > replace_threshold_pct
            size_changed = abs(existing_size - remaining_qty) > 1e-12
            if price_moved or size_changed:
                oid = existing.get("oid")
                if oid is not None:
                    self.client.cancel_order(symbol, int(oid))
                    canceled += 1
            else:
                self.sync_open_orders(symbol)
                return {"submitted": False, "active_cleanup_orders": 1, "canceled": canceled, "price": existing_price, "size": existing_size, "reason": "existing_order_kept", "price_moved": False}
        submitted = self._submit_live_limit(symbol, side, remaining_qty, target_price, reduce_only=True)
        self.sync_open_orders(symbol)
        active_count = sum(1 for o in self.open_orders if o.get("symbol") == symbol and bool(o.get("reduce_only", False)) and o.get("side") == side)
        return {"submitted": submitted, "active_cleanup_orders": active_count, "canceled": canceled, "price": target_price, "size": remaining_qty, "reason": "submitted" if submitted else "submit_failed", "price_moved": price_moved, "size_changed": size_changed}

    def flatten_position(self, symbol: str, mark_price: float, *, maker_first: bool | None = None) -> bool:
        self.sync_account(symbol, mark_price)
        if abs(self.state.position_size) < 1e-12:
            self._flatten_pending = None
            return False
        side = "buy" if self.state.position_size < 0 else "sell"
        qty = abs(self.state.position_size)
        use_maker = self.flatten_maker_first if maker_first is None else maker_first
        if use_maker and self.flatten_maker_wait_seconds > 0:
            # Passive reduce-only on our own side of the book (post-only, non-crossing)
            # so the forced exit pays the maker fee, not a taker cross. If it has not
            # filled by the deadline, _maybe_escalate_flatten() crosses aggressively.
            maker_px = mark_price * 0.9999 if side == "sell" else mark_price * 1.0001
            placed = self._submit_live_limit(symbol, side, qty, maker_px, reduce_only=True, prefer_post_only=True)
            if placed:
                self._flatten_pending = {"symbol": symbol, "deadline": time.time() + self.flatten_maker_wait_seconds, "side": side}
                logger.info("flatten_maker_first_submitted symbol=%s side=%s qty=%s px=%s wait=%s", symbol, side, qty, maker_px, self.flatten_maker_wait_seconds)
                return True
        # Aggressive taker cross (escalation, or maker disabled).
        limit_price = mark_price * 1.002 if side == "buy" else mark_price * 0.998
        self._flatten_pending = None
        return self._submit_live_limit(symbol, side, qty, limit_price, reduce_only=True)

    def _maybe_escalate_flatten(self, symbol: str, mark_price: float) -> None:
        """If a maker-first flatten has not filled by its deadline and the position
        is still open, cancel any resting orders and cross aggressively (taker)."""
        pending = self._flatten_pending
        if not pending or time.time() < float(pending.get("deadline", 0.0)):
            return
        self.sync_account(symbol, mark_price)
        if abs(self.state.position_size) < 1e-12:
            self._flatten_pending = None
            return
        logger.warning("flatten_maker_escalation symbol=%s remaining=%s -> taker cross", symbol, self.state.position_size)
        self.cancel_all_orders(symbol, force=True)
        self.flatten_position(symbol, mark_price, maker_first=False)

    def on_candle(self, candle: dict, *, regime: str = "unknown", mode: str = "unknown", risk_state: str = "OK", pause_reason: str = "", strategy_status: str = "running", orderbook_classification: str = "", orderbook_imbalance_ratio: float | None = None, orderbook_pressure_score: float | None = None, prediction_bias: str = "") -> list[dict]:
        symbol = str(candle.get("symbol") or "")
        if not symbol and self.open_orders:
            symbol = str(self.open_orders[0].get("symbol", ""))
        if self._flatten_pending and symbol:
            mark = float(candle.get("close", candle.get("c", 0.0)) or 0.0)
            if mark > 0:
                self._maybe_escalate_flatten(symbol, mark)
        # No candle-touch fill simulation in live mode. This only polls exchange fills.
        return self.sync_user_fills(symbol or None, regime=regime, mode=mode, risk_state=risk_state, pause_reason=pause_reason, orderbook_classification=orderbook_classification, orderbook_imbalance_ratio=orderbook_imbalance_ratio, orderbook_pressure_score=orderbook_pressure_score, prediction_bias=prediction_bias)

    def sync_user_fills(self, symbol: str | None = None, *, regime: str = "unknown", mode: str = "unknown", risk_state: str = "OK", pause_reason: str = "", orderbook_classification: str = "", orderbook_imbalance_ratio: float | None = None, orderbook_pressure_score: float | None = None, prediction_bias: str = "") -> list[dict]:
        fills = self.client.get_user_fills(symbol)
        new_entries: list[dict] = []
        new_seen_fill = False
        for raw in fills:
            fill_id = self._fill_id(raw)
            if fill_id in self._seen_fill_ids:
                continue
            is_bot_fill = self._is_bot_exchange_fill(raw)
            self._seen_fill_ids.add(fill_id)
            new_seen_fill = True
            if not is_bot_fill:
                logger.info("external_fill_ignored_for_bot_pnl symbol=%s oid=%s tid=%s", raw.get("coin", symbol or ""), raw.get("oid"), raw.get("tid"))
                continue
            entry = self._ledger_entry_from_exchange_fill(raw, regime=regime, mode=mode, risk_state=risk_state, pause_reason=pause_reason, orderbook_classification=orderbook_classification, orderbook_imbalance_ratio=orderbook_imbalance_ratio, orderbook_pressure_score=orderbook_pressure_score, prediction_bias=prediction_bias, is_bot_fill=True)
            realized_delta = float(entry.get("realized_pnl_delta", 0.0) or 0.0)
            fee = float(entry.get("fee", 0.0) or 0.0)
            self.state.realized_pnl += realized_delta
            self.state.fees_paid += fee
            self.state.bot_realized_pnl += realized_delta
            self.state.bot_fees_paid += fee
            self.state.bot_daily_realized_pnl += realized_delta
            self.state.bot_daily_fees_paid += fee
            self.state.bot_position_size += _signed_order_size(str(entry.get("side", "")), float(entry.get("qty", 0.0) or 0.0))
            if abs(self.state.bot_position_size) < 1e-12:
                self.state.bot_position_size = 0.0
            if entry.get("is_flip_trade"):
                self.flip_trade_count += 1
                self.last_flip_trade_ts = datetime.now(timezone.utc)
                entry["flip_trade_count"] = self.flip_trade_count
            entry["realized_pnl_total"] = self.state.realized_pnl
            entry["fees_paid"] = self.state.fees_paid
            entry["bot_position_size"] = self.state.bot_position_size
            entry["bot_daily_realized_pnl"] = self.state.bot_daily_realized_pnl
            entry["bot_daily_fees_paid"] = self.state.bot_daily_fees_paid
            self._record_order_event("filled", symbol=str(entry.get("symbol", symbol or "")), side=str(entry.get("side", "")), price=float(entry.get("price", 0.0) or 0.0), size=float(entry.get("qty", 0.0) or 0.0))
            self._record_fill_health(entry)
            entry["paired_tp_placed"] = self._maybe_place_paired_take_profit(str(entry.get("symbol") or symbol or ""), entry, raw)
            self.trade_log.append(entry)
            self._append_trade_ledger(entry)
            new_entries.append(entry)
        if new_entries:
            self.no_fill_cycles = 0
            self.last_real_trade_ts = datetime.now(timezone.utc)
            if symbol:
                self.sync_account(symbol, None)
            self.save_state()
        else:
            self.no_fill_cycles += 1
            if new_seen_fill:
                self.save_state()
        return new_entries


    def _is_bot_exchange_fill(self, raw: dict) -> bool:
        oid = raw.get("oid")
        if oid is None:
            return False
        try:
            return int(oid) in self._bot_order_oids
        except (TypeError, ValueError):
            return False


    def _append_trade_ledger(self, entry: dict) -> None:
        self.trade_ledger_csv.parent.mkdir(parents=True, exist_ok=True)
        fields = ["timestamp", "symbol", "side", "price", "qty", "notional", "fee", "fill_liquidity", "is_maker_fill", "is_taker_fill", "dust_fill", "realized_pnl_delta", "realized_pnl_total", "cash", "equity", "position_before", "position_after", "position_size", "position_notional", "exposure_action", "trade_category", "is_flip_trade", "flip_trade_count", "regime", "mode", "risk_state", "pause_reason", "reason", "order_source", "is_bot_fill", "sub_10_usd_fill", "sub_5_usd_fill", "orderbook_classification", "orderbook_imbalance_ratio", "orderbook_pressure_score", "prediction_bias"]
        write_header = not self.trade_ledger_csv.exists()
        with self.trade_ledger_csv.open("a", encoding="utf-8") as f:
            if write_header:
                f.write(",".join(fields) + "\n")
            f.write(",".join(str(entry.get(k, "")) for k in fields) + "\n")
        with self.trade_ledger_jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def last_closed_trade_net_pnls(self, limit: int) -> list[float]:
        if limit <= 0 or not self.trade_ledger_csv.exists():
            return []
        import csv
        pnls: list[float] = []
        with self.trade_ledger_csv.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    realized = float(row.get("realized_pnl_delta", 0.0) or 0.0)
                    fee = abs(float(row.get("fee", 0.0) or 0.0))
                except (TypeError, ValueError):
                    continue
                if abs(realized) > 1e-12:
                    pnls.append(realized - fee)
        return pnls[-limit:]

    def consume_trade_log(self) -> list[dict]:
        out = list(self.trade_log)
        self.trade_log.clear()
        return out

    def _submit_live_limit(self, symbol: str, side: str, size: float, price: float, *, reduce_only: bool, prefer_post_only: bool = False) -> bool:
        self.last_submit_error_kind = None
        # During an address rate-limit cooldown every extra request only deepens
        # the deficit, so skip new exposure; reduce-only closes stay allowed.
        if not reduce_only and time.time() < self.rate_limited_until:
            self.rate_limited_skip_count += 1
            logger.warning(
                "live_order_skipped reason=rate_limit_cooldown seconds_remaining=%.0f side=%s price=%s size=%s rate_limited_skip_count=%s",
                self.rate_limited_until - time.time(),
                side,
                price,
                size,
                self.rate_limited_skip_count,
            )
            return False
        normalized_price = normalize_price(symbol, price)
        normalized_size = normalize_size(symbol, size)
        notional_decimal = abs(Decimal(str(normalized_size)) * Decimal(str(normalized_price)))
        min_notional_decimal = Decimal(str(self.min_order_notional_usd if not reduce_only else min(self.min_notional_usd, 5.0)))
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
            if not reduce_only:
                self.min_notional_blocked_count += 1
                logger.warning(
                    "live_order_skipped reason=min_notional min_notional_blocked side=%s price=%s qty=%s notional=%s min_order_notional_usd=%s",
                    side,
                    normalized_price,
                    normalized_size,
                    notional_decimal,
                    self.min_order_notional_usd,
                )
            else:
                logger.warning(
                    "live_order_skipped reason=non_positive_reduce_only side=%s price=%s qty=%s",
                    side,
                    normalized_price,
                    normalized_size,
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
        if not reduce_only and abs((self.state.position_size + _signed_order_size(side, normalized_size)) * normalized_price) > self.max_position_notional_usd + 1e-9:
            logger.warning("live_order_blocked reason=max_position_notional side=%s price=%s size=%s", side, normalized_price, normalized_size)
            return False
        # Grid entries are post-only (Alo) so they always rest as maker; reduce-only
        # closes default to Gtc because they may need to cross the book, but
        # callers (paired TP) can prefer post-only to keep the maker rate.
        post_only = self.use_alo_orders and (not reduce_only or prefer_post_only)
        logger.info("order_submitted mode=live side=%s price=%s qty=%s notional=%s reduce_only=%s post_only=%s", side, normalized_price, normalized_size, notional, reduce_only, post_only)
        self._record_exchange_action()
        try:
            response = self.client.place_limit_order(symbol, side, normalized_size, normalized_price, reduce_only=reduce_only, post_only=post_only)
        except ValueError as exc:
            logger.warning("live_order_skipped reason=sdk_wire_rounding_error side=%s price=%s size=%s error=%s", side, normalized_price, normalized_size, exc)
            return False
        error = _order_submit_error(response)
        if error:
            if post_only and _is_post_only_reject(error):
                # The level would have crossed the spread (taker fill); skipping it
                # is the intended fee protection, the next tick re-plans the grid.
                self.last_submit_error_kind = "alo_reject"
                self.alo_rejected_count += 1
                logger.warning("live_order_alo_rejected side=%s price=%s size=%s alo_rejected_count=%s error=%s", side, normalized_price, normalized_size, self.alo_rejected_count, error)
            elif _is_cumulative_rate_limit_error(error):
                self.last_submit_error_kind = "rate_limit"
                self.rate_limit_trigger_count += 1
                self.last_rate_limit_ts = time.time()
                self.last_request_deficit = _extract_request_deficit(error)
                deficit_cooldown = self.last_request_deficit * self.rate_limit_deficit_cooldown_seconds_per_request
                cooldown_seconds = min(max(self.rate_limit_cooldown_seconds, deficit_cooldown), self.rate_limit_max_cooldown_seconds)
                if cooldown_seconds > 0:
                    self.rate_limited_until = time.time() + cooldown_seconds
                logger.warning(
                    "live_order_rate_limited cooldown_seconds=%.0f request_deficit=%.0f rate_limit_trigger_count=%s side=%s price=%s size=%s error=%s",
                    cooldown_seconds,
                    self.last_request_deficit,
                    self.rate_limit_trigger_count,
                    side,
                    normalized_price,
                    normalized_size,
                    error,
                )
            else:
                self.last_submit_error_kind = "rejected"
                logger.warning("live_order_rejected side=%s price=%s size=%s reduce_only=%s error=%s", side, normalized_price, normalized_size, reduce_only, error)
            return False
        oid = _extract_order_oid(response)
        if oid is not None:
            self._bot_order_oids.add(oid)
        self._record_order_event("submitted", symbol=symbol, side=side, price=normalized_price, size=normalized_size)
        return True

    def _is_opening_fill(self, raw: dict, entry: dict) -> bool:
        if bool(raw.get("reduceOnly", raw.get("reduce_only", False))):
            return False
        direction = str(raw.get("dir", "")).lower()
        if direction:
            return direction.startswith("open")
        return entry.get("exposure_action") == "increasing"

    def _new_exposure_qty(self, entry: dict, fill_qty: float) -> float:
        """Quantity of NEW exposure this fill opened, for TP sizing.

        A flip (Short>Long / Long>Short) closes the old side and opens a small
        residual in the new direction; only that residual (position_after) needs
        a take-profit, not the whole flip size. A plain opening/increasing fill
        opened its full quantity.
        """
        if entry.get("is_flip_trade") or entry.get("exposure_action") == "flipping":
            return abs(float(entry.get("position_after", 0.0) or 0.0))
        return fill_qty

    def _maybe_place_paired_take_profit(self, symbol: str, entry: dict, raw: dict) -> bool:
        """Pair a fill that opened new exposure with a reduce-only take-profit.

        This realizes the grid roundtrip immediately instead of waiting for a
        regime flip or recenter to close inventory. Covers plain opening fills
        AND the residual a flip leaves behind, so a flip never strands an
        un-managed position without a close order (observed live: a $7 long left
        by a Short>Long flip lingered with no TP).
        """
        if not self.paired_take_profit_enabled or not symbol:
            return False
        is_flip = bool(entry.get("is_flip_trade")) or entry.get("exposure_action") == "flipping"
        if not is_flip and not self._is_opening_fill(raw, entry):
            return False
        fill_price = float(entry.get("price", 0.0) or 0.0)
        tp_qty = self._new_exposure_qty(entry, float(entry.get("qty", 0.0) or 0.0))
        spacing = max(float(self.last_grid_spacing_pct), 0.0) * self.paired_tp_spacing_multiplier
        if fill_price <= 0 or tp_qty <= 0 or spacing <= 0:
            return False
        side = str(entry.get("side", "")).lower()
        tp_side = "sell" if side == "buy" else "buy"
        tp_price = fill_price * (1.0 + spacing) if side == "buy" else fill_price * (1.0 - spacing)
        try:
            # Post-only so the TP rests as maker (a third of the taker fee).
            # If ALO is rejected the price already moved past the TP level —
            # crossing as taker would pay 3x fees and was the #1 source of
            # taker losses in live data.  Skip the TP; position management
            # or the next grid cycle will handle the inventory.
            submitted = self._submit_live_limit(symbol, tp_side, tp_qty, tp_price, reduce_only=True, prefer_post_only=self.paired_tp_post_only)
            if not submitted and self.paired_tp_post_only and self.last_submit_error_kind == "alo_reject":
                logger.info("paired_tp_alo_rejected_skip tp_side=%s tp_price=%s reason=avoid_taker_fee", tp_side, tp_price)
                return False
        except Exception as exc:
            # TP placement must never break fill ingestion; the next tick's
            # position management still covers the open inventory.
            logger.warning("paired_tp_error entry_side=%s tp_side=%s tp_price=%s error=%s", side, tp_side, tp_price, exc)
            return False
        if submitted:
            self.paired_tp_placed_count += 1
            logger.info(
                "paired_tp_submitted entry_side=%s entry_price=%s qty=%s tp_side=%s tp_price=%s spacing_pct=%s is_flip=%s paired_tp_placed_count=%s",
                side, fill_price, tp_qty, tp_side, tp_price, spacing, is_flip, self.paired_tp_placed_count,
            )
        else:
            logger.warning(
                "paired_tp_skipped entry_side=%s entry_price=%s qty=%s tp_side=%s tp_price=%s spacing_pct=%s reason=submit_failed",
                side, fill_price, tp_qty, tp_side, tp_price, spacing,
            )
        return submitted

    def _fill_id(self, raw: dict) -> str:
        for key in ("tid", "hash", "oid"):
            if raw.get(key) is not None:
                return f"{key}:{raw.get(key)}:{raw.get('time', '')}"
        return json.dumps(raw, sort_keys=True)

    def _ledger_entry_from_exchange_fill(self, raw: dict, *, regime: str, mode: str, risk_state: str, pause_reason: str, orderbook_classification: str = "", orderbook_imbalance_ratio: float | None = None, orderbook_pressure_score: float | None = None, prediction_bias: str = "", is_bot_fill: bool = False) -> dict:
        price = float(raw.get("px", raw.get("price", 0.0)) or 0.0)
        size = float(raw.get("sz", raw.get("size", 0.0)) or 0.0)
        side_raw = str(raw.get("side", "")).lower()
        side = "buy" if side_raw in {"b", "buy"} else "sell"
        notional = abs(price * size)
        fee = abs(float(raw.get("fee", 0.0) or 0.0))
        fill_liquidity = _classify_fill_liquidity(raw, default="maker")
        is_dust_fill = _is_dust_notional(notional, self.dust_position_notional_usd)
        realized_delta = float(raw.get("closedPnl", raw.get("closed_pnl", 0.0)) or 0.0)
        ts_raw = raw.get("time")
        if ts_raw is not None:
            timestamp = datetime.fromtimestamp(float(ts_raw) / 1000, tz=timezone.utc).isoformat()
        else:
            timestamp = datetime.now(timezone.utc).isoformat()
        position_before = self.state.position_size
        exposure_action = classify_exposure_action(position_before, side, size)
        trade_category = trade_category_for_action(exposure_action)
        is_flip_trade = exposure_action == "flipping"
        position_after = position_before + _signed_order_size(side, size)
        position_notional = abs(self.state.position_size * price)
        return {
            "timestamp": timestamp,
            "symbol": raw.get("coin", ""),
            "side": side,
            "price": price,
            "qty": size,
            "notional": notional,
            "fee": fee,
            "fill_liquidity": fill_liquidity,
            "is_maker_fill": fill_liquidity == "maker",
            "is_taker_fill": fill_liquidity == "taker",
            "dust_fill": is_dust_fill,
            "realized_pnl_delta": realized_delta,
            "realized_pnl_total": self.state.realized_pnl + realized_delta,
            "cash": self.state.cash,
            "equity": self.state.cash,
            "position_before": position_before,
            "position_after": position_after,
            "position_size": self.state.position_size,
            "position_notional": position_notional,
            "exposure_action": exposure_action,
            "trade_category": trade_category,
            "is_flip_trade": is_flip_trade,
            "flip_trade_count": self.flip_trade_count,
            "regime": regime,
            "mode": mode,
            "risk_state": risk_state,
            "pause_reason": pause_reason,
            "reason": "exchange_fill",
            "order_source": raw.get("order_source") or ("reduce_only" if bool(raw.get("reduceOnly", raw.get("reduce_only", False))) else "normal_entry"),
            "is_bot_fill": is_bot_fill,
            "sub_10_usd_fill": 0.0 < notional < 10.0,
            "sub_5_usd_fill": 0.0 < notional < 5.0,
            "orderbook_classification": orderbook_classification,
            "orderbook_imbalance_ratio": orderbook_imbalance_ratio,
            "orderbook_pressure_score": orderbook_pressure_score,
            "prediction_bias": prediction_bias,
        }


# Offline simulation utility retained for unit tests only. Production uses LiveExecutionEngine.
ExecutionEngine = PaperExecutionEngine
