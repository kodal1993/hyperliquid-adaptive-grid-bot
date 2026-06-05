from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
import pandas as pd
from .config import BotConfig
from .execution_engine import ExecutionEngine
from .grid_manager import GridManager, GridPlan
from .regime_detector import RegimeDetector
from .risk_manager import RiskManager
from .types import GridMode, MarketRegime


logger = logging.getLogger(__name__)


class StrategyOrchestrator:
    def __init__(self, config: BotConfig, execution_engine: ExecutionEngine) -> None:
        self.config = config
        self.execution_engine = execution_engine
        self.detector = RegimeDetector()
        self.risk = RiskManager(config.max_drawdown_pct, config.daily_loss_limit_pct)
        self.grid_manager = GridManager()
        self.last_recenter_ts: datetime | None = None
        self.last_trend_regime: MarketRegime | None = None
        self.trend_flip_cooldown_remaining = 0
        self.trend_flip_cooldown_ticks = 6
        self.wrong_way_exit_loss_pct = 0.0035
        self.orphan_order_cleanup_count = 0
        self.stale_order_cleanup_count = 0

    def on_tick(self, candles: pd.DataFrame, equity: float, daily_pnl_pct: float, symbol: str, position_notional: float = 0.0) -> dict:
        regime = self.detector.detect(candles, trend_lookback_candles=self.config.trend_lookback_candles, trend_move_threshold_pct=self.config.trend_move_threshold_pct, ema_slope_threshold_pct=self.config.ema_slope_threshold_pct)
        price = float(candles["close"].iloc[-1])
        pos_size = self.execution_engine.paper.position_size
        avg_entry = float(getattr(self.execution_engine.paper, "avg_entry", 0.0) or 0.0)
        raw_position_notional = abs(position_notional)
        dust_threshold_usd = max(float(self.config.dust_position_notional_usd), 0.0)
        is_dust_position = 0 < raw_position_notional < dust_threshold_usd
        effective_position_notional = 0.0 if is_dust_position else raw_position_notional
        effective_position_size = 0.0 if is_dust_position else pos_size
        effective_position_side = "LONG" if effective_position_size > 0 else ("SHORT" if effective_position_size < 0 else "FLAT")
        auto_dust_cleanup_usd = max(float(self.config.auto_dust_cleanup_usd), 0.0)
        dust_cleanup_requested = 0 < raw_position_notional < auto_dust_cleanup_usd
        dust_context = {
            "position_notional_raw": raw_position_notional,
            "effective_position_notional": effective_position_notional,
            "effective_position_side": effective_position_side,
            "is_dust_position": is_dust_position,
            "dust_threshold_usd": dust_threshold_usd,
            "auto_dust_cleanup_usd": auto_dust_cleanup_usd,
            "dust_cleanup_requested": dust_cleanup_requested,
        }

        mode = GridMode.NEUTRAL
        neutral_entries_blocked = False
        if regime == MarketRegime.TREND_UP:
            if self.config.allow_long_biased:
                mode = GridMode.LONG_BIASED
            else:
                neutral_entries_blocked = True
        elif regime == MarketRegime.TREND_DOWN:
            if self.config.allow_short_biased:
                mode = GridMode.SHORT_BIASED
            else:
                neutral_entries_blocked = True

        if dust_cleanup_requested:
            canceled = self.execution_engine.cancel_all_orders(symbol)
            flattened = self.execution_engine.flatten_position(symbol, price)
            logger.warning(
                "dust_cleanup_requested symbol=%s position_notional=%.8f threshold_usd=%.2f canceled=%s flattened=%s",
                symbol,
                raw_position_notional,
                auto_dust_cleanup_usd,
                canceled,
                flattened,
            )
            return {
                "status": "managing_position",
                "regime": regime.value,
                "risk": None,
                "mode": mode.value,
                "reason": "dust_cleanup_requested",
                "allowed_to_trade": False,
                "allowed_to_reduce": True,
                "canceled_orders": canceled,
                "flattened": flattened,
                "position_management_action": "flatten_dust",
                **dust_context,
            }

        trend_flip_detected = False
        if regime in {MarketRegime.TREND_UP, MarketRegime.TREND_DOWN}:
            if self.last_trend_regime in {MarketRegime.TREND_UP, MarketRegime.TREND_DOWN} and self.last_trend_regime != regime:
                trend_flip_detected = True
                self.trend_flip_cooldown_remaining = self.trend_flip_cooldown_ticks
                logger.info(
                    "trend_flip_detected previous=%s current=%s cooldown_ticks=%s",
                    self.last_trend_regime.value,
                    regime.value,
                    self.trend_flip_cooldown_remaining,
                )
            self.last_trend_regime = regime

        vol = candles["close"].pct_change().std()
        order_size = self._calculate_order_size(price)
        order_notional = order_size * price

        long_exposure = max(effective_position_size, 0.0) * price
        short_exposure = max(-effective_position_size, 0.0) * price
        threshold = self.config.max_position_notional_usd * self.config.max_directional_exposure_pct
        allow_buys = long_exposure < threshold
        allow_sells = short_exposure < threshold

        if effective_position_side == "LONG":
            allow_buys = False
        elif effective_position_side == "SHORT":
            allow_sells = False

        soft_cap = self.config.max_position_notional_usd * 0.6
        rebalance_cap = self.config.max_position_notional_usd * 0.8
        if effective_position_notional > soft_cap:
            if effective_position_side == "LONG":
                allow_buys = False
            elif effective_position_side == "SHORT":
                allow_sells = False
        force_reduce_only = effective_position_notional > rebalance_cap

        wrong_way_trend_position = (
            (regime == MarketRegime.TREND_DOWN and effective_position_side == "LONG")
            or (regime == MarketRegime.TREND_UP and effective_position_side == "SHORT")
        )
        wrong_way_loss_pct = 0.0
        if wrong_way_trend_position and avg_entry > 0:
            if effective_position_side == "LONG":
                wrong_way_loss_pct = max((avg_entry - price) / avg_entry, 0.0)
            elif effective_position_side == "SHORT":
                wrong_way_loss_pct = max((price - avg_entry) / avg_entry, 0.0)

        if wrong_way_trend_position and wrong_way_loss_pct >= self.wrong_way_exit_loss_pct:
            canceled = self.execution_engine.cancel_all_orders(symbol)
            flattened = self.execution_engine.flatten_position(symbol, price)
            self.trend_flip_cooldown_remaining = max(self.trend_flip_cooldown_remaining, self.trend_flip_cooldown_ticks)
            logger.warning(
                "wrong_way_trend_exit_requested regime=%s side=%s entry=%s price=%s loss_pct=%.5f threshold=%.5f canceled=%s flattened=%s",
                regime.value,
                effective_position_side,
                avg_entry,
                price,
                wrong_way_loss_pct,
                self.wrong_way_exit_loss_pct,
                canceled,
                flattened,
            )
            return {
                "status": "managing_position",
                "regime": regime.value,
                "risk": None,
                "mode": mode.value,
                "reason": "wrong_way_trend_exit_requested",
                "allowed_to_trade": False,
                "allowed_to_reduce": True,
                "canceled_orders": canceled,
                "flattened": flattened,
                "wrong_way_loss_pct": wrong_way_loss_pct,
                "wrong_way_exit_loss_pct": self.wrong_way_exit_loss_pct,
                **dust_context,
            }

        if force_reduce_only:
            if effective_position_side == "LONG":
                allow_sells = True
            elif effective_position_side == "SHORT":
                allow_buys = True

        if neutral_entries_blocked:
            if effective_position_side == "LONG":
                allow_buys = False
                allow_sells = True
            elif effective_position_side == "SHORT":
                allow_sells = False
                allow_buys = True
            else:
                allow_buys = False
                allow_sells = False

        if regime == MarketRegime.TREND_UP and mode == GridMode.LONG_BIASED and not force_reduce_only:
            allow_buys = effective_position_side != "LONG" and long_exposure < threshold
            allow_sells = effective_position_side == "LONG"
        if regime == MarketRegime.TREND_DOWN and mode == GridMode.SHORT_BIASED and not force_reduce_only:
            allow_sells = effective_position_side != "SHORT" and short_exposure < threshold
            allow_buys = effective_position_side == "SHORT"

        flip_cooldown_active = self.trend_flip_cooldown_remaining > 0
        if flip_cooldown_active and not force_reduce_only:
            if effective_position_side == "FLAT":
                allow_buys = False
                allow_sells = False
                self.trend_flip_cooldown_remaining -= 1
            elif regime == MarketRegime.TREND_UP and effective_position_side == "SHORT":
                allow_buys = True
                allow_sells = False
            elif regime == MarketRegime.TREND_DOWN and effective_position_side == "LONG":
                allow_buys = False
                allow_sells = True
            else:
                self.trend_flip_cooldown_remaining = max(self.trend_flip_cooldown_remaining - 1, 0)

        if is_dust_position and regime == MarketRegime.RANGE and mode == GridMode.NEUTRAL and not force_reduce_only:
            allow_buys = True
            allow_sells = True

        attempted_side = "both" if (allow_buys and allow_sells) else ("buy" if allow_buys else ("sell" if allow_sells else "none"))
        would_increase_exposure = (effective_position_side == "LONG" and attempted_side in {"buy", "both"}) or (effective_position_side == "SHORT" and attempted_side in {"sell", "both"})

        liquidation_distance_pct = 1.0 if self.config.paper_mode else 0.5
        one_direction_exposure_pct = 0.0 if equity <= 0 else effective_position_notional / equity
        risk_state = self.risk.evaluate(
            equity=equity, daily_pnl_pct=daily_pnl_pct, emergency_stop=self.config.emergency_stop, stop_file=self.config.emergency_stop_file,
            position_notional=effective_position_notional, max_position_notional=self.config.max_position_notional_usd,
            one_direction_exposure_pct=one_direction_exposure_pct, max_one_direction_exposure_pct=self.config.max_one_direction_exposure_pct,
            liquidation_distance_pct=liquidation_distance_pct, min_liquidation_distance_pct=self.config.liquidation_distance_min_pct,
            attempted_side=attempted_side, would_increase_exposure=would_increase_exposure,
        )
        logger.info(
            "risk_check risk_state=%s pause_reason=%s current_position_notional=%.2f effective_position_notional=%.2f is_dust_position=%s max_position_notional_usd=%.2f attempted_side=%s would_increase_exposure=%s exposure_reducing_override=%s decision=%s trend_flip_cooldown_remaining=%s wrong_way_loss_pct=%.5f",
            risk_state.reason or "ok",
            "" if risk_state.can_trade else risk_state.reason,
            raw_position_notional,
            effective_position_notional,
            is_dust_position,
            self.config.max_position_notional_usd,
            attempted_side,
            would_increase_exposure,
            risk_state.exposure_reducing_override,
            risk_state.decision,
            self.trend_flip_cooldown_remaining,
            wrong_way_loss_pct,
        )
        if effective_position_notional > self.config.max_position_notional_usd:
            canceled = self.execution_engine.cancel_all_orders(symbol)
            flattened = False
            if self.config.paper_mode and self.config.paper_auto_flatten_on_max_position:
                flattened = self.execution_engine.flatten_position(symbol, price)
            reduce_only_placed = 0 if flattened else self.execution_engine.place_reduce_only_orders(symbol, pos_size, price, order_size)
            logger.warning("reduce_only_requested symbol=%s reason=max_position_notional canceled=%s reduce_only_placed=%s", symbol, canceled, reduce_only_placed)
            return {"status": "paused", "regime": regime.value, "risk": risk_state, "reduce_only": True, "reason": "reduce_only_requested", "canceled_orders": canceled, "reduce_only_placed": reduce_only_placed, "flattened": flattened, **dust_context}

        if not risk_state.can_trade or regime == MarketRegime.RISK_OFF:
            if risk_state.reason == "max_position_notional":
                canceled = self.execution_engine.cancel_all_orders(symbol)
                flattened = False
                if self.config.paper_mode and self.config.paper_auto_flatten_on_max_position:
                    flattened = self.execution_engine.flatten_position(symbol, price)
                reduce_only_placed = 0 if flattened else self.execution_engine.place_reduce_only_orders(symbol, pos_size, price, self._calculate_order_size(price))
                logger.warning("reduce_only_requested symbol=%s reason=%s canceled=%s reduce_only_placed=%s", symbol, risk_state.reason, canceled, reduce_only_placed)
                return {"status": "paused", "regime": regime.value, "risk": risk_state, "reduce_only": True, "reason": "reduce_only_requested", "canceled_orders": canceled, "reduce_only_placed": reduce_only_placed, "flattened": flattened, **dust_context}
            return {"status": "paused", "regime": regime.value, "risk": risk_state, "reduce_only": False, **dust_context}

        no_fill_cycles = self.execution_engine.no_fill_cycles
        last_trade_age_hours = 0.0
        if self.execution_engine.last_real_trade_ts is not None:
            last_trade_age_hours = (datetime.now(timezone.utc) - self.execution_engine.last_real_trade_ts).total_seconds() / 3600

        now = datetime.now(timezone.utc)
        in_position = effective_position_side in {"LONG", "SHORT"}
        recenter_cooldown_seconds = self.config.position_recenter_cooldown_seconds if in_position else self.config.recenter_cooldown_seconds
        seconds_since_recenter = None
        if self.last_recenter_ts is not None:
            seconds_since_recenter = (now - self.last_recenter_ts).total_seconds()
        recenter_blocked_by_cooldown = seconds_since_recenter is not None and seconds_since_recenter < recenter_cooldown_seconds
        stale_recenter_requested = last_trade_age_hours >= self.config.grid_stale_max_hours
        no_fill_recenter_requested = no_fill_cycles >= self.config.no_fill_cycles_before_recenter
        recenter_requested = no_fill_recenter_requested or stale_recenter_requested
        open_orders = [o for o in getattr(self.execution_engine, "open_orders", []) if str(o.get("symbol", symbol)).upper() == symbol.upper()]
        buy_order_count = sum(1 for o in open_orders if str(o.get("side", "")).lower() == "buy")
        sell_order_count = sum(1 for o in open_orders if str(o.get("side", "")).lower() == "sell")
        orphan_position_no_orders = in_position and len(open_orders) == 0
        flat_without_orders = effective_position_side == "FLAT" and len(open_orders) == 0
        orphan_order_detected, orphan_reason = self._detect_flat_orphan_orders(
            open_orders=open_orders,
            position_side=effective_position_side,
            mode=mode,
            allowed_buys=allow_buys,
            allowed_sells=allow_sells,
        )
        stale_order_detected = self._has_stale_flat_order(open_orders, effective_position_side)
        cleanup_requested = orphan_order_detected or stale_order_detected
        cleanup_canceled = 0
        state_uncertain_skip_cycle = False
        if cleanup_requested:
            cleanup_label = "stale_order_detected" if stale_order_detected else "orphan_order_detected"
            if stale_order_detected:
                self.stale_order_cleanup_count += 1
            if orphan_order_detected:
                self.orphan_order_cleanup_count += 1
            logger.warning(
                "%s symbol=%s position_side=%s open_orders=%s buy_orders=%s sell_orders=%s mode=%s reason=%s stale_timeout_sec=%s",
                cleanup_label,
                symbol,
                effective_position_side,
                len(open_orders),
                buy_order_count,
                sell_order_count,
                mode.value,
                orphan_reason or ("stale_flat_order" if stale_order_detected else "unknown"),
                self.config.stale_order_max_age_sec,
            )
            try:
                cleanup_canceled = self.execution_engine.cancel_all_orders(symbol)
            except Exception as exc:
                logger.warning("state_uncertain_skip_cycle reason=orphan_cleanup_cancel_error error=%s", exc)
                return {
                    "status": "paused",
                    "regime": regime.value,
                    "risk": risk_state,
                    "mode": mode.value,
                    "reason": "state_uncertain_skip_cycle",
                    "allowed_to_trade": False,
                    "allowed_to_reduce": False,
                    "orphan_order_detected": orphan_order_detected,
                    "stale_order_detected": stale_order_detected,
                    "orphan_order_cleanup_count": self.orphan_order_cleanup_count,
                    "stale_order_cleanup_count": self.stale_order_cleanup_count,
                    "cleanup_canceled_orders": cleanup_canceled,
                    "state_uncertain_skip_cycle": True,
                    **dust_context,
                }
            post_cancel_orders = [o for o in getattr(self.execution_engine, "open_orders", []) if str(o.get("symbol", symbol)).upper() == symbol.upper()]
            if post_cancel_orders:
                state_uncertain_skip_cycle = True
                logger.warning("state_uncertain_skip_cycle reason=orphan_cleanup_cancel_not_confirmed remaining_open_orders=%s", len(post_cancel_orders))
                return {
                    "status": "paused",
                    "regime": regime.value,
                    "risk": risk_state,
                    "mode": mode.value,
                    "reason": "state_uncertain_skip_cycle",
                    "allowed_to_trade": False,
                    "allowed_to_reduce": False,
                    "orphan_order_detected": orphan_order_detected,
                    "stale_order_detected": stale_order_detected,
                    "orphan_order_cleanup_count": self.orphan_order_cleanup_count,
                    "stale_order_cleanup_count": self.stale_order_cleanup_count,
                    "cleanup_canceled_orders": cleanup_canceled,
                    "state_uncertain_skip_cycle": state_uncertain_skip_cycle,
                    **dust_context,
                }
            open_orders = []
            buy_order_count = 0
            sell_order_count = 0
        force_recenter = (recenter_requested and not recenter_blocked_by_cooldown) or orphan_position_no_orders or flat_without_orders or cleanup_requested
        if orphan_position_no_orders:
            logger.warning(
                "orphan_position_no_orders symbol=%s side=%s position_notional=%.2f forcing_recenter=True",
                symbol,
                effective_position_side,
                effective_position_notional,
            )
        if force_recenter:
            self.last_recenter_ts = now
            logger.info("grid_rebuild_requested reason=%s old_center=%s new_center=%s last_trade_age=%.3f no_fill_cycles=%s cooldown_seconds=%s in_position=%s orphan_position_no_orders=%s flat_without_orders=%s cleanup_requested=%s", "orphan_or_stale_cleanup" if cleanup_requested else ("flat_without_orders" if flat_without_orders else "recenter"), self.grid_manager.last_mid, price, last_trade_age_hours, no_fill_cycles, recenter_cooldown_seconds, in_position, orphan_position_no_orders, flat_without_orders, cleanup_requested)
        elif recenter_requested and recenter_blocked_by_cooldown:
            logger.info("grid_recenter_skipped_cooldown last_trade_age=%.3f no_fill_cycles=%s seconds_since_recenter=%.1f cooldown_seconds=%s in_position=%s", last_trade_age_hours, no_fill_cycles, seconds_since_recenter or 0.0, recenter_cooldown_seconds, in_position)
        recenter_context = {
            "last_recenter_ts": self.last_recenter_ts.isoformat() if self.last_recenter_ts else None,
            "seconds_since_recenter": seconds_since_recenter,
            "recenter_cooldown_seconds": recenter_cooldown_seconds,
            "recenter_requested": recenter_requested,
            "recenter_blocked_by_cooldown": recenter_blocked_by_cooldown,
            "force_recenter": force_recenter,
            "orphan_position_no_orders": orphan_position_no_orders,
            "flat_without_orders": flat_without_orders,
            "orphan_order_detected": orphan_order_detected,
            "stale_order_detected": stale_order_detected,
            "orphan_order_cleanup_count": self.orphan_order_cleanup_count,
            "stale_order_cleanup_count": self.stale_order_cleanup_count,
            "cleanup_canceled_orders": cleanup_canceled,
            "state_uncertain_skip_cycle": state_uncertain_skip_cycle,
            "pre_rebuild_open_orders": len(open_orders),
            "pre_rebuild_buy_orders": buy_order_count,
            "pre_rebuild_sell_orders": sell_order_count,
            "in_position_for_recenter": in_position,
            "trend_flip_cooldown_remaining": self.trend_flip_cooldown_remaining,
            "wrong_way_loss_pct": wrong_way_loss_pct,
        }

        if order_notional + 1e-9 < self.config.min_notional_usd:
            logger.warning("no_new_order_placed reason=min_notional order_notional=%.4f min_notional_usd=%.4f price=%.4f order_size=%.8f", order_notional, self.config.min_notional_usd, price, order_size)
        effective_grid_levels = self._exposure_limited_grid_levels(mode, allow_buys, allow_sells, order_notional, threshold)
        projected_one_side = self._projected_grid_notional(mode, allow_buys, allow_sells, order_notional, levels=effective_grid_levels)
        if effective_grid_levels < self.config.grid_levels:
            logger.warning("grid_levels_limited_by_exposure configured_levels=%s effective_levels=%s projected_one_side_notional=%.4f threshold=%.4f max_position_notional_usd=%.4f", self.config.grid_levels, effective_grid_levels, projected_one_side, threshold, self.config.max_position_notional_usd)
        build_allow_buys = allow_buys and effective_grid_levels > 0
        build_allow_sells = allow_sells and effective_grid_levels > 0
        if effective_grid_levels <= 0 and (allow_buys or allow_sells):
            logger.warning("no_new_order_placed reason=max_exposure_too_small_for_min_order order_notional=%.4f threshold=%.4f", order_notional, threshold)
        plan: GridPlan = self.grid_manager.build_grid(price, max(effective_grid_levels, 1), self.config.grid_spacing_pct, float(vol), regime, order_size, self.config.regrid_threshold_pct, self.config.min_grid_profit_over_fees_pct, mode, allow_buys=build_allow_buys, allow_sells=build_allow_sells, force_recenter=force_recenter)
        try:
            result = self.execution_engine.cancel_replace_grid(symbol, plan)
        except Exception as exc:
            logger.warning("state_uncertain_skip_cycle reason=grid_rebuild_api_error error=%s", exc)
            return {"status": "paused", "regime": regime.value, "risk": risk_state, "mode": mode.value, "reason": "state_uncertain_skip_cycle", "allowed_to_trade": False, "allowed_to_reduce": False, **dust_context, **recenter_context}
        if result.get("error"):
            logger.warning("state_uncertain_skip_cycle reason=%s", result.get("error"))
            return {"status": "paused", "regime": regime.value, "risk": risk_state, "mode": mode.value, "reason": "state_uncertain_skip_cycle", "orders": result, "allowed_to_trade": False, "allowed_to_reduce": False, **dust_context, **recenter_context}
        logger.info("grid_state regime=%s mode=%s position_side=%s open_orders_before=%s buy_before=%s sell_before=%s allow_buys=%s allow_sells=%s placed=%s canceled=%s order_notional=%.4f", regime.value, mode.value, effective_position_side, len(open_orders), buy_order_count, sell_order_count, allow_buys, allow_sells, result.get("placed", 0), result.get("canceled", 0), order_notional)
        if neutral_entries_blocked and abs(effective_position_size) > 1e-12:
            return {"status": "managing_position", "regime": regime.value, "risk": risk_state, "orders": result, "mode": mode.value, "order_size": order_size, "order_notional": order_notional, "allow_buys": allow_buys, "allow_sells": allow_sells, "allowed_to_trade": False, "allowed_to_reduce": True, "reason": "neutral_entries_blocked_in_trend", "position_management_action": "reduce_only_grid", **dust_context, **recenter_context}
        if neutral_entries_blocked:
            canceled = self.execution_engine.cancel_all_orders(symbol)
            return {"status": "paused", "regime": regime.value, "risk": risk_state, "reason": "neutral_blocked_in_trend", "canceled_orders": canceled, "allowed_to_trade": False, "allowed_to_reduce": False, "position_management_action": "none", **dust_context, **recenter_context}
        return {"status": "running", "regime": regime.value, "risk": risk_state, "orders": result, "mode": mode.value, "order_size": order_size, "order_notional": order_notional, "allow_buys": allow_buys, "allow_sells": allow_sells, "allowed_to_trade": True, "allowed_to_reduce": effective_position_side in {"LONG", "SHORT"}, **dust_context, **recenter_context}

    def _expected_min_entry_levels(self, mode: GridMode, allowed_buys: bool, allowed_sells: bool) -> int:
        buy_count, sell_count = self.grid_manager._level_counts(self.config.grid_levels, mode)
        active_side_levels = []
        if allowed_buys:
            active_side_levels.append(buy_count)
        if allowed_sells:
            active_side_levels.append(sell_count)
        if not active_side_levels:
            return 0
        return min(max(2, min(active_side_levels)), 3)

    def _detect_flat_orphan_orders(self, *, open_orders: list[dict], position_side: str, mode: GridMode, allowed_buys: bool, allowed_sells: bool) -> tuple[bool, str]:
        if position_side != "FLAT" or not open_orders:
            return False, ""
        buy_count = sum(1 for o in open_orders if str(o.get("side", "")).lower() == "buy")
        sell_count = sum(1 for o in open_orders if str(o.get("side", "")).lower() == "sell")
        if buy_count and not allowed_buys:
            return True, "buy_order_not_allowed_by_current_strategy"
        if sell_count and not allowed_sells:
            return True, "sell_order_not_allowed_by_current_strategy"
        one_sided = (buy_count == 0) ^ (sell_count == 0)
        if one_sided:
            side_count = buy_count or sell_count
            expected_min = self._expected_min_entry_levels(mode, allowed_buys, allowed_sells)
            if side_count < expected_min:
                return True, f"one_sided_flat_grid_too_small:{side_count}<{expected_min}"
        return False, ""

    def _has_stale_flat_order(self, open_orders: list[dict], position_side: str) -> bool:
        if position_side != "FLAT" or self.config.stale_order_max_age_sec <= 0:
            return False
        now_ts = time.time()
        for order in open_orders:
            created_ts = order.get("created_ts") or order.get("timestamp") or order.get("time")
            if created_ts is None:
                continue
            try:
                created = float(created_ts)
            except (TypeError, ValueError):
                continue
            if created > 1_000_000_000_000:
                created /= 1000.0
            if now_ts - created >= self.config.stale_order_max_age_sec:
                return True
        return False

    def _projected_grid_notional(self, mode: GridMode, allow_buys: bool, allow_sells: bool, order_notional: float, *, levels: int | None = None) -> float:
        buy_levels, sell_levels = self.grid_manager._level_counts(self.config.grid_levels if levels is None else levels, mode)
        return max((buy_levels if allow_buys else 0) * order_notional, (sell_levels if allow_sells else 0) * order_notional)

    def _exposure_limited_grid_levels(self, mode: GridMode, allow_buys: bool, allow_sells: bool, order_notional: float, threshold: float) -> int:
        if order_notional <= 0 or threshold <= 0:
            return 0
        configured = max(int(self.config.grid_levels), 1)
        for levels in range(configured, 0, -1):
            if self._projected_grid_notional(mode, allow_buys, allow_sells, order_notional, levels=levels) <= threshold + 1e-9:
                return levels
        return 0

    def _calculate_order_size(self, price: float) -> float:
        raw_size = self.config.max_notional_per_trade_usd / max(price, 1e-9)
        size = min(max(raw_size, self.config.min_order_size), self.config.max_order_size)
        notional = size * price
        if notional > self.config.max_notional_per_trade_usd:
            size = self.config.max_notional_per_trade_usd / max(price, 1e-9)
            notional = size * price
        if notional < self.config.min_notional_usd:
            size = self.config.min_notional_usd / max(price, 1e-9)
        size = min(size, self.config.max_order_size)
        if size * price > self.config.max_notional_per_trade_usd:
            size = self.config.max_notional_per_trade_usd / max(price, 1e-9)
        return max(size, 0.0)
