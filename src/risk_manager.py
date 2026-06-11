from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class RiskSnapshot:
    drawdown: float
    daily_pnl_pct: float
    preservation_mode: bool
    can_trade: bool
    reason: str = ""
    would_increase_exposure: bool = False
    exposure_reducing_override: bool = False
    attempted_side: str = "none"
    decision: str = "allow"


class RiskManager:
    def __init__(self, max_drawdown_pct: float, daily_loss_limit_pct: float) -> None:
        self.max_drawdown_pct = max_drawdown_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.peak_equity = 0.0

    def evaluate(self, equity: float, daily_pnl_pct: float, emergency_stop: bool = False, stop_file: str = "state/STOP", position_notional: float = 0.0, max_position_notional: float = 1e18, one_direction_exposure_pct: float = 0.0, max_one_direction_exposure_pct: float = 1.0, liquidation_distance_pct: float = 1.0, min_liquidation_distance_pct: float = 0.05, attempted_side: str = "none", would_increase_exposure: bool = False) -> RiskSnapshot:
        self.peak_equity = max(self.peak_equity, equity)
        drawdown = 0.0 if self.peak_equity <= 0 else (self.peak_equity - equity) / self.peak_equity
        preservation = drawdown >= self.max_drawdown_pct * 0.7
        reason = ""
        exposure_reducing_override = attempted_side != "none" and not would_increase_exposure
        blocked = emergency_stop or Path(stop_file).exists()
        if blocked:
            reason = "emergency_stop"
        elif drawdown >= self.max_drawdown_pct:
            blocked, reason = True, "max_drawdown"
        elif daily_pnl_pct <= -self.daily_loss_limit_pct and not exposure_reducing_override:
            blocked, reason = True, "daily_loss"
        elif would_increase_exposure:
            if position_notional > max_position_notional:
                blocked, reason = True, "max_position_notional"
            elif one_direction_exposure_pct > max_one_direction_exposure_pct:
                blocked, reason = True, "one_direction_exposure"
        elif liquidation_distance_pct < min_liquidation_distance_pct:
            blocked, reason = True, "liq_distance"
        decision = "block" if blocked else "allow"
        return RiskSnapshot(drawdown=drawdown, daily_pnl_pct=daily_pnl_pct, preservation_mode=preservation, can_trade=not blocked, reason=reason, would_increase_exposure=would_increase_exposure, exposure_reducing_override=exposure_reducing_override, attempted_side=attempted_side, decision=decision)
