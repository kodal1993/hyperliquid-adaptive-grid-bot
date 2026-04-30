from __future__ import annotations

from .config import BotConfig
from .hyperliquid_client import HyperliquidClient


def run_startup_validation(config: BotConfig) -> dict:
    if not config.paper_mode and not config.enable_live_trading:
        raise ValueError("Live trading refused: ENABLE_LIVE_TRADING must be true")
    if config.hl_network not in {"testnet", "mainnet"}:
        raise ValueError("HL_NETWORK must be testnet or mainnet")
    if config.grid_levels <= 0 or config.grid_spacing_pct <= 0:
        raise ValueError("Invalid grid config")
    client = HyperliquidClient(config.private_key, config.account_address, config.hl_network)
    client.connect()
    mids = client.get_mid_price(config.default_symbol)
    if mids <= 0:
        raise ValueError("Symbol not found or invalid mid")
    return {"status": "ok", "symbol": config.default_symbol}
