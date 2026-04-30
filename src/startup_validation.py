from __future__ import annotations

from .config import BotConfig
from .hyperliquid_client import HyperliquidClient


def run_startup_validation(config: BotConfig) -> dict:
    if not config.paper_mode and not config.enable_live_trading:
        raise ValueError("Live trading refused: ENABLE_LIVE_TRADING must be true")
    if config.hl_network not in {"testnet", "mainnet"}:
        raise ValueError("HL_NETWORK must be testnet or mainnet")
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
        "warnings": warnings,
    }
    print(f"Startup summary: {summary}")
    return summary
