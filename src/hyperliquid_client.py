from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class HyperliquidClient:
    def __init__(self, private_key: str, account_address: str, network: str = "testnet") -> None:
        self.private_key = private_key
        self.account_address = account_address
        self.network = network
        self.initialized = False

    def connect(self) -> None:
        self.initialized = True
        logger.info("Hyperliquid client initialized for network=%s", self.network)

    def get_balance(self) -> dict[str, Any]:
        return {"equity": 500.0, "available": 500.0}

    def get_positions(self) -> list[dict[str, Any]]:
        return []

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        logger.info("Set leverage %s on %s", leverage, symbol)
        return True

    def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return []
