from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


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
    regrid_threshold_pct: float
    min_grid_profit_over_fees_pct: float
    max_notional_per_trade_usd: float
    min_order_size: float
    max_order_size: float
    min_notional_usd: float
    max_position_notional_usd: float
    max_one_direction_exposure_pct: float
    max_drawdown_pct: float
    daily_loss_limit_pct: float
    liquidation_distance_min_pct: float
    emergency_stop: bool
    emergency_stop_file: str
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_report_interval_seconds: int
    telegram_send_startup: bool
    telegram_send_fills: bool
    telegram_send_risk_alerts: bool
    telegram_send_periodic_status: bool
    state_file: str
    paper_mode: bool
    enable_live_trading: bool
    allow_long_biased: bool
    allow_short_biased: bool
    log_level: str
    paper_start_balance_usd: float
    fill_model: str

    @classmethod
    def from_env(cls) -> "BotConfig":
        default_loaded = load_dotenv(override=False)
        env_profile = os.getenv("ENV_PROFILE", "paper")
        profile_path = Path("config") / f"{env_profile}.env"
        profile_loaded = False
        if profile_path.exists():
            profile_loaded = load_dotenv(dotenv_path=profile_path, override=True)

        logger.info(
            "Config loading: env_profile=%s default_env_loaded=%s profile_path=%s profile_loaded=%s",
            env_profile,
            default_loaded,
            profile_path,
            profile_loaded,
        )

        return cls(
            env_profile=env_profile,
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
            regrid_threshold_pct=float(os.getenv("REGRID_THRESHOLD_PCT", "0.003")),
            min_grid_profit_over_fees_pct=float(os.getenv("MIN_GRID_PROFIT_OVER_FEES_PCT", "0.0005")),
            max_notional_per_trade_usd=float(os.getenv("MAX_NOTIONAL_PER_TRADE_USD", "50")),
            min_order_size=float(os.getenv("MIN_ORDER_SIZE", "0.0001")),
            max_order_size=float(os.getenv("MAX_ORDER_SIZE", "10")),
            min_notional_usd=float(os.getenv("MIN_NOTIONAL_USD", "10")),
            max_position_notional_usd=float(os.getenv("MAX_POSITION_NOTIONAL_USD", "500")),
            max_one_direction_exposure_pct=float(os.getenv("MAX_ONE_DIRECTION_EXPOSURE_PCT", "0.6")),
            max_drawdown_pct=float(os.getenv("MAX_DRAWDOWN_PCT", "0.30")),
            daily_loss_limit_pct=float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.06")),
            liquidation_distance_min_pct=float(os.getenv("LIQUIDATION_DISTANCE_MIN_PCT", "0.05")),
            emergency_stop=os.getenv("EMERGENCY_STOP", "false").lower() == "true",
            emergency_stop_file=os.getenv("EMERGENCY_STOP_FILE", "state/STOP"),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            telegram_report_interval_seconds=int(os.getenv("TELEGRAM_REPORT_INTERVAL_SECONDS", "300")),
            telegram_send_startup=os.getenv("TELEGRAM_SEND_STARTUP", "true").lower() == "true",
            telegram_send_fills=os.getenv("TELEGRAM_SEND_FILLS", "true").lower() == "true",
            telegram_send_risk_alerts=os.getenv("TELEGRAM_SEND_RISK_ALERTS", "true").lower() == "true",
            telegram_send_periodic_status=os.getenv("TELEGRAM_SEND_PERIODIC_STATUS", "true").lower() == "true",
            state_file=os.getenv("STATE_FILE", "state/bot_state.json"),
            paper_mode=os.getenv("PAPER_MODE", "true").lower() == "true",
            enable_live_trading=os.getenv("ENABLE_LIVE_TRADING", "false").lower() == "true",
            allow_long_biased=os.getenv("ALLOW_LONG_BIASED", "false").lower() == "true",
            allow_short_biased=os.getenv("ALLOW_SHORT_BIASED", "false").lower() == "true",
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            paper_start_balance_usd=float(os.getenv("PAPER_START_BALANCE_USD", "500")),
            fill_model=os.getenv("FILL_MODEL", "optimistic").lower(),
        )
