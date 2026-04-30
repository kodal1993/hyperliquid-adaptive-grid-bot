from __future__ import annotations

import pandas as pd
from .config import BotConfig
from .execution_engine import ExecutionEngine
from .grid_manager import GridManager, GridPlan
from .regime_detector import RegimeDetector
from .risk_manager import RiskManager
from .types import GridMode, MarketRegime


class StrategyOrchestrator:
    def __init__(self, config: BotConfig, execution_engine: ExecutionEngine) -> None:
        self.config = config
        self.execution_engine = execution_engine
        self.detector = RegimeDetector()
        self.risk = RiskManager(config.max_drawdown_pct, config.daily_loss_limit_pct)
        self.grid_manager = GridManager()

    def on_tick(self, candles: pd.DataFrame, equity: float, daily_pnl_pct: float, symbol: str) -> dict:
        regime = self.detector.detect(candles)
        mode = GridMode.NEUTRAL
        if regime == MarketRegime.TREND_UP and self.config.allow_long_biased:
            mode = GridMode.LONG_BIASED
        elif regime == MarketRegime.TREND_DOWN and self.config.allow_short_biased:
            mode = GridMode.SHORT_BIASED
        elif regime in (MarketRegime.TREND_UP, MarketRegime.TREND_DOWN):
            return {"status": "paused", "regime": regime.value, "reason": "neutral_blocked_in_trend"}

        risk_state = self.risk.evaluate(equity=equity, daily_pnl_pct=daily_pnl_pct, emergency_stop=self.config.emergency_stop, stop_file=self.config.emergency_stop_file)
        if not risk_state.can_trade or regime == MarketRegime.RISK_OFF:
            return {"status": "paused", "regime": regime.value, "risk": risk_state}

        vol = candles["close"].pct_change().std()
        price = float(candles["close"].iloc[-1])
        order_size = self._calculate_order_size(price)
        order_notional = order_size * price
        plan: GridPlan = self.grid_manager.build_grid(price, self.config.grid_levels, self.config.grid_spacing_pct, float(vol), regime, order_size, self.config.regrid_threshold_pct, self.config.min_grid_profit_over_fees_pct, mode)
        result = self.execution_engine.cancel_replace_grid(symbol, plan)
        return {"status": "running", "regime": regime.value, "risk": risk_state, "orders": result, "mode": mode.value, "order_size": order_size, "order_notional": order_notional}

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
