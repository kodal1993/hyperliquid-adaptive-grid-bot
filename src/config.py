from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
from dotenv import dotenv_values, load_dotenv

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
    grid_spacing_multiplier: float
    grid_spacing_min_pct: float
    grid_spacing_max_pct: float
    min_grid_levels: int
    max_grid_levels: int
    vol_low_threshold: float
    vol_high_threshold: float
    vol_extreme_threshold: float
    trend_bias_base: float
    trend_bias_strong: float
    trend_bias_weak: float
    trend_confidence_strong: float
    trend_confidence_weak: float
    regime_confirmation_bars: int
    regime_min_confidence: float
    trend_flip_cooldown_ticks: int
    maker_fee_rate: float
    taker_fee_rate: float
    use_alo_orders: bool
    paired_take_profit_enabled: bool
    paired_tp_spacing_multiplier: float
    use_websocket: bool
    order_notional_usd: float
    max_active_exposure_usd: float
    regrid_threshold_pct: float
    min_grid_profit_over_fees_pct: float
    min_expected_net_edge_usd: float
    min_expected_net_edge_pct: float
    min_rr_ratio: float
    min_expected_net_profit_fee_multiple: float
    min_net_profit_fee_multiplier: float
    counter_trend_entry_edge_score: float
    strong_momentum_block_threshold: float
    adverse_move_exit_pct: float
    min_reversion_confirmation_pct: float
    grid_spacing_low_vol_min_pct: float
    grid_spacing_low_vol_max_pct: float
    grid_spacing_normal_vol_min_pct: float
    grid_spacing_normal_vol_max_pct: float
    grid_spacing_high_vol_min_pct: float
    grid_spacing_high_vol_max_pct: float
    grid_spacing_pct_min: float
    grid_spacing_pct_max: float
    max_notional_per_trade_usd: float
    min_order_notional_usd: float
    min_order_size: float
    max_order_size: float
    min_notional_usd: float
    max_position_notional_usd: float
    max_one_direction_exposure_pct: float
    max_directional_exposure_pct: float
    max_drawdown_pct: float
    daily_loss_limit_pct: float
    liquidation_distance_min_pct: float
    emergency_stop: bool
    emergency_stop_file: str
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_report_interval_seconds: int
    telegram_risk_alert_cooldown_seconds: int
    telegram_enable_commands: bool
    telegram_send_startup: bool
    telegram_send_fills: bool
    telegram_send_hard_risk_alerts: bool
    telegram_send_strategy_pause_alerts: bool
    telegram_send_periodic_status: bool
    state_file: str
    paper_mode: bool
    enable_live_trading: bool
    live_execution_enabled: bool
    allow_long_biased: bool
    allow_short_biased: bool
    trend_lookback_candles: int
    trend_move_threshold_pct: float
    ema_slope_threshold_pct: float
    trend_strength_threshold: float
    stuck_position_minutes: int
    log_level: str
    soft_exposure_cap_pct: float
    hard_exposure_cap_pct: float
    absolute_exposure_cap_pct: float
    allow_position_flip: bool
    stale_position_max_hours: float
    min_residual_notional_usd: float
    dust_position_notional_usd: float
    dust_cleanup_mode: str
    dust_cleanup_cooldown_seconds: int
    auto_dust_cleanup_usd: float
    exclude_dust_from_performance_stats: bool
    no_fill_cycles_before_recenter: int
    grid_stale_max_hours: float
    recenter_cooldown_seconds: int
    position_recenter_cooldown_seconds: int
    stale_order_max_age_sec: int
    enable_market_stress_filter: bool
    high_vol_atr_pct: float
    extreme_vol_atr_pct: float
    btc_5m_shock_pct: float
    btc_1h_shock_pct: float
    trend_strong_confidence: float
    trend_weak_confidence: float
    panic_score_threshold: float
    market_stress_extreme_flat: bool
    anti_chop_last_n_trades: int
    anti_chop_cooldown_minutes: int
    orderbook_filter_enabled: bool
    orderbook_soft_mode: bool
    orderbook_top_levels: int
    orderbook_smoothing_window: int
    orderbook_min_samples: int
    orderbook_max_level_reduction_pct: float
    orderbook_max_age_seconds: float
    orderbook_counter_edge_score: float
    orderbook_imbalance_ema_alpha: float
    orderbook_min_confidence: float
    prediction_layer_enabled: bool
    prediction_soft_mode: bool
    prediction_confidence_threshold: float
    prediction_max_level_bias_pct: float
    prediction_max_size_multiplier: float
    prediction_min_size_multiplier: float
    no_fill_watchdog_enabled: bool
    no_fill_max_minutes: int
    no_fill_spacing_reduction_pct: float
    no_fill_min_spacing_pct: float
    min_order_lifetime_seconds: int
    min_reprice_distance_pct: float
    max_order_ops_per_cycle: int
    rate_limit_cooldown_seconds: float
    rate_limit_deficit_cooldown_seconds_per_request: float
    rate_limit_max_cooldown_seconds: float
    rate_limit_orphan_grace_seconds: float

    @classmethod
    def from_env(cls) -> "BotConfig":
        default_env_path = Path.cwd() / ".env"
        default_env_profile = None
        if default_env_path.exists():
            default_env_profile = dotenv_values(default_env_path).get("ENV_PROFILE")

        env_profile = os.getenv("ENV_PROFILE") or default_env_profile or "live"
        profile_path = Path("config") / f"{env_profile}.env"
        profile_loaded = False
        if profile_path.exists():
            profile_loaded = load_dotenv(dotenv_path=profile_path, override=False)

        default_loaded = load_dotenv(dotenv_path=default_env_path, override=True)

        logger.info(
            "Config loading: env_profile=%s default_env_loaded=%s profile_path=%s profile_loaded=%s",
            env_profile,
            default_loaded,
            profile_path,
            profile_loaded,
        )

        cfg = cls(
            env_profile=env_profile,
            hl_network=os.getenv("HL_NETWORK", "testnet"),
            private_key=os.getenv("HL_PRIVATE_KEY", ""),
            account_address=os.getenv("HL_ACCOUNT_ADDRESS", ""),
            default_symbol=os.getenv("HL_DEFAULT_SYMBOL", "BTC"),
            tick_seconds=int(os.getenv("BOT_TICK_SECONDS", "10")),
            leverage_min=int(os.getenv("LEVERAGE_MIN", "3")),
            leverage_max=int(os.getenv("LEVERAGE_MAX", "8")),
            base_leverage=int(os.getenv("BASE_LEVERAGE", "5")),
            grid_levels=int(os.getenv("GRID_LEVELS", "3")),
            grid_spacing_pct=float(os.getenv("GRID_SPACING_PCT", "0.003")),
            grid_spacing_vol_multiplier=float(os.getenv("GRID_SPACING_VOL_MULTIPLIER", "1.2")),
            grid_spacing_multiplier=float(os.getenv("GRID_SPACING_MULTIPLIER", "1.20")),
            grid_spacing_min_pct=float(os.getenv("GRID_SPACING_MIN_PCT", "0.0018")),
            grid_spacing_max_pct=float(os.getenv("GRID_SPACING_MAX_PCT", "0.012")),
            min_grid_levels=int(os.getenv("MIN_GRID_LEVELS", "2")),
            max_grid_levels=int(os.getenv("MAX_GRID_LEVELS", "4")),
            vol_low_threshold=float(os.getenv("VOL_LOW_THRESHOLD", "0.003")),
            vol_high_threshold=float(os.getenv("VOL_HIGH_THRESHOLD", "0.012")),
            vol_extreme_threshold=float(os.getenv("VOL_EXTREME_THRESHOLD", "0.025")),
            trend_bias_base=float(os.getenv("TREND_BIAS_BASE", "0.70")),
            trend_bias_strong=float(os.getenv("TREND_BIAS_STRONG", "0.80")),
            trend_bias_weak=float(os.getenv("TREND_BIAS_WEAK", "0.60")),
            trend_confidence_strong=float(os.getenv("TREND_CONFIDENCE_STRONG", "0.75")),
            trend_confidence_weak=float(os.getenv("TREND_CONFIDENCE_WEAK", "0.35")),
            regime_confirmation_bars=int(os.getenv("REGIME_CONFIRMATION_BARS", "1")),
            regime_min_confidence=float(os.getenv("REGIME_MIN_CONFIDENCE", os.getenv("REGIME_CONFIDENCE_MIN", "0.0"))),
            trend_flip_cooldown_ticks=int(os.getenv("TREND_FLIP_COOLDOWN_TICKS", os.getenv("FLIP_COOLDOWN_TICKS", "6"))),
            maker_fee_rate=float(os.getenv("MAKER_FEE_RATE", "0.00015")),
            taker_fee_rate=float(os.getenv("TAKER_FEE_RATE", "0.00045")),
            use_alo_orders=os.getenv("USE_ALO_ORDERS", "true").lower() == "true",
            paired_take_profit_enabled=os.getenv("PAIRED_TAKE_PROFIT_ENABLED", "true").lower() == "true",
            paired_tp_spacing_multiplier=float(os.getenv("PAIRED_TP_SPACING_MULTIPLIER", "1.0")),
            use_websocket=os.getenv("USE_WEBSOCKET", "true").lower() == "true",
            order_notional_usd=float(os.getenv("ORDER_NOTIONAL_USD", os.getenv("MAX_NOTIONAL_PER_TRADE_USD", "10"))),
            max_active_exposure_usd=float(os.getenv("MAX_ACTIVE_EXPOSURE_USD", "50")),
            regrid_threshold_pct=float(os.getenv("REGRID_THRESHOLD_PCT", "0.003")),
            min_grid_profit_over_fees_pct=float(os.getenv("MIN_GRID_PROFIT_OVER_FEES_PCT", "0.0005")),
            min_expected_net_edge_usd=float(os.getenv("MIN_EXPECTED_NET_EDGE_USD", "0")),
            min_expected_net_edge_pct=float(os.getenv("MIN_EXPECTED_NET_EDGE_PCT", "0")),
            min_rr_ratio=float(os.getenv("MIN_RR_RATIO", "1.0")),
            min_expected_net_profit_fee_multiple=float(os.getenv("MIN_EXPECTED_NET_PROFIT_FEE_MULTIPLE", os.getenv("MIN_NET_PROFIT_FEE_MULTIPLIER", "2.75"))),
            min_net_profit_fee_multiplier=float(os.getenv("MIN_NET_PROFIT_FEE_MULTIPLIER", os.getenv("MIN_EXPECTED_NET_PROFIT_FEE_MULTIPLE", "2.75"))),
            counter_trend_entry_edge_score=float(os.getenv("COUNTER_TREND_ENTRY_EDGE_SCORE", "3.5")),
            strong_momentum_block_threshold=float(os.getenv("STRONG_MOMENTUM_BLOCK_THRESHOLD", "0.65")),
            adverse_move_exit_pct=float(os.getenv("ADVERSE_MOVE_EXIT_PCT", "0.0045")),
            min_reversion_confirmation_pct=float(os.getenv("MIN_REVERSION_CONFIRMATION_PCT", "0.0015")),
            grid_spacing_low_vol_min_pct=float(os.getenv("GRID_SPACING_LOW_VOL_MIN_PCT", "0.0025")),
            grid_spacing_low_vol_max_pct=float(os.getenv("GRID_SPACING_LOW_VOL_MAX_PCT", "0.0030")),
            grid_spacing_normal_vol_min_pct=float(os.getenv("GRID_SPACING_NORMAL_VOL_MIN_PCT", "0.0035")),
            grid_spacing_normal_vol_max_pct=float(os.getenv("GRID_SPACING_NORMAL_VOL_MAX_PCT", "0.0045")),
            grid_spacing_high_vol_min_pct=float(os.getenv("GRID_SPACING_HIGH_VOL_MIN_PCT", "0.0055")),
            grid_spacing_high_vol_max_pct=float(os.getenv("GRID_SPACING_HIGH_VOL_MAX_PCT", "0.0075")),
            grid_spacing_pct_min=float(os.getenv("GRID_SPACING_PCT_MIN", os.getenv("GRID_SPACING_MIN_PCT", "0.0025"))),
            grid_spacing_pct_max=float(os.getenv("GRID_SPACING_PCT_MAX", os.getenv("GRID_SPACING_MAX_PCT", "0.0055"))),
            max_notional_per_trade_usd=float(os.getenv("MAX_NOTIONAL_PER_TRADE_USD", os.getenv("ORDER_NOTIONAL_USD", "10"))),
            min_order_notional_usd=float(os.getenv("MIN_ORDER_NOTIONAL_USD", os.getenv("MIN_NOTIONAL_USD", "10.0"))),
            min_order_size=float(os.getenv("MIN_ORDER_SIZE", "0.0001")),
            max_order_size=float(os.getenv("MAX_ORDER_SIZE", "10")),
            min_notional_usd=float(os.getenv("MIN_NOTIONAL_USD", "10")),
            max_position_notional_usd=float(os.getenv("MAX_POSITION_NOTIONAL_USD", "50")),
            max_one_direction_exposure_pct=float(os.getenv("MAX_ONE_DIRECTION_EXPOSURE_PCT", "0.6")),
            max_directional_exposure_pct=float(os.getenv("MAX_DIRECTIONAL_EXPOSURE_PCT", "0.65")),
            max_drawdown_pct=float(os.getenv("MAX_DRAWDOWN_PCT", "0.30")),
            daily_loss_limit_pct=float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.06")),
            liquidation_distance_min_pct=float(os.getenv("LIQUIDATION_DISTANCE_MIN_PCT", "0.05")),
            emergency_stop=os.getenv("EMERGENCY_STOP", "false").lower() == "true",
            emergency_stop_file=os.getenv("EMERGENCY_STOP_FILE", "state/STOP"),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            telegram_report_interval_seconds=int(os.getenv("TELEGRAM_REPORT_INTERVAL_SECONDS", "3600")),
            telegram_risk_alert_cooldown_seconds=int(os.getenv("TELEGRAM_RISK_ALERT_COOLDOWN_SECONDS", "3600")),
            telegram_enable_commands=os.getenv("TELEGRAM_ENABLE_COMMANDS", "true").lower() == "true",
            telegram_send_startup=os.getenv("TELEGRAM_SEND_STARTUP", "true").lower() == "true",
            telegram_send_fills=os.getenv("TELEGRAM_SEND_FILLS", "true").lower() == "true",
            telegram_send_hard_risk_alerts=os.getenv("TELEGRAM_SEND_HARD_RISK_ALERTS", os.getenv("TELEGRAM_SEND_RISK_ALERTS", "true")).lower() == "true",
            telegram_send_strategy_pause_alerts=os.getenv("TELEGRAM_SEND_STRATEGY_PAUSE_ALERTS", "false").lower() == "true",
            telegram_send_periodic_status=os.getenv("TELEGRAM_SEND_PERIODIC_STATUS", "true").lower() == "true",
            state_file=os.getenv("STATE_FILE", "state/bot_state.json"),
            paper_mode=os.getenv("PAPER_MODE", "false").lower() == "true",
            enable_live_trading=os.getenv("ENABLE_LIVE_TRADING", "true").lower() == "true",
            live_execution_enabled=os.getenv("LIVE_EXECUTION_ENABLED", "true").lower() == "true",
            allow_long_biased=os.getenv("ALLOW_LONG_BIASED", "true").lower() == "true",
            allow_short_biased=os.getenv("ALLOW_SHORT_BIASED", "true").lower() == "true",
            trend_lookback_candles=int(os.getenv("TREND_LOOKBACK_CANDLES", "60")),
            trend_move_threshold_pct=float(os.getenv("TREND_MOVE_THRESHOLD_PCT", "0.006")),
            ema_slope_threshold_pct=float(os.getenv("EMA_SLOPE_THRESHOLD_PCT", "0.003")),
            trend_strength_threshold=float(os.getenv("TREND_STRENGTH_THRESHOLD", "0.65")),
            stuck_position_minutes=int(os.getenv("STUCK_POSITION_MINUTES", "60")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            soft_exposure_cap_pct=float(os.getenv("SOFT_EXPOSURE_CAP_PCT", "0.6")),
            hard_exposure_cap_pct=float(os.getenv("HARD_EXPOSURE_CAP_PCT", "0.8")),
            absolute_exposure_cap_pct=float(os.getenv("ABSOLUTE_EXPOSURE_CAP_PCT", "1.0")),
            allow_position_flip=os.getenv("ENABLE_POSITION_FLIP", os.getenv("ALLOW_POSITION_FLIP", "false")).lower() == "true",
            stale_position_max_hours=float(os.getenv("STALE_POSITION_MAX_HOURS", "12")),
            min_residual_notional_usd=float(os.getenv("MIN_RESIDUAL_NOTIONAL_USD", "10")),
            dust_position_notional_usd=float(os.getenv("DUST_POSITION_NOTIONAL_USD", "2.0")),
            dust_cleanup_mode=os.getenv("DUST_CLEANUP_MODE", "hold").strip().lower(),
            dust_cleanup_cooldown_seconds=int(os.getenv("DUST_CLEANUP_COOLDOWN_SECONDS", "300")),
            auto_dust_cleanup_usd=float(os.getenv("AUTO_DUST_CLEANUP_USD", "5")),
            exclude_dust_from_performance_stats=os.getenv("EXCLUDE_DUST_FROM_PERFORMANCE_STATS", "true").lower() == "true",
            no_fill_cycles_before_recenter=int(os.getenv("NO_FILL_CYCLES_BEFORE_RECENTER", "360")),
            grid_stale_max_hours=float(os.getenv("GRID_STALE_MAX_HOURS", "2")),
            recenter_cooldown_seconds=int(os.getenv("RECENTER_COOLDOWN_SECONDS", "600")),
            position_recenter_cooldown_seconds=int(os.getenv("POSITION_RECENTER_COOLDOWN_SECONDS", "900")),
            stale_order_max_age_sec=int(os.getenv("STALE_ORDER_MAX_AGE_SEC", "1800")),
            enable_market_stress_filter=os.getenv("ENABLE_MARKET_STRESS_FILTER", "true").lower() == "true",
            high_vol_atr_pct=float(os.getenv("HIGH_VOL_ATR_PCT", "0.006")),
            extreme_vol_atr_pct=float(os.getenv("EXTREME_VOL_ATR_PCT", "0.010")),
            btc_5m_shock_pct=float(os.getenv("BTC_5M_SHOCK_PCT", "0.008")),
            btc_1h_shock_pct=float(os.getenv("BTC_1H_SHOCK_PCT", "0.025")),
            trend_strong_confidence=float(os.getenv("TREND_STRONG_CONFIDENCE", "0.75")),
            trend_weak_confidence=float(os.getenv("TREND_WEAK_CONFIDENCE", "0.45")),
            panic_score_threshold=float(os.getenv("PANIC_SCORE_THRESHOLD", "0.80")),
            market_stress_extreme_flat=os.getenv("MARKET_STRESS_EXTREME_FLAT", "false").lower() == "true",
            anti_chop_last_n_trades=int(os.getenv("ANTI_CHOP_LAST_N_TRADES", "5")),
            anti_chop_cooldown_minutes=int(os.getenv("ANTI_CHOP_COOLDOWN_MINUTES", "5")),
            orderbook_filter_enabled=os.getenv("ORDERBOOK_FILTER_ENABLED", "true").lower() == "true",
            orderbook_soft_mode=os.getenv("ORDERBOOK_SOFT_MODE", "true").lower() == "true",
            orderbook_top_levels=int(os.getenv("ORDERBOOK_TOP_N_LEVELS", os.getenv("ORDERBOOK_TOP_LEVELS", "10"))),
            orderbook_smoothing_window=int(os.getenv("ORDERBOOK_SMOOTHING_SNAPSHOTS", os.getenv("ORDERBOOK_SMOOTHING_WINDOW", "5"))),
            orderbook_min_samples=int(os.getenv("ORDERBOOK_MIN_SAMPLES", os.getenv("ORDERBOOK_SMOOTHING_SNAPSHOTS", os.getenv("ORDERBOOK_SMOOTHING_WINDOW", "5")))),
            orderbook_max_level_reduction_pct=float(os.getenv("ORDERBOOK_MAX_LEVEL_REDUCTION_PCT", "0.5")),
            orderbook_max_age_seconds=float(os.getenv("ORDERBOOK_MAX_AGE_SECONDS", "5")),
            orderbook_counter_edge_score=float(os.getenv("ORDERBOOK_COUNTER_EDGE_SCORE", os.getenv("COUNTER_TREND_ENTRY_EDGE_SCORE", "1.25"))),
            orderbook_imbalance_ema_alpha=float(os.getenv("ORDERBOOK_IMBALANCE_EMA_ALPHA", "0.35")),
            orderbook_min_confidence=float(os.getenv("ORDERBOOK_MIN_CONFIDENCE", "0.55")),
            prediction_layer_enabled=os.getenv("PREDICTION_LAYER_ENABLED", "true").lower() == "true",
            prediction_soft_mode=os.getenv("PREDICTION_SOFT_MODE", "true").lower() == "true",
            prediction_confidence_threshold=float(os.getenv("PREDICTION_CONFIDENCE_THRESHOLD", "0.55")),
            prediction_max_level_bias_pct=float(os.getenv("PREDICTION_MAX_LEVEL_BIAS_PCT", "0.35")),
            prediction_max_size_multiplier=float(os.getenv("PREDICTION_MAX_SIZE_MULTIPLIER", "1.20")),
            prediction_min_size_multiplier=float(os.getenv("PREDICTION_MIN_SIZE_MULTIPLIER", "0.80")),
            no_fill_watchdog_enabled=os.getenv("NO_FILL_WATCHDOG_ENABLED", "true").lower() == "true",
            no_fill_max_minutes=int(os.getenv("NO_FILL_MAX_MINUTES", "60")),
            no_fill_spacing_reduction_pct=float(os.getenv("NO_FILL_SPACING_REDUCTION_PCT", "0.20")),
            no_fill_min_spacing_pct=float(os.getenv("NO_FILL_MIN_SPACING_PCT", "0.0022")),
            min_order_lifetime_seconds=int(os.getenv("MIN_ORDER_LIFETIME_SECONDS", "90")),
            min_reprice_distance_pct=float(os.getenv("MIN_REPRICE_DISTANCE_PCT", "0.0015")),
            max_order_ops_per_cycle=int(os.getenv("MAX_ORDER_OPS_PER_CYCLE", "2")),
            rate_limit_cooldown_seconds=float(os.getenv("RATE_LIMIT_COOLDOWN_SECONDS", "120")),
            rate_limit_deficit_cooldown_seconds_per_request=float(os.getenv("RATE_LIMIT_DEFICIT_COOLDOWN_SECONDS_PER_REQUEST", "0.05")),
            rate_limit_max_cooldown_seconds=float(os.getenv("RATE_LIMIT_MAX_COOLDOWN_SECONDS", "900")),
            rate_limit_orphan_grace_seconds=float(os.getenv("RATE_LIMIT_ORPHAN_GRACE_SECONDS", "600")),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        errors = []
        if self.grid_levels < 1:
            errors.append(f"grid_levels must be >= 1, got {self.grid_levels}")
        if not (0.0 < self.max_drawdown_pct <= 1.0):
            errors.append(f"max_drawdown_pct must be in (0, 1], got {self.max_drawdown_pct}")
        if not (0.0 < self.daily_loss_limit_pct <= 1.0):
            errors.append(f"daily_loss_limit_pct must be in (0, 1], got {self.daily_loss_limit_pct}")
        if self.base_leverage < 1 or self.base_leverage > 50:
            errors.append(f"base_leverage must be 1-50, got {self.base_leverage}")
        if self.order_notional_usd <= 0:
            errors.append(f"order_notional_usd must be > 0, got {self.order_notional_usd}")
        if self.grid_spacing_pct <= 0:
            errors.append(f"grid_spacing_pct must be > 0, got {self.grid_spacing_pct}")
        if self.tick_seconds < 1:
            errors.append(f"tick_seconds must be >= 1, got {self.tick_seconds}")
        if self.max_order_ops_per_cycle < 1:
            errors.append(f"max_order_ops_per_cycle must be >= 1, got {self.max_order_ops_per_cycle}")
        if self.maker_fee_rate < 0 or self.maker_fee_rate > 0.01:
            errors.append(f"maker_fee_rate must be in [0, 0.01], got {self.maker_fee_rate}")
        if self.taker_fee_rate < 0 or self.taker_fee_rate > 0.01:
            errors.append(f"taker_fee_rate must be in [0, 0.01], got {self.taker_fee_rate}")
        if self.paired_tp_spacing_multiplier <= 0:
            errors.append(f"paired_tp_spacing_multiplier must be > 0, got {self.paired_tp_spacing_multiplier}")
        if errors:
            raise ValueError("BotConfig validation failed:\n" + "\n".join(f"  - {e}" for e in errors))
