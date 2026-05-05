from __future__ import annotations

import logging
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

    def on_tick(self, candles: pd.DataFrame, equity: float, daily_pnl_pct: float, symbol: str, position_notional: float = 0.0) -> dict:
        regime = self.detector.detect(candles, trend_lookback_candles=self.config.trend_lookback_candles, trend_move_threshold_pct=self.config.trend_move_threshold_pct, ema_slope_threshold_pct=self.config.ema_slope_threshold_pct)
        price = float(candles["close"].iloc[-1])
        pos_size = self.execution_engine.paper.position_size

        mode = GridMode.NEUTRAL
        if regime == MarketRegime.TREND_UP:
            if self.config.allow_long_biased:
                mode = GridMode.LONG_BIASED
            else:
                return {"status": "paused", "regime": regime.value, "reason": "neutral_blocked_in_trend"}
        elif regime == MarketRegime.TREND_DOWN:
            if self.config.allow_short_biased:
                mode = GridMode.SHORT_BIASED
            else:
                return {"status": "paused", "regime": regime.value, "reason": "neutral_blocked_in_trend"}

        vol = candles["close"].pct_change().std()
        order_size = self._calculate_order_size(price)
        order_notional = order_size * price

        long_exposure = max(pos_size, 0.0) * price
        short_exposure = max(-pos_size, 0.0) * price
        threshold = self.config.max_position_notional_usd * self.config.max_directional_exposure_pct
        allow_buys = long_exposure < threshold
        allow_sells = short_exposure < threshold

        if pos_size > 0:
            allow_buys = False
        elif pos_size < 0:
            allow_sells = False

        soft_cap = self.config.max_position_notional_usd * 0.6
        rebalance_cap = self.config.max_position_notional_usd * 0.8
        if position_notional > soft_cap:
            if pos_size > 0:
                allow_buys = False
            elif pos_size < 0:
                allow_sells = False
        force_reduce_only = position_notional > rebalance_cap

        if force_reduce_only:
            if pos_size > 0:
                allow_sells = True
            elif pos_size < 0:
                allow_buys = True

        if regime == MarketRegime.TREND_UP and mode == GridMode.LONG_BIASED and not force_reduce_only:
            allow_sells = False
        if regime == MarketRegime.TREND_DOWN and mode == GridMode.SHORT_BIASED and not force_reduce_only:
            allow_buys = False

        attempted_side = "both" if (allow_buys and allow_sells) else ("buy" if allow_buys else ("sell" if allow_sells else "none"))
        would_increase_exposure = (pos_size > 0 and attempted_side in {"buy", "both"}) or (pos_size < 0 and attempted_side in {"sell", "both"})

        liquidation_distance_pct = 1.0 if self.config.paper_mode else 0.5
        one_direction_exposure_pct = 0.0 if equity <= 0 else position_notional / equity
        risk_state = self.risk.evaluate(
            equity=equity, daily_pnl_pct=daily_pnl_pct, emergency_stop=self.config.emergency_stop, stop_file=self.config.emergency_stop_file,
            position_notional=position_notional, max_position_notional=self.config.max_position_notional_usd,
            one_direction_exposure_pct=one_direction_exposure_pct, max_one_direction_exposure_pct=self.config.max_one_direction_exposure_pct,
            liquidation_distance_pct=liquidation_distance_pct, min_liquidation_distance_pct=self.config.liquidation_distance_min_pct,
            attempted_side=attempted_side, would_increase_exposure=would_increase_exposure,
        )
        logger.info("risk_check risk_state=%s pause_reason=%s current_position_notional=%.2f max_position_notional_usd=%.2f attempted_side=%s would_increase_exposure=%s decision=%s", risk_state.reason or "ok", "" if risk_state.can_trade else risk_state.reason, position_notional, self.config.max_position_notional_usd, attempted_side, would_increase_exposure, risk_state.decision)
        if position_notional > self.config.max_position_notional_usd:
            canceled = self.execution_engine.cancel_all_orders(symbol)
            flattened = False
            if self.config.paper_mode and self.config.paper_auto_flatten_on_max_position:
                flattened = self.execution_engine.flatten_position(symbol, price)
            reduce_only_placed = 0 if flattened else self.execution_engine.place_reduce_only_orders(symbol, pos_size, price, order_size)
            logger.warning("reduce_only_requested symbol=%s reason=max_position_notional canceled=%s reduce_only_placed=%s", symbol, canceled, reduce_only_placed)
            return {"status": "paused", "regime": regime.value, "risk": risk_state, "reduce_only": True, "reason": "reduce_only_requested", "canceled_orders": canceled, "reduce_only_placed": reduce_only_placed, "flattened": flattened}

        if not risk_state.can_trade or regime == MarketRegime.RISK_OFF:
            if risk_state.reason == "max_position_notional":
                canceled = self.execution_engine.cancel_all_orders(symbol)
                flattened = False
                if self.config.paper_mode and self.config.paper_auto_flatten_on_max_position:
                    flattened = self.execution_engine.flatten_position(symbol, price)
                reduce_only_placed = 0 if flattened else self.execution_engine.place_reduce_only_orders(symbol, pos_size, price, self._calculate_order_size(price))
                logger.warning("reduce_only_requested symbol=%s reason=%s canceled=%s reduce_only_placed=%s", symbol, risk_state.reason, canceled, reduce_only_placed)
                return {"status": "paused", "regime": regime.value, "risk": risk_state, "reduce_only": True, "reason": "reduce_only_requested", "canceled_orders": canceled, "reduce_only_placed": reduce_only_placed, "flattened": flattened}
            return {"status": "paused", "regime": regime.value, "risk": risk_state, "reduce_only": False}

        plan: GridPlan = self.grid_manager.build_grid(price, self.config.grid_levels, self.config.grid_spacing_pct, float(vol), regime, order_size, self.config.regrid_threshold_pct, self.config.min_grid_profit_over_fees_pct, mode, allow_buys=allow_buys, allow_sells=allow_sells)
        result = self.execution_engine.cancel_replace_grid(symbol, plan)
        return {"status": "running", "regime": regime.value, "risk": risk_state, "orders": result, "mode": mode.value, "order_size": order_size, "order_notional": order_notional, "allow_buys": allow_buys, "allow_sells": allow_sells}

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
