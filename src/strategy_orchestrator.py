from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Protocol

import pandas as pd

from .config import BotConfig
from .grid_manager import GridManager, GridPlan
from .market_stress import OpportunityMode, decide_market_stress
from .orderbook_analyzer import OrderBookAnalyzer
from .prediction_layer import PredictionLayer, PredictionResult
from .regime_detector import RegimeDetector, calculate_atr_pct
from .risk_manager import RiskManager
from .types import GridMode, MarketRegime


class ExecutionEngineProtocol(Protocol):
    open_orders: list[dict]
    no_fill_cycles: int
    last_real_trade_ts: object

    def account_state(self, symbol: str, mark_price: float): ...
    def cancel_replace_grid(self, symbol: str, plan): ...
    def cancel_all_orders(self, symbol: str): ...
    def flatten_position(self, symbol: str, mark_price: float): ...
    def place_reduce_only_orders(self, symbol: str, position_size: float, mark_price: float, order_size: float, levels: int = 3): ...
    def ensure_reduce_only_close_order(self, symbol: str, position_size: float, mark_price: float, replace_threshold_pct: float = 0.0015): ...

logger = logging.getLogger(__name__)


class StrategyOrchestrator:
    def __init__(self, config: BotConfig, execution_engine: ExecutionEngineProtocol) -> None:
        self.config = config
        self.execution_engine = execution_engine
        self.detector = RegimeDetector()
        self.risk = RiskManager(config.max_drawdown_pct, config.daily_loss_limit_pct)
        self.grid_manager = GridManager()
        self.prediction_layer = PredictionLayer()
        self.orderbook_analyzer = OrderBookAnalyzer(
            top_levels=getattr(config, "orderbook_top_levels", 10),
            smoothing_window=getattr(config, "orderbook_smoothing_window", 5),
            min_samples=getattr(config, "orderbook_min_samples", 5),
            max_age_seconds=getattr(config, "orderbook_max_age_seconds", 5.0),
            ema_alpha=getattr(config, "orderbook_imbalance_ema_alpha", 0.35),
            min_confidence=getattr(config, "orderbook_min_confidence", 0.55),
        )
        self.orderbook_bullish_entries = 0
        self.orderbook_bearish_entries = 0
        self.orderbook_filtered_entries = 0
        self.dust_cleanup_count = 0
        self.last_recenter_ts: datetime | None = None
        self.last_trend_regime: MarketRegime | None = None
        self.pending_regime: MarketRegime | None = None
        self.pending_regime_count = 0
        self.trend_flip_cooldown_remaining = 0
        self.trend_flip_cooldown_ticks = max(int(getattr(config, "trend_flip_cooldown_ticks", 6)), 0)
        # Tick-based cooldowns shrink with the tick rate (6 ticks @ 10s = 60s),
        # which let regime flips churn minutes apart in choppy trends; the
        # minute-based floor makes the cooldown timer-real regardless of tick.
        flip_cooldown_minutes = max(float(getattr(config, "trend_flip_cooldown_minutes", 0.0) or 0.0), 0.0)
        if flip_cooldown_minutes > 0:
            tick_seconds = max(int(getattr(config, "tick_seconds", 10)), 1)
            self.trend_flip_cooldown_ticks = max(self.trend_flip_cooldown_ticks, int(flip_cooldown_minutes * 60 / tick_seconds))
        self.wrong_way_exit_loss_pct = 0.0035
        self.adverse_move_exit_pct = max(float(getattr(config, "adverse_move_exit_pct", 0.0045)), 0.0)
        self.orphan_order_cleanup_count = 0
        self.stale_order_cleanup_count = 0
        self.min_notional_blocked_count = 0
        self.anti_chop_trigger_count = 0
        self.anti_chop_cooldown_until: datetime | None = None
        self._last_anti_chop_signature: tuple[float, ...] = ()
        self.dust_cleanup_in_progress = False
        self.last_dust_cleanup_ts: float | None = None

    def on_tick(self, candles: pd.DataFrame, equity: float, daily_pnl_pct: float, symbol: str, position_notional: float = 0.0, order_book_snapshot: dict | None = None) -> dict:
        regime_signal = self.detector.detect_signal(candles, trend_lookback_candles=self.config.trend_lookback_candles, trend_move_threshold_pct=self.config.trend_move_threshold_pct, ema_slope_threshold_pct=self.config.ema_slope_threshold_pct, trend_strength_threshold=self.config.trend_strength_threshold)
        raw_regime = regime_signal.regime
        raw_regime_confidence = regime_signal.confidence
        regime = self._confirmed_regime(raw_regime, raw_regime_confidence, regime_signal.transition_confidence)
        regime_confidence = raw_regime_confidence if regime == raw_regime else min(raw_regime_confidence, self.config.trend_confidence_weak)
        if regime == MarketRegime.RANGE and regime_signal.transition_confidence >= 0.45:
            regime_confidence *= max(0.25, 1.0 - regime_signal.transition_confidence)
        price = float(candles["close"].iloc[-1])
        account_state = self.execution_engine.account_state(symbol, price)
        logger.info("execution_state state_source=%s equity=%.4f position_size=%.8f position_notional=%.4f", account_state.state_source, account_state.equity, account_state.position_size, account_state.position_notional)
        equity = account_state.equity
        pos_size = account_state.position_size
        avg_entry = float(account_state.avg_entry or 0.0)
        raw_position_notional = abs(account_state.position_notional if position_notional == 0.0 else position_notional)
        dust_threshold_usd = max(float(self.config.dust_position_notional_usd), 0.0)
        is_dust_position = 0 < raw_position_notional < dust_threshold_usd
        effective_position_notional = 0.0 if is_dust_position else raw_position_notional
        effective_position_size = 0.0 if is_dust_position else pos_size
        effective_position_side = "LONG" if effective_position_size > 0 else ("SHORT" if effective_position_size < 0 else "FLAT")
        if bool(getattr(self.config, "orderbook_filter_enabled", True)):
            orderbook_analysis = self.orderbook_analyzer.analyze(order_book_snapshot)
        else:
            orderbook_analysis = self.orderbook_analyzer.unavailable("orderbook_filter_disabled", fallback_reason="disabled")
        orderbook_context = orderbook_analysis.to_dict()
        logger.info(
            "orderbook_analysis available=%s ready=%s bid_volume_top10=%.8f ask_volume_top10=%.8f imbalance_ratio=%.6f pressure_score=%.6f classification=%s long_multiplier=%.2f short_multiplier=%.2f decision=%s orderbook_imbalance_ema=%s",
            orderbook_analysis.available,
            orderbook_analysis.ready,
            orderbook_analysis.bid_volume_top10,
            orderbook_analysis.ask_volume_top10,
            orderbook_analysis.imbalance_ratio,
            orderbook_analysis.pressure_score,
            orderbook_analysis.classification,
            orderbook_analysis.long_size_multiplier,
            orderbook_analysis.short_size_multiplier,
            orderbook_analysis.decision,
            orderbook_analysis.orderbook_imbalance_ema,
        )
        if not orderbook_analysis.available:
            logger.warning(
                "orderbook_unavailable symbol=%s reason=%s fallback=existing_strategy decision=%s",
                symbol,
                orderbook_analysis.fallback_reason or orderbook_analysis.decision,
                orderbook_analysis.decision,
            )
        prediction = self._build_prediction(
            candles=candles,
            regime=regime,
            regime_confidence=regime_confidence,
            regime_signal=regime_signal,
            orderbook_context=orderbook_context,
        )
        prediction_context = prediction.to_dict()
        logger.info(
            "prediction_layer available=%s directional_score=%.6f bullish_probability=%.4f bearish_probability=%.4f neutral_probability=%.4f confidence_score=%.4f prediction_bias=%s signal_weights_used=%s contributing_signals=%s fallback_reason=%s",
            prediction.available,
            prediction.directional_score,
            prediction.bullish_probability,
            prediction.bearish_probability,
            prediction.neutral_probability,
            prediction.confidence_score,
            prediction.prediction_bias,
            prediction.signal_weights_used,
            prediction.contributing_signals,
            prediction.fallback_reason,
        )
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
            "higher_high_sequence_score": regime_signal.higher_high_sequence_score,
            "higher_low_sequence_score": regime_signal.higher_low_sequence_score,
            "ema_slope_acceleration_score": regime_signal.ema_slope_acceleration_score,
            "momentum_acceleration_score": regime_signal.momentum_acceleration_score,
            "trend_transition_score": regime_signal.trend_transition_score,
            "transition_direction": regime_signal.transition_direction,
            "transition_confidence": regime_signal.transition_confidence,
            "trend_transition_delta": regime_signal.trend_transition_delta,
            "regime_hysteresis_score": regime_signal.regime_hysteresis_score,
            **orderbook_context,
            **prediction_context,
        }

        mode = GridMode.NEUTRAL
        neutral_entries_blocked = False
        if regime in {MarketRegime.TREND_UP, MarketRegime.TREND_UP_PULLBACK}:
            if self.config.allow_long_biased:
                mode = GridMode.LONG_BIASED
            else:
                neutral_entries_blocked = True
        elif regime in {MarketRegime.TREND_DOWN, MarketRegime.TREND_DOWN_PULLBACK}:
            if self.config.allow_short_biased:
                mode = GridMode.SHORT_BIASED
            else:
                neutral_entries_blocked = True

        if self.dust_cleanup_in_progress and abs(pos_size) < 1e-12:
            self.dust_cleanup_in_progress = False
            logger.info(
                "dust_cleanup_completed remaining_qty=%.8f remaining_notional=%.8f",
                pos_size,
                raw_position_notional,
            )
            dust_context["dust_cleanup_action"] = "completed"
            dust_context["dust_cleanup_in_progress"] = False

        if is_dust_position:
            dust_cleanup_action = "hold_for_next_normal_reduce_only_close"
            logger.info(
                "dust_detected dust_cleanup_action=%s remaining_qty=%.8f remaining_notional=%.8f threshold=%.2f",
                dust_cleanup_action,
                pos_size,
                raw_position_notional,
                dust_threshold_usd,
            )
            dust_context["dust_cleanup_action"] = dust_cleanup_action

            if str(getattr(self.config, "dust_cleanup_mode", "hold")).lower() == "reduce_only_once":
                now_ts = time.time()
                cooldown_seconds = max(int(getattr(self.config, "dust_cleanup_cooldown_seconds", 300)), 0)
                seconds_since_cleanup = None if self.last_dust_cleanup_ts is None else now_ts - self.last_dust_cleanup_ts
                cooldown_active = (
                    not self.dust_cleanup_in_progress
                    and self.last_dust_cleanup_ts is not None
                    and seconds_since_cleanup is not None
                    and seconds_since_cleanup < cooldown_seconds
                )
                dust_context.update({
                    "dust_cleanup_mode": "reduce_only_once",
                    "dust_cleanup_in_progress": self.dust_cleanup_in_progress,
                    "dust_cleanup_cooldown_seconds": cooldown_seconds,
                    "seconds_since_dust_cleanup": seconds_since_cleanup,
                    "remaining_qty": pos_size,
                    "remaining_notional": raw_position_notional,
                    "reduce_only": True,
                })
                if cooldown_active:
                    seconds_remaining = max(cooldown_seconds - float(seconds_since_cleanup or 0.0), 0.0)
                    logger.info(
                        "dust_cleanup_skipped_cooldown remaining_qty=%.8f remaining_notional=%.8f cooldown_seconds=%s seconds_remaining=%.1f reduce_only=true",
                        pos_size,
                        raw_position_notional,
                        cooldown_seconds,
                        seconds_remaining,
                    )
                    dust_context.update({"dust_cleanup_action": "skipped_cooldown", "dust_cleanup_cooldown_remaining_seconds": seconds_remaining})
                    return {"status": "managing_position", "regime": regime.value, "risk": None, "mode": mode.value, "reason": "dust_cleanup_skipped_cooldown", "allowed_to_trade": False, "allowed_to_reduce": True, "state_source": account_state.state_source, **dust_context}

                min_dust_cleanup_notional = 5.0
                if raw_position_notional + 1e-9 < min_dust_cleanup_notional:
                    logger.info(
                        "dust_cleanup_skipped_min_notional remaining_qty=%.8f remaining_notional=%.8f min_dust_cleanup_notional=%.2f source=dust_cleanup",
                        pos_size,
                        raw_position_notional,
                        min_dust_cleanup_notional,
                    )
                    dust_context.update({
                        "dust_cleanup_action": "skipped_below_5_usd",
                        "dust_cleanup_min_notional_usd": min_dust_cleanup_notional,
                        "dust_cleanup_in_progress": self.dust_cleanup_in_progress,
                        "order_source": "dust_cleanup",
                    })
                    return {"status": "managing_position", "regime": regime.value, "risk": None, "mode": mode.value, "reason": "dust_cleanup_below_min_notional", "allowed_to_trade": False, "allowed_to_reduce": True, "state_source": account_state.state_source, **dust_context}

                cleanup_result = self.execution_engine.ensure_reduce_only_close_order(symbol, pos_size, price, replace_threshold_pct=0.0015)
                active_cleanup_orders = int(cleanup_result.get("active_cleanup_orders", 0) or 0)
                self.dust_cleanup_in_progress = bool(cleanup_result.get("submitted") or active_cleanup_orders > 0)
                if cleanup_result.get("submitted") or not self.dust_cleanup_in_progress:
                    self.last_dust_cleanup_ts = now_ts
                if cleanup_result.get("submitted"):
                    self.dust_cleanup_count += 1
                    dust_context["dust_cleanup_count"] = self.dust_cleanup_count
                    logger.info(
                        "dust_cleanup_submitted remaining_qty=%.8f remaining_notional=%.8f price=%s reduce_only=true canceled_orders=%s active_cleanup_orders=%s",
                        pos_size,
                        raw_position_notional,
                        cleanup_result.get("price"),
                        cleanup_result.get("canceled", 0),
                        active_cleanup_orders,
                    )
                    dust_cleanup_action = "submitted"
                else:
                    logger.info(
                        "dust_cleanup_in_progress remaining_qty=%.8f remaining_notional=%.8f active_cleanup_orders=%s price=%s reduce_only=true reason=%s price_moved=%s",
                        pos_size,
                        raw_position_notional,
                        active_cleanup_orders,
                        cleanup_result.get("price"),
                        cleanup_result.get("reason"),
                        cleanup_result.get("price_moved", False),
                    )
                    dust_cleanup_action = "in_progress"
                dust_context.update({
                    "dust_cleanup_action": dust_cleanup_action,
                    "dust_cleanup_in_progress": self.dust_cleanup_in_progress,
                    "dust_cleanup_result": cleanup_result,
                    "active_dust_cleanup_orders": active_cleanup_orders,
                })
                return {"status": "managing_position", "regime": regime.value, "risk": None, "mode": mode.value, "reason": "dust_cleanup", "allowed_to_trade": False, "allowed_to_reduce": True, "state_source": account_state.state_source, **dust_context}

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

        vol = float(candles["close"].pct_change().std() or 0.0)
        atr_pct = regime_signal.atr_pct or calculate_atr_pct(candles, period=14)
        final_spacing_pct, spacing_source = self._calculate_spacing_pct(atr_pct, vol)
        volatility_grid_levels = self._volatility_adjusted_grid_levels(atr_pct)
        trend_bias = self._trend_bias(regime_confidence)
        logger.info("grid_spacing spacing_source=%s atr_pct=%.6f return_vol_pct=%.6f final_spacing_pct=%.6f volatility_grid_levels=%s raw_regime=%s regime=%s regime_confidence=%.3f trend_bias=%.2f", spacing_source, atr_pct, vol, final_spacing_pct, volatility_grid_levels, raw_regime.value, regime.value, regime_confidence, trend_bias)
        order_size = self._calculate_order_size(price)
        order_notional = order_size * price

        long_exposure = max(effective_position_size, 0.0) * price
        short_exposure = max(-effective_position_size, 0.0) * price
        threshold = min(self.config.max_position_notional_usd, self.config.max_active_exposure_usd) * self.config.max_directional_exposure_pct
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
            (regime in {MarketRegime.TREND_DOWN, MarketRegime.TREND_DOWN_PULLBACK} and effective_position_side == "LONG")
            or (regime in {MarketRegime.TREND_UP, MarketRegime.TREND_UP_PULLBACK} and effective_position_side == "SHORT")
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

        adverse_move_pct = self._adverse_move_pct(effective_position_side, avg_entry, price)
        mean_reversion_confirmed = self._mean_reversion_confirmed(
            candles,
            effective_position_side,
            avg_entry,
            price,
            regime_signal.transition_direction,
        )
        if (
            effective_position_side in {"LONG", "SHORT"}
            and adverse_move_pct > self.adverse_move_exit_pct
            and not mean_reversion_confirmed
        ):
            canceled = self.execution_engine.cancel_all_orders(symbol)
            flattened = self.execution_engine.flatten_position(symbol, price)
            logger.warning(
                "adverse_move_protection_exit_requested side=%s entry=%s price=%s adverse_move_pct=%.5f threshold=%.5f mean_reversion_confirmed=%s canceled=%s flattened=%s",
                effective_position_side,
                avg_entry,
                price,
                adverse_move_pct,
                self.adverse_move_exit_pct,
                mean_reversion_confirmed,
                canceled,
                flattened,
            )
            return {
                "status": "managing_position",
                "regime": regime.value,
                "risk": None,
                "mode": mode.value,
                "reason": "adverse_move_protection_exit_requested",
                "allowed_to_trade": False,
                "allowed_to_reduce": True,
                "canceled_orders": canceled,
                "flattened": flattened,
                "position_management_action": "adverse_move_close",
                "adverse_move_pct": adverse_move_pct,
                "adverse_move_exit_pct": self.adverse_move_exit_pct,
                "mean_reversion_confirmed": mean_reversion_confirmed,
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

        if regime in {MarketRegime.TREND_UP, MarketRegime.TREND_UP_PULLBACK} and mode == GridMode.LONG_BIASED and not force_reduce_only:
            allow_buys = effective_position_side != "LONG" and long_exposure < threshold
            allow_sells = effective_position_side == "LONG"
        if regime in {MarketRegime.TREND_DOWN, MarketRegime.TREND_DOWN_PULLBACK} and mode == GridMode.SHORT_BIASED and not force_reduce_only:
            allow_sells = effective_position_side != "SHORT" and short_exposure < threshold
            allow_buys = effective_position_side == "SHORT"

        flip_cooldown_active = self.trend_flip_cooldown_remaining > 0
        if flip_cooldown_active and not force_reduce_only:
            if effective_position_side == "FLAT":
                # Emergency flat fail-open: the trend-flip cooldown is allowed to
                # delay adding to an existing wrong-way position, but it must not
                # leave a live, risk-OK service flat with no replacement grid.
                self.trend_flip_cooldown_remaining -= 1
                logger.critical(
                    "trend_flip_cooldown_flat_fail_open_override regime=%s mode=%s allow_buys=%s allow_sells=%s cooldown_remaining_after=%s",
                    regime.value,
                    mode.value,
                    allow_buys,
                    allow_sells,
                    self.trend_flip_cooldown_remaining,
                )
            elif regime in {MarketRegime.TREND_UP, MarketRegime.TREND_UP_PULLBACK} and effective_position_side == "SHORT":
                allow_buys = True
                allow_sells = False
            elif regime in {MarketRegime.TREND_DOWN, MarketRegime.TREND_DOWN_PULLBACK} and effective_position_side == "LONG":
                allow_buys = False
                allow_sells = True
            else:
                self.trend_flip_cooldown_remaining = max(self.trend_flip_cooldown_remaining - 1, 0)

        if regime == MarketRegime.RANGE and not force_reduce_only and regime_signal.transition_confidence >= 0.55:
            if effective_position_side == "FLAT":
                logger.critical(
                    "range_transition_flat_fail_open_override transition_direction=%s transition_confidence=%.3f allow_buys=%s allow_sells=%s",
                    regime_signal.transition_direction,
                    regime_signal.transition_confidence,
                    allow_buys,
                    allow_sells,
                )
            elif regime_signal.transition_direction == "UP":
                allow_sells = effective_position_side == "LONG"
            elif regime_signal.transition_direction == "DOWN":
                allow_buys = effective_position_side == "SHORT"

        entry_filter_context = self._apply_counter_trend_entry_filter(
            allow_buys=allow_buys,
            allow_sells=allow_sells,
            position_side=effective_position_side,
            regime=regime,
            regime_signal=regime_signal,
            spacing_pct=final_spacing_pct,
            fee_rate=self._planning_fee_rate(),
            force_reduce_only=force_reduce_only,
        )
        allow_buys = entry_filter_context.pop("allow_buys")
        allow_sells = entry_filter_context.pop("allow_sells")

        orderbook_filter_context = self._apply_orderbook_entry_filter(
            allow_buys=allow_buys,
            allow_sells=allow_sells,
            position_side=effective_position_side,
            regime=regime,
            orderbook=orderbook_context,
            spacing_pct=final_spacing_pct,
            fee_rate=self._planning_fee_rate(),
            force_reduce_only=force_reduce_only,
        )
        allow_buys = orderbook_filter_context.pop("allow_buys")
        allow_sells = orderbook_filter_context.pop("allow_sells")
        if orderbook_filter_context.get("orderbook_filtered_buy"):
            self.orderbook_filtered_entries += 1
        if orderbook_filter_context.get("orderbook_filtered_sell"):
            self.orderbook_filtered_entries += 1
        orderbook_filter_context["orderbook_filtered_entries"] = self.orderbook_filtered_entries
        orderbook_filter_context["orderbook_bullish_entries"] = self.orderbook_bullish_entries
        orderbook_filter_context["orderbook_bearish_entries"] = self.orderbook_bearish_entries

        prediction_filter_context = self._apply_prediction_entry_filter(
            allow_buys=allow_buys,
            allow_sells=allow_sells,
            position_side=effective_position_side,
            regime=regime,
            prediction=prediction,
            force_reduce_only=force_reduce_only,
        )
        allow_buys = prediction_filter_context.pop("allow_buys")
        allow_sells = prediction_filter_context.pop("allow_sells")

        long_selectivity_context = self._apply_long_side_selectivity(
            allow_buys=allow_buys,
            allow_sells=allow_sells,
            position_side=effective_position_side,
            regime=regime,
            prediction=prediction,
            orderbook=orderbook_context,
            force_reduce_only=force_reduce_only,
        )
        allow_buys = long_selectivity_context.pop("allow_buys")
        allow_sells = long_selectivity_context.pop("allow_sells")

        allow_buys_before_stress = allow_buys
        allow_sells_before_stress = allow_sells
        grid_levels_before_stress = volatility_grid_levels
        spacing_before_stress = final_spacing_pct
        market_stress = decide_market_stress(
            candles=candles,
            raw_regime=raw_regime,
            confirmed_regime=regime,
            regime_confidence=regime_confidence,
            atr_pct=atr_pct,
            regime_trend_strength_score=regime_signal.trend_strength_score,
            position_side=effective_position_side,
            position_notional=effective_position_notional,
            config=self.config,
            allow_buys=allow_buys,
            allow_sells=allow_sells,
            current_levels=volatility_grid_levels,
        )
        allow_buys = market_stress.allow_buys
        allow_sells = market_stress.allow_sells
        volatility_grid_levels = market_stress.grid_levels
        final_spacing_pct *= market_stress.spacing_multiplier
        market_stress_context = {**entry_filter_context, **orderbook_filter_context, **prediction_filter_context, **market_stress.telemetry()}
        logger.info(
            "market_stress_decision volatility_mode=%s market_stress_score=%.3f trend_strength_score=%.3f opportunity_mode=%s allow_buys_before=%s allow_buys_after=%s allow_sells_before=%s allow_sells_after=%s grid_levels_before=%s grid_levels_after=%s spacing_before=%.6f spacing_after=%.6f reason=%s",
            market_stress.volatility_mode.value,
            market_stress.market_stress_score,
            market_stress.trend_strength_score,
            market_stress.opportunity_mode.value,
            allow_buys_before_stress,
            allow_buys,
            allow_sells_before_stress,
            allow_sells,
            grid_levels_before_stress,
            volatility_grid_levels,
            spacing_before_stress,
            final_spacing_pct,
            market_stress.reason,
        )
        if market_stress.emergency_flat:
            canceled = self.execution_engine.cancel_all_orders(symbol)
            flattened = self.execution_engine.flatten_position(symbol, price)
            logger.warning(
                "market_stress_emergency_flat symbol=%s position_side=%s position_notional=%.4f canceled=%s flattened=%s reason=%s",
                symbol,
                effective_position_side,
                effective_position_notional,
                canceled,
                flattened,
                market_stress.reason,
            )
            return {
                "status": "managing_position",
                "regime": regime.value,
                "risk": None,
                "mode": mode.value,
                "reason": "market_stress_emergency_flat",
                "allowed_to_trade": False,
                "allowed_to_reduce": True,
                "canceled_orders": canceled,
                "flattened": flattened,
                "position_management_action": "market_stress_emergency_flat",
                "regime_confidence": regime_confidence,
                "grid_spacing_pct": final_spacing_pct,
                "spacing_source": spacing_source,
                "atr_pct": atr_pct,
                "grid_levels": volatility_grid_levels,
                "volatility_grid_levels": volatility_grid_levels,
                "trend_bias": trend_bias,
                **dust_context,
                **market_stress_context,
            }

        attempted_side = "both" if (allow_buys and allow_sells) else ("buy" if allow_buys else ("sell" if allow_sells else "none"))
        would_increase_exposure = (
            (effective_position_side == "FLAT" and attempted_side != "none")
            or (effective_position_side == "LONG" and attempted_side in {"buy", "both"})
            or (effective_position_side == "SHORT" and attempted_side in {"sell", "both"})
        )
        daily_loss_limit_active = daily_pnl_pct <= -self.config.daily_loss_limit_pct

        liquidation_distance_pct = 0.5
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
            reduce_only_placed = self.execution_engine.place_reduce_only_orders(symbol, pos_size, price, order_size)
            logger.warning("reduce_only_requested symbol=%s reason=max_position_notional canceled=%s reduce_only_placed=%s", symbol, canceled, reduce_only_placed)
            return {"status": "paused", "regime": regime.value, "risk": risk_state, "reduce_only": True, "reason": "reduce_only_requested", "canceled_orders": canceled, "reduce_only_placed": reduce_only_placed, "flattened": flattened, "state_source": account_state.state_source, **dust_context, **market_stress_context}

        if market_stress.opportunity_mode in {OpportunityMode.NO_NEW_EXPOSURE, OpportunityMode.REDUCE_ONLY} and effective_position_side == "FLAT":
            canceled = self.execution_engine.cancel_all_orders(symbol)
            logger.warning(
                "market_stress_no_new_exposure symbol=%s opportunity_mode=%s canceled=%s reason=%s",
                symbol,
                market_stress.opportunity_mode.value,
                canceled,
                market_stress.reason,
            )
            return {
                "status": "paused",
                "regime": regime.value,
                "risk": risk_state,
                "mode": mode.value,
                "reason": "market_stress_no_new_exposure" if market_stress.opportunity_mode == OpportunityMode.NO_NEW_EXPOSURE else "market_stress_reduce_only_flat",
                "canceled_orders": canceled,
                "allowed_to_trade": False,
                "allowed_to_reduce": False,
                "position_management_action": "none",
                "state_source": account_state.state_source,
                "regime_confidence": regime_confidence,
                "grid_spacing_pct": final_spacing_pct,
                "spacing_source": spacing_source,
                "atr_pct": atr_pct,
                "grid_levels": volatility_grid_levels,
                "volatility_grid_levels": volatility_grid_levels,
                "trend_bias": trend_bias,
                **dust_context,
                **market_stress_context,
            }

        anti_chop_context = self._apply_anti_chop_cooldown(
            position_side=effective_position_side,
            allow_buys=allow_buys,
            allow_sells=allow_sells,
        )
        allow_buys = anti_chop_context.pop("allow_buys")
        allow_sells = anti_chop_context.pop("allow_sells")

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
        orphan_order_detected, orphan_reason, orphan_decision_context = self._detect_flat_orphan_orders(
            open_orders=open_orders,
            position_side=effective_position_side,
            mode=mode,
            allowed_buys=allow_buys,
            allowed_sells=allow_sells,
        )
        logger.info(
            "orphan_order_decision symbol=%s position_side=%s mode=%s allow_buys=%s allow_sells=%s expected_buy_orders=%s expected_sell_orders=%s actual_buy_orders=%s actual_sell_orders=%s orphan_detected=%s orphan_decision_reason=%s",
            symbol,
            effective_position_side,
            mode.value,
            allow_buys,
            allow_sells,
            orphan_decision_context["expected_buy_orders"],
            orphan_decision_context["expected_sell_orders"],
            orphan_decision_context["actual_buy_orders"],
            orphan_decision_context["actual_sell_orders"],
            orphan_order_detected,
            orphan_reason,
        )
        stale_order_detected = self._has_stale_flat_order(open_orders, effective_position_side)
        cleanup_requested = orphan_order_detected or stale_order_detected
        cleanup_canceled = 0
        state_uncertain_skip_cycle = False
        rate_limited_until = float(getattr(self.execution_engine, "rate_limited_until", 0.0) or 0.0)
        last_rate_limit_ts = float(getattr(self.execution_engine, "last_rate_limit_ts", 0.0) or 0.0)
        rate_limit_grace = max(float(getattr(self.execution_engine, "rate_limit_orphan_grace_seconds", 600.0) or 0.0), 0.0)
        # The cooldown alone is not enough: right after it expires the cleanup
        # runs before any placement attempt, so it would cancel the surviving
        # side again. Keep cleanup off for a grace period after the last hit.
        rate_limit_recently = last_rate_limit_ts > 0 and (time.time() - last_rate_limit_ts) < rate_limit_grace
        orphan_cleanup_skipped_rate_limit = False
        if cleanup_requested and (time.time() < rate_limited_until or rate_limit_recently):
            # While the address is over its cumulative request budget, a one-sided
            # grid is expected (placements get rejected). Canceling the surviving
            # side here only burns the trickle budget on cancel/replace loops.
            orphan_cleanup_skipped_rate_limit = True
            cleanup_requested = False
            orphan_order_detected = False
            stale_order_detected = False
            logger.warning(
                "orphan_cleanup_skipped_rate_limit_cooldown symbol=%s seconds_remaining=%.0f open_orders=%s",
                symbol,
                rate_limited_until - time.time(),
                len(open_orders),
            )
        if cleanup_requested:
            cleanup_label = "stale_order_detected" if stale_order_detected else "orphan_order_detected"
            if stale_order_detected:
                self.stale_order_cleanup_count += 1
            if orphan_order_detected:
                self.orphan_order_cleanup_count += 1
            logger.warning(
                "%s symbol=%s position_side=%s open_orders=%s buy_orders=%s sell_orders=%s mode=%s expected_buy_orders=%s expected_sell_orders=%s actual_buy_orders=%s actual_sell_orders=%s orphan_decision_reason=%s reason=%s stale_timeout_sec=%s",
                cleanup_label,
                symbol,
                effective_position_side,
                len(open_orders),
                buy_order_count,
                sell_order_count,
                mode.value,
                orphan_decision_context["expected_buy_orders"],
                orphan_decision_context["expected_sell_orders"],
                orphan_decision_context["actual_buy_orders"],
                orphan_decision_context["actual_sell_orders"],
                orphan_reason,
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
                    **orphan_decision_context,
                    "orphan_decision_reason": orphan_reason,
                    **dust_context,
                }
            post_cancel_orders = [o for o in getattr(self.execution_engine, "open_orders", []) if str(o.get("symbol", symbol)).upper() == symbol.upper()]
            post_cancel_buy_count = sum(1 for o in post_cancel_orders if str(o.get("side", "")).lower() == "buy")
            post_cancel_sell_count = sum(1 for o in post_cancel_orders if str(o.get("side", "")).lower() == "sell")
            if post_cancel_orders:
                state_uncertain_skip_cycle = True
                logger.warning(
                    "orphan_cleanup_cancel_not_confirmed symbol=%s remaining_open_orders=%s remaining_buy_orders=%s remaining_sell_orders=%s cleanup_label=%s cleanup_canceled_orders=%s rebuild_will_wait=True",
                    symbol,
                    len(post_cancel_orders),
                    post_cancel_buy_count,
                    post_cancel_sell_count,
                    cleanup_label,
                    cleanup_canceled,
                )
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
                    **orphan_decision_context,
                    "orphan_decision_reason": orphan_reason,
                    **dust_context,
                }
            logger.info(
                "orphan_cleanup_cancel_confirmed symbol=%s cleanup_label=%s canceled_orders=%s rebuild_requested_next=True",
                symbol,
                cleanup_label,
                cleanup_canceled,
            )
            open_orders = []
            buy_order_count = 0
            sell_order_count = 0

        hard_risk_block_active = (not risk_state.can_trade) or regime == MarketRegime.RISK_OFF
        daily_loss_position_management_active = daily_loss_limit_active or risk_state.reason in {"daily_loss", "daily_loss_limit"}
        if daily_loss_position_management_active:
            canceled = self.execution_engine.cancel_all_orders(symbol) if open_orders else 0
            reduce_only_placed = 0
            if effective_position_side in {"LONG", "SHORT"}:
                reduce_only_placed = self.execution_engine.place_reduce_only_orders(symbol, pos_size, price, self._calculate_order_size(price))
            logger.warning(
                "daily_loss_limit_position_management symbol=%s position_side=%s position_notional=%.4f canceled=%s reduce_only_placed=%s daily_pnl_pct=%.6f limit=%.6f",
                symbol,
                effective_position_side,
                effective_position_notional,
                canceled,
                reduce_only_placed,
                daily_pnl_pct,
                self.config.daily_loss_limit_pct,
            )
            return {
                "status": "paused",
                "regime": regime.value,
                "risk": risk_state,
                "mode": mode.value,
                "reduce_only": effective_position_side in {"LONG", "SHORT"},
                "reason": "daily_loss_limit_position_management",
                "allowed_to_trade": False,
                "allowed_to_reduce": effective_position_side in {"LONG", "SHORT"},
                "canceled_orders": canceled,
                "reduce_only_placed": reduce_only_placed,
                "position_management_action": "reduce_only_grid" if effective_position_side in {"LONG", "SHORT"} else "cancel_entries",
                "state_source": account_state.state_source,
                **dust_context,
                **market_stress_context,
            }

        if hard_risk_block_active:
            canceled = self.execution_engine.cancel_all_orders(symbol) if open_orders else 0
            reduce_only_placed = 0
            if effective_position_side in {"LONG", "SHORT"}:
                reduce_only_placed = self.execution_engine.place_reduce_only_orders(symbol, pos_size, price, self._calculate_order_size(price))
            logger.warning(
                "hard_risk_reduce_only_allowed symbol=%s reason=%s position_side=%s canceled=%s reduce_only_placed=%s",
                symbol,
                risk_state.reason or ("risk_off" if regime == MarketRegime.RISK_OFF else "unknown"),
                effective_position_side,
                canceled,
                reduce_only_placed,
            )
            return {
                "status": "paused",
                "regime": regime.value,
                "risk": risk_state,
                "mode": mode.value,
                "reduce_only": effective_position_side in {"LONG", "SHORT"},
                "reason": "reduce_only_requested" if effective_position_side in {"LONG", "SHORT"} else (risk_state.reason or "risk_off"),
                "allowed_to_trade": False,
                "allowed_to_reduce": effective_position_side in {"LONG", "SHORT"},
                "canceled_orders": canceled,
                "reduce_only_placed": reduce_only_placed,
                "position_management_action": "reduce_only_grid" if effective_position_side in {"LONG", "SHORT"} else "none",
                "state_source": account_state.state_source,
                **dust_context,
                **market_stress_context,
            }

        forced_rebuild = bool(orphan_position_no_orders or flat_without_orders or cleanup_requested)
        rebuild_reason = "stale_cleanup" if stale_order_detected else ("orphan_cleanup" if orphan_order_detected else ("orphan_position_no_orders" if orphan_position_no_orders else ("flat_without_orders" if flat_without_orders else ("stale_recenter" if stale_recenter_requested else ("no_fill_recenter" if no_fill_recenter_requested else "none")))))
        force_recenter = (recenter_requested and not recenter_blocked_by_cooldown) or forced_rebuild
        if orphan_position_no_orders:
            logger.warning(
                "orphan_position_no_orders symbol=%s side=%s position_notional=%.2f forcing_recenter=True",
                symbol,
                effective_position_side,
                effective_position_notional,
            )
        if force_recenter:
            self.last_recenter_ts = now
            logger.info("grid_rebuild_requested rebuild_reason=%s forced_rebuild=%s old_center=%s new_center=%s last_trade_age=%.3f no_fill_cycles=%s cooldown_seconds=%s in_position=%s orphan_position_no_orders=%s flat_without_orders=%s cleanup_requested=%s", rebuild_reason, forced_rebuild, self.grid_manager.last_mid, price, last_trade_age_hours, no_fill_cycles, recenter_cooldown_seconds, in_position, orphan_position_no_orders, flat_without_orders, cleanup_requested)
        elif recenter_requested and recenter_blocked_by_cooldown:
            seconds_remaining = max(recenter_cooldown_seconds - (seconds_since_recenter or 0.0), 0.0)
            cooldown_type = "position_recenter" if in_position else "recenter"
            logger.info("grid_recenter_skipped_cooldown cooldown_type=%s seconds_remaining=%.1f rebuild_reason=%s last_trade_age=%.3f no_fill_cycles=%s seconds_since_recenter=%.1f cooldown_seconds=%s in_position=%s", cooldown_type, seconds_remaining, rebuild_reason, last_trade_age_hours, no_fill_cycles, seconds_since_recenter or 0.0, recenter_cooldown_seconds, in_position)
        recenter_context = {
            "last_recenter_ts": self.last_recenter_ts.isoformat() if self.last_recenter_ts else None,
            "seconds_since_recenter": seconds_since_recenter,
            "recenter_cooldown_seconds": recenter_cooldown_seconds,
            "recenter_requested": recenter_requested,
            "recenter_blocked_by_cooldown": recenter_blocked_by_cooldown,
            "force_recenter": force_recenter,
            "forced_rebuild": forced_rebuild,
            "rebuild_reason": rebuild_reason,
            "orphan_position_no_orders": orphan_position_no_orders,
            "flat_without_orders": flat_without_orders,
            "orphan_order_detected": orphan_order_detected,
            "stale_order_detected": stale_order_detected,
            "orphan_order_cleanup_count": self.orphan_order_cleanup_count,
            "stale_order_cleanup_count": self.stale_order_cleanup_count,
            "cleanup_canceled_orders": cleanup_canceled,
            "state_uncertain_skip_cycle": state_uncertain_skip_cycle,
            "orphan_cleanup_skipped_rate_limit": orphan_cleanup_skipped_rate_limit,
            "pre_rebuild_open_orders": len(open_orders),
            "pre_rebuild_buy_orders": buy_order_count,
            "pre_rebuild_sell_orders": sell_order_count,
            **orphan_decision_context,
            "orphan_decision_reason": orphan_reason,
            "in_position_for_recenter": in_position,
            "trend_flip_cooldown_remaining": self.trend_flip_cooldown_remaining,
            "trend_flip_cooldown_ticks": self.trend_flip_cooldown_ticks,
            "regime_confirmation_bars": self.config.regime_confirmation_bars,
            "regime_min_confidence": self.config.regime_min_confidence,
            "raw_regime": raw_regime.value,
            "raw_regime_confidence": raw_regime_confidence,
            "regime_trend_structure_score": regime_signal.trend_structure_score,
            "regime_momentum_score": regime_signal.momentum_score,
            "regime_trend_strength_score": regime_signal.trend_strength_score,
            "higher_high_sequence_score": regime_signal.higher_high_sequence_score,
            "higher_low_sequence_score": regime_signal.higher_low_sequence_score,
            "ema_slope_acceleration_score": regime_signal.ema_slope_acceleration_score,
            "momentum_acceleration_score": regime_signal.momentum_acceleration_score,
            "trend_transition_score": regime_signal.trend_transition_score,
            "transition_direction": regime_signal.transition_direction,
            "transition_confidence": regime_signal.transition_confidence,
            "trend_transition_delta": regime_signal.trend_transition_delta,
            "regime_hysteresis_score": regime_signal.regime_hysteresis_score,
            "pending_regime": self.pending_regime.value if self.pending_regime else None,
            "pending_regime_count": self.pending_regime_count,
            "wrong_way_loss_pct": wrong_way_loss_pct,
        }

        orderbook_level_multiplier = float(orderbook_filter_context.get("orderbook_grid_level_multiplier", 1.0) or 1.0)
        before_orderbook_levels = volatility_grid_levels
        if effective_position_side == "FLAT" and orderbook_level_multiplier < 1.0 and (allow_buys or allow_sells):
            volatility_grid_levels = max(1, int(round(volatility_grid_levels * orderbook_level_multiplier)))
            logger.info(
                "orderbook_grid_level_adjustment before=%s after=%s multiplier=%.2f decision=%s",
                before_orderbook_levels,
                volatility_grid_levels,
                orderbook_level_multiplier,
                orderbook_filter_context.get("orderbook_decision"),
            )
        orderbook_filter_context["orderbook_levels_before"] = before_orderbook_levels
        orderbook_filter_context["orderbook_levels_after"] = volatility_grid_levels
        market_stress_context.update({
            "orderbook_levels_before": before_orderbook_levels,
            "orderbook_levels_after": volatility_grid_levels,
            "orderbook_action_taken": orderbook_filter_context.get("orderbook_action_taken"),
            "orderbook_fallback_reason": orderbook_filter_context.get("orderbook_fallback_reason", ""),
        })
        logger.info(
            "orderbook_filter_state enabled=%s soft_mode=%s available=%s stale=%s classification=%s imbalance_ratio=%.6f pressure_score=%.6f action_taken=%s levels_before=%s levels_after=%s fallback_reason=%s",
            bool(getattr(self.config, "orderbook_filter_enabled", True)),
            bool(getattr(self.config, "orderbook_soft_mode", True)),
            bool(orderbook_context.get("available")),
            bool(orderbook_context.get("stale")),
            orderbook_context.get("classification") or "Unavailable",
            float(orderbook_context.get("imbalance_ratio", 1.0) or 1.0),
            float(orderbook_context.get("pressure_score", 0.0) or 0.0),
            orderbook_filter_context.get("orderbook_action_taken", orderbook_filter_context.get("orderbook_decision", "none")),
            before_orderbook_levels,
            volatility_grid_levels,
            orderbook_context.get("fallback_reason") or orderbook_filter_context.get("orderbook_fallback_reason") or "",
        )

        min_order_notional_usd = max(float(getattr(self.config, "min_order_notional_usd", self.config.min_notional_usd)), 0.0)
        allow_buys_before_min_notional = allow_buys
        allow_sells_before_min_notional = allow_sells
        if order_notional + 1e-9 < min_order_notional_usd:
            blocked_sides = []
            if allow_buys and effective_position_side != "SHORT":
                allow_buys = False
                blocked_sides.append("buy")
            if allow_sells and effective_position_side != "LONG":
                allow_sells = False
                blocked_sides.append("sell")
            for blocked_side in blocked_sides:
                self.min_notional_blocked_count += 1
                logger.warning(
                    "min_notional_blocked side=%s price=%.4f qty=%.8f notional=%.4f min_order_notional_usd=%.4f",
                    blocked_side,
                    price,
                    order_size,
                    order_notional,
                    min_order_notional_usd,
                )
        configured_levels = max(int(self.config.grid_levels), 1)
        risk_adjusted_levels = self._exposure_limited_grid_levels(mode, allow_buys, allow_sells, order_notional, threshold, configured_levels=volatility_grid_levels)
        effective_grid_levels, reduction_reason = self._final_effective_grid_levels(
            configured_levels=configured_levels,
            volatility_grid_levels=volatility_grid_levels,
            risk_adjusted_levels=risk_adjusted_levels,
            position_side=effective_position_side,
            allow_buys=allow_buys,
            allow_sells=allow_sells,
            order_notional=order_notional,
            threshold=threshold,
            market_stress_reason=str(market_stress_context.get("market_stress_reason") or ""),
        )
        projected_one_side = self._projected_grid_notional(mode, allow_buys, allow_sells, order_notional, levels=effective_grid_levels)
        logger.warning(
            "grid_levels_decision configured_levels=%s volatility_grid_levels=%s risk_adjusted_levels=%s final_effective_levels=%s reduction_reason=%s position_side=%s allow_buys=%s allow_sells=%s projected_one_side_notional=%.4f threshold=%.4f max_position_notional_usd=%.4f",
            configured_levels,
            volatility_grid_levels,
            risk_adjusted_levels,
            effective_grid_levels,
            reduction_reason,
            effective_position_side,
            allow_buys,
            allow_sells,
            projected_one_side,
            threshold,
            self.config.max_position_notional_usd,
        )
        build_allow_buys = allow_buys and effective_grid_levels > 0
        build_allow_sells = allow_sells and effective_grid_levels > 0
        no_grid_orders_generated_reason = ""
        if effective_grid_levels <= 0 and (allow_buys or allow_sells):
            no_grid_orders_generated_reason = "max_exposure_too_small_for_min_order"
            logger.warning("no_new_order_placed reason=max_exposure_too_small_for_min_order order_notional=%.4f threshold=%.4f", order_notional, threshold)
        elif not build_allow_buys and not build_allow_sells:
            no_grid_orders_generated_reason = "no_allowed_sides"
        raw_buy_levels, raw_sell_levels = self.grid_manager._level_counts(max(effective_grid_levels, 1), mode, trend_bias=trend_bias)
        raw_grid_orders = (raw_buy_levels if allow_buys_before_min_notional else 0) + (raw_sell_levels if allow_sells_before_min_notional else 0)
        no_fill_watchdog_context = self._no_fill_watchdog_context(
            symbol=symbol,
            price=price,
            risk_can_trade=bool(risk_state.can_trade),
            pause_reason="none",
            position_notional=effective_position_notional,
        )
        if no_fill_watchdog_context["no_fill_watchdog_active"]:
            before_watchdog_spacing = final_spacing_pct
            reduction_pct = min(max(float(getattr(self.config, "no_fill_spacing_reduction_pct", 0.20)), 0.0), 1.0)
            min_watchdog_spacing = max(
                float(getattr(self.config, "no_fill_min_spacing_pct", 0.0022)),
                float(self.config.min_grid_profit_over_fees_pct),
            )
            final_spacing_pct = max(final_spacing_pct * (1.0 - reduction_pct), min_watchdog_spacing)
            no_fill_watchdog_context["no_fill_spacing_before_pct"] = before_watchdog_spacing
            no_fill_watchdog_context["no_fill_spacing_after_pct"] = final_spacing_pct
            no_fill_watchdog_context["no_fill_spacing_reduction_pct"] = reduction_pct
            no_fill_watchdog_context["no_fill_min_spacing_pct"] = min_watchdog_spacing
            spacing_source = f"{spacing_source}_no_fill_watchdog"
            force_recenter = True
            recenter_context["force_recenter"] = True
            recenter_context["forced_rebuild"] = True
            recenter_context["rebuild_reason"] = "no_fill_watchdog"
            logger.info("maker_fill_proximity_mode enabled=true spacing_pct_before=%.6f spacing_pct_after=%.6f reduction_pct=%.3f min_spacing_pct=%.6f", before_watchdog_spacing, final_spacing_pct, reduction_pct, min_watchdog_spacing)
        recenter_context.update(no_fill_watchdog_context)
        plan: GridPlan = self.grid_manager.build_grid(price, max(effective_grid_levels, 1), final_spacing_pct, 0.0, regime, order_size, self.config.regrid_threshold_pct, self.config.min_grid_profit_over_fees_pct, mode, allow_buys=build_allow_buys, allow_sells=build_allow_sells, force_recenter=force_recenter, spacing_source=spacing_source, atr_pct=atr_pct, trend_bias=trend_bias, regime_confidence=regime_confidence)
        if effective_position_side == "FLAT" and orderbook_analysis.available and orderbook_analysis.ready:
            self._apply_orderbook_size_multipliers(plan, price, orderbook_analysis.long_size_multiplier, orderbook_analysis.short_size_multiplier)
        prediction_bias_context = self._apply_prediction_grid_bias(
            plan,
            price=price,
            prediction=prediction,
            position_side=effective_position_side,
            max_one_side_notional=threshold,
        )
        range_orderbook_long_context = self._apply_range_orderbook_long_level_reduction(
            plan,
            position_side=effective_position_side,
            orderbook=orderbook_context,
        )
        after_min_notional = len(plan.long_levels) + len(plan.short_levels)
        after_dust_filter = after_min_notional
        after_anti_chop = after_min_notional
        edge_context = self._apply_minimum_expected_edge_filter(plan, mid_price=price, position_size=effective_position_size, avg_entry=avg_entry)
        after_net_profit_filter = len(plan.long_levels) + len(plan.short_levels)
        final_orders_to_submit = after_net_profit_filter
        if not plan.long_levels and not plan.short_levels:
            if edge_context.get("edge_filter_skipped_orders"):
                no_grid_orders_generated_reason = "edge_filter_removed_all_orders"
            elif not no_grid_orders_generated_reason and not plan.should_regrid:
                no_grid_orders_generated_reason = "regrid_not_required"
            elif not no_grid_orders_generated_reason:
                no_grid_orders_generated_reason = "grid_plan_empty"
            logger.warning(
                "no_grid_orders_generated reason=%s configured_levels=%s volatility_grid_levels=%s risk_adjusted_levels=%s final_effective_levels=%s reduction_reason=%s allow_buys=%s allow_sells=%s build_allow_buys=%s build_allow_sells=%s should_regrid=%s edge_filter_skipped_orders=%s edge_filter_skipped_by_reason=%s",
                no_grid_orders_generated_reason,
                configured_levels,
                volatility_grid_levels,
                risk_adjusted_levels,
                effective_grid_levels,
                reduction_reason,
                allow_buys,
                allow_sells,
                build_allow_buys,
                build_allow_sells,
                plan.should_regrid,
                edge_context.get("edge_filter_skipped_orders", 0),
                edge_context.get("edge_filter_skipped_by_reason", {}),
            )
        if not no_grid_orders_generated_reason and final_orders_to_submit <= 0:
            no_grid_orders_generated_reason = "final_order_plan_empty"
        if neutral_entries_blocked:
            cycle_pause_reason = "neutral_entries_blocked_in_trend"
        elif not risk_state.can_trade:
            cycle_pause_reason = risk_state.reason or "risk_blocked"
        else:
            cycle_pause_reason = "none"
        final_skip_reason = no_grid_orders_generated_reason or "none"
        risk_state_label = "OK" if risk_state.can_trade else "BLOCKED"
        if effective_position_side == "FLAT":
            logger.info(
                "FLAT_STATE_CHECK risk_state=%s pause_reason=%s regime=%s mode=%s allow_buys=%s allow_sells=%s candidate_orders_before_filters=%s after_min_notional=%s after_net_profit_filter=%s after_dust_filter=%s after_anti_chop=%s final_order_count=%s final_skip_reason=%s blocking_conditions=%s",
                risk_state_label,
                cycle_pause_reason,
                regime.value,
                mode.value,
                allow_buys,
                allow_sells,
                raw_grid_orders,
                after_min_notional,
                after_net_profit_filter,
                after_dust_filter,
                after_anti_chop,
                final_orders_to_submit,
                final_skip_reason,
                {
                    "risk_can_trade": risk_state.can_trade,
                    "risk_reason": risk_state.reason or "none",
                    "neutral_entries_blocked": neutral_entries_blocked,
                    "force_reduce_only": force_reduce_only,
                    "flip_cooldown_active": flip_cooldown_active,
                    "trend_flip_cooldown_remaining": self.trend_flip_cooldown_remaining,
                    "market_stress_opportunity_mode": market_stress.opportunity_mode.value,
                    "market_stress_reason": market_stress.reason,
                    "anti_chop_cooldown_active": anti_chop_context.get("anti_chop_cooldown_active", False),
                    "counter_trend_entry_filter_reason": entry_filter_context.get("counter_trend_entry_filter_reason", ""),
                    "edge_filter_skipped_by_reason": edge_context.get("edge_filter_skipped_by_reason", {}),
                    "order_notional": order_notional,
                    "min_order_notional_usd": min_order_notional_usd,
                    "effective_grid_levels": effective_grid_levels,
                    "reduction_reason": reduction_reason,
                    "build_allow_buys": build_allow_buys,
                    "build_allow_sells": build_allow_sells,
                    "should_regrid": plan.should_regrid,
                    "trading_enabled": bool(getattr(self.config, "enable_live_trading", True)),
                },
            )
            if risk_state.can_trade and cycle_pause_reason == "none" and final_orders_to_submit == 0:
                logger.critical(
                    "NO_ORDERS_GENERATED_WHILE_FLAT risk_state=%s pause_reason=%s regime=%s mode=%s allow_buys=%s allow_sells=%s candidate_orders_before_filters=%s after_min_notional=%s after_net_profit_filter=%s after_dust_filter=%s after_anti_chop=%s final_order_count=%s final_skip_reason=%s blocking_conditions=%s",
                    risk_state_label,
                    cycle_pause_reason,
                    regime.value,
                    mode.value,
                    allow_buys,
                    allow_sells,
                    raw_grid_orders,
                    after_min_notional,
                    after_net_profit_filter,
                    after_dust_filter,
                    after_anti_chop,
                    final_orders_to_submit,
                    final_skip_reason,
                    {
                        "risk_reason": risk_state.reason or "none",
                        "neutral_entries_blocked": neutral_entries_blocked,
                        "force_reduce_only": force_reduce_only,
                        "flip_cooldown_active": flip_cooldown_active,
                        "market_stress_opportunity_mode": market_stress.opportunity_mode.value,
                        "anti_chop_cooldown_active": anti_chop_context.get("anti_chop_cooldown_active", False),
                        "edge_filter_skipped_by_reason": edge_context.get("edge_filter_skipped_by_reason", {}),
                        "order_notional": order_notional,
                        "min_order_notional_usd": min_order_notional_usd,
                        "effective_grid_levels": effective_grid_levels,
                        "reduction_reason": reduction_reason,
                        "should_regrid": plan.should_regrid,
                    },
                )
        if (
            effective_position_side == "FLAT"
            and risk_state.can_trade
            and not neutral_entries_blocked
            and (allow_buys or allow_sells)
            and order_notional + 1e-9 >= min_order_notional_usd
            and final_orders_to_submit <= 0
        ):
            logger.warning(
                "flat_fail_open_violation symbol=%s regime=%s mode=%s price=%.4f allow_buys=%s allow_sells=%s order_notional=%.4f min_order_notional_usd=%.4f skip_reason=%s",
                symbol,
                regime.value,
                mode.value,
                price,
                allow_buys,
                allow_sells,
                order_notional,
                min_order_notional_usd,
                no_grid_orders_generated_reason or "unknown",
            )
        logger.info(
            "order_pipeline_summary symbol=%s regime=%s mode=%s price=%.4f position_side=%s position_notional=%.4f allow_buys=%s allow_sells=%s raw_grid_orders=%s after_min_notional=%s after_dust_filter=%s after_net_profit_filter=%s after_anti_chop=%s final_orders_to_submit=%s skip_reason=%s",
            symbol,
            regime.value,
            mode.value,
            price,
            effective_position_side,
            effective_position_notional,
            allow_buys,
            allow_sells,
            raw_grid_orders,
            after_min_notional,
            after_dust_filter,
            after_net_profit_filter,
            after_anti_chop,
            final_orders_to_submit,
            final_skip_reason,
        )
        level_decision_context = {
            "configured_levels": configured_levels,
            "volatility_grid_levels": volatility_grid_levels,
            "risk_adjusted_levels": risk_adjusted_levels,
            "final_effective_levels": effective_grid_levels,
            "grid_level_reduction_reason": reduction_reason,
            "reduction_reason": reduction_reason,
            "no_grid_orders_generated_reason": no_grid_orders_generated_reason,
            "min_notional_blocked_count": self.min_notional_blocked_count,
            "raw_grid_orders": raw_grid_orders,
            "after_min_notional": after_min_notional,
            "after_dust_filter": after_dust_filter,
            "after_net_profit_filter": after_net_profit_filter,
            "after_anti_chop": after_anti_chop,
            "final_orders_to_submit": final_orders_to_submit,
            "skip_reason": no_grid_orders_generated_reason or "none",
            "anti_chop_trigger_count": self.anti_chop_trigger_count,
            **prediction_bias_context,
            **long_selectivity_context,
            **range_orderbook_long_context,
            **anti_chop_context,
        }
        try:
            result = self.execution_engine.cancel_replace_grid(symbol, plan)
        except Exception as exc:
            logger.warning("state_uncertain_skip_cycle reason=grid_rebuild_api_error error=%s", exc)
            return {"status": "paused", "regime": regime.value, "risk": risk_state, "mode": mode.value, "reason": "state_uncertain_skip_cycle", "allowed_to_trade": False, "allowed_to_reduce": False, "state_source": account_state.state_source, "regime_confidence": regime_confidence, "grid_spacing_pct": final_spacing_pct, "spacing_source": spacing_source, "atr_pct": atr_pct, "grid_levels": effective_grid_levels, "volatility_grid_levels": volatility_grid_levels, "trend_bias": trend_bias, **dust_context, **market_stress_context, **recenter_context, **edge_context, **level_decision_context}
        if result.get("error"):
            logger.warning("state_uncertain_skip_cycle reason=%s", result.get("error"))
            return {"status": "paused", "regime": regime.value, "risk": risk_state, "mode": mode.value, "reason": "state_uncertain_skip_cycle", "orders": result, "allowed_to_trade": False, "allowed_to_reduce": False, "state_source": account_state.state_source, "regime_confidence": regime_confidence, "grid_spacing_pct": final_spacing_pct, "spacing_source": spacing_source, "atr_pct": atr_pct, "grid_levels": effective_grid_levels, "volatility_grid_levels": volatility_grid_levels, "trend_bias": trend_bias, **dust_context, **market_stress_context, **recenter_context, **edge_context, **level_decision_context}
        logger.info("grid_state regime=%s mode=%s position_side=%s open_orders_before=%s buy_before=%s sell_before=%s allow_buys=%s allow_sells=%s placed=%s canceled=%s order_notional=%.4f spacing_source=%s atr_pct=%.6f final_spacing_pct=%.6f state_source=%s", regime.value, mode.value, effective_position_side, len(open_orders), buy_order_count, sell_order_count, allow_buys, allow_sells, result.get("placed", 0), result.get("canceled", 0), order_notional, spacing_source, atr_pct, final_spacing_pct, account_state.state_source)
        if neutral_entries_blocked and abs(effective_position_size) > 1e-12:
            return {"status": "managing_position", "regime": regime.value, "risk": risk_state, "orders": result, "mode": mode.value, "order_size": order_size, "order_notional": order_notional, "allow_buys": allow_buys, "allow_sells": allow_sells, "allowed_to_trade": False, "allowed_to_reduce": True, "reason": "neutral_entries_blocked_in_trend", "position_management_action": "reduce_only_grid", "state_source": account_state.state_source, "regime_confidence": regime_confidence, "grid_spacing_pct": final_spacing_pct, "spacing_source": spacing_source, "atr_pct": atr_pct, "grid_levels": effective_grid_levels, "volatility_grid_levels": volatility_grid_levels, "trend_bias": trend_bias, **dust_context, **market_stress_context, **recenter_context, **edge_context, **level_decision_context}
        if neutral_entries_blocked:
            canceled = self.execution_engine.cancel_all_orders(symbol)
            return {"status": "paused", "regime": regime.value, "risk": risk_state, "reason": "neutral_blocked_in_trend", "canceled_orders": canceled, "allowed_to_trade": False, "allowed_to_reduce": False, "position_management_action": "none", "state_source": account_state.state_source, "regime_confidence": regime_confidence, "grid_spacing_pct": final_spacing_pct, "spacing_source": spacing_source, "atr_pct": atr_pct, "grid_levels": effective_grid_levels, "volatility_grid_levels": volatility_grid_levels, "trend_bias": trend_bias, **dust_context, **market_stress_context, **recenter_context, **edge_context, **level_decision_context}
        return {"status": "running", "regime": regime.value, "risk": risk_state, "orders": result, "mode": mode.value, "order_size": order_size, "order_notional": order_notional, "allow_buys": allow_buys, "allow_sells": allow_sells, "allowed_to_trade": True, "allowed_to_reduce": effective_position_side in {"LONG", "SHORT"}, "state_source": account_state.state_source, "regime_confidence": regime_confidence, "grid_spacing_pct": final_spacing_pct, "spacing_source": spacing_source, "atr_pct": atr_pct, "grid_levels": effective_grid_levels, "volatility_grid_levels": volatility_grid_levels, "trend_bias": trend_bias, **dust_context, **market_stress_context, **recenter_context, **edge_context, **level_decision_context}

    def _nearest_order_distances(self, symbol: str, mid_price: float) -> tuple[float | None, float | None]:
        orders = [o for o in getattr(self.execution_engine, "open_orders", []) if str(o.get("symbol", symbol)).upper() == symbol.upper()]
        buys = [float(o.get("price", 0.0) or 0.0) for o in orders if str(o.get("side", "")).lower() == "buy"]
        sells = [float(o.get("price", 0.0) or 0.0) for o in orders if str(o.get("side", "")).lower() == "sell"]
        nearest_buy = max(buys) if buys else None
        nearest_sell = min(sells) if sells else None
        buy_dist = ((mid_price - nearest_buy) / mid_price) if nearest_buy else None
        sell_dist = ((nearest_sell - mid_price) / mid_price) if nearest_sell else None
        return buy_dist, sell_dist

    def _no_fill_watchdog_context(self, *, symbol: str, price: float, risk_can_trade: bool, pause_reason: str, position_notional: float) -> dict:
        enabled = bool(getattr(self.config, "no_fill_watchdog_enabled", True))
        max_minutes = float(getattr(self.config, "no_fill_max_minutes", 60))
        reduction_pct = min(max(float(getattr(self.config, "no_fill_spacing_reduction_pct", 0.20)), 0.0), 1.0)
        min_spacing_pct = float(getattr(self.config, "no_fill_min_spacing_pct", 0.0022))
        buy_dist, sell_dist = self._nearest_order_distances(symbol, price)
        distances = [d for d in (buy_dist, sell_dist) if d is not None]
        nearest_distance = min(distances) if distances else None
        last_fill_minutes = None
        if getattr(self.execution_engine, "last_real_trade_ts", None) is not None:
            last_fill_minutes = (datetime.now(timezone.utc) - self.execution_engine.last_real_trade_ts).total_seconds() / 60.0
        cycle_minutes = float(getattr(self.execution_engine, "no_fill_cycles", 0) or 0) * float(getattr(self.config, "tick_seconds", 10)) / 60.0
        effective_no_fill_minutes = max(last_fill_minutes if last_fill_minutes is not None else 0.0, cycle_minutes)
        if last_fill_minutes is None and cycle_minutes <= 0:
            effective_no_fill_minutes = max_minutes + 1.0
        open_orders_exist = any(str(o.get("symbol", symbol)).upper() == symbol.upper() for o in getattr(self.execution_engine, "open_orders", []))
        low_exposure = position_notional <= max(float(getattr(self.config, "max_position_notional_usd", 0.0)) * 0.25, float(getattr(self.config, "dust_position_notional_usd", 0.0)))
        active = bool(enabled and risk_can_trade and pause_reason == "none" and open_orders_exist and effective_no_fill_minutes >= max_minutes)
        if active:
            logger.warning("no_fill_watchdog_triggered no_fill_minutes=%.2f max_minutes=%.2f nearest_distance_pct=%s open_orders_exist=%s low_exposure=%s reduction_pct=%.3f min_spacing_pct=%.6f", effective_no_fill_minutes, max_minutes, nearest_distance, open_orders_exist, low_exposure, reduction_pct, min_spacing_pct)
        return {
            "no_fill_watchdog_active": active,
            "no_fill_watchdog_enabled": enabled,
            "no_fill_minutes": effective_no_fill_minutes,
            "no_fill_max_minutes": max_minutes,
            "no_fill_spacing_reduction_pct": reduction_pct,
            "no_fill_min_spacing_pct": min_spacing_pct,
            "nearest_buy_distance_pct": buy_dist,
            "nearest_sell_distance_pct": sell_dist,
            "nearest_order_distance_pct": nearest_distance,
            "no_fill_watchdog_reason": "active" if active else "conditions_not_met",
        }

    def _apply_long_side_selectivity(
        self,
        *,
        allow_buys: bool,
        allow_sells: bool,
        position_side: str,
        regime: MarketRegime,
        prediction: PredictionResult,
        orderbook: dict,
        force_reduce_only: bool,
    ) -> dict:
        context = {
            "allow_buys": allow_buys,
            "allow_sells": allow_sells,
            "long_selectivity_action": "none",
            "long_selectivity_blocked": False,
        }
        if force_reduce_only or position_side == "SHORT" or not allow_buys:
            return context
        if regime not in {MarketRegime.TREND_DOWN, MarketRegime.TREND_DOWN_PULLBACK}:
            return context

        classification = str(orderbook.get("classification") or "")
        bullish_orderbook = classification in {"Bullish", "Strong Bullish"}
        prediction_long = prediction.available and prediction.prediction_bias in {"LONG", "STRONG_LONG"}
        confidence_ok = float(getattr(prediction, "confidence_score", 0.0) or 0.0) > 0.70
        if prediction_long and confidence_ok and bullish_orderbook:
            context["long_selectivity_action"] = "allow_trend_down_long_exception"
            return context

        context.update({
            "allow_buys": False,
            "long_selectivity_action": "block_new_long_in_trend_down",
            "long_selectivity_blocked": True,
            "long_selectivity_reason": "requires_long_prediction_confidence_gt_0_70_and_bullish_orderbook",
        })
        logger.info(
            "long_selectivity_blocked regime=%s position_side=%s prediction_bias=%s confidence=%.4f orderbook_classification=%s",
            regime.value,
            position_side,
            getattr(prediction, "prediction_bias", ""),
            float(getattr(prediction, "confidence_score", 0.0) or 0.0),
            classification or "Unavailable",
        )
        return context

    def _apply_range_orderbook_long_level_reduction(self, plan: GridPlan, *, position_side: str, orderbook: dict) -> dict:
        context = {
            "range_orderbook_long_reduction_pct": 0.0,
            "range_long_levels_before": len(plan.long_levels),
            "range_long_levels_after": len(plan.long_levels),
            "range_orderbook_long_action": "none",
        }
        if plan.regime != MarketRegime.RANGE or position_side != "FLAT" or not plan.long_levels:
            return context
        if not bool(orderbook.get("available")) or not bool(orderbook.get("ready")) or bool(orderbook.get("stale")):
            context["range_orderbook_long_action"] = "fallback_existing_strategy"
            return context
        classification = str(orderbook.get("classification") or "Neutral")
        if classification not in {"Neutral", "Bearish", "Strong Bearish"}:
            return context
        keep = max(1, int(round(len(plan.long_levels) * 0.75)))
        del plan.long_levels[keep:]
        context.update({
            "range_orderbook_long_reduction_pct": 0.25,
            "range_long_levels_after": len(plan.long_levels),
            "range_orderbook_long_action": "reduce_long_levels_25pct",
        })
        logger.info(
            "range_orderbook_long_level_reduction classification=%s before=%s after=%s reduction_pct=0.25",
            classification,
            context["range_long_levels_before"],
            context["range_long_levels_after"],
        )
        return context

    def _build_prediction(
        self,
        *,
        candles: pd.DataFrame,
        regime: MarketRegime,
        regime_confidence: float,
        regime_signal,
        orderbook_context: dict,
    ) -> PredictionResult:
        if not bool(getattr(self.config, "prediction_layer_enabled", True)):
            return self.prediction_layer.unavailable("prediction_layer_disabled")
        recent_taker_pressure = getattr(self.execution_engine, "recent_taker_pressure", None)
        return self.prediction_layer.predict(
            candles=candles,
            regime=regime,
            regime_confidence=regime_confidence,
            regime_signal=regime_signal,
            orderbook=orderbook_context,
            recent_taker_pressure=recent_taker_pressure,
            ema_slope_threshold_pct=self.config.ema_slope_threshold_pct,
        )

    def _prediction_allows_countertrend(self, prediction: PredictionResult, allowed_biases: set[str]) -> bool:
        threshold = max(float(getattr(self.config, "prediction_confidence_threshold", 0.65)), 0.0)
        return prediction.available and prediction.prediction_bias in allowed_biases and prediction.confidence_score > threshold

    def _apply_prediction_entry_filter(
        self,
        *,
        allow_buys: bool,
        allow_sells: bool,
        position_side: str,
        regime: MarketRegime,
        prediction: PredictionResult,
        force_reduce_only: bool,
    ) -> dict:
        context = {
            "allow_buys": allow_buys,
            "allow_sells": allow_sells,
            "prediction_entry_action": "none",
            "prediction_filtered_buy": False,
            "prediction_filtered_sell": False,
        }
        if not prediction.available or force_reduce_only:
            context["prediction_entry_action"] = "fallback_existing_strategy" if not prediction.available else "none"
            return context

        soft_mode = bool(getattr(self.config, "prediction_soft_mode", True))
        actions: list[str] = []
        strong_short_counter = self._prediction_allows_countertrend(prediction, {"SHORT", "STRONG_SHORT"})
        strong_long_counter = self._prediction_allows_countertrend(prediction, {"LONG", "STRONG_LONG"})
        if regime in {MarketRegime.TREND_UP, MarketRegime.TREND_UP_PULLBACK} and position_side != "LONG":
            if allow_sells and not strong_short_counter:
                if soft_mode:
                    actions.append("soft_keep_existing_trend_up_short_state")
                else:
                    allow_sells = False
                    context["prediction_filtered_sell"] = True
                    actions.append("block_short_without_strong_short_prediction")
            elif not allow_sells and strong_short_counter and position_side == "FLAT":
                allow_sells = True
                actions.append("allow_short_on_strong_short_prediction")
        if regime in {MarketRegime.TREND_DOWN, MarketRegime.TREND_DOWN_PULLBACK} and position_side != "SHORT":
            if allow_buys and not strong_long_counter:
                if soft_mode:
                    actions.append("soft_keep_existing_trend_down_long_state")
                else:
                    allow_buys = False
                    context["prediction_filtered_buy"] = True
                    actions.append("block_long_without_strong_long_prediction")
            elif not allow_buys and strong_long_counter and position_side == "FLAT":
                allow_buys = True
                actions.append("allow_long_on_strong_long_prediction")

        if soft_mode and position_side == "FLAT" and not force_reduce_only and not allow_buys and not allow_sells:
            allow_buys = bool(context["allow_buys"])
            allow_sells = bool(context["allow_sells"])
            actions.append("soft_mode_fail_open_keep_allowed_sides")

        context.update({
            "allow_buys": allow_buys,
            "allow_sells": allow_sells,
            "prediction_entry_action": ",".join(actions) if actions else "none",
        })
        return context

    def _apply_prediction_grid_bias(
        self,
        plan: GridPlan,
        *,
        price: float,
        prediction: PredictionResult,
        position_side: str,
        max_one_side_notional: float,
    ) -> dict:
        context = {
            "prediction_grid_action": "none",
            "prediction_long_size_multiplier": 1.0,
            "prediction_short_size_multiplier": 1.0,
            "prediction_long_levels_before": len(plan.long_levels),
            "prediction_short_levels_before": len(plan.short_levels),
            "prediction_long_levels_after": len(plan.long_levels),
            "prediction_short_levels_after": len(plan.short_levels),
            "long_level_multiplier": 1.0,
            "short_level_multiplier": 1.0,
            "long_size_multiplier": 1.0,
            "short_size_multiplier": 1.0,
        }
        if not prediction.available:
            context["prediction_grid_action"] = "fallback_existing_strategy"
            return context
        if plan.regime != MarketRegime.RANGE or position_side != "FLAT":
            return context
        if prediction.prediction_bias not in {"LONG", "STRONG_LONG", "SHORT", "STRONG_SHORT"}:
            return context

        configured_max_bias = min(max(float(getattr(self.config, "prediction_max_level_bias_pct", 0.35)), 0.0), 1.0)
        requested_level_bias = 0.35 if prediction.prediction_bias in {"STRONG_LONG", "STRONG_SHORT"} else 0.25
        level_bias_pct = min(requested_level_bias, configured_max_bias)
        confidence_threshold = max(float(getattr(self.config, "prediction_confidence_threshold", 0.55)), 0.0)
        high_confidence = prediction.confidence_score >= confidence_threshold
        max_size_multiplier = max(float(getattr(self.config, "prediction_max_size_multiplier", 1.20)), 1.0)
        min_size_multiplier = min(max(float(getattr(self.config, "prediction_min_size_multiplier", 0.80)), 0.0), 1.0)

        if high_confidence:
            if prediction.prediction_bias == "STRONG_LONG":
                long_mult, short_mult = 1.20, 0.80
            elif prediction.prediction_bias == "LONG":
                long_mult, short_mult = 1.10, 0.90
            elif prediction.prediction_bias == "SHORT":
                long_mult, short_mult = 0.90, 1.10
            else:
                long_mult, short_mult = 0.80, 1.20
            long_mult = min(max(long_mult, min_size_multiplier), max_size_multiplier)
            short_mult = min(max(short_mult, min_size_multiplier), max_size_multiplier)
        else:
            # Fee-aware fallback: low-confidence predictions may tilt level count,
            # but they must not tighten economics by increasing order size.
            long_mult = short_mult = 1.0

        bullish = prediction.prediction_bias in {"LONG", "STRONG_LONG"}
        favored_levels = plan.long_levels if bullish else plan.short_levels
        reduced_levels = plan.short_levels if bullish else plan.long_levels
        favored_before = len(favored_levels)
        reduced_before = len(reduced_levels)

        def side_notional(levels: list) -> float:
            return sum(level.price * level.size for level in levels)

        def append_level(levels: list, side: str, multiplier: float) -> bool:
            if not levels:
                return False
            next_index = len(levels) + 1
            base_size = levels[0].size
            next_price = price * (1.0 - plan.spacing_pct * next_index) if side == "buy" else price * (1.0 + plan.spacing_pct * next_index)
            max_size_by_trade = self.config.max_notional_per_trade_usd / max(next_price, 1e-9)
            max_size = max(0.0, min(float(self.config.max_order_size), max_size_by_trade))
            size = min(max(base_size, 0.0), max_size)
            if (side_notional(levels) + next_price * size) > max_one_side_notional + 1e-9:
                return False
            from .types import GridLevel
            levels.append(GridLevel(price=next_price, side=side, size=size))
            return True

        favored_target = max(favored_before, int(round(favored_before * (1.0 + level_bias_pct))))
        if level_bias_pct > 0 and favored_before > 0 and favored_target == favored_before:
            favored_target += 1
        reduced_target = max(1, int(round(reduced_before * (1.0 - level_bias_pct)))) if reduced_before > 0 else 0
        if reduced_before > 0:
            del reduced_levels[reduced_target:]
        while len(favored_levels) < favored_target:
            if not append_level(favored_levels, "buy" if bullish else "sell", long_mult if bullish else short_mult):
                break

        max_size_by_notional = self.config.max_notional_per_trade_usd / max(price, 1e-9)
        max_size = max(0.0, min(float(self.config.max_order_size), max_size_by_notional))
        for level in plan.long_levels:
            level.size = self._floor_size_to_min_order_notional(level.price, min(max(level.size * long_mult, 0.0), max_size))
        for level in plan.short_levels:
            level.size = self._floor_size_to_min_order_notional(level.price, min(max(level.size * short_mult, 0.0), max_size))

        long_after = len(plan.long_levels)
        short_after = len(plan.short_levels)
        context.update({
            "prediction_grid_action": "range_long_bias" if bullish else "range_short_bias",
            "prediction_long_size_multiplier": long_mult,
            "prediction_short_size_multiplier": short_mult,
            "prediction_long_levels_after": long_after,
            "prediction_short_levels_after": short_after,
            "long_level_multiplier": long_after / max(context["prediction_long_levels_before"], 1),
            "short_level_multiplier": short_after / max(context["prediction_short_levels_before"], 1),
            "long_size_multiplier": long_mult,
            "short_size_multiplier": short_mult,
        })
        logger.info(
            "prediction_grid_bias action=%s bias=%s confidence=%.4f long_levels_before=%s short_levels_before=%s long_levels_after=%s short_levels_after=%s long_size_multiplier=%.3f short_size_multiplier=%.3f long_level_multiplier=%.3f short_level_multiplier=%.3f",
            context["prediction_grid_action"],
            prediction.prediction_bias,
            prediction.confidence_score,
            context["prediction_long_levels_before"],
            context["prediction_short_levels_before"],
            context["prediction_long_levels_after"],
            context["prediction_short_levels_after"],
            context["prediction_long_size_multiplier"],
            context["prediction_short_size_multiplier"],
            context["long_level_multiplier"],
            context["short_level_multiplier"],
        )
        return context

    def _planning_fee_rate(self) -> float:
        """Per-side fee rate used for edge/spacing planning.

        Grid orders are submitted post-only (Alo), so both the entry and the
        paired exit rest as maker; planning with the taker rate would block
        profitable levels and force wider spacing than necessary.
        """
        maker = getattr(self.execution_engine, "maker_fee_rate", None)
        if maker is None:
            maker = getattr(self.execution_engine, "fee_rate", 0.0)
        return max(float(maker or 0.0), 0.0)

    def _entry_edge_score(self, spacing_pct: float, fee_rate: float) -> float:
        roundtrip_fee_pct = max(fee_rate * 2.0, 1e-9)
        return max(spacing_pct, 0.0) / roundtrip_fee_pct

    def _floor_size_to_min_order_notional(self, level_price: float, size: float) -> float:
        """Bias multipliers tilt order size, but must not shrink a level below
        the exchange minimum notional — that silently erases the level instead
        of reducing it (observed live: 0.75x on a $12 order -> $9 -> skipped)."""
        min_notional = max(float(getattr(self.config, "min_order_notional_usd", self.config.min_notional_usd)), 0.0)
        if min_notional <= 0 or size <= 0 or level_price <= 0:
            return size
        if size * level_price + 1e-9 >= min_notional:
            return size
        max_size_by_trade = self.config.max_notional_per_trade_usd / max(level_price, 1e-9)
        max_size = max(0.0, min(float(self.config.max_order_size), max_size_by_trade))
        if max_size <= 0:
            return size
        return min(min_notional / level_price, max_size)

    def _apply_orderbook_size_multipliers(self, plan: GridPlan, price: float, long_multiplier: float, short_multiplier: float) -> None:
        max_size_by_notional = self.config.max_notional_per_trade_usd / max(price, 1e-9)
        max_size = max(0.0, min(float(self.config.max_order_size), max_size_by_notional))
        for level in plan.long_levels:
            level.size = self._floor_size_to_min_order_notional(level.price, min(max(level.size * long_multiplier, 0.0), max_size))
        for level in plan.short_levels:
            level.size = self._floor_size_to_min_order_notional(level.price, min(max(level.size * short_multiplier, 0.0), max_size))

    def _apply_orderbook_entry_filter(
        self,
        *,
        allow_buys: bool,
        allow_sells: bool,
        position_side: str,
        regime: MarketRegime,
        orderbook: dict,
        spacing_pct: float,
        fee_rate: float,
        force_reduce_only: bool,
    ) -> dict:
        enabled = bool(getattr(self.config, "orderbook_filter_enabled", True))
        soft_mode = bool(getattr(self.config, "orderbook_soft_mode", True))
        context = {
            "allow_buys": allow_buys,
            "allow_sells": allow_sells,
            "orderbook_filter_enabled": enabled,
            "orderbook_soft_mode": soft_mode,
            "orderbook_decision": orderbook.get("decision", "orderbook_unavailable"),
            "orderbook_action_taken": "none",
            "orderbook_fallback_reason": orderbook.get("fallback_reason", ""),
            "orderbook_grid_level_multiplier": 1.0,
            "orderbook_filtered_buy": False,
            "orderbook_filtered_sell": False,
            "orderbook_bullish_entries": self.orderbook_bullish_entries,
            "orderbook_bearish_entries": self.orderbook_bearish_entries,
            "orderbook_filtered_entries": self.orderbook_filtered_entries,
        }
        if not enabled:
            context["orderbook_decision"] = "orderbook_filter_disabled"
            context["orderbook_action_taken"] = "fallback_existing_strategy"
            context["orderbook_fallback_reason"] = "disabled"
            return context
        if force_reduce_only or position_side != "FLAT":
            context["orderbook_decision"] = "orderbook_skip_not_flat_or_reduce_only"
            context["orderbook_action_taken"] = "none"
            return context
        if not bool(orderbook.get("available")) or not bool(orderbook.get("ready")) or bool(orderbook.get("stale")):
            context["orderbook_decision"] = str(orderbook.get("decision") or "orderbook_fail_open")
            context["orderbook_action_taken"] = "fallback_existing_strategy"
            context["orderbook_fallback_reason"] = str(orderbook.get("fallback_reason") or orderbook.get("decision") or "unavailable")
            return context

        ratio = float(orderbook.get("imbalance_ratio", 1.0) or 1.0)
        classification = str(orderbook.get("classification") or "Neutral")
        edge_score = self._entry_edge_score(spacing_pct, fee_rate)
        required_counter_edge = max(float(getattr(self.config, "orderbook_counter_edge_score", getattr(self.config, "counter_trend_entry_edge_score", 1.25))), 0.0)
        max_reduction_pct = min(max(float(getattr(self.config, "orderbook_max_level_reduction_pct", 0.5)), 0.0), 1.0)
        capped_multiplier = max(1.0 - max_reduction_pct, 0.0)
        decisions: list[str] = []

        orderbook_bullish = ratio > 1.15
        orderbook_bearish = ratio < 0.85
        if allow_buys and orderbook_bullish:
            self.orderbook_bullish_entries += 1
        if allow_sells and orderbook_bearish:
            self.orderbook_bearish_entries += 1

        def reduce_levels(reason: str) -> None:
            context["orderbook_grid_level_multiplier"] = min(float(context["orderbook_grid_level_multiplier"]), max(capped_multiplier, 0.0))
            decisions.append(reason)

        if regime in {MarketRegime.TREND_UP, MarketRegime.TREND_UP_PULLBACK} and allow_sells and orderbook_bullish:
            if soft_mode:
                reduce_levels("soft_reduce_short_in_uptrend_bullish_orderbook")
            else:
                allow_sells = False
                context["orderbook_filtered_sell"] = True
                decisions.append("restrict_short_in_uptrend_bullish_orderbook")
        if regime in {MarketRegime.TREND_DOWN, MarketRegime.TREND_DOWN_PULLBACK} and allow_buys and orderbook_bearish:
            if soft_mode:
                reduce_levels("soft_reduce_long_in_downtrend_bearish_orderbook")
            else:
                allow_buys = False
                context["orderbook_filtered_buy"] = True
                decisions.append("restrict_long_in_downtrend_bearish_orderbook")

        counter_buy = allow_buys and orderbook_bearish
        counter_sell = allow_sells and orderbook_bullish
        if counter_buy or counter_sell:
            if edge_score < required_counter_edge:
                if soft_mode:
                    reduce_levels("soft_counter_orderbook_reduce_grid_levels")
                else:
                    if counter_buy:
                        allow_buys = False
                        context["orderbook_filtered_buy"] = True
                    if counter_sell:
                        allow_sells = False
                        context["orderbook_filtered_sell"] = True
                    decisions.append("counter_orderbook_edge_too_low")
            else:
                reduce_levels("counter_orderbook_reduce_grid_levels")

        if soft_mode and position_side == "FLAT" and not force_reduce_only:
            # LIVE safety: order book imbalance is additive only in soft mode. It
            # must never be the sole reason a flat, risk-OK strategy has no side.
            allow_buys = bool(context["allow_buys"])
            allow_sells = bool(context["allow_sells"])
            if decisions and not allow_buys and not allow_sells:
                context["orderbook_fallback_reason"] = "soft_mode_no_zero_order_flat_state"
                decisions.append("soft_mode_fail_open_keep_allowed_sides")

        action_taken = "fallback_existing_strategy" if context.get("orderbook_fallback_reason") else ("reduce_grid_levels" if float(context["orderbook_grid_level_multiplier"]) < 1.0 else ("hard_filter" if (context["orderbook_filtered_buy"] or context["orderbook_filtered_sell"]) else "none"))
        context.update(
            {
                "allow_buys": allow_buys,
                "allow_sells": allow_sells,
                "orderbook_decision": ",".join(dict.fromkeys(decisions)) if decisions else "orderbook_entry_aligned_or_neutral",
                "orderbook_action_taken": action_taken,
                "orderbook_entry_edge_score": edge_score,
                "orderbook_required_counter_edge_score": required_counter_edge,
                "orderbook_entry_classification": classification,
                "orderbook_long_preferred": ratio > 1.15,
                "orderbook_short_preferred": ratio < 0.85,
                "orderbook_bullish_entries": self.orderbook_bullish_entries,
                "orderbook_bearish_entries": self.orderbook_bearish_entries,
            }
        )
        logger.info(
            "orderbook_entry_filter classification=%s ratio=%.6f edge_score=%.3f required_counter_edge=%.3f allow_buys=%s allow_sells=%s soft_mode=%s decision=%s action_taken=%s long_multiplier=%.2f short_multiplier=%.2f level_multiplier=%.2f",
            classification,
            ratio,
            edge_score,
            required_counter_edge,
            allow_buys,
            allow_sells,
            soft_mode,
            context["orderbook_decision"],
            context["orderbook_action_taken"],
            float(orderbook.get("long_size_multiplier", 1.0) or 1.0),
            float(orderbook.get("short_size_multiplier", 1.0) or 1.0),
            float(context["orderbook_grid_level_multiplier"]),
        )
        return context

    def _has_strong_bullish_momentum(self, signal) -> bool:
        threshold = max(float(getattr(self.config, "strong_momentum_block_threshold", 0.65)), 0.0)
        return (
            signal.transition_direction == "UP" and signal.transition_confidence >= threshold
        ) or signal.momentum_score >= threshold or signal.ema_slope_acceleration_score >= threshold

    def _has_strong_bearish_momentum(self, signal) -> bool:
        threshold = max(float(getattr(self.config, "strong_momentum_block_threshold", 0.65)), 0.0)
        return (
            signal.transition_direction == "DOWN" and signal.transition_confidence >= threshold
        ) or signal.momentum_score <= -threshold or signal.ema_slope_acceleration_score <= -threshold

    def _apply_counter_trend_entry_filter(
        self,
        *,
        allow_buys: bool,
        allow_sells: bool,
        position_side: str,
        regime: MarketRegime,
        regime_signal,
        spacing_pct: float,
        fee_rate: float,
        force_reduce_only: bool,
    ) -> dict:
        edge_score = self._entry_edge_score(spacing_pct, fee_rate)
        required_edge_score = max(float(getattr(self.config, "counter_trend_entry_edge_score", 3.5)), 0.0)
        strong_bullish = self._has_strong_bullish_momentum(regime_signal)
        strong_bearish = self._has_strong_bearish_momentum(regime_signal)
        short_blocked = False
        long_blocked = False
        reasons: list[str] = []

        if not force_reduce_only and edge_score < required_edge_score:
            if allow_sells and position_side != "LONG" and (regime == MarketRegime.TREND_UP or strong_bullish):
                allow_sells = False
                short_blocked = True
                reasons.append("short_blocked_bullish_trend_or_momentum")
            if allow_buys and position_side != "SHORT" and (regime == MarketRegime.TREND_DOWN or strong_bearish):
                allow_buys = False
                long_blocked = True
                reasons.append("long_blocked_bearish_trend_or_momentum")

        return {
            "allow_buys": allow_buys,
            "allow_sells": allow_sells,
            "counter_trend_entry_filter_enabled": True,
            "counter_trend_edge_score": edge_score,
            "counter_trend_required_edge_score": required_edge_score,
            "strong_bullish_momentum": strong_bullish,
            "strong_bearish_momentum": strong_bearish,
            "short_entry_blocked_by_trend_filter": short_blocked,
            "long_entry_blocked_by_trend_filter": long_blocked,
            "counter_trend_entry_filter_reason": ",".join(reasons),
        }

    def _adverse_move_pct(self, position_side: str, avg_entry: float, price: float) -> float:
        if avg_entry <= 0:
            return 0.0
        if position_side == "LONG":
            return max((avg_entry - price) / avg_entry, 0.0)
        if position_side == "SHORT":
            return max((price - avg_entry) / avg_entry, 0.0)
        return 0.0

    def _mean_reversion_confirmed(self, candles: pd.DataFrame, position_side: str, avg_entry: float, price: float, transition_direction: str) -> bool:
        if avg_entry <= 0 or len(candles) < 2:
            return False
        previous_close = float(candles["close"].iloc[-2])
        min_reversion = max(float(getattr(self.config, "min_reversion_confirmation_pct", 0.0015)), 0.0)
        if position_side == "LONG":
            recovered_pct = (price - previous_close) / max(previous_close, 1e-9)
            return recovered_pct >= min_reversion or transition_direction == "UP"
        if position_side == "SHORT":
            recovered_pct = (previous_close - price) / max(previous_close, 1e-9)
            return recovered_pct >= min_reversion or transition_direction == "DOWN"
        return False

    def _confirmed_regime(self, raw_regime: MarketRegime, confidence: float, transition_confidence: float = 0.0) -> MarketRegime:
        """Require configured confirmation and confidence before acting on trend regimes."""
        min_confidence = max(float(getattr(self.config, "regime_min_confidence", 0.0)), 0.0)
        confirmation_bars = max(int(getattr(self.config, "regime_confirmation_bars", 1)), 1)
        trend_regimes = {MarketRegime.TREND_UP, MarketRegime.TREND_DOWN}

        if raw_regime not in trend_regimes:
            self.pending_regime = None
            self.pending_regime_count = 0
            return raw_regime

        transition_override = transition_confidence >= 0.70

        if confidence < min_confidence and not transition_override:
            logger.info(
                "regime_confidence_check_blocked raw_regime=%s confidence=%.3f min_confidence=%.3f",
                raw_regime.value,
                confidence,
                min_confidence,
            )
            return self.last_trend_regime if self.last_trend_regime == raw_regime else MarketRegime.RANGE

        if self.pending_regime == raw_regime:
            self.pending_regime_count += 1
        else:
            self.pending_regime = raw_regime
            self.pending_regime_count = 1

        if self.pending_regime_count < confirmation_bars and not transition_override:
            logger.info(
                "regime_confirmation_pending raw_regime=%s confirmation_count=%s confirmation_bars=%s",
                raw_regime.value,
                self.pending_regime_count,
                confirmation_bars,
            )
            return self.last_trend_regime if self.last_trend_regime == raw_regime else MarketRegime.RANGE

        return raw_regime


    def _apply_minimum_expected_edge_filter(self, plan: GridPlan, *, mid_price: float, position_size: float, avg_entry: float = 0.0) -> dict:
        """Remove only orders that are unsafe after the fee/profitability checks.

        Opening grid entries must remain fail-open while flat.  The minimum net
        profit fee-multiple guard is a closing/profit-taking guard; applying it
        to normal opening grid entries can remove every level in live trading.
        Exposure-reducing/flatting/flip orders are checked against the realized
        entry price when available, because those are the orders that should
        prove enough expected net profit after fees.
        """
        estimates: list[dict] = []
        kept_long = []
        kept_short = []
        skipped = 0
        skipped_by_reason: dict[str, int] = {}
        fee_rate = self._planning_fee_rate()
        min_edge_usd = max(float(self.config.min_expected_net_edge_usd), 0.0)
        min_edge_pct = max(float(self.config.min_expected_net_edge_pct), 0.0)
        min_rr = max(float(self.config.min_rr_ratio), 0.0)
        min_net_fee_multiple = max(float(getattr(self.config, "min_net_profit_fee_multiplier", getattr(self.config, "min_expected_net_profit_fee_multiple", 3.0))), 0.0)

        for order in plan.long_levels + plan.short_levels:
            action = self._classify_order_action(position_size, order.side, order.size)
            estimate = self._expected_edge_for_order(
                price=order.price,
                size=order.size,
                mid_price=mid_price,
                grid_spacing_pct=plan.spacing_pct,
                fee_rate=fee_rate,
            )
            estimate.update({"side": order.side, "price": order.price, "size": order.size, "exposure_action": action})
            block_reason = ""
            if action == "increasing":
                if abs(position_size) < 1e-12:
                    # Temporary emergency disable for the offending flat-entry edge
                    # filter: fee/edge gates may rank entries, but must not erase
                    # every opening grid level while the account is flat and risk OK.
                    logger.critical(
                        "flat_entry_edge_filter_fail_open side=%s price=%s size=%s expected_net_edge=%.6f expected_net_edge_pct=%.6f rr_ratio=%.3f min_expected_net_edge_usd=%.6f min_expected_net_edge_pct=%.6f min_rr_ratio=%.3f",
                        order.side,
                        order.price,
                        order.size,
                        estimate["expected_net_edge"],
                        estimate["expected_net_edge_pct"],
                        estimate["rr_ratio"],
                        min_edge_usd,
                        min_edge_pct,
                        min_rr,
                    )
                elif estimate["expected_net_edge"] <= 0:
                    block_reason = "non_positive_expected_net_edge"
                elif estimate["expected_net_edge"] < min_edge_usd:
                    block_reason = "below_min_expected_net_edge_usd"
                elif estimate["expected_net_edge_pct"] < min_edge_pct:
                    block_reason = "below_min_expected_net_edge_pct"
                elif estimate["rr_ratio"] < min_rr:
                    block_reason = "below_min_rr_ratio"
            elif avg_entry > 0:
                close_estimate = self._expected_close_profit_for_order(
                    side=order.side,
                    qty=order.size,
                    entry_price=avg_entry,
                    target_price=order.price,
                    fee_rate=fee_rate,
                    position_size=position_size,
                )
                estimate.update(close_estimate)
                required_net_profit = close_estimate["estimated_roundtrip_fee"] * min_net_fee_multiple
                if close_estimate["expected_net_profit"] < required_net_profit:
                    block_reason = "below_min_net_profit_after_fees"
                logger.info(
                    "net_profit_filter side=%s entry_price=%s target_price=%s qty=%s expected_gross_profit=%.6f estimated_roundtrip_fee=%.6f expected_net_profit=%.6f required_net_profit=%.6f decision=%s reason=%s",
                    order.side,
                    avg_entry,
                    order.price,
                    order.size,
                    close_estimate["expected_gross_profit"],
                    close_estimate["estimated_roundtrip_fee"],
                    close_estimate["expected_net_profit"],
                    required_net_profit,
                    "block" if block_reason else "allow",
                    block_reason or "profit_target_meets_fee_floor",
                )

            estimate["edge_filter_decision"] = "block" if block_reason else "allow"
            estimate["edge_filter_reason"] = block_reason
            estimates.append(estimate)
            if block_reason:
                skipped += 1
                skipped_by_reason[block_reason] = skipped_by_reason.get(block_reason, 0) + 1
                logger.info(
                    "order_skipped reason=edge_filter side=%s price=%s size=%s expected_move_pct=%.6f grid_spacing_pct=%.6f expected_gross_pnl=%.6f expected_fee_cost=%.6f expected_net_edge=%.6f expected_net_edge_pct=%.6f expected_profit_after_fees=%.6f net_fee_multiple=%.3f min_net_fee_multiple=%.3f rr_ratio=%.3f block_reason=%s",
                    order.side,
                    order.price,
                    order.size,
                    estimate["expected_move_pct"],
                    estimate["grid_spacing_pct"],
                    estimate["expected_gross_pnl"],
                    estimate["expected_fee_cost"],
                    estimate["expected_net_edge"],
                    estimate["expected_net_edge_pct"],
                    estimate["expected_profit_after_fees"],
                    estimate["net_fee_multiple"],
                    min_net_fee_multiple,
                    estimate["rr_ratio"],
                    block_reason,
                )
                continue
            if order.side == "buy":
                kept_long.append(order)
            else:
                kept_short.append(order)

        plan.long_levels = kept_long
        plan.short_levels = kept_short
        allowed_estimates = [e for e in estimates if e["edge_filter_decision"] == "allow"]
        best = max(allowed_estimates or estimates, key=lambda e: e["expected_net_edge"], default={})
        return {
            "edge_filter_enabled": True,
            "edge_filter_skipped_orders": skipped,
            "edge_filter_skipped_by_reason": skipped_by_reason,
            "min_expected_net_edge_usd": min_edge_usd,
            "min_expected_net_edge_pct": min_edge_pct,
            "min_rr_ratio": min_rr,
            "min_expected_net_profit_fee_multiple": min_net_fee_multiple,
            "expected_move_pct": best.get("expected_move_pct"),
            "expected_gross_pnl": best.get("expected_gross_pnl"),
            "expected_fee_cost": best.get("expected_fee_cost"),
            "expected_net_edge": best.get("expected_net_edge"),
            "expected_profit_after_fees": best.get("expected_profit_after_fees"),
            "expected_net_fee_multiple": best.get("net_fee_multiple"),
            "expected_net_edge_pct": best.get("expected_net_edge_pct"),
            "expected_rr_ratio": best.get("rr_ratio"),
        }

    def _expected_close_profit_for_order(self, *, side: str, qty: float, entry_price: float, target_price: float, fee_rate: float, position_size: float) -> dict:
        qty = abs(qty)
        close_qty = min(qty, abs(position_size)) if abs(position_size) > 1e-12 else qty
        if position_size > 0 and side.lower() == "sell":
            expected_gross_profit = max(target_price - entry_price, 0.0) * close_qty
        elif position_size < 0 and side.lower() == "buy":
            expected_gross_profit = max(entry_price - target_price, 0.0) * close_qty
        else:
            expected_gross_profit = 0.0
        estimated_open_fee = abs(entry_price * close_qty) * fee_rate
        estimated_close_fee = abs(target_price * close_qty) * fee_rate
        estimated_roundtrip_fee = estimated_open_fee + estimated_close_fee
        expected_net_profit = expected_gross_profit - estimated_open_fee - estimated_close_fee
        return {
            "expected_gross_profit": expected_gross_profit,
            "estimated_open_fee": estimated_open_fee,
            "estimated_close_fee": estimated_close_fee,
            "estimated_roundtrip_fee": estimated_roundtrip_fee,
            "expected_net_profit": expected_net_profit,
        }

    def _apply_anti_chop_cooldown(self, *, position_side: str, allow_buys: bool, allow_sells: bool) -> dict:
        now = datetime.now(timezone.utc)
        last_n = max(int(getattr(self.config, "anti_chop_last_n_trades", 5)), 0)
        cooldown_minutes = max(int(getattr(self.config, "anti_chop_cooldown_minutes", 15)), 0)
        net_pnls: list[float] = []
        if last_n > 0 and hasattr(self.execution_engine, "last_closed_trade_net_pnls"):
            net_pnls = list(self.execution_engine.last_closed_trade_net_pnls(last_n))
        signature = tuple(round(x, 10) for x in net_pnls)
        triggered = False
        if len(net_pnls) >= last_n and sum(net_pnls) < 0 and cooldown_minutes > 0:
            if self.anti_chop_cooldown_until is None or now >= self.anti_chop_cooldown_until or signature != self._last_anti_chop_signature:
                self.anti_chop_cooldown_until = now + timedelta(minutes=cooldown_minutes)
                self._last_anti_chop_signature = signature
                self.anti_chop_trigger_count += 1
                triggered = True
                logger.warning(
                    "anti_chop_triggered last_n_trades=%s net_pnl=%.6f cooldown_until=%s",
                    last_n,
                    sum(net_pnls),
                    self.anti_chop_cooldown_until.isoformat(),
                )
        cooldown_active = self.anti_chop_cooldown_until is not None and now < self.anti_chop_cooldown_until
        if cooldown_active:
            original_allow_buys = allow_buys
            original_allow_sells = allow_sells
            if position_side == "FLAT":
                logger.critical(
                    "anti_chop_flat_fail_open_override original_allow_buys=%s original_allow_sells=%s cooldown_until=%s",
                    original_allow_buys,
                    original_allow_sells,
                    self.anti_chop_cooldown_until.isoformat() if self.anti_chop_cooldown_until else "",
                )
            elif position_side == "LONG":
                allow_buys = False
            elif position_side == "SHORT":
                allow_sells = False
            logger.warning(
                "anti_chop_active position_side=%s original_allow_buys=%s original_allow_sells=%s allow_buys=%s allow_sells=%s cooldown_until=%s pause_scope=exposure_increasing_entries_only",
                position_side,
                original_allow_buys,
                original_allow_sells,
                allow_buys,
                allow_sells,
                self.anti_chop_cooldown_until.isoformat() if self.anti_chop_cooldown_until else "",
            )
        return {
            "allow_buys": allow_buys,
            "allow_sells": allow_sells,
            "anti_chop_cooldown_active": cooldown_active,
            "anti_chop_triggered": triggered,
            "anti_chop_last_n_trades": last_n,
            "anti_chop_net_pnl": sum(net_pnls) if net_pnls else 0.0,
            "anti_chop_cooldown_until": self.anti_chop_cooldown_until.isoformat() if self.anti_chop_cooldown_until else "",
            "anti_chop_trigger_count": self.anti_chop_trigger_count,
        }

    def _expected_edge_for_order(self, *, price: float, size: float, mid_price: float, grid_spacing_pct: float, fee_rate: float) -> dict:
        notional = abs(price * size)
        expected_move_pct = abs(mid_price - price) / max(price, 1e-9)
        expected_gross_pnl = notional * expected_move_pct
        expected_fee_cost = notional * fee_rate * 2
        expected_net_edge = expected_gross_pnl - expected_fee_cost
        expected_net_edge_pct = expected_net_edge / max(notional, 1e-9)
        rr_ratio = expected_gross_pnl / max(expected_fee_cost, 1e-9)
        net_fee_multiple = expected_net_edge / max(expected_fee_cost, 1e-9)
        return {
            "expected_move_pct": expected_move_pct,
            "grid_spacing_pct": grid_spacing_pct,
            "expected_gross_pnl": expected_gross_pnl,
            "expected_fee_cost": expected_fee_cost,
            "expected_net_edge": expected_net_edge,
            "expected_profit_after_fees": expected_net_edge,
            "net_fee_multiple": net_fee_multiple,
            "expected_net_edge_pct": expected_net_edge_pct,
            "rr_ratio": rr_ratio,
        }

    def _classify_order_action(self, position_size: float, side: str, qty: float) -> str:
        signed_order = qty if side.lower() == "buy" else -qty
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

    def _expected_active_side_orders(self, mode: GridMode, allowed_buys: bool, allowed_sells: bool) -> tuple[int, int]:
        buy_levels, sell_levels = self.grid_manager._level_counts(self.config.grid_levels, mode)
        expected_buy_orders = 1 if allowed_buys and buy_levels > 0 else 0
        expected_sell_orders = 1 if allowed_sells and sell_levels > 0 else 0
        return expected_buy_orders, expected_sell_orders

    def _detect_flat_orphan_orders(self, *, open_orders: list[dict], position_side: str, mode: GridMode, allowed_buys: bool, allowed_sells: bool) -> tuple[bool, str, dict]:
        buy_count = sum(1 for o in open_orders if str(o.get("side", "")).lower() == "buy")
        sell_count = sum(1 for o in open_orders if str(o.get("side", "")).lower() == "sell")
        expected_buy_orders, expected_sell_orders = self._expected_active_side_orders(mode, allowed_buys, allowed_sells)
        context = {
            "expected_buy_orders": expected_buy_orders,
            "expected_sell_orders": expected_sell_orders,
            "actual_buy_orders": buy_count,
            "actual_sell_orders": sell_count,
        }
        if position_side != "FLAT":
            return False, "not_flat", context
        if not open_orders:
            return False, "no_open_orders", context
        if expected_buy_orders == 0 and expected_sell_orders == 0:
            return False, "no_active_sides_expected", context
        missing_active_sides = []
        if expected_buy_orders > 0 and buy_count == 0:
            missing_active_sides.append("buy")
        if expected_sell_orders > 0 and sell_count == 0:
            missing_active_sides.append("sell")
        if missing_active_sides:
            return True, f"expected_active_side_missing:{','.join(missing_active_sides)}", context
        return False, "active_allowed_sides_present", context

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

    def _projected_grid_notional(self, mode: GridMode, allow_buys: bool, allow_sells: bool, order_notional: float, *, levels: int | None = None, trend_bias: float | None = None) -> float:
        buy_levels, sell_levels = self.grid_manager._level_counts(self.config.grid_levels if levels is None else levels, mode, trend_bias=self.config.trend_bias_base if trend_bias is None else trend_bias)
        return max((buy_levels if allow_buys else 0) * order_notional, (sell_levels if allow_sells else 0) * order_notional)

    def _exposure_limited_grid_levels(self, mode: GridMode, allow_buys: bool, allow_sells: bool, order_notional: float, threshold: float, *, configured_levels: int | None = None) -> int:
        if order_notional <= 0 or threshold <= 0:
            return 0
        configured = max(int(self.config.grid_levels if configured_levels is None else configured_levels), 1)
        for levels in range(configured, 0, -1):
            if self._projected_grid_notional(mode, allow_buys, allow_sells, order_notional, levels=levels) <= threshold + 1e-9:
                return levels
        return 0


    def _final_effective_grid_levels(
        self,
        *,
        configured_levels: int,
        volatility_grid_levels: int,
        risk_adjusted_levels: int,
        position_side: str,
        allow_buys: bool,
        allow_sells: bool,
        order_notional: float,
        threshold: float,
        market_stress_reason: str = "",
    ) -> tuple[int, str]:
        """Return final grid levels and a compact reason for any reduction/floor.

        The decision chain is intentionally explicit because live diagnostics need
        to distinguish volatility reductions, market-stress reductions, exposure
        reductions, and the flat-grid safety floor.
        """
        configured = max(int(configured_levels), 1)
        volatility_levels = max(int(volatility_grid_levels), 0)
        risk_levels = max(int(risk_adjusted_levels), 0)
        final_levels = risk_levels
        reasons: list[str] = []

        if volatility_levels < configured:
            reasons.append("volatility_reduced")
        if market_stress_reason and market_stress_reason != "normal":
            reasons.append(f"market_stress:{market_stress_reason}")
        if risk_levels < volatility_levels:
            if order_notional <= 0 or threshold <= 0:
                reasons.append("risk_exposure_zero_capacity")
            else:
                reasons.append("risk_exposure_reduced")

        flat_min_levels = 2
        if position_side == "FLAT" and (allow_buys or allow_sells) and final_levels < flat_min_levels:
            final_levels = flat_min_levels
            reasons.append("flat_min_grid_levels_floor:2")

        if final_levels < configured and not reasons:
            reasons.append("reduced_unknown")
        if final_levels > risk_levels and risk_levels < flat_min_levels:
            reasons.append("risk_floor_override_while_flat")
        return final_levels, ",".join(dict.fromkeys(reasons)) or "none"

    def _trend_bias(self, regime_confidence: float) -> float:
        if regime_confidence >= self.config.trend_confidence_strong:
            return self.config.trend_bias_strong
        if regime_confidence <= self.config.trend_confidence_weak:
            return self.config.trend_bias_weak
        return self.config.trend_bias_base

    def _calculate_spacing_pct(self, atr_pct: float, return_vol_pct: float) -> tuple[float, str]:
        realized_vol = max(float(atr_pct or 0.0), float(return_vol_pct or 0.0))
        spacing_multiplier = max(float(getattr(self.config, "grid_spacing_multiplier", 1.0)), 0.0)
        if realized_vol >= self.config.vol_high_threshold:
            bucket = "high_volatility"
            bucket_min = float(getattr(self.config, "grid_spacing_high_vol_min_pct", 0.0055))
            bucket_max = float(getattr(self.config, "grid_spacing_high_vol_max_pct", 0.0075))
        elif realized_vol <= self.config.vol_low_threshold:
            bucket = "low_volatility"
            bucket_min = float(getattr(self.config, "grid_spacing_low_vol_min_pct", 0.0025))
            bucket_max = float(getattr(self.config, "grid_spacing_low_vol_max_pct", 0.0030))
        else:
            bucket = "normal_volatility"
            bucket_min = float(getattr(self.config, "grid_spacing_normal_vol_min_pct", 0.0035))
            bucket_max = float(getattr(self.config, "grid_spacing_normal_vol_max_pct", 0.0045))

        atr_component = atr_pct * self.config.grid_spacing_vol_multiplier
        vol_component = return_vol_pct * self.config.grid_spacing_vol_multiplier
        raw_spacing = max(self.config.grid_spacing_pct, atr_component, vol_component) * spacing_multiplier
        fee_rate = self._planning_fee_rate()
        fee_floor_pct = fee_rate * 2.0 * max(float(getattr(self.config, "min_net_profit_fee_multiplier", 3.0)), 1.0)
        global_min = max(float(getattr(self.config, "grid_spacing_pct_min", 0.004)), fee_floor_pct)
        global_max = max(float(getattr(self.config, "grid_spacing_pct_max", 0.0075)), global_min)
        effective_min = max(bucket_min, global_min)
        effective_max = min(bucket_max, global_max) if bucket_max >= global_min else global_max
        final_spacing = min(max(raw_spacing, effective_min), effective_max)
        driver = "atr14" if atr_component >= vol_component else "return_volatility"
        source = f"{bucket}_{driver}"
        reason = "raw_within_bounds"
        if abs(spacing_multiplier - 1.0) > 1e-12:
            source = f"{source}_x{spacing_multiplier:.2f}"
        if final_spacing == effective_min and raw_spacing < final_spacing:
            source = f"{source}_fee_floor"
            reason = "fee_floor" if fee_floor_pct >= float(getattr(self.config, "grid_spacing_pct_min", 0.004)) else "configured_min_floor"
        elif final_spacing == effective_max and raw_spacing > final_spacing:
            source = f"{source}_configured_cap"
            reason = "configured_max_cap"
        logger.info(
            "grid_spacing_adjusted raw_spacing_pct=%.6f final_spacing_pct=%.6f fee_floor_pct=%.6f reason=%s",
            raw_spacing,
            final_spacing,
            fee_floor_pct,
            reason,
        )
        return final_spacing, source

    def _volatility_adjusted_grid_levels(self, atr_pct: float) -> int:
        if atr_pct >= self.config.vol_extreme_threshold:
            return 1
        if atr_pct >= self.config.vol_high_threshold:
            desired = 2
        elif atr_pct <= self.config.vol_low_threshold:
            desired = 4
        else:
            desired = 3
        return min(max(desired, self.config.min_grid_levels), self.config.max_grid_levels)

    def _calculate_order_size(self, price: float) -> float:
        target_notional = min(self.config.order_notional_usd, self.config.max_notional_per_trade_usd)
        raw_size = target_notional / max(price, 1e-9)
        size = min(max(raw_size, self.config.min_order_size), self.config.max_order_size)
        notional = size * price
        if notional > target_notional:
            size = target_notional / max(price, 1e-9)
            notional = size * price
        if notional < self.config.min_notional_usd:
            size = self.config.min_notional_usd / max(price, 1e-9)
        size = min(size, self.config.max_order_size)
        if size * price > target_notional:
            size = target_notional / max(price, 1e-9)
        return max(size, 0.0)
