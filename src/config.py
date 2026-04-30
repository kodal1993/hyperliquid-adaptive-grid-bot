from __future__ import annotations

from dataclasses import dataclass
import os
from dotenv import load_dotenv


@dataclass(slots=True)
class BotConfig:
    env_profile: str
    hl_network: str
    private_key: str
    account_address: str
    default_symbol: str
    tick_seconds: int
    leverage_min: int
    leverage_max: int
    base_leverage: int
    grid_levels: int
    grid_spacing_pct: float
    grid_spacing_vol_multiplier: float
    max_notional_per_trade_usd: float
    max_drawdown_pct: float
    daily_loss_limit_pct: float
    emergency_stop: bool
    telegram_bot_token: str
    telegram_chat_id: str
    state_file: str
    log_level: str
    paper_start_balance_usd: float

    @classmethod
    def from_env(cls) -> "BotConfig":
        load_dotenv()
        return cls(
            env_profile=os.getenv("ENV_PROFILE", "paper"),
            hl_network=os.getenv("HL_NETWORK", "testnet"),
            private_key=os.getenv("HL_PRIVATE_KEY", ""),
            account_address=os.getenv("HL_ACCOUNT_ADDRESS", ""),
            default_symbol=os.getenv("HL_DEFAULT_SYMBOL", "BTC"),
            tick_seconds=int(os.getenv("BOT_TICK_SECONDS", "10")),
            leverage_min=int(os.getenv("LEVERAGE_MIN", "3")),
            leverage_max=int(os.getenv("LEVERAGE_MAX", "8")),
            base_leverage=int(os.getenv("BASE_LEVERAGE", "5")),
            grid_levels=int(os.getenv("GRID_LEVELS", "8")),
            grid_spacing_pct=float(os.getenv("GRID_SPACING_PCT", "0.003")),
            grid_spacing_vol_multiplier=float(os.getenv("GRID_SPACING_VOL_MULTIPLIER", "1.2")),
            max_notional_per_trade_usd=float(os.getenv("MAX_NOTIONAL_PER_TRADE_USD", "50")),
            max_drawdown_pct=float(os.getenv("MAX_DRAWDOWN_PCT", "0.30")),
            daily_loss_limit_pct=float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.06")),
            emergency_stop=os.getenv("EMERGENCY_STOP", "false").lower() == "true",
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            state_file=os.getenv("STATE_FILE", "state/bot_state.json"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            paper_start_balance_usd=float(os.getenv("PAPER_START_BALANCE_USD", "500")),
        )
