from __future__ import annotations

import pandas as pd
from .config import BotConfig
from .execution_engine import ExecutionEngine
from .grid_manager import GridManager, GridPlan
from .regime_detector import RegimeDetector
from .risk_manager import RiskManager
from .types import MarketRegime


class StrategyOrchestrator:
    def __init__(self, config: BotConfig, execution_engine: ExecutionEngine) -> None:
        self.config = config
        self.execution_engine = execution_engine
        self.detector = RegimeDetector()
        self.risk = RiskManager(config.max_drawdown_pct, config.daily_loss_limit_pct)
        self.grid_manager = GridManager()

    def on_tick(self, candles: pd.DataFrame, equity: float, daily_pnl_pct: float, symbol: str) -> dict:
        regime = self.detector.detect(candles)
        risk_state = self.risk.evaluate(equity=equity, daily_pnl_pct=daily_pnl_pct, emergency_stop=self.config.emergency_stop)
        if not risk_state.can_trade or regime == MarketRegime.RISK_OFF:
            return {"status": "paused", "regime": regime.value, "risk": risk_state}

        vol = candles["close"].pct_change().std()
        plan: GridPlan = self.grid_manager.build_grid(
            mid_price=float(candles["close"].iloc[-1]),
            levels=self.config.grid_levels,
            base_spacing_pct=self.config.grid_spacing_pct,
            volatility=float(vol),
            regime=regime,
            order_size=max(1.0, self.config.max_notional_per_trade_usd / float(candles["close"].iloc[-1])),
        )
        result = self.execution_engine.cancel_replace_grid(symbol, plan)
        return {"status": "running", "regime": regime.value, "risk": risk_state, "orders": result}
