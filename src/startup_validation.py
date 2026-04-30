from __future__ import annotations

from .config import BotConfig
from .hyperliquid_client import HyperliquidClient


def run_startup_validation(config: BotConfig) -> dict:
    client = HyperliquidClient(config.private_key, config.account_address, config.hl_network)
    client.connect()
    balance = client.get_balance()
    return {
        "network": config.hl_network,
        "equity": balance.get("equity", 0),
        "symbol": config.default_symbol,
        "status": "ok",
    }
