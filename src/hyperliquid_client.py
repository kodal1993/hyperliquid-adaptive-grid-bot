from __future__ import annotations

import logging
from typing import Any
import requests

logger = logging.getLogger(__name__)


class HyperliquidClient:
    def __init__(self, private_key: str, account_address: str, network: str = "testnet") -> None:
        self.private_key = private_key
        self.account_address = account_address
        self.network = network
        self.info: Any | None = None
        self.base_url = "https://api.hyperliquid-testnet.xyz" if network == "testnet" else "https://api.hyperliquid.xyz"

    def connect(self) -> None:
        try:
            from hyperliquid.info import Info

            self.info = Info(self.base_url, skip_ws=True)
            logger.info("Hyperliquid SDK Info connected network=%s", self.network)
        except Exception as exc:
            self.info = None
            logger.warning("SDK init failed, using HTTP fallback: %s", exc)

    def _post_info(self, payload: dict[str, Any]) -> Any:
        resp = requests.post(f"{self.base_url}/info", json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_mid_price(self, symbol: str) -> float:
        if self.info is not None:
            mids = self.info.all_mids()
            if symbol in mids:
                return float(mids[symbol])
        mids = self._post_info({"type": "allMids"})
        return float(mids[symbol])

    def get_candles(self, symbol: str, interval: str = "1m", lookback: int = 200) -> list[dict[str, Any]]:
        if self.info is not None:
            try:
                import time

                end_ms = int(time.time() * 1000)
                start_ms = end_ms - lookback * 60 * 1000
                return self.info.candles_snapshot(symbol, interval, start_ms, end_ms)
            except Exception:
                pass
        return self._post_info({"type": "candleSnapshot", "req": {"coin": symbol, "interval": interval, "startTime": 0, "endTime": 0}})

    def get_balance(self) -> dict[str, Any]:
        if self.info is not None and self.account_address:
            state = self.info.user_state(self.account_address)
            return {"equity": float(state.get("marginSummary", {}).get("accountValue", 0.0))}
        return {"equity": 0.0}

    def get_positions(self) -> list[dict[str, Any]]:
        if self.info is not None and self.account_address:
            state = self.info.user_state(self.account_address)
            return state.get("assetPositions", [])
        return []

    def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        if self.info is not None and self.account_address:
            orders = self.info.open_orders(self.account_address)
            return [o for o in orders if o.get("coin") == symbol]
        return []
