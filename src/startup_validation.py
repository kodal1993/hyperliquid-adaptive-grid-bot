from __future__ import annotations

from .config import BotConfig
from .hyperliquid_client import HyperliquidClient


LIVE_EXECUTION_DISABLED = "live_execution_disabled"
LIVE_EXECUTION_NOT_IMPLEMENTED = "live_execution_not_implemented"


def validate_live_execution_gate(config: BotConfig, client: HyperliquidClient | None = None) -> None:
    """Fail fast before a non-paper bot can run simulated fills as live trades."""
    if config.paper_mode:
        return
    if not config.enable_live_trading:
        raise RuntimeError("live_execution_disabled: ENABLE_LIVE_TRADING must be true when PAPER_MODE=false")
    if not config.live_execution_enabled:
        raise RuntimeError("live_execution_disabled: LIVE_EXECUTION_ENABLED must be true for live execution")
    execution_client = client or HyperliquidClient(config.private_key, config.account_address, config.hl_network)
    if not execution_client.has_live_execution_support():
        raise RuntimeError("live_execution_not_implemented: authenticated exchange order/cancel/fill execution is not implemented")


def run_startup_validation(config: BotConfig) -> dict:
    if config.hl_network not in {"testnet", "mainnet"}:
        raise ValueError("HL_NETWORK must be testnet or mainnet")
    validate_live_execution_gate(config)
    if config.grid_levels <= 0 or config.grid_spacing_pct <= 0 or config.max_position_notional_usd <= 0:
        raise ValueError("Invalid grid config")
    if config.min_notional_usd > config.max_notional_per_trade_usd:
        raise ValueError("MIN_NOTIONAL_USD must be <= MAX_NOTIONAL_PER_TRADE_USD")
    client = HyperliquidClient(config.private_key, config.account_address, config.hl_network)
    client.connect()
    mids = client.get_mid_price(config.default_symbol)
    if mids <= 0:
        raise ValueError("Symbol not found or invalid mid")
    warnings = []
    if config.min_order_size * mids > config.max_notional_per_trade_usd:
        warnings.append("MIN_ORDER_SIZE * price exceeds MAX_NOTIONAL_PER_TRADE_USD; order size will be capped")
    summary = {
        "status": "ok",
        "symbol": config.default_symbol,
        "mid_price": mids,
        "grid_levels": config.grid_levels,
        "grid_spacing_pct": config.grid_spacing_pct,
        "max_trade_notional_usd": config.max_notional_per_trade_usd,
        "max_position_notional_usd": config.max_position_notional_usd,
        "paper_mode": config.paper_mode,
        "enable_live_trading": config.enable_live_trading,
        "live_execution_enabled": config.live_execution_enabled,
        "warnings": warnings,
    }
    print(f"Startup summary: {summary}")
    return summary
