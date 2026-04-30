from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class RiskSnapshot:
    drawdown: float
    daily_pnl_pct: float
    preservation_mode: bool
    can_trade: bool


class RiskManager:
    def __init__(self, max_drawdown_pct: float, daily_loss_limit_pct: float) -> None:
        self.max_drawdown_pct = max_drawdown_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.peak_equity = 0.0

    def evaluate(self, equity: float, daily_pnl_pct: float, emergency_stop: bool = False) -> RiskSnapshot:
        self.peak_equity = max(self.peak_equity, equity)
        drawdown = 0.0 if self.peak_equity <= 0 else (self.peak_equity - equity) / self.peak_equity
        preservation = drawdown >= self.max_drawdown_pct * 0.7
        blocked = emergency_stop or drawdown >= self.max_drawdown_pct or daily_pnl_pct <= -self.daily_loss_limit_pct
        return RiskSnapshot(drawdown=drawdown, daily_pnl_pct=daily_pnl_pct, preservation_mode=preservation, can_trade=not blocked)
